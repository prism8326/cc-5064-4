"""兼容入口：瓦片规则校验实现位于 :mod:`tile_raster.tile_rules`。"""
from __future__ import annotations

from .tile_rules import (  # noqa: F401
    Issue,
    check_tile_rules,
    coverage_set_from_result,
    evaluate_mini_grid,
    execution_anomalies,
    long_chain_scope,
    main,
    make_chain_scene,
    per_tile_coverage_sets,
    rule_evidence,
    run_tile_rules_suite,
    serial_winner_map,
    verify_tile_rules,
    winner_seq_map_from_trace,
)

verify_all = run_tile_rules_suite
run_all_checks = run_tile_rules_suite

if __name__ == "__main__":
    raise SystemExit(main())
