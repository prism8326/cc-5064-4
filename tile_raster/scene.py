"""场景数据模型与内置场景。

所有场景都通过仿射变换从局部单位形状生成，因此进入分箱阶段的确实是
“变换后的三角形”。图元按添加顺序获得全局提交序号 ``seq``，瓦片内严格
按该序号处理，以保证深度与混合的提交次序。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Sequence

from .geometry import Mat3, compose_affine, mat3_apply


@dataclass(frozen=True)
class Vertex:
    x: float
    y: float
    r: float = 1.0
    g: float = 1.0
    b: float = 1.0
    a: float = 1.0
    z: float = 0.5


@dataclass
class Material:
    translucent: bool = False
    depth_test: bool = True
    depth_write: bool = True
    blend: str = "alpha"  # 目前仅支持 straight-alpha


@dataclass
class Triangle:
    seq: int
    verts: tuple[Vertex, Vertex, Vertex]
    material: Material = field(default_factory=Material)
    paper_id: int = -1
    submission: int = -1
    label: str = ""

    def xs(self) -> list[float]:
        return [v.x for v in self.verts]

    def ys(self) -> list[float]:
        return [v.y for v in self.verts]


@dataclass
class Scene:
    name: str
    width: int
    height: int
    triangles: list[Triangle] = field(default_factory=list)
    background: tuple[float, float, float] = (0.075, 0.09, 0.12)
    background_alpha: float = 1.0
    description: str = ""
    submission_cursor: int = 0

    # ---- 构建辅助 ----
    def add_triangle(
        self,
        p0: tuple[float, float],
        p1: tuple[float, float],
        p2: tuple[float, float],
        color: tuple[float, float, float, float] | tuple[float, float, float] = (1, 1, 1, 1),
        z: float | tuple[float, float, float] = 0.5,
        translucent: bool | None = None,
        depth_test: bool = True,
        depth_write: bool | None = None,
        paper_id: int = -1,
        submission: int = -1,
        label: str = "",
        vertex_colors: Sequence[tuple[float, float, float]] | None = None,
    ) -> Triangle:
        if len(color) == 3:
            cr, cg, cb = color
            ca = 1.0
        else:
            cr, cg, cb, ca = color  # type: ignore[misc]
        is_trans = ca < 0.999 if translucent is None else translucent
        if depth_write is None:
            depth_write = not is_trans
        if isinstance(z, (int, float)):
            zs = (float(z), float(z), float(z))
        else:
            zs = (float(z[0]), float(z[1]), float(z[2]))
        if vertex_colors is None:
            cs = ((cr, cg, cb),) * 3
        else:
            cs = tuple(vertex_colors)  # type: ignore[assignment]
        verts = (
            Vertex(p0[0], p0[1], cs[0][0], cs[0][1], cs[0][2], ca, zs[0]),
            Vertex(p1[0], p1[1], cs[1][0], cs[1][1], cs[1][2], ca, zs[1]),
            Vertex(p2[0], p2[1], cs[2][0], cs[2][1], cs[2][2], ca, zs[2]),
        )
        seq = len(self.triangles)
        tri = Triangle(
            seq=seq,
            verts=verts,
            material=Material(
                translucent=is_trans,
                depth_test=depth_test,
                depth_write=depth_write,
            ),
            paper_id=paper_id,
            submission=submission if submission >= 0 else seq,
            label=label,
        )
        self.triangles.append(tri)
        return tri

    def add_quad(
        self,
        cx: float,
        cy: float,
        w: float,
        h: float,
        color,
        rotation_deg: float = 0.0,
        transform: Mat3 | None = None,
        z: float | tuple[float, float, float] = 0.5,
        translucent: bool | None = None,
        depth_test: bool = True,
        depth_write: bool | None = None,
        paper_id: int = -1,
        submission: int | None = None,
        label: str = "",
        vertex_colors: Sequence[tuple[float, float, float]] | None = None,
    ) -> tuple[Triangle, Triangle]:
        """添加轴对齐/旋转矩形（两张三角形），返回这两张三角形。"""
        sub = self.next_submission() if submission is None else submission
        local = [
            (-w / 2, -h / 2),
            (w / 2, -h / 2),
            (w / 2, h / 2),
            (-w / 2, h / 2),
        ]
        m = compose_affine(tx=cx, ty=cy, rotation_deg=rotation_deg)
        pts = [mat3_apply(m, x, y) for x, y in local]
        if transform is not None:
            pts = [mat3_apply(transform, x, y) for x, y in pts]
        t0 = self.add_triangle(
            pts[0], pts[1], pts[2], color, z=z,
            translucent=translucent, depth_test=depth_test,
            depth_write=depth_write,
            paper_id=paper_id, submission=sub, label=label + "#a",
            vertex_colors=vertex_colors,
        )
        t1 = self.add_triangle(
            pts[0], pts[2], pts[3], color, z=z,
            translucent=translucent, depth_test=depth_test,
            depth_write=depth_write,
            paper_id=paper_id, submission=sub, label=label + "#b",
            vertex_colors=vertex_colors,
        )
        return t0, t1

    def next_submission(self) -> int:
        s = self.submission_cursor
        self.submission_cursor += 1
        return s

    def transformed_copy(self, matrix: Mat3) -> "Scene":
        """返回所有顶点再经过一次矩阵变换的新场景（演示显式变换阶段）。"""
        out = Scene(self.name + "_xf", self.width, self.height,
                    background=self.background, description=self.description)
        for tri in self.triangles:
            nvs = []
            for v in tri.verts:
                nx, ny = mat3_apply(matrix, v.x, v.y)
                nvs.append(Vertex(nx, ny, v.r, v.g, v.b, v.a, v.z))
            nt = Triangle(
                seq=tri.seq,
                verts=tuple(nvs),  # type: ignore[arg-type]
                material=tri.material,
                paper_id=tri.paper_id,
                submission=tri.submission,
                label=tri.label,
            )
            out.triangles.append(nt)
        out.submission_cursor = self.submission_cursor
        return out


# ---------------------------------------------------------------------------
# 内置场景
# ---------------------------------------------------------------------------

def make_empty_scene(width: int = 96, height: int = 72) -> Scene:
    return Scene(
        "empty", width, height,
        description="空场景：没有任何图元，输出应为纯背景，所有负载计数为 0。",
    )


def make_giant_scene(width: int = 128, height: int = 96) -> Scene:
    """覆盖全屏的大面 + 超宽三角形，用于测量超大图元的跨箱数量。"""
    s = Scene(
        "giant", width, height,
        background=(0.05, 0.06, 0.09),
        description="3 个覆盖全屏或横跨所有列的超大三角形，验证保守分箱与跨箱计数。",
    )
    s.add_triangle(
        (-width * 0.2, -height * 0.2),
        (width * 1.2, height * 0.1),
        (width * 0.5, height * 1.25),
        (0.85, 0.25, 0.2, 1.0), z=0.9, label="giant-red",
    )
    s.add_triangle(
        (-width * 0.25, height * 1.15),
        (width * 1.25, height * 0.55),
        (width * 0.2, -height * 0.2),
        (0.2, 0.75, 0.45, 0.85), z=0.6, label="giant-green",
    )
    s.add_triangle(
        (-width * 0.1, height * 0.45),
        (width * 1.1, height * 0.2),
        (width * 1.1, height * 0.7),
        (0.25, 0.45, 0.95, 0.8), z=0.3, label="giant-blue-band",
    )
    return s


def make_dense_scene(
    width: int = 128,
    height: int = 96,
    count: int = 220,
    seed: int = 20261125,
) -> Scene:
    """集中在小区域内的密集小三角形（带深度与顶点色）。"""
    rng = random.Random(seed)
    s = Scene(
        "dense", width, height,
        background=(0.08, 0.08, 0.11),
        description=f"{count} 个密集重叠、集中在中心小区域的三角形。",
    )
    cx, cy = width * 0.5, height * 0.52
    spread_x, spread_y = width * 0.22, height * 0.24
    for i in range(count):
        x = cx + rng.gauss(0, spread_x * 0.55)
        y = cy + rng.gauss(0, spread_y * 0.55)
        size = rng.uniform(3.0, 11.0)
        rot = rng.uniform(0, math.tau)
        local = [(0.0, -size), (size * 0.9, size * 0.7), (-size * 0.9, size * 0.7)]
        m = compose_affine(tx=x, ty=y, rotation_deg=math.degrees(rot))
        pts = [mat3_apply(m, px, py) for px, py in local]
        base_h = rng.random()
        col = _hsv_to_rgb(0.55 + 0.35 * base_h, 0.65, 0.95)
        z = rng.uniform(0.15, 0.95)
        vcols = [
            tuple(min(1.0, c * k) for c in col)
            for k in (1.0, 0.55, 0.75)
        ]
        s.add_triangle(
            pts[0], pts[1], pts[2],
            color=(*col, 1.0), z=(z, z + rng.uniform(-0.08, 0.08), z),
            vertex_colors=vcols, label=f"dense-{i}",
        )
    return s


def make_tiny_scene(width: int = 64, height: int = 48) -> Scene:
    """极小/亚像素三角形，验证不会漏箱、漏像素。"""
    s = Scene("tiny", width, height, description="极小与亚像素三角形边界用例。")
    # 覆盖像素中心的 2px 小三角
    s.add_triangle((10, 10), (13, 10), (11, 13), (1, 0.8, 0.2, 1), label="tiny-cover")
    # 亚像素：可能没有任何中心被覆盖，但必须被分箱
    s.add_triangle((30.1, 20.1), (30.4, 20.1), (30.2, 20.4),
                   (0.3, 0.9, 1, 1), label="subpixel")
    # 单像素附近
    s.add_triangle((44.2, 30.2), (46.8, 30.4), (45.1, 33.0),
                   (0.9, 0.4, 0.9, 1), label="small")
    return s


def make_paper_overflow_scene(
    width: int = 96,
    height: int = 72,
    tile: int = 24,
) -> Scene:
    """半透明彩纸溢出实验场景。

    瓦片网格 4×3（tile=24）。相邻瓦片 A=(1,1)（像素 x24..48）、B=(2,1)
    （x48..72）是主角：一系列小彩纸按全局提交序号先密集落入 A、再落入 B，
    使两块在**不同提交序号**达到桶容量；一张大彩纸 P*（paper_id=777）骑在
    A/B 分界线 x=48 上，对两块都有真实片元贡献。底部是不透明可信底图。

    所有贴边元素都内缩 ≥2px，避免保守外扩 1px 改变成员关系，便于读出序号。
    """
    s = Scene(
        "paper-overflow", width, height,
        background=(0.05, 0.06, 0.09),
        description="半透明彩纸：相邻瓦片在不同提交序号溢出，一张大纸横跨两块。",
    )

    # 可信底图：单个覆盖全屏的不透明大三角（只占 1 个提交序号）
    s.add_triangle(
        (-width * 0.2, -height * 0.2),
        (width * 1.2, height * 0.0),
        (width * 0.5, height * 1.3),
        (0.16, 0.18, 0.24, 1.0), z=0.98, label="table",
    )
    # A/B 底色块，内缩 3px，严格只落在各自瓦片（随分辨率等比缩放）
    sx = width / 96.0
    sy = height / 72.0
    s.add_quad(36 * sx, 36 * sy, 17 * sx, 17 * sy,
               (0.32, 0.25, 0.15, 1.0), z=0.92, label="base-A")
    s.add_quad(60 * sx, 36 * sy, 17 * sx, 17 * sy,
               (0.14, 0.22, 0.32, 1.0), z=0.92, label="base-B")

    def P(x, y, w, h, rgba, rot=0.0, pid=0, z=0.5, label="paper"):
        return s.add_quad(x * sx, y * sy, w * sx, h * sy, rgba,
                          rotation_deg=rot, z=z, paper_id=pid, label=label)

    # ---- 先在 A 内放 3 张纸（6 个三角形），A 会明显更早溢出 ----
    a_spots = [
        (32, 31, 9, 8, (1.0, 0.35, 0.30, 0.55), 12),
        (40, 32, 8, 9, (1.0, 0.75, 0.25, 0.5), -10),
        (35, 41, 10, 7, (0.95, 0.45, 0.85, 0.5), 18),
    ]
    for i, (x, y, w, h, c, rot) in enumerate(a_spots):
        P(x, y, w, h, c, rot=rot, pid=100 + i, label=f"A-note-{i}")

    # ---- 再在 B 内放 2 张纸（4 个三角形），B 更晚才溢出 ----
    b_spots = [
        (56, 32, 9, 8, (0.4, 0.9, 1.0, 0.5), -8),
        (64, 40, 8, 8, (0.7, 0.5, 1.0, 0.55), 14),
    ]
    for i, (x, y, w, h, c, rot) in enumerate(b_spots):
        P(x, y, w, h, c, rot=rot, pid=200 + i, label=f"B-note-{i}")

    # ---- 关键跨瓦片大彩纸 P*：中心压在 A/B 分界线 x=48 上 ----
    P(48, 37, 27, 12, (1.0, 0.95, 0.35, 0.42), rot=-6,
      pid=777, z=0.45, label="bridge-paper")

    # ---- 溢出之后的收尾纸，检验严格按序补绘 ----
    P(38, 44, 8, 6, (0.2, 1.0, 0.8, 0.5), rot=16, pid=301, label="A-tail")
    P(60, 33, 8, 6, (1.0, 0.4, 0.6, 0.5), rot=-12, pid=302, label="B-tail")
    P(48, 29, 22, 6, (0.9, 0.9, 0.95, 0.35), rot=3, pid=778, label="bridge-thin")

    return s


def make_midway_scene(
    width: int = 96,
    height: int = 48,
    capacity: int = 4,
) -> Scene:
    """专门构造“跨多瓦图元在中途瓦片顶爆候选桶”的场景。

    规范观察参数：tile=24、bucket_capacity=capacity(=4)。瓦片网格 4×2。
    前 ``capacity`` 个小三角严格只落在中间瓦片 R=(1,0)，把该桶恰好填满
    （尚未溢出）；随后的大桥三角横跨 (0,0)、(1,0)、(2,0)，按 ty→tx 顺序
    处理到它的第 2 个瓦片 R 时，R 已满，于是溢出事件的
    ``midway_in_primitive=True``。这让 continuation 与 rebin 在完全相同的
    “图元跨瓦中途”决策点上可直接比较。
    """
    s = Scene(
        "midway-overflow", width, height,
        background=(0.05, 0.06, 0.09),
        description="跨三瓦的大桥图元在第二个瓦片中途触发候选桶溢出。",
    )
    # 不放全屏底图：任何全屏图元都会占用 R 桶槽位，破坏“恰好填满”的构造。
    # 可信底图由清屏背景在第一张半透明图元前捕获。
    for i in range(capacity):
        cx = 34 + (i % 3) * 2
        cy = 9 + (i // 3) * 3
        s.add_triangle(
            (cx, cy - 2), (cx + 3, cy + 2), (cx - 3, cy + 2),
            (0.3 + 0.1 * (i % 5), 0.8, 0.9, 0.85),
            paper_id=100 + i, label=f"r-fill-{i}",
        )
    s.add_triangle(
        (8, 20), (62, 6), (60, 22),
        (1.0, 0.85, 0.2, 0.6), paper_id=777, z=0.45,
        label="midway-bridge",
    )
    return s


def make_depth_order_scene(width: int = 64, height: int = 48) -> Scene:
    """深度+混合提交次序敏感场景：乱序执行会得到错误像素。"""
    s = Scene("depth-order", width, height,
              background=(0.02, 0.02, 0.03,),
              description="重叠半透明面与深度写入，检验提交次序保证。")
    s.add_quad(32, 24, 40, 30, (0.2, 0.2, 0.25, 1), z=0.95, label="base")
    # 三张大面积重叠纸，z 交错，其中一张开深度写
    s.add_quad(26, 22, 26, 24, (1, 0.25, 0.25, 0.6), z=0.6,
               depth_write=True, label="red-zwrite")
    s.add_quad(38, 26, 26, 24, (0.25, 1, 0.4, 0.55), z=0.4,
               depth_test=True, depth_write=False, label="green-test")
    s.add_quad(32, 30, 30, 18, (0.3, 0.5, 1, 0.55), z=0.8,
               depth_test=True, depth_write=False, label="blue-far")
    return s


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i %= 6
    table = [
        (v, t, p), (q, v, p), (p, v, t),
        (p, q, v), (t, p, v), (v, p, q),
    ]
    return table[i]


SCENE_FACTORIES = {
    "paper": make_paper_overflow_scene,
    "dense": make_dense_scene,
    "giant": make_giant_scene,
    "empty": make_empty_scene,
    "tiny": make_tiny_scene,
    "depth-order": make_depth_order_scene,
    "midway": make_midway_scene,
}


def build_scene(name: str, width: int | None = None, height: int | None = None) -> Scene:
    if name not in SCENE_FACTORIES:
        raise KeyError(f"未知场景: {name}（可选: {sorted(SCENE_FACTORIES)}）")
    fn = SCENE_FACTORIES[name]
    if name in ("paper",):
        return fn(width or 96, height or 72)  # type: ignore[misc]
    if name == "dense":
        return fn(width or 128, height or 96)  # type: ignore[misc]
    if name in ("giant",):
        return fn(width or 128, height or 96)
    if name == "empty":
        return fn(width or 96, height or 72)
    if name == "tiny":
        return fn(width or 64, height or 48)
    if name == "depth-order":
        return fn(width or 64, height or 48)
    if name == "midway":
        return fn(width or 96, height or 48)
    return fn()  # type: ignore[call-arg]
