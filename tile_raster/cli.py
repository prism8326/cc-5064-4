"""命令行辅助入口。

图形入口（推荐）：

    python -m tile_raster gui

命令行只做无界面辅助：headless 出图、自检、基准与溢出实验。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .benchmarks import benchmark_scenes, run_check_suite
from .binning import BinConfig
from .diagnostics import buffer_diagnosis
from .executor import ExecuteConfig, TileExecutor
from .imaging import encode_png
from .overflow_lab import run_overflow_lab
from .scene import build_scene
from .server import serve
from .service import render_payload


def _add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scene", default="paper",
                   choices=["paper", "dense", "giant", "empty", "tiny",
                            "depth-order", "midway"])
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument("--tile", type=int, default=None,
                   help="瓦片边长；缺省取场景推荐值")
    p.add_argument("--bucket-capacity", type=int, default=None,
                   help="候选桶容量；缺省取场景推荐值")
    p.add_argument("--strategy", default="continuation",
                   choices=["continuation", "rebin"])
    p.add_argument("--work-order", default="serial",
                   choices=["serial", "shuffle"])
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--outdir", default="out")


def cmd_gui(args) -> int:
    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_render(args) -> int:
    params = {
        "scene": args.scene,
        "width": args.width,
        "height": args.height,
        "tile": args.tile,
        "bucket_capacity": args.bucket_capacity,
        "strategy": args.strategy,
        "work_order": args.work_order,
        "seed": args.seed,
    }
    payload = render_payload({k: v for k, v in params.items() if v is not None})
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    import base64
    for key, name in (("image_png", "final.png"),
                      ("coverage_png", "coverage.png"),
                      ("load_png", "load.png")):
        b64 = payload[key].split(",", 1)[1]
        (outdir / name).write_bytes(base64.b64decode(b64))

    metrics = payload["metrics"]
    diag = payload["diagnosis"]
    (outdir / "metrics.json").write_text(
        json.dumps({"metrics": metrics, "diagnosis": diag},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"已写出 {outdir/'final.png'} / coverage.png / load.png / metrics.json")
    for line in diag["lines"]:
        print(f"  [{line['level']}] {line['msg']}")
    t = metrics["timings_ms"]
    print(f"耗时(ms): bin={t['bin']} process={t['process']} "
          f"writeback={t['writeback']} quantize={t['quantize']}")
    return 0


def cmd_check(args) -> int:
    ok, report, lines = run_check_suite()
    print("\n".join(lines))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化报告 -> {args.json}")
    return 0 if ok else 1


def cmd_bench(args) -> int:
    data = benchmark_scenes(repeat=args.repeat)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"基准报告 -> {args.out}")
    return 0


def cmd_lab(args) -> int:
    scene = build_scene("paper", 96, 72)
    rep = run_overflow_lab(
        scene, tile=24, bucket_capacity=args.bucket_capacity,
        extra_buffer_capacity=args.extra, seed=args.seed,
        trusted_base_available=not args.fault,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if not rep.get("fatal_error") else 2


def cmd_tile_rules(args) -> int:
    from .tile_rules import run_tile_rules_suite

    out = Path(args.out)
    if args.clean and out.exists():
        import shutil
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    ok, report, lines = run_tile_rules_suite(
        outdir=out, verify_docs=not args.no_docs)
    print("\n".join(lines))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


def cmd_maintenance(args) -> int:
    from .maintenance import run_maintenance_suite

    ok, report, lines = run_maintenance_suite(update=args.update)
    print("\n".join(lines))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


def cmd_mini_grid(args) -> int:
    from .mini_grid import main as mini_main

    return mini_main(["--out", args.out])


def cmd_samples(args) -> int:
    from .samples import main as samples_main

    argv = []
    if args.out:
        argv += ["--out", args.out]
    if args.clean:
        argv.append("--clean")
    if args.legacy_names:
        argv.append("--legacy-names")
    if args.verify_only:
        argv.append("--verify-only")
    return samples_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tile_raster",
        description="计算式瓦片光栅渲染器（图形入口：python -m tile_raster gui）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    g = sub.add_parser("gui", help="启动瓦片光栅调试视窗（图形入口）")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true",
                   help="不自动打开浏览器（容器/无头环境）")
    g.set_defaults(func=cmd_gui)

    r = sub.add_parser("render", help="无界面渲染并写出 PNG+诊断（辅助）")
    _add_render_args(r)
    r.set_defaults(func=cmd_render)

    c = sub.add_parser("check", help="运行像素一致性与容量自检")
    c.add_argument("--json", default=None)
    c.set_defaults(func=cmd_check)

    b = sub.add_parser("bench", help="运行 dense/giant/empty 基准")
    b.add_argument("--repeat", type=int, default=5)
    b.add_argument("--out", default=None)
    b.set_defaults(func=cmd_bench)

    l = sub.add_parser("lab", help="运行彩纸溢出/重读对比实验")
    l.add_argument("--bucket-capacity", type=int, default=6)
    l.add_argument("--extra", type=int, default=1)
    l.add_argument("--seed", type=int, default=7)
    l.add_argument("--fault", action="store_true",
                   help="注入可信底图丢失故障，演示异常")
    l.set_defaults(func=cmd_lab)

    tr = sub.add_parser(
        "tile-rules", aliases=["rules", "verify"],
        help="瓦片规则校验：边界/归属/覆盖集合/溢出状态/换序一致性（可在净目录运行）")
    tr.add_argument("--out", default="tile_rules_out")
    tr.add_argument("--clean", action="store_true",
                    help="先删除并重建 --out（仅删除该显式目录）")
    tr.add_argument("--no-docs", action="store_true", help="跳过文档声明核对")
    tr.add_argument("--json", default=None)
    tr.set_defaults(func=cmd_tile_rules)

    mt = sub.add_parser(
        "maintenance", aliases=["maintain"],
        help="维护校验：文档声明 vs 代码复算 + 旧样例净目录运行")
    mt.add_argument("--update", action="store_true",
                    help="用代码复算结果刷新 docs/tile-rules.md 声明块")
    mt.add_argument("--json", default=None)
    mt.set_defaults(func=cmd_maintenance)

    mg = sub.add_parser(
        "mini-grid", aliases=["small-grid"],
        help="生成 8×8 小网格与独立参考绘制（JSON/ASCII/PPM/PNG）")
    mg.add_argument("--out", default="mini_grid_out")
    mg.set_defaults(func=cmd_mini_grid)

    sp = sub.add_parser(
        "samples",
        help="旧样例净目录复跑，或刷新 out/ 下陈旧 PNG 材料")
    sp.add_argument("--out", default="out/samples")
    sp.add_argument("--clean", action="store_true")
    sp.add_argument("--legacy-names", action="store_true")
    sp.add_argument("--verify-only", action="store_true")
    sp.set_defaults(func=cmd_samples)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # 无参数：打印帮助。图形入口明确为 `python -m tile_raster gui`。
        parser.print_help()
        print("\n图形调试视窗入口：python -m tile_raster gui")
        return 0
    try:
        return args.func(args)
    except (ValueError, RuntimeError) as e:
        print(f"错误：{type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
