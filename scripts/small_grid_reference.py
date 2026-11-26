#!/usr/bin/env python3
"""小网格生成与参考绘制脚本（可在任意净目录运行）。

用法::

    python scripts/small_grid_reference.py --out "$(mktemp -d)"

它等价于 ``python -m tile_raster mini-grid``，额外保证直接以文件路径调用时
也能找到仓库内的 ``tile_raster`` 包。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.mini_grid import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
