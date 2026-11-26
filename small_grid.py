#!/usr/bin/env python3
"""仓库根便捷入口：生成小网格覆盖参考产物。

    python3 small_grid.py --out /tmp/mini
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.mini_grid import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
