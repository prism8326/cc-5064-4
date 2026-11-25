"""真实片元光栅核心。

瓦片执行器与串行参考路径都调用同一个 :func:`draw_triangle`，保证覆盖判定、
重心插值、深度和混合的算术完全一致，从而可做像素级一致性比较。

实现要点
--------
* 顶点先量化到 1/256 子像素定点坐标，边函数为精确整数，共享边的 top-left
  填充约定不会产生二义性（相邻三角形不重复覆盖、不留缝）。
* 每个覆盖像素执行真实的属性插值、深度测试/写入与 straight-alpha 混合。
* 所有工作量计数器都在真实像素循环中累加，不能用列表长度替代。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .scene import Scene, Triangle

SUBPIXEL_BITS = 8
SUBPIXEL_SCALE = 1 << SUBPIXEL_BITS
HALF_SAMPLE = SUBPIXEL_SCALE >> 1


class BufferCapacityError(RuntimeError):
    """超过显式容量上限时抛出；绝不允许静默丢失几何。"""


class OrderUnrecoverableError(RuntimeError):
    """目标里只剩已经混好的颜色、无法恢复提交次序，且没有可信底图可重算。"""


@dataclass
class PixelTrace:
    """单个探针像素上的一次片元事件。"""
    seq: int
    submission: int
    paper_id: int
    label: str
    covered: bool
    depth_passed: bool | None
    src_rgba: tuple[float, float, float, float]
    dst_before: tuple[float, float, float, float] | None = None
    dst_after: tuple[float, float, float, float] | None = None
    action: str = ""


@dataclass
class TileStats:
    pixel_tests: int = 0
    edge_evals: int = 0
    covered: int = 0
    depth_rejected: int = 0
    shaded: int = 0
    blend_fragments: int = 0
    blend_dst_reads: int = 0
    color_writes: int = 0
    draw_calls: int = 0
    replay_fragments: int = 0  # 因重算/续绘而重复执行的真实片元

    def add(self, other: "TileStats") -> None:
        for name in (
            "pixel_tests", "edge_evals", "covered", "depth_rejected",
            "shaded", "blend_fragments", "blend_dst_reads", "color_writes",
            "draw_calls", "replay_fragments",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_dict(self) -> dict:
        return {
            "pixel_tests": self.pixel_tests,
            "edge_evals": self.edge_evals,
            "covered": self.covered,
            "depth_rejected": self.depth_rejected,
            "shaded": self.shaded,
            "blend_fragments": self.blend_fragments,
            "blend_dst_reads": self.blend_dst_reads,
            "color_writes": self.color_writes,
            "draw_calls": self.draw_calls,
            "replay_fragments": self.replay_fragments,
        }


@dataclass
class RasterTarget:
    """一块可光栅化的矩形目标（整屏或紧凑瓦片）。"""
    x0: int
    y0: int
    x1: int
    y1: int
    color: list[float]
    depth: list[float]
    cover: list[int]

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def pixel_count(self) -> int:
        return self.w * self.h

    @classmethod
    def create(
        cls,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        bg: tuple[float, float, float] = (0, 0, 0),
        bg_alpha: float = 1.0,
        clear_depth: float = 1.0,
    ) -> "RasterTarget":
        w, h = x1 - x0, y1 - y0
        n = w * h
        color = [0.0] * (n * 4)
        for i in range(n):
            color[i * 4 + 0] = bg[0]
            color[i * 4 + 1] = bg[1]
            color[i * 4 + 2] = bg[2]
            color[i * 4 + 3] = bg_alpha
        depth = [clear_depth] * n
        cover = [0] * n
        return cls(x0, y0, x1, y1, color, depth, cover)

    def index(self, gx: int, gy: int) -> int:
        return (gy - self.y0) * self.w + (gx - self.x0)

    def snapshot(self) -> tuple[list[float], list[float], list[int]]:
        return self.color.copy(), self.depth.copy(), self.cover.copy()

    def restore(self, snap) -> None:
        c, d, cv = snap
        # 整瓦片快照尺寸必然一致，整段替换内容即可。
        self.color[:] = c
        self.depth[:] = d
        self.cover[:] = cv


def _to_fixed(x: float, y: float) -> tuple[int, int]:
    return int(round(x * SUBPIXEL_SCALE)), int(round(y * SUBPIXEL_SCALE))


def _is_top_left(ax: int, ay: int, bx: int, by: int) -> bool:
    """正向绕序下的 top-left 边判定（已用共享边分割实验验证）。

    * 顶边：水平且定向为从左向右；
    * 左边：非水平且定向为自下向上（by < ay）。
    """
    if ay == by:
        return bx > ax
    return by < ay


def draw_triangle(
    tri: Triangle,
    target: RasterTarget,
    stats: TileStats | None = None,
    *,
    trace: dict[tuple[int, int], list[PixelTrace]] | None = None,
    count_as_replay: bool = False,
    record_coverage: bool = True,
    pixel_clip: tuple[int, int, int, int] | None = None,
) -> TileStats:
    """把一个三角形真实光栅化到 ``target``。

    ``pixel_clip`` 为额外的全局像素裁剪框 [x0,x1,y0,y1)，默认用 target 范围。
    """
    if stats is None:
        stats = TileStats()
    stats.draw_calls += 1

    verts = tri.verts
    (ax, ay), (bx, by), (cx, cy) = (_to_fixed(v.x, v.y) for v in verts)

    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if area == 0:
        return stats
    if area < 0:
        # 统一为正向绕序：交换 b/c（颜色等属性同步交换）。
        bx, cx = cx, bx
        by, cy = cy, by
        verts = (verts[0], verts[2], verts[1])
        ax, ay = _to_fixed(verts[0].x, verts[0].y)
        bx, by = _to_fixed(verts[1].x, verts[1].y)
        cx, cy = _to_fixed(verts[2].x, verts[2].y)
        area = -area

    tl0 = _is_top_left(bx, by, cx, cy)  # w0 对应边 v1->v2
    tl1 = _is_top_left(cx, cy, ax, ay)  # w1 对应边 v2->v0
    tl2 = _is_top_left(ax, ay, bx, by)  # w2 对应边 v0->v1

    xs = [v.x for v in verts]
    ys = [v.y for v in verts]
    fx0 = max(target.x0, int(math.floor(min(xs))) - 1)
    fx1 = min(target.x1, int(math.ceil(max(xs))) + 1)
    fy0 = max(target.y0, int(math.floor(min(ys))) - 1)
    fy1 = min(target.y1, int(math.ceil(max(ys))) + 1)
    if pixel_clip is not None:
        px0, px1, py0, py1 = pixel_clip
        fx0 = max(fx0, px0)
        fx1 = min(fx1, px1)
        fy0 = max(fy0, py0)
        fy1 = min(fy1, py1)
    if fx1 <= fx0 or fy1 <= fy0:
        return stats

    m = tri.material
    v0, v1, v2 = verts
    inv_area = 1.0 / area

    for gy in range(fy0, fy1):
        py = gy * SUBPIXEL_SCALE + HALF_SAMPLE
        ly = gy - target.y0
        row_base = ly * target.w
        for gx in range(fx0, fx1):
            px = gx * SUBPIXEL_SCALE + HALF_SAMPLE
            stats.pixel_tests += 1

            w0 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
            w1 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
            w2 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
            stats.edge_evals += 3

            if w0 < 0 or w1 < 0 or w2 < 0:
                continue
            if w0 == 0 and not tl0:
                continue
            if w1 == 0 and not tl1:
                continue
            if w2 == 0 and not tl2:
                continue

            idx = row_base + (gx - target.x0)
            stats.covered += 1
            if record_coverage:
                target.cover[idx] += 1
            if count_as_replay:
                stats.replay_fragments += 1

            l0 = w0 * inv_area
            l1 = w1 * inv_area
            l2 = w2 * inv_area

            r = l0 * v0.r + l1 * v1.r + l2 * v2.r
            g = l0 * v0.g + l1 * v1.g + l2 * v2.g
            b = l0 * v0.b + l1 * v1.b + l2 * v2.b
            a = l0 * v0.a + l1 * v1.a + l2 * v2.a
            z = l0 * v0.z + l1 * v1.z + l2 * v2.z

            trace_list = trace.get((gx, gy)) if trace is not None else None
            dst_before = None
            if trace_list is not None:
                o = idx * 4
                dst_before = (
                    target.color[o], target.color[o + 1],
                    target.color[o + 2], target.color[o + 3],
                )

            if m.depth_test:
                if z >= target.depth[idx]:
                    stats.depth_rejected += 1
                    if trace_list is not None:
                        trace_list.append(PixelTrace(
                            seq=tri.seq, submission=tri.submission,
                            paper_id=tri.paper_id, label=tri.label,
                            covered=True, depth_passed=False,
                            src_rgba=(r, g, b, a), dst_before=dst_before,
                            action="depth-fail",
                        ))
                    continue

            stats.shaded += 1
            o = idx * 4
            if m.translucent:
                stats.blend_fragments += 1
                stats.blend_dst_reads += 1
                dr, dg, db, da = target.color[o:o + 4]
                if trace_list is not None and dst_before is None:
                    dst_before = (dr, dg, db, da)
                ia = 1.0 - a
                nr = r * a + dr * ia
                ng = g * a + dg * ia
                nb = b * a + db * ia
                na = a + da * ia
                target.color[o] = nr
                target.color[o + 1] = ng
                target.color[o + 2] = nb
                target.color[o + 3] = na
                action = "blend"
            else:
                target.color[o] = r
                target.color[o + 1] = g
                target.color[o + 2] = b
                target.color[o + 3] = a
                action = "opaque-write"
            stats.color_writes += 1

            if m.depth_write:
                target.depth[idx] = z

            if trace_list is not None:
                dst_after = (
                    target.color[o], target.color[o + 1],
                    target.color[o + 2], target.color[o + 3],
                )
                trace_list.append(PixelTrace(
                    seq=tri.seq, submission=tri.submission,
                    paper_id=tri.paper_id, label=tri.label,
                    covered=True, depth_passed=True,
                    src_rgba=(r, g, b, a), dst_before=dst_before,
                    dst_after=dst_after, action=action,
                ))

    return stats


def render_serial_reference(
    scene: Scene,
    *,
    trace: dict[tuple[int, int], list[PixelTrace]] | None = None,
) -> tuple[RasterTarget, TileStats]:
    """简单串行参考路径：按全局提交序号逐图元全屏绘制。"""
    fb = RasterTarget.create(
        0, 0, scene.width, scene.height,
        bg=scene.background, bg_alpha=scene.background_alpha,
    )
    total = TileStats()
    for tri in scene.triangles:  # scene 中 seq 即添加顺序
        draw_triangle(tri, fb, total, trace=trace)
    return fb, total
