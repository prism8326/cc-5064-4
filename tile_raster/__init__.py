"""计算式瓦片光栅渲染器。

公开入口：

* :func:`render_tiled` —— 配置并运行瓦片管线，返回 :class:`RenderResult`
* :func:`render_serial_reference` —— 简单串行参考路径
* :func:`run_overflow_lab` —— 彩纸溢出/重读对比实验
* :class:`TileExecutor` / :class:`BinConfig` / :class:`ExecuteConfig`
"""
from __future__ import annotations

from .binning import BinConfig, TileBinner
from .executor import ExecuteConfig, RenderResult, TileExecutor
from .overflow_lab import run_overflow_lab
from .raster import (
    BufferCapacityError,
    OrderUnrecoverableError,
    RasterTarget,
    TileStats,
    render_serial_reference,
)
from .scene import Scene, build_scene

__version__ = "1.0.0"

__all__ = [
    "render_tiled",
    "render_serial_reference",
    "run_overflow_lab",
    "TileExecutor",
    "TileBinner",
    "BinConfig",
    "ExecuteConfig",
    "RenderResult",
    "Scene",
    "build_scene",
    "RasterTarget",
    "TileStats",
    "BufferCapacityError",
    "OrderUnrecoverableError",
    "__version__",
]


def render_tiled(
    scene: Scene,
    *,
    tile_w: int = 16,
    tile_h: int | None = None,
    bucket_capacity: int = 8,
    strategy: str = "continuation",
    work_order: str = "serial",
    seed: int = 1234,
) -> RenderResult:
    """一行运行瓦片管线的便捷函数。"""
    cfg = BinConfig(
        tile_w=tile_w,
        tile_h=tile_h or tile_w,
        bucket_capacity=bucket_capacity,
        strategy=strategy,
    )
    ex = ExecuteConfig(work_order=work_order, seed=seed)
    return TileExecutor(scene, cfg, ex).run()
