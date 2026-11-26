#!/usr/bin/env python3
"""仓库根便捷入口：维护/文档一致性校验。

    python3 check_maintenance.py [--update]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.maintenance import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
