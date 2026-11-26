#!/usr/bin/env python3
"""瓦片规则校验脚本（分箱/瓦片边界/片元归属/覆盖集合/溢出状态）。

可在净目录直接运行，证据默认写到 ``./tile_rules_out`` 与
``./tile_rules_report.json``；文档核对通过模块位置定位仓库，不依赖当前目录。

用法::

    python scripts/check_tile_rules.py
    python scripts/check_tile_rules.py --clean --out "$(mktemp -d)/rules"
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.tile_rules import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
