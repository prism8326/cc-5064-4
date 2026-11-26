#!/usr/bin/env python3
"""仓库根兼容入口；实现见 :mod:`tile_raster.maintenance`。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.maintenance import *  # noqa: E402,F401,F403
from tile_raster.maintenance import main  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
