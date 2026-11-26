#!/usr/bin/env python3
"""别名入口：等价于 scripts/small_grid_reference.py。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.mini_grid import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
