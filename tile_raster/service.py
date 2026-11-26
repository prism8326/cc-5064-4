"""图形视窗与命令行共用的应用服务层。

所有函数返回 JSON 可序列化对象；参数错误抛 :class:`ValueError`（中文消息），
由 HTTP 层转成界面可见的异常提示。
"""
from __future__ import annotations

import base64

from .benchmarks import benchmark_scenes, run_check_suite
from .binning import BinConfig
from .diagnostics import buffer_diagnosis
from .executor import ExecuteConfig, TileExecutor
from .overflow_lab import run_overflow_lab
from .scene import SCENE_FACTORIES, build_scene
from .tile_rules import rule_evidence, run_tile_rules_suite

LIMITS = {
    "width": (32, 640),
    "height": (24, 480),
    "tile": (4, 256),
    "bucket_capacity": (1, 100000),
    "extra_buffer_capacity": (1, 64),
    "seed": (0, 2_000_000_000),
}

SCENE_DEFAULTS = {
    "paper": (96, 72, 24, 6),
    "dense": (160, 120, 16, 128),
    "giant": (128, 96, 16, 64),
    "empty": (128, 96, 32, 16),
    "tiny": (64, 48, 16, 8),
    "depth-order": (64, 48, 16, 8),
    "midway": (96, 48, 24, 4),
}


def _int(params: dict, key: str, default: int) -> int:
    if key not in params or params[key] in (None, ""):
        return default
    try:
        return int(params[key])
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 必须是整数，收到 {params[key]!r}")


def _bounded(v: int, limit_key: str) -> int:
    lo, hi = LIMITS[limit_key]
    if not lo <= v <= hi:
        raise ValueError(f"参数 {limit_key}={v} 超出允许范围 [{lo}, {hi}]")
    return v


def _range_int(params: dict, key: str, default: int) -> int:
    return _bounded(_int(params, key, default), key)


def validate_render_params(params: dict) -> dict:
    scene = str(params.get("scene", "paper"))
    if scene not in SCENE_FACTORIES:
        raise ValueError(f"未知场景 {scene!r}，可选 {sorted(SCENE_FACTORIES)}")

    d_w, d_h, d_t, d_cap = SCENE_DEFAULTS[scene]
    width = _range_int(params, "width", d_w)
    height = _range_int(params, "height", d_h)
    tile_default = _int(params, "tile", d_t)
    tile_w = _bounded(_int(params, "tile_w", tile_default), "tile")
    tile_h = _bounded(_int(params, "tile_h", tile_default), "tile")
    bucket = _range_int(params, "bucket_capacity", d_cap)
    seed = _range_int(params, "seed", 7)

    strategy = str(params.get("strategy", "continuation"))
    if strategy not in ("continuation", "rebin"):
        raise ValueError("分箱策略只能是 continuation（分批续绘）或 rebin（重分桶）")
    order = str(params.get("work_order", "shuffle"))
    if order not in ("serial", "shuffle"):
        raise ValueError("工作组顺序只能是 serial 或 shuffle")

    return {
        "scene": scene, "width": width, "height": height,
        "tile_w": tile_w, "tile_h": tile_h,
        "bucket_capacity": bucket, "seed": seed,
        "strategy": strategy, "work_order": order,
    }


def _b64(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def render_payload(params: dict) -> dict:
    p = validate_render_params(params)
    scene = build_scene(p["scene"], p["width"], p["height"])
    cfg = BinConfig(
        tile_w=p["tile_w"], tile_h=p["tile_h"],
        bucket_capacity=p["bucket_capacity"],
        strategy=p["strategy"], max_bucket_capacity=100000,
    )
    ex = ExecuteConfig(work_order=p["work_order"], seed=p["seed"])
    result = TileExecutor(scene, cfg, ex).run()

    metrics = result.metrics_dict()
    diagnosis = buffer_diagnosis(metrics)
    evidence = rule_evidence(result)
    return {
        "ok": True,
        "params": p,
        "scene_description": scene.description,
        "image_png": _b64(result.final_png_bytes()),
        "coverage_png": _b64(result.coverage_png_bytes()),
        "load_png": _b64(result.load_png_bytes()),
        "metrics": metrics,
        "diagnosis": diagnosis,
        "rule_evidence": evidence,
        "anomalies": evidence["anomalies"],
    }


def lab_payload(params: dict) -> dict:
    bucket = _range_int(params, "bucket_capacity", 6)
    extra = _range_int(params, "extra_buffer_capacity", 1)
    seed = _range_int(params, "seed", 7)
    fault = bool(params.get("fault_no_base", False))
    scene = build_scene("paper", 96, 72)
    report = run_overflow_lab(
        scene, tile=24, bucket_capacity=bucket,
        extra_buffer_capacity=extra, seed=seed,
        trusted_base_available=not fault,
    )
    if report.get("fatal_error"):
        return {"ok": False, "error": report["fatal_error"],
                "error_type": "OrderUnrecoverableError", "lab": report}
    return {"ok": True, "lab": report}


def check_payload() -> dict:
    ok, report, lines = run_check_suite()
    slim = {
        "ok": ok,
        "lines": lines,
        "strategy_comparison": report["strategies"],
        "growth": {k: v for k, v in report["growth"].items()
                   if k not in ("giant_primitive_spans",)},
        "consistency_checks": report["consistency"]["checks"],
        "failed_cases": [
            c for c in report["consistency"]["matrix"] if not c["ok"]
        ][:20],
    }
    return slim


def bench_payload() -> dict:
    return {"ok": True, "benchmarks": benchmark_scenes(repeat=5)}


def tile_rules_payload(params: dict | None = None) -> dict:
    """瓦片规则校验的 API 形态：现场复算，不读手改数据。"""
    params = params or {}
    verify_docs = not bool(params.get("no_docs", False))
    ok, report, lines = run_tile_rules_suite(
        verify_docs=verify_docs, write_files=False)
    return {
        "ok": ok,
        "lines": lines,
        "issue_count": len(report.get("issues", [])),
        "failed_issues": [
            i for i in report.get("issues", []) if not i.get("ok")
        ],
        "mini_grid": report.get("claims", {}).get("mini_grid"),
        "long_chain_scope": report.get("claims", {}).get("long_chain_scope"),
    }
