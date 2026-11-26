"""兼容入口：维护/文档校验实现位于 :mod:`tile_raster.maintenance`。"""
from __future__ import annotations

from .maintenance import (  # noqa: F401
    check_documentation,
    compute_claims,
    extract_doc_claims,
    main,
    render_claims_block,
    run_maintenance_suite,
    update_documentation,
)

check_docs = check_documentation
run_maintenance_checks = run_maintenance_suite

if __name__ == "__main__":
    raise SystemExit(main())
