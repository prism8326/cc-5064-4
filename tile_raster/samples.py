"""瓦片光栅“旧样例”批量运行与陈旧产物处置。

仓库历史上在 ``out/`` 留下过一批手工渲染的 PNG（dense 还是旧的 128×96
默认尺寸等）。本模块保证这些旧样例配置全部能在**净目录**里从头运行到底：
分箱 → 瓦片执行 → 三张 PNG + metrics，并把覆盖集合/溢出状态对应到执行证据
（:func:`tile_raster.tile_rules.rule_evidence`）。

``out/`` 已在 :file:`.gitignore` 中，属于可再生产物；要处置陈旧材料：

```bash
python3 -m tile_raster samples --out out --legacy-names   # 原地刷新旧文件名
python3 -m tile_raster samples --clean --out "$(mktemp -d)"  # 净目录复跑
```
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .binning import BinConfig
from .executor import ExecuteConfig, TileExecutor
from .scene import build_scene
from .tile_rules import rule_evidence

# 与历史 out/*.png 尺寸/名称对应的旧样例配置。
LEGACY_SPECS = [
    {"stem": "paper", "scene": "paper", "width": 96, "height": 72,
     "tile": 24, "bucket_capacity": 6, "strategy": "continuation",
     "work_order": "serial", "seed": 7},
    {"stem": "paper_big", "scene": "paper", "width": 384, "height": 288,
     "tile": 48, "bucket_capacity": 12, "strategy": "continuation",
     "work_order": "shuffle", "seed": 7},
    {"stem": "dense", "scene": "dense", "width": 128, "height": 96,
     "tile": 16, "bucket_capacity": 128, "strategy": "continuation",
     "work_order": "serial", "seed": 7},
    {"stem": "giant", "scene": "giant", "width": 128, "height": 96,
     "tile": 16, "bucket_capacity": 64, "strategy": "continuation",
     "work_order": "serial", "seed": 7},
    {"stem": "empty", "scene": "empty", "width": 96, "height": 72,
     "tile": 32, "bucket_capacity": 16, "strategy": "continuation",
     "work_order": "serial", "seed": 7},
]

# 所有内置场景的规范小样（“旧样例运行到底”的最小集合）。
CANONICAL_SPECS = [
    {"stem": "paper", "scene": "paper", "width": 96, "height": 72,
     "tile": 24, "bucket_capacity": 6},
    {"stem": "midway", "scene": "midway", "width": 96, "height": 48,
     "tile": 24, "bucket_capacity": 4},
    {"stem": "tiny", "scene": "tiny", "width": 64, "height": 48,
     "tile": 16, "bucket_capacity": 8},
    {"stem": "depth-order", "scene": "depth-order", "width": 64, "height": 48,
     "tile": 16, "bucket_capacity": 8},
    {"stem": "dense-small", "scene": "dense", "width": 96, "height": 72,
     "tile": 16, "bucket_capacity": 128},
    {"stem": "giant-small", "scene": "giant", "width": 96, "height": 72,
     "tile": 16, "bucket_capacity": 64},
    {"stem": "empty-small", "scene": "empty", "width": 96, "height": 72,
     "tile": 16, "bucket_capacity": 16},
]


def run_sample(spec: dict) -> dict:
    scene = build_scene(spec["scene"], spec["width"], spec["height"])
    cfg = BinConfig(
        tile_w=spec["tile"], tile_h=spec["tile"],
        bucket_capacity=spec.get("bucket_capacity", 8),
        strategy=spec.get("strategy", "continuation"),
        max_bucket_capacity=spec.get("max_bucket_capacity", 100000),
    )
    ex = ExecuteConfig(
        work_order=spec.get("work_order", "serial"),
        seed=spec.get("seed", 7),
    )
    result = TileExecutor(scene, cfg, ex).run()
    evidence = rule_evidence(result)
    return {
        "spec": spec,
        "result": result,
        "evidence": evidence,
        "pngs": {
            "final": result.final_png_bytes(),
            "coverage": result.coverage_png_bytes(),
            "load": result.load_png_bytes(),
        },
    }


def _check_png_magic(blob: bytes) -> bool:
    return blob[:8] == b"\x89PNG\r\n\x1a\n"


def render_sample_bundle(outdir: str | Path, specs=LEGACY_SPECS,
                         legacy_names: bool = True) -> dict:
    """把样例 PNG/metrics 写到 outdir；legacy_names 时沿用历史 *_cover 命名。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"samples": []}
    for spec in specs:
        ran = run_sample(spec)
        result = ran["result"]
        stem = spec["stem"]
        names = {
            "final": out / f"{stem}_final.png",
            "coverage": out / (f"{stem}_cover.png" if legacy_names
                               else f"{stem}_coverage.png"),
            "load": out / f"{stem}_load.png",
        }
        # 同时写规范 coverage 名，避免新代码只认 coverage.png。
        names["coverage_canonical"] = out / f"{stem}_coverage.png"
        for key, path in names.items():
            png_key = "coverage" if key.startswith("coverage") else key
            path.write_bytes(ran["pngs"][png_key])
        metrics = result.metrics_dict()
        (out / f"{stem}_metrics.json").write_text(
            json.dumps({
                "spec": spec,
                "evidence_ok": ran["evidence"]["ok"],
                "anomalies": ran["evidence"]["anomalies"],
                "totals": ran["evidence"]["totals"],
                "metrics": metrics,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest["samples"].append({
            "stem": stem,
            "files": {k: str(v.name) for k, v in names.items()},
            "evidence_ok": ran["evidence"]["ok"],
            "totals": ran["evidence"]["totals"],
        })
    (out / "samples-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify_legacy_samples(outdir: str | Path | None = None):
    """维护校验调用：全部规范旧样例在净目录运行到底且证据自洽。"""
    lines: list[str] = []
    target = Path(outdir) if outdir else Path(
        tempfile.mkdtemp(prefix="tile-samples-clean-"))
    target.mkdir(parents=True, exist_ok=True)

    all_ok = True
    sample_report = {"outdir": str(target), "samples": []}
    for spec in CANONICAL_SPECS:
        ran = run_sample(spec)
        ev = ran["evidence"]
        png_ok = all(_check_png_magic(b) for b in ran["pngs"].values())
        ok = ev["ok"] and png_ok
        all_ok &= ok
        lines.append(
            f"[{'PASS' if ok else 'FAIL'}] sample:{spec['stem']} "
            f"{spec['width']}x{spec['height']} tile{spec['tile']} cap"
            f"{spec['bucket_capacity']} -> "
            f"{ev['totals']['covered_fragments']} 片元 / "
            f"{ev['totals']['overflow_tiles']} 溢瓦 / PNG签名"
            f"{'OK' if png_ok else 'BAD'}"
        )
        if not ok:
            for a in ev["anomalies"][:5]:
                lines.append(f"    anomaly {a['check_id']}: {a['msg']}")
        sample_report["samples"].append({
            "stem": spec["stem"], "ok": ok,
            "totals": ev["totals"], "anomalies": ev["anomalies"],
            "png_magic_ok": png_ok,
        })

    # 历史大样例 paper_big 也要运行到底（只核对证据与 PNG，不做串行全参考）。
    big = dict(LEGACY_SPECS[1])
    try:
        ran = run_sample(big)
        png_ok = all(_check_png_magic(b) for b in ran["pngs"].values())
        ok = ran["evidence"]["ok"] and png_ok
        all_ok &= ok
        lines.append(
            f"[{'PASS' if ok else 'FAIL'}] legacy-big:{big['stem']} "
            f"{big['width']}x{big['height']} 运行到底，"
            f"{ran['evidence']['totals']['covered_fragments']} 片元")
        sample_report["paper_big_ok"] = ok
    except Exception as e:
        all_ok = False
        sample_report["paper_big_ok"] = False
        lines.append(f"[FAIL] legacy-big:{big['stem']} 崩溃: "
                     f"{type(e).__name__}: {e}")

    sample_report["ok"] = all_ok
    return all_ok, sample_report, lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m tile_raster.samples",
        description="旧样例净目录复跑 / 刷新 out 下的陈旧 PNG 材料")
    p.add_argument("--out", default="out/samples")
    p.add_argument("--clean", action="store_true",
                   help="先删除并重建 --out（仅删除该显式目录）")
    p.add_argument("--legacy-names", action="store_true",
                   help="使用历史 *_cover.png 命名并包含 paper_big 等旧尺寸")
    p.add_argument("--verify-only", action="store_true",
                   help="只在临时净目录校验规范样例，不向 --out 写文件")
    args = p.parse_args(argv)

    if args.verify_only:
        ok, _, lines = verify_legacy_samples()
        print("\n".join(lines))
        return 0 if ok else 1

    out = Path(args.out)
    if args.clean and out.exists():
        import shutil
        shutil.rmtree(out)
    specs = LEGACY_SPECS if args.legacy_names else (
        LEGACY_SPECS + CANONICAL_SPECS)
    manifest = render_sample_bundle(out, specs=specs,
                                    legacy_names=args.legacy_names)
    ok = all(s["evidence_ok"] for s in manifest["samples"])
    print(f"样例包已写出：{out.resolve()}（{len(manifest['samples'])} 个样例）")
    for s in manifest["samples"]:
        print(f"  {'PASS' if s['evidence_ok'] else 'FAIL'} {s['stem']} "
              f"-> {s['files']}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
