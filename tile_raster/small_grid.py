"""兼容入口：小网格生成与独立参考绘制实现位于 :mod:`tile_raster.mini_grid`。"""
from __future__ import annotations

from .mini_grid import *  # noqa: F401,F403
from .mini_grid import main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
