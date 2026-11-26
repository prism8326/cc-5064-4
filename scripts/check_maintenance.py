#!/usr/bin/env python3
"""维护校验：文档声明 vs 代码复算 + 旧样例净目录运行。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.maintenance import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
