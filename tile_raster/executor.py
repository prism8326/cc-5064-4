"""瓦片执行器：分箱结果 -> 每瓦片真实光栅 -> 写回完整图像。

最终像素全部由本模块调用 :func:`tile_raster.raster.draw_triangle` 产生，
不存在“把普通渲染结果切块显示”的路径。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .binning import BinConfig, TileBinner
from .imaging import float_rgb_to_bytes
from .raster import RasterTarget, TileStats, draw_triangle
from .scene import Scene


@dataclass
class ExecuteConfig:
    work_order: str = "serial"   # "serial" | "shuffle"
    seed: int = 1234
    commit_frame: bool = True


@dataclass
class TrustedBase:
    """不透明阶段之后、半透明彩纸之前的可信底图（整屏，瓦片执行器自产）。"""
    width: int
    height: int
    color: list[float]
    depth: list[float]
    cover: list[int]
    tiles_captured: set[tuple[int, int]] = field(default_factory=set)

    @classmethod
    def empty(cls, w: int, h: int) -> "TrustedBase":
        n = w * h
        return cls(w, h, [0.0] * n * 4, [1.0] * n, [0] * n)

    def capture_tile(self, tx: int, ty: int, target: RasterTarget,
                     tile_w: int, tile_h: int) -> None:
        for ly in range(target.h):
            gy = target.y0 + ly
            src_row = ly * target.w
            dst_row = gy * self.width + target.x0
            for lx in range(target.w):
                gi = dst_row + lx
                si = src_row + lx
                o4 = si * 4
                go4 = gi * 4
                self.color[go4:go4 + 4] = target.color[o4:o4 + 4]
                self.depth[gi] = target.depth[si]
                self.cover[gi] = target.cover[si]
        self.tiles_captured.add((tx, ty))

    def restore_into(self, tx: int, ty: int, target: RasterTarget) -> int:
        """从可信底图整区恢复瓦片，返回读取的像素数。"""
        if (tx, ty) not in self.tiles_captured:
            raise KeyError(f"可信底图中没有瓦片 ({tx},{ty}) 的检查点")
        for ly in range(target.h):
            gy = target.y0 + ly
            src_row = gy * self.width + target.x0
            dst_row = ly * target.w
            for lx in range(target.w):
                gi = src_row + lx
                di = dst_row + lx
                self.color[di * 4:di * 4 + 4] = self.color[gi * 4:gi * 4 + 4]
                target.depth[di] = self.depth[gi]
                target.cover[di] = self.cover[gi]
        return target.pixel_count


@dataclass
class TileDiagnostic:
    tx: int
    ty: int
    tile_index: int
    x0: int
    y0: int
    x1: int
    y1: int
    status: str                      # empty | immediate | overflow-continuation | overflow-rebin
    assigned_ids: int
    overflow_seq: int | None
    overflow_submission: int | None
    continuation_passes: int
    rebins: int
    base_rereads: int
    cache_hits: int
    exec_ms: float
    writeback_ms: float
    load_fragments: int
    load_pixel_tests: int
    load_score: float
    stats: dict

    def as_dict(self) -> dict:
        d = {
            "tx": self.tx, "ty": self.ty, "tile_index": self.tile_index,
            "rect": [self.x0, self.y0, self.x1, self.y1],
            "status": self.status,
            "assigned_ids": self.assigned_ids,
            "overflow_seq": self.overflow_seq,
            "overflow_submission": self.overflow_submission,
            "continuation_passes": self.continuation_passes,
            "rebins": self.rebins,
            "base_rereads": self.base_rereads,
            "cache_hits": self.cache_hits,
            "exec_ms": round(self.exec_ms, 6),
            "writeback_ms": round(self.writeback_ms, 6),
            "load_fragments": self.load_fragments,
            "load_pixel_tests": self.load_pixel_tests,
            "load_score": round(self.load_score, 3),
        }
        d.update(self.stats)
        return d


@dataclass
class RenderResult:
    width: int
    height: int
    rgb: bytes
    frame_color: list[float]
    coverage: list[int]
    bin_config: BinConfig
    exec_config: ExecuteConfig
    binner: TileBinner
    tile_diags: list[TileDiagnostic]
    order: list[int]
    timings: dict
    total_stats: TileStats
    trusted_base: TrustedBase

    @property
    def cols(self) -> int:
        return self.binner.cols

    @property
    def rows(self) -> int:
        return self.binner.rows

    def coverage_png_bytes(self) -> bytes:
        from .imaging import encode_png
        return encode_png(self.width, self.height, self.coverage_rgb())

    def final_png_bytes(self) -> bytes:
        from .imaging import encode_png
        return encode_png(self.width, self.height, self.rgb)

    def coverage_rgb(self) -> bytes:
        """实际覆盖次数（overdraw）热力图，叠瓦片边界。"""
        from .imaging import heat_color
        max_cov = max(1, max(self.coverage))
        out = bytearray(self.width * self.height * 3)
        for i, c in enumerate(self.coverage):
            if c <= 0:
                r, g, b = 12, 14, 22
            else:
                r, g, b = heat_color(0.15 + 0.85 * (c / max_cov) ** 0.7)
            out[i * 3] = r
            out[i * 3 + 1] = g
            out[i * 3 + 2] = b
        self._draw_grid(out, (255, 255, 255), 0.35)
        return bytes(out)

    def load_rgb(self) -> bytes:
        """每瓦片真实片元负载热力图，叠瓦片边界。"""
        from .imaging import heat_color
        out = bytearray(self.width * self.height * 3)
        loads = [d.load_fragments for d in self.tile_diags]
        max_load = max(1, max(loads, default=1))
        by_index = {d.tile_index: d for d in self.tile_diags}
        tw, th = self.bin_config.tile_w, self.bin_config.tile_h
        for ty in range(self.rows):
            for tx in range(self.cols):
                idx = ty * self.cols + tx
                d = by_index.get(idx)
                load = d.load_fragments if d else 0
                r, g, b = heat_color(load / max_load if load else 0.0)
                x0, y0 = tx * tw, ty * th
                x1, y1 = min(self.width, x0 + tw), min(self.height, y0 + th)
                for y in range(y0, y1):
                    row = y * self.width
                    for x in range(x0, x1):
                        o = (row + x) * 3
                        out[o] = int(r * 0.85 + 18)
                        out[o + 1] = int(g * 0.85 + 18)
                        out[o + 2] = int(b * 0.85 + 18)
        self._draw_grid(out, (255, 255, 255), 0.5)
        return bytes(out)

    def load_png_bytes(self) -> bytes:
        from .imaging import encode_png
        return encode_png(self.width, self.height, self.load_rgb())

    def _draw_grid(self, out: bytearray, color: tuple[int, int, int],
                   alpha: float = 1.0) -> None:
        tw, th = self.bin_config.tile_w, self.bin_config.tile_h
        w, h = self.width, self.height

        def setpx(x: int, y: int) -> None:
            if 0 <= x < w and 0 <= y < h:
                o = (y * w + x) * 3
                out[o] = int(out[o] * (1 - alpha) + color[0] * alpha)
                out[o + 1] = int(out[o + 1] * (1 - alpha) + color[1] * alpha)
                out[o + 2] = int(out[o + 2] * (1 - alpha) + color[2] * alpha)

        for tx in range(0, self.cols + 1):
            x = min(w - 1, tx * tw)
            for y in range(h):
                setpx(x, y)
        for ty in range(0, self.rows + 1):
            y = min(h - 1, ty * th)
            for x in range(w):
                setpx(x, y)

    def metrics_dict(self) -> dict:
        rep = self.binner.report().as_dict()
        return {
            "width": self.width,
            "height": self.height,
            "tile": [self.bin_config.tile_w, self.bin_config.tile_h],
            "bucket_capacity": self.bin_config.bucket_capacity,
            "bin_strategy": self.bin_config.strategy,
            "work_order": self.exec_config.work_order,
            "seed": self.exec_config.seed,
            "timings_ms": {k: round(v * 1000.0, 3) for k, v in self.timings.items()},
            "total": self.total_stats.as_dict(),
            "binning": rep,
            "tiles": [d.as_dict() for d in self.tile_diags],
            "order": self.order,
        }


class TileExecutor:
    def __init__(self, scene: Scene, bin_config: BinConfig,
                 exec_config: ExecuteConfig | None = None):
        self.scene = scene
        self.bin_config = bin_config
        self.exec_config = exec_config or ExecuteConfig()
        self.binner = TileBinner(scene, bin_config)

    def run(self, *, trace=None) -> RenderResult:
        t0 = time.perf_counter()
        self.binner.bin_scene()
        t_bin = time.perf_counter() - t0

        w, h = self.scene.width, self.scene.height
        cols, rows = self.binner.cols, self.binner.rows
        tw, th = self.bin_config.tile_w, self.bin_config.tile_h

        frame_color = self._initialized_frame_color()
        frame_cover = [0] * (w * h)
        base = TrustedBase.empty(w, h)

        indices = list(range(cols * rows))
        if self.exec_config.work_order == "shuffle":
            random.Random(self.exec_config.seed).shuffle(indices)
        elif self.exec_config.work_order != "serial":
            raise ValueError("work_order 只能是 serial 或 shuffle")

        diags: list[TileDiagnostic] = []
        total = TileStats()
        t_process = 0.0
        t_writeback = 0.0

        for tile_index in indices:
            ty, tx = divmod(tile_index, cols)
            x0, y0 = tx * tw, ty * th
            x1, y1 = min(w, x0 + tw), min(h, y0 + th)
            target = RasterTarget.create(
                x0, y0, x1, y1,
                bg=self.scene.background,
                bg_alpha=self.scene.background_alpha,
            )
            stats = TileStats()
            base_rereads = 0
            cache_hits = 0
            cont_passes = 0
            t_draw_start = time.perf_counter()

            key = (tx, ty)
            b = self.binner.bins.get(key)
            first_translucent_captured = False

            def draw_seq(seq: int, replay: bool = False) -> None:
                nonlocal first_translucent_captured
                tri = self.scene.triangles[seq]
                if (tri.material.translucent and not first_translucent_captured):
                    # 第一张半透明纸之前，把不透明成果固化为可信底图。
                    base.capture_tile(tx, ty, target, tw, th)
                    first_translucent_captured = True
                draw_triangle(
                    tri, target, stats, trace=trace,
                    count_as_replay=replay,
                )

            if b is not None:
                for seq in b.ids:
                    draw_seq(seq)
                if self.bin_config.strategy == "continuation":
                    for chunk in self.binner.iter_continuation_chunks(key):
                        cont_passes += 1
                        for seq in chunk:
                            draw_seq(seq, replay=True)
                assigned = len(self.binner.resolve_ordered_ids(key))
                overflow_seq = b.overflow_seq
                overflow_sub = b.overflow_submission
                rebins = b.rebins
            else:
                assigned = 0
                overflow_seq = None
                overflow_sub = None
                rebins = 0

            exec_ms = (time.perf_counter() - t_draw_start) * 1000.0
            t_process += exec_ms / 1000.0

            wb0 = time.perf_counter()
            self._commit_tile(target, frame_color, frame_cover, w)
            wb_ms = (time.perf_counter() - wb0) * 1000.0
            t_writeback += wb_ms / 1000.0

            total.add(stats)
            if b is None or assigned == 0:
                status = "empty"
            elif overflow_seq is None:
                status = "immediate"
            elif self.bin_config.strategy == "continuation":
                status = "overflow-continuation"
            else:
                status = "overflow-rebin"

            load = float(stats.covered)
            diags.append(TileDiagnostic(
                tx=tx, ty=ty, tile_index=tile_index,
                x0=x0, y0=y0, x1=x1, y1=y1,
                status=status, assigned_ids=assigned,
                overflow_seq=overflow_seq,
                overflow_submission=overflow_sub,
                continuation_passes=cont_passes, rebins=rebins,
                base_rereads=base_rereads, cache_hits=cache_hits,
                exec_ms=exec_ms, writeback_ms=wb_ms,
                load_fragments=stats.covered,
                load_pixel_tests=stats.pixel_tests,
                load_score=load,
                stats=stats.as_dict(),
            ))

        t_q0 = time.perf_counter()
        rgb = float_rgb_to_bytes(frame_color, w, h)
        t_quant = time.perf_counter() - t_q0

        timings = {
            "bin": t_bin,
            "process": t_process,
            "writeback": t_writeback,
            "quantize": t_quant,
            "total": time.perf_counter() - t0,
        }
        return RenderResult(
            width=w, height=h, rgb=rgb, frame_color=frame_color,
            coverage=frame_cover, bin_config=self.bin_config,
            exec_config=self.exec_config, binner=self.binner,
            tile_diags=diags, order=indices, timings=timings,
            total_stats=total, trusted_base=base,
        )

    def _initialized_frame_color(self) -> list[float]:
        w, h = self.scene.width, self.scene.height
        bg = self.scene.background
        color = [0.0] * (w * h * 4)
        for i in range(w * h):
            color[i * 4] = bg[0]
            color[i * 4 + 1] = bg[1]
            color[i * 4 + 2] = bg[2]
            color[i * 4 + 3] = self.scene.background_alpha
        return color

    @staticmethod
    def _commit_tile(target: RasterTarget, frame_color: list[float],
                     frame_cover: list[int], screen_w: int) -> None:
        tw = target.w
        for ly in range(target.h):
            gy = target.y0 + ly
            tile_row = ly * tw
            screen_row = gy * screen_w + target.x0
            for lx in range(tw):
                si = tile_row + lx
                gi = screen_row + lx
                o4 = si * 4
                go4 = gi * 4
                frame_color[go4] = target.color[o4]
                frame_color[go4 + 1] = target.color[o4 + 1]
                frame_color[go4 + 2] = target.color[o4 + 2]
                frame_color[go4 + 3] = target.color[o4 + 3]
                frame_cover[gi] = target.cover[si]
