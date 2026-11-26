#!/usr/bin/env python3
"""仓库根便捷入口：瓦片规则校验。

    python3 check_tile_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tile_raster.tile_rules import (  # noqa: E402,F401
    Issue,
    check_tile_rules,
    coverage_set_from_result,
    evaluate_mini_grid,
    execution_anomalies,
    long_chain_scope,
    main,
    per_tile_coverage_sets,
    rule_evidence,
    run_tile_rules_suite,
    verify_tile_rules,
)

verify_all = run_tile_rules_suite

if __name__ == "__main__":
    raise SystemExit(main())
