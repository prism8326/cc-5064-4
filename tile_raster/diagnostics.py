"""缓冲诊断：把分箱/容量/生命周期计数器整理成可读诊断与告警。

图形视窗的“缓冲诊断”面板和 CLI ``render --diagnose`` 都使用这里的结论，
保证界面提示来自真实计数而非统计列表长度的臆测。
"""
from __future__ import annotations


def buffer_diagnosis(metrics: dict) -> dict:
    b = metrics["binning"]
    tiles = metrics["tiles"]
    overflow_tiles = [t for t in tiles if t["overflow_seq"] is not None]
    mid_events = [e for e in b["overflow_events"] if e.get("midway_in_primitive")]
    lines: list[dict] = []

    lines.append({
        "level": "info",
        "msg": (
            f"网格 {b['cols']}×{b['rows']}，非空瓦片 {b['nonempty_tiles']}/"
            f"{b['tile_count_grid']}；容量检查 {b['capacity_checks']} 次，"
            f"追加 {b['total_bin_appends']} 次。几何权威存储为 Scene，"
            "桶内仅保存 seq，溢出不丢几何。"
        ),
    })

    if b["strategy"] == "continuation":
        lines.append({
            "level": "info",
            "msg": (
                f"分批续绘：{b['continuation_tiles']} 个瓦片溢出并封存前缀，"
                "执行期从 Scene 重扫、按 seq 分批补绘；存储生命周期为 "
                "bin -> tile drain -> writeback 后释放。"
            ),
        })
    else:
        lines.append({
            "level": "info",
            "msg": (
                f"重分桶：共 {b['rebin_count']} 次，复制 {b['rebin_copies']} 个 id；"
                "旧桶在回填后释放，容量倍增且受 max_bucket_capacity 硬顶约束。"
            ),
        })

    if overflow_tiles:
        sample = ", ".join(
            f"({t['tx']},{t['ty']})@seq{t['overflow_seq']}"
            for t in overflow_tiles[:6]
        )
        more = " …" if len(overflow_tiles) > 6 else ""
        lines.append({
            "level": "warn",
            "msg": f"{len(overflow_tiles)} 个瓦片在候选桶提交序号溢出：{sample}{more}",
        })

    if mid_events:
        e0 = mid_events[0]
        lines.append({
            "level": "warn",
            "msg": (
                f"{len(mid_events)} 次“图元跨多瓦且中途溢出候选桶”，例如 "
                f"seq={e0['seq']}({e0['label']}) 跨 {e0['primitive_span_tiles']} 瓦，"
                f"在第 {e0['span_index']} 个瓦 ({e0['tx']},{e0['ty']}) 处触发 "
                f"{e0['action']}。"
            ),
        })

    lines.append({
        "level": "info",
        "msg": (
            f"最大图元跨箱 {b['max_primitive_span']} 个瓦片"
            f"（seq={b['max_primitive_span_seq']}）；"
            f"最长瓦片列表 {b['max_tile_list_length']} "
            f"@ {b['max_tile_list_coord']}。分箱范围保守外扩且有屏幕界，"
            "超大图元与极小图元均不会漏箱。"
        ),
    })

    heaviest = max(tiles, key=lambda t: t["load_fragments"], default=None)
    if heaviest is not None:
        lines.append({
            "level": "info",
            "msg": (
                f"最重瓦片 ({heaviest['tx']},{heaviest['ty']}) 实际覆盖 "
                f"{heaviest['load_fragments']} 片元、"
                f"测试 {heaviest['pixel_tests']} 像素、"
                f"耗时 {heaviest['exec_ms']} ms。"
            ),
        })

    return {
        "lines": lines,
        "overflow_tile_count": len(overflow_tiles),
        "mid_primitive_event_count": len(mid_events),
        "max_primitive_span": b["max_primitive_span"],
        "max_tile_list_length": b["max_tile_list_length"],
    }
