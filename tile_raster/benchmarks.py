"""一致性矩阵、分箱策略对比、容量增长证明与场景基准。

这些函数同时被：

* ``python -m tile_raster check`` 命令行自检；
* 图形视窗的“一致性自检”面板；
* ``tests/`` 下的单元测试

调用，保证文档宣称的性质都可重复验证。
"""
from __future__ import annotations

import time
from dataclasses import asdict

from .binning import BinConfig, TileBinner
from .executor import ExecuteConfig, TileExecutor
from .raster import render_serial_reference
from .scene import Scene, build_scene


def frames_equal(ref_target, result) -> bool:
    ref_rgb = bytearray(ref_target.w * ref_target.h * 3)
    for i in range(ref_target.w * ref_target.h):
        for c in range(3):
            v = ref_target.color[i * 4 + c]
            v = 0.0 if v < 0 else 1.0 if v > 1 else v
            ref_rgb[i * 3 + c] = int(v * 255.0 + 0.5)
    return bytes(ref_rgb) == result.rgb


def consistency_suite(verbose: bool = False) -> dict:
    """跨场景/瓦片尺寸/容量/策略/执行顺序的像素一致性矩阵。"""
    matrix = []
    all_ok = True
    cases = [
        ("paper", 96, 72),
        ("tiny", 64, 48),
        ("depth-order", 64, 48),
        ("empty", 96, 72),
        ("giant", 96, 72),
        ("dense", 96, 72),
        ("midway", 96, 48),
    ]
    tile_sizes = [8, 16, 24]
    capacities = [2, 6, 64]
    n_checks = 0
    for name, w, h in cases:
        scene = build_scene(name, w, h)
        ref, _ = render_serial_reference(scene)
        for tile in tile_sizes:
            if tile > min(w, h):
                continue
            for cap in capacities:
                for strategy in ("continuation", "rebin"):
                    for order in ("serial", "shuffle"):
                        n_checks += 1
                        cfg = BinConfig(
                            tile_w=tile, tile_h=tile,
                            bucket_capacity=cap, strategy=strategy,
                            max_bucket_capacity=20000,
                        )
                        res = TileExecutor(
                            scene, cfg,
                            ExecuteConfig(work_order=order, seed=11),
                        ).run()
                        exact = frames_equal(ref, res)
                        bin_errors = res.binner.verify_completeness()
                        ok = exact and not bin_errors
                        all_ok &= ok
                        matrix.append({
                            "scene": name, "tile": tile, "cap": cap,
                            "strategy": strategy, "order": order,
                            "pixel_exact": exact,
                            "bin_completeness_errors": len(bin_errors),
                            "ok": ok,
                        })
                        if verbose and not ok:
                            print("FAIL", matrix[-1], bin_errors[:2])
    return {"ok": all_ok, "checks": n_checks, "matrix": matrix}


def compare_bin_strategies() -> dict:
    """分批续绘 vs 重分桶：像素都必须正确，成本指标不同。"""
    scene = build_scene("paper", 96, 72)
    out = {}
    ref, _ = render_serial_reference(scene)
    for strategy in ("continuation", "rebin"):
        cfg = BinConfig(tile_w=24, tile_h=24, bucket_capacity=6,
                        strategy=strategy, max_bucket_capacity=20000)
        t0 = time.perf_counter()
        res = TileExecutor(scene, cfg, ExecuteConfig("serial")).run()
        wall = time.perf_counter() - t0
        rep = res.binner.report()
        out[strategy] = {
            "pixel_exact": frames_equal(ref, res),
            "bin_events": len(rep.overflow_events),
            "continuation_tiles": rep.continuation_tiles,
            "rebin_count": rep.rebin_count,
            "rebin_copies": rep.rebin_copies,
            "max_tile_list_length": rep.max_tile_list_length,
            "exec_wall_ms": round(wall * 1000, 3),
            "bin_ms": round(res.timings["bin"] * 1000, 3),
            "process_ms": round(res.timings["process"] * 1000, 3),
        }
    out["identical_pixels_between_strategies"] = (
        out["continuation"]["pixel_exact"] and out["rebin"]["pixel_exact"]
    )
    out["midway"] = _compare_midway()
    return out


def _compare_midway() -> dict:
    """同一“跨三瓦、在第 2 瓦中途顶爆桶”图元下比较两种策略。"""
    from .scene import make_midway_scene

    scene = make_midway_scene()
    ref, _ = render_serial_reference(scene)
    block: dict = {}
    for strategy in ("continuation", "rebin"):
        cfg = BinConfig(tile_w=24, tile_h=24, bucket_capacity=4,
                        strategy=strategy, max_bucket_capacity=4096)
        res = TileExecutor(scene, cfg, ExecuteConfig("serial")).run()
        rep = res.binner.report()
        midway_events = [e for e in rep.overflow_events if e.midway_in_primitive]
        key = (1, 0)
        tile_bin = res.binner.bins[key]
        chunks = (
            list(res.binner.iter_continuation_chunks(key))
            if strategy == "continuation" else []
        )
        block[strategy] = {
            "pixel_exact": frames_equal(ref, res),
            "midway_event_count": len(midway_events),
            "midway_tile": [midway_events[0].tx, midway_events[0].ty]
            if midway_events else None,
            "midway_span_index": midway_events[0].span_index
            if midway_events else None,
            "primitive_span_tiles": midway_events[0].primitive_span_tiles
            if midway_events else None,
            "continuation_tiles": rep.continuation_tiles,
            "continuation_chunks_on_critical_tile": len(chunks),
            "deferred_on_critical_tile": tile_bin.deferred_count,
            "rebin_count": rep.rebin_count,
            "rebin_copies": rep.rebin_copies,
            "critical_tile_final_ids": len(
                res.binner.resolve_ordered_ids(key)),
        }
    block["both_detected_same_midway_point"] = (
        block["continuation"]["midway_event_count"] == 1
        and block["rebin"]["midway_event_count"] == 1
        and block["continuation"]["midway_tile"]
        == block["rebin"]["midway_tile"]
        and block["continuation"]["midway_span_index"]
        == block["rebin"]["midway_span_index"]
    )
    block["pixels_exact"] = (
        block["continuation"]["pixel_exact"]
        and block["rebin"]["pixel_exact"]
    )
    return block


def make_growth_stress_scene(n: int = 400) -> Scene:
    """列表增长最剧烈的输入：n 个小三角形全部落在同一个瓦片内。"""
    s = Scene(
        "growth-stress", 64, 64,
        description=f"{n} 个三角形全部挤在瓦片 (0,0)，考验桶容量检查与增长。",
    )
    for i in range(n):
        # 确定性伪抖动，全部围绕 (15,15)，尺寸 2-4px，保守范围不出 (0,0) 瓦。
        x = 13 + ((i * 37) % 7) * 0.7
        y = 13 + ((i * 53) % 7) * 0.7
        k = 2.0 + (i % 3)
        s.add_triangle(
            (x, y - k), (x + k, y + k * 0.7), (x - k, y + k * 0.7),
            (0.2 + (i % 5) * 0.15, 0.5, 0.9 - (i % 4) * 0.15, 1.0),
            z=0.5 + (i % 7) * 0.03, label=f"g{i}",
        )
    return s


def capacity_growth_proof() -> dict:
    """证明容量检查覆盖了列表增长最剧烈的输入与超大跨箱图元。"""
    stress = make_growth_stress_scene(400)
    cfg = BinConfig(tile_w=32, tile_h=32, bucket_capacity=4,
                    strategy="rebin", max_bucket_capacity=20000)
    binner = TileBinner(stress, cfg).bin_scene()
    key = (0, 0)
    b = binner.bins[key]
    capacities = [e.new_capacity for e in binner.events if e.tx == 0 and e.ty == 0]
    expected_checks = sum(sp[4] for sp in binner.spans)
    result_stress = TileExecutor(
        stress, cfg, ExecuteConfig("serial")).run()
    ref, _ = render_serial_reference(stress)

    giant = build_scene("giant", 128, 96)
    gcfg = BinConfig(tile_w=16, tile_h=16, bucket_capacity=64)
    gbin = TileBinner(giant, gcfg).bin_scene()
    cols, rows = gbin.cols, gbin.rows
    full_screen_spans = [
        {"seq": t.seq, "span_tiles": gbin.spans[t.seq][4]}
        for t in giant.triangles
    ]
    max_span = max(d["span_tiles"] for d in full_screen_spans)

    return {
        "stress_triangles": len(stress.triangles),
        "stress_tile": list(key),
        "final_list_length": len(b.ids),
        "rebin_capacity_growth": capacities,
        "rebin_count": b.rebins,
        "capacity_checks": binner.capacity_checks,
        "expected_append_attempts": expected_checks,
        "every_append_checked": binner.capacity_checks == expected_checks,
        "completeness_errors": len(binner.verify_completeness()),
        "pixel_exact": frames_equal(ref, result_stress),
        "giant_grid": [cols, rows],
        "giant_full_grid_tiles": cols * rows,
        "giant_primitive_spans": full_screen_spans,
        "giant_max_span": max_span,
        "giant_span_covers_full_grid": max_span == cols * rows,
        "ok": (
            len(b.ids) == 400
            and binner.capacity_checks == expected_checks
            and not binner.verify_completeness()
            and frames_equal(ref, result_stress)
            and max_span == cols * rows
        ),
    }


def benchmark_scenes(repeat: int = 5) -> dict:
    """测量分箱、处理、写回成本（取多次最小值，单位毫秒）。"""
    specs = [
        ("dense", 160, 120, 16),
        ("giant", 256, 192, 32),
        ("paper", 96, 72, 24),
        ("empty", 320, 240, 32),
    ]
    out = {}
    for name, w, h, tile in specs:
        scene = build_scene(name, w, h)
        cfg = BinConfig(tile_w=tile, tile_h=tile, bucket_capacity=128)
        best = None
        last = None
        for _ in range(repeat):
            res = TileExecutor(scene, cfg, ExecuteConfig("serial")).run()
            t = res.timings
            if best is None or t["total"] < best["total_s"]:
                best = {
                    "total_s": t["total"],
                    "bin_ms": t["bin"] * 1000,
                    "process_ms": t["process"] * 1000,
                    "writeback_ms": t["writeback"] * 1000,
                    "quantize_ms": t["quantize"] * 1000,
                }
            last = res
        rep = last.binner.report()
        out[name] = {
            "width": w, "height": h, "tile": tile,
            "triangles": len(scene.triangles),
            "runs": repeat,
            **{k: round(v, 4) for k, v in best.items()},
            "covered_fragments": last.total_stats.covered,
            "pixel_tests": last.total_stats.pixel_tests,
            "blend_fragments": last.total_stats.blend_fragments,
            "nonempty_tiles": rep.nonempty_tiles,
            "max_primitive_span": rep.max_primitive_span,
        }
    return out


def run_check_suite() -> tuple[bool, dict, list[str]]:
    """CLI/GUI 自检入口，返回 (是否全过, 结构化报告, 文本行)。"""
    lines: list[str] = []
    cons = consistency_suite()
    lines.append(
        f"[{'PASS' if cons['ok'] else 'FAIL'}] 像素一致性矩阵："
        f"{cons['checks']} 个配置（场景×瓦片×容量×策略×顺序）"
    )
    strat = compare_bin_strategies()
    lines.append(
        f"[{'PASS' if strat['identical_pixels_between_strategies'] else 'FAIL'}] "
        "分批续绘 vs 重分桶像素一致"
    )
    mw = strat["midway"]
    mw_ok = mw["both_detected_same_midway_point"] and mw["pixels_exact"]
    c, r = mw["continuation"], mw["rebin"]
    lines.append(
        f"[{'PASS' if mw_ok else 'FAIL'}] 跨瓦中途溢出：两种策略都在 "
        f"{mw['continuation']['midway_tile']} 第 "
        f"{mw['continuation']['midway_span_index']}/"
        f"{mw['continuation']['primitive_span_tiles']} 瓦处捕获同一事件；"
        f"续绘 {c['continuation_chunks_on_critical_tile']} 批/"
        f"延迟 {c['deferred_on_critical_tile']}，"
        f"重分桶复制 {r['rebin_copies']} 项"
    )
    growth = capacity_growth_proof()
    lines.append(
        f"[{'PASS' if growth['ok'] else 'FAIL'}] 容量增长证明："
        f"400 图元单瓦列表 -> {growth['final_list_length']}，"
        f"容量链 {growth['rebin_capacity_growth']}，"
        f"检查 {growth['capacity_checks']}/{growth['expected_append_attempts']}，"
        f"超大三角跨 {growth['giant_max_span']} 箱"
        f"（全屏 {growth['giant_full_grid_tiles']}）"
    )
    ok = (cons["ok"] and strat["identical_pixels_between_strategies"]
          and growth["ok"] and mw_ok)
    report = {"consistency": cons, "strategies": strat, "growth": growth}
    lines.append("总体: " + ("ALL PASS" if ok else "HAS FAILURES"))
    return ok, report, lines
