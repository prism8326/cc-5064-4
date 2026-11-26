"""瓦片规则校验：分箱、瓦片边界、片元归属、覆盖集合与溢出状态的可执行约定。

这个模块把 ``docs/tile-rules.md`` 里的文字约定变成**每次都从代码现场复算**
的检查，绝不读取任何手改数据或历史渲染产物：

1. **瓦片边界 / 片元归属**：像素 (x,y) 唯一归属 ``ty=y//th, tx=x//tw`` 瓦片，
   归属图是屏幕的一个不相交划分；任何覆盖像素都必须落在其归属瓦矩形内。
2. **覆盖集合 ↔ 计算负载**：每瓦覆盖计数之和必须等于该瓦执行器真实
   ``stats.covered`` / ``TileDiagnostic.load_fragments``；全帧之和等于
   ``total_stats.covered``。覆盖集合在 serial/shuffle/continuation/rebin 下
   必须完全相同。
3. **溢出状态 ↔ 执行证据**：诊断里的 ``overflow_seq / status / rebins /
   continuation_passes / assigned_ids`` 必须与分箱器状态及续绘生成器一致。
4. **改变工作组顺序仍还原同一片元归属**：serial 与固定种子 shuffle 不仅
   RGB 逐字节一致，覆盖集合和逐像素“最后写入 seq”归属图也必须一致。
5. **超长可续读工作链的适用范围**：以“溢出时按原序再次分桶”的 rebin 为
   参照，扫描链长；硬上限内两策略完全等价，超过 rebin 硬上限时 rebin 必须
   响亮报错，而游标续读仍能精确还原。

命令行（可在任意净目录执行，产物只写到 ``--out`` 与当前目录报告文件）::

    python -m tile_raster.tile_rules
    python -m tile_raster tile-rules --out $(mktemp -d)
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .binning import BinConfig, TileBinner
from .executor import ExecuteConfig, TileExecutor
from .mini_grid import (
    HEAVY_TILE,
    MINI_BUCKET_CAPACITY,
    MINI_COLS,
    MINI_HEIGHT,
    MINI_MAX_BUCKET_CAPACITY,
    MINI_ROWS,
    MINI_STRATEGY,
    MINI_TILE_H,
    MINI_TILE_W,
    MINI_WIDTH,
    SCHEMA_VERSION,
    ascii_coverage,
    build_mini_grid,
    independent_reference_cover,
    owner_tile_index,
    tile_owner_map,
)
from .raster import BufferCapacityError, render_serial_reference
from .scene import Scene


# ---------------------------------------------------------------------------
# 基础结构
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    check_id: str
    scope: str          # ownership | coverage | load | overflow | order | docs | sample
    ok: bool
    msg: str
    severity: str = "error"   # error | warn | info
    expected: object = None
    actual: object = None

    def as_dict(self) -> dict:
        return {
            "check_id": self.check_id, "scope": self.scope, "ok": self.ok,
            "severity": self.severity, "msg": self.msg,
            "expected": self.expected, "actual": self.actual,
        }


class _TraceBook(dict):
    """让 draw_triangle 的 ``trace.get(key)`` 为每个探针现场创建列表。"""

    def get(self, key, default=None):  # type: ignore[override]
        value = dict.get(self, key)
        if value is None:
            value = []
            self[key] = value
        return value


# ---------------------------------------------------------------------------
# 覆盖集合 / 归属
# ---------------------------------------------------------------------------

def coverage_set_from_result(result) -> set[tuple[int, int]]:
    return {
        (i % result.width, i // result.width)
        for i, c in enumerate(result.coverage) if c > 0
    }


def per_tile_coverage_sets(result) -> dict[tuple[int, int], set[tuple[int, int]]]:
    """按归属瓦切分的覆盖集合（键为 (tx,ty)）。"""
    tw, th = result.bin_config.tile_w, result.bin_config.tile_h
    cols = result.cols
    sets: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for y in range(result.height):
        for x in range(result.width):
            c = result.coverage[y * result.width + x]
            if c > 0:
                idx = owner_tile_index(x, y, tw, th, cols)
                ty, tx = divmod(idx, cols)
                sets.setdefault((tx, ty), set()).add((x, y))
    return sets


def winner_seq_map_from_trace(trace: dict, width: int, height: int) -> list[int]:
    """逐像素最后一个通过深度并写入/混合的片元 seq；背景为 -1。"""
    winners = [-1] * width * height
    for (gx, gy), events in trace.items():
        winner = -1
        for ev in events:
            if ev.covered and ev.depth_passed and ev.action in (
                "opaque-write", "blend"
            ):
                winner = ev.seq
        if 0 <= gx < width and 0 <= gy < height:
            winners[gy * width + gx] = winner
    return winners


def collect_winner_map(scene: Scene, cfg: BinConfig,
                       order: str = "serial", seed: int = 17) -> list[int]:
    book = _TraceBook()
    TileExecutor(scene, cfg, ExecuteConfig(work_order=order, seed=seed)).run(
        trace=book)
    return winner_seq_map_from_trace(book, scene.width, scene.height)


def serial_winner_map(scene: Scene) -> list[int]:
    book = _TraceBook()
    render_serial_reference(scene, trace=book)
    return winner_seq_map_from_trace(book, scene.width, scene.height)


# ---------------------------------------------------------------------------
# 执行证据核对（GUI / API 也调用，保证界面异常不是另一套逻辑）
# ---------------------------------------------------------------------------

def rule_evidence(result) -> dict:
    """对一次 RenderResult 核对全部执行约定，返回结构化证据。

    ``ok`` 为 False 时 ``anomalies`` 给出覆盖图 / 计算负载 / 归属 / 溢出相关
    异常；GUI 与 ``/api/render`` 直接展示同一结果，避免“伪报”。
    """
    issues: list[Issue] = []
    w, h = result.width, result.height
    cols, rows = result.cols, result.rows
    tw, th = result.bin_config.tile_w, result.bin_config.tile_h
    n_tiles = cols * rows

    def add(check_id, scope, ok, msg, **kw):
        issues.append(Issue(check_id, scope, bool(ok), msg, **kw))

    # 1) 工作组顺序必须是全部瓦片的一个排列（并行执行的唯一合法自由度）。
    add(
        "order-is-permutation", "order",
        sorted(result.order) == list(range(n_tiles))
        and len(result.order) == n_tiles,
        f"工作组完成顺序是 {n_tiles} 个瓦片的完整排列",
        expected=list(range(n_tiles)), actual=list(result.order),
    )

    # 2) 每个网格瓦都有诊断，且矩形与边界约定一致。
    diags = {d.tile_index: d for d in result.tile_diags}
    add("one-diagnostic-per-tile", "ownership",
        len(result.tile_diags) == n_tiles and set(diags) == set(range(n_tiles)),
        f"{len(result.tile_diags)}/{n_tiles} 个瓦片都有执行诊断")

    owner = tile_owner_map(w, h, tw, th, cols)
    rect_bad: list[str] = []
    load_mismatch: list[dict] = []
    overflow_mismatch: list[str] = []
    assigned_mismatch: list[str] = []
    pass_mismatch: list[str] = []
    status_mismatch: list[str] = []
    sum_rect_cover = 0

    for d in result.tile_diags:
        x0, y0, x1, y1 = d.x0, d.y0, d.x1, d.y1
        exp_x0, exp_y0 = d.tx * tw, d.ty * th
        exp_x1 = min(w, exp_x0 + tw)
        exp_y1 = min(h, exp_y0 + th)
        if (x0, y0, x1, y1) != (exp_x0, exp_y0, exp_x1, exp_y1):
            rect_bad.append(f"#{d.tile_index}")

        tile_cover_sum = 0
        outside = 0
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                c = result.coverage[yy * w + xx]
                tile_cover_sum += c
                if c > 0 and owner[yy * w + xx] != d.tile_index:
                    outside += 1
        if outside:
            rect_bad.append(f"#{d.tile_index} 越界覆盖 {outside}px")

        sum_rect_cover += tile_cover_sum
        if tile_cover_sum != d.load_fragments:
            load_mismatch.append({
                "tile": [d.tx, d.ty],
                "cover_sum": tile_cover_sum,
                "load_fragments": d.load_fragments,
            })
        if d.load_pixel_tests < d.load_fragments:
            load_mismatch.append({
                "tile": [d.tx, d.ty],
                "pixel_tests": d.load_pixel_tests,
                "load_fragments": d.load_fragments,
                "reason": "pixel_tests<covered",
            })

        key = (d.tx, d.ty)
        b = result.binner.bins.get(key)
        try:
            ordered = result.binner.resolve_ordered_ids(key) if b else []
        except Exception as e:  # 校验器本身不能崩
            assigned_mismatch.append(f"#{d.tile_index} resolve 失败: {e}")
            ordered = None
        if ordered is not None and d.assigned_ids != len(ordered):
            assigned_mismatch.append(
                f"#{d.tile_index} diag={d.assigned_ids} ordered={len(ordered)}")

        if b is not None:
            if d.overflow_seq != b.overflow_seq:
                overflow_mismatch.append(
                    f"#{d.tile_index} diag={d.overflow_seq} bin={b.overflow_seq}")
            if d.overflow_submission != b.overflow_submission:
                overflow_mismatch.append(f"#{d.tile_index} overflow_submission")
            if d.rebins != b.rebins:
                overflow_mismatch.append(
                    f"#{d.tile_index} rebins diag={d.rebins} bin={b.rebins}")
            if result.bin_config.strategy == "continuation":
                try:
                    passes = len(list(result.binner.iter_continuation_chunks(key)))
                except Exception:
                    passes = -1
                if d.continuation_passes != passes:
                    pass_mismatch.append(
                        f"#{d.tile_index} diag={d.continuation_passes} gen={passes}")
            expected_status = _expected_status(d, b, result.bin_config.strategy,
                                               len(ordered or []))
            if d.status != expected_status:
                status_mismatch.append(
                    f"#{d.tile_index} {d.status}!={expected_status}")
        elif d.status != "empty" or d.assigned_ids != 0:
            status_mismatch.append(f"#{d.tile_index} 无 bin 但状态 {d.status}")

    add("tile-rect-boundary", "ownership", not rect_bad,
        "所有瓦片矩形符合 [tx*tw,min((tx+1)*tw,W)) 边界约定",
        actual=rect_bad[:8])
    add("coverage-load-per-tile", "load", not load_mismatch,
        "每瓦覆盖计数之和 == load_fragments（真实着色片元数）",
        actual=load_mismatch[:8])
    add("assigned-ids-evidence", "overflow", not assigned_mismatch,
        "assigned_ids 与 resolve_ordered_ids 长度一致",
        actual=assigned_mismatch[:8])
    add("overflow-state-evidence", "overflow", not overflow_mismatch,
        "overflow_seq/submission/rebins 与分箱器状态一致",
        actual=overflow_mismatch[:8])
    add("continuation-passes-evidence", "overflow", not pass_mismatch,
        "continuation_passes 与续绘生成器批数一致", actual=pass_mismatch[:8])
    add("tile-status-evidence", "overflow", not status_mismatch,
        "瓦片 status 与溢出状态/策略一致", actual=status_mismatch[:8])

    frame_sum = sum(result.coverage)
    add("coverage-frame-total", "coverage",
        frame_sum == result.total_stats.covered,
        f"全帧覆盖计数和 {frame_sum} == total.covered "
        f"{result.total_stats.covered}",
        expected=result.total_stats.covered, actual=frame_sum)
    add("coverage-tile-sum", "coverage",
        sum_rect_cover == frame_sum,
        f"各瓦覆盖和 {sum_rect_cover} == 全帧 {frame_sum}（瓦集是不相交划分）")

    errors = [i for i in issues if not i.ok and i.severity == "error"]
    return {
        "ok": not errors,
        "checks": [i.as_dict() for i in issues],
        "anomalies": [i.as_dict() for i in errors],
        "totals": {
            "covered_fragments": result.total_stats.covered,
            "covered_pixels": len(coverage_set_from_result(result)),
            "frame_cover_sum": frame_sum,
            "tiles": n_tiles,
            "overflow_tiles": sum(
                1 for d in result.tile_diags if d.overflow_seq is not None),
        },
    }


def _expected_status(d, b, strategy: str, assigned: int) -> str:
    if b is None or assigned == 0:
        return "empty"
    if b.overflow_seq is None:
        return "immediate"
    return "overflow-continuation" if strategy == "continuation" else "overflow-rebin"


def execution_anomalies(result) -> list[dict]:
    """只返回异常列表（空列表表示覆盖/负载/归属/溢出证据全部自洽）。"""
    return rule_evidence(result)["anomalies"]


# ---------------------------------------------------------------------------
# 小网格：覆盖集合 + 溢出状态 + 换序归属一致性
# ---------------------------------------------------------------------------

def _run_mini(strategy: str, order: str):
    cfg = BinConfig(
        tile_w=MINI_TILE_W, tile_h=MINI_TILE_H,
        bucket_capacity=MINI_BUCKET_CAPACITY,
        strategy=strategy,
        max_bucket_capacity=MINI_MAX_BUCKET_CAPACITY,
    )
    return TileExecutor(
        build_mini_grid(), cfg, ExecuteConfig(work_order=order, seed=20261126)
    ).run()


def _rgb_bytes(target) -> bytes:
    out = bytearray(target.w * target.h * 3)
    for i in range(target.w * target.h):
        for c in range(3):
            v = target.color[i * 4 + c]
            v = 0.0 if v < 0 else 1.0 if v > 1 else v
            out[i * 3 + c] = int(v * 255.0 + 0.5)
    return bytes(out)


def evaluate_mini_grid() -> tuple[list[Issue], dict]:
    """完整复算小网格，返回 (检查项, 声明/证据字典)。"""
    issues: list[Issue] = []

    def add(cid, scope, ok, msg, **kw):
        issues.append(Issue(cid, scope, bool(ok), msg, **kw))

    scene = build_mini_grid()
    indep_cover = independent_reference_cover(scene)
    ref_target, _ = render_serial_reference(scene)

    add("mini-independent-reference", "coverage",
        indep_cover == ref_target.cover,
        "独立暴力参考覆盖集合 == draw_triangle 串行参考",
        actual=None if indep_cover == ref_target.cover else "覆盖计数不一致")

    runs: dict[str, object] = {}
    for strategy in ("continuation", "rebin"):
        for order in ("serial", "shuffle"):
            runs[f"{strategy}-{order}"] = _run_mini(strategy, order)

    base = runs["continuation-serial"]
    base_sets = per_tile_coverage_sets(base)
    base_winners = collect_winner_map(
        scene,
        BinConfig(MINI_TILE_W, MINI_TILE_H,
                  bucket_capacity=MINI_BUCKET_CAPACITY,
                  strategy="continuation",
                  max_bucket_capacity=MINI_MAX_BUCKET_CAPACITY),
        "shuffle",
    )
    serial_winners = serial_winner_map(scene)

    all_pixel_exact = True
    all_cover_exact = True
    sets_stable = True
    winner_stable = base_winners == serial_winners
    run_evidence: dict[str, dict] = {}

    ref_rgb = _rgb_bytes(ref_target)
    for name, res in runs.items():
        pixel_exact = res.rgb == ref_rgb
        cover_exact = res.coverage == ref_target.cover
        ev = rule_evidence(res)
        run_evidence[name] = {
            "pixel_exact_vs_serial": pixel_exact,
            "coverage_exact_vs_serial": cover_exact,
            "rule_evidence_ok": ev["ok"],
            "anomaly_count": len(ev["anomalies"]),
        }
        all_pixel_exact &= pixel_exact
        all_cover_exact &= cover_exact and ev["ok"]
        if per_tile_coverage_sets(res) != base_sets:
            sets_stable = False
        if name.endswith("shuffle"):
            wm = collect_winner_map(
                scene, res.bin_config, "shuffle", seed=20261126)
            if wm != base_winners:
                winner_stable = False

    add("mini-pixel-exact-all-modes", "coverage", all_pixel_exact,
        "小网格在 2 策略 × serial/shuffle 下 RGB 均与串行参考逐字节一致")
    add("mini-coverage-exact-all-modes", "coverage", all_cover_exact,
        "小网格覆盖计数/集合在所有模式下与串行参考一致且执行证据自洽")
    add("mini-coverage-sets-order-invariant", "ownership", sets_stable,
        "改变工作组顺序/溢出策略，每瓦覆盖集合完全不变")
    add("mini-winner-attribution-order-invariant", "ownership", winner_stable,
        "逐像素最后写入 seq 归属图在 serial/shuffle 及串行参考间一致")

    # 归属划分：owner map 与执行证据中的实际覆盖位置一致。
    owner = tile_owner_map(MINI_WIDTH, MINI_HEIGHT, MINI_TILE_W, MINI_TILE_H,
                           MINI_COLS)
    partition_ok = len(set(owner)) == MINI_COLS * MINI_ROWS
    for idx, tile_set in base_sets.items():
        ty, tx = idx[1], idx[0]
        linear = ty * MINI_COLS + tx
        for (x, y) in tile_set:
            if owner[y * MINI_WIDTH + x] != linear:
                partition_ok = False
    add("mini-owner-partition", "ownership", partition_ok,
        "归属图把 8×8 屏幕唯一划分给 4 块瓦，覆盖像素均在其归属瓦内")

    claims = _build_claims(base, indep_cover, run_evidence, {
        "pixel_exact_all_modes": all_pixel_exact,
        "coverage_exact_all_modes": all_cover_exact,
        "coverage_sets_order_invariant": sets_stable,
        "winner_attribution_order_invariant": winner_stable,
        "owner_partition": partition_ok,
        "independent_reference_matches": indep_cover == ref_target.cover,
    })
    return issues, claims


def _sorted_pairs(pairs) -> list[list[int]]:
    return [[int(x), int(y)] for x, y in sorted(pairs)]


def _build_claims(result, indep_cover, run_evidence, invariance) -> dict:
    binner = result.binner
    tiles_claim = []
    for ty in range(MINI_ROWS):
        for tx in range(MINI_COLS):
            idx = ty * MINI_COLS + tx
            diag = next(d for d in result.tile_diags if d.tile_index == idx)
            key = (tx, ty)
            b = binner.bins.get(key)
            ordered = binner.resolve_ordered_ids(key) if b else []
            chunks = (
                [list(c) for c in binner.iter_continuation_chunks(key)]
                if b is not None else []
            )
            x0, y0 = tx * MINI_TILE_W, ty * MINI_TILE_H
            x1, y1 = min(MINI_WIDTH, x0 + MINI_TILE_W), min(
                MINI_HEIGHT, y0 + MINI_TILE_H)
            cset = []
            multiplicity = []
            cover_sum = 0
            for yy in range(y0, y1):
                for xx in range(x0, x1):
                    c = result.coverage[yy * MINI_WIDTH + xx]
                    cover_sum += c
                    if c > 0:
                        cset.append((xx, yy))
                    if c > 1:
                        multiplicity.append([xx, yy, c])
            tiles_claim.append({
                "index": idx, "tx": tx, "ty": ty,
                "rect": [x0, y0, x1, y1],
                "coverage_set": _sorted_pairs(cset),
                "cover_multiplicity": sorted(multiplicity),
                "covered_pixel_count": len(cset),
                "cover_sum": cover_sum,
                "load_fragments": diag.load_fragments,
                "status": diag.status,
                "ordered_ids": list(ordered),
                "overflow_seq": b.overflow_seq if b else None,
                "continuation_chunks": chunks,
            })

    events_claim = [{
        "tile": [e.tx, e.ty], "seq": e.seq, "action": e.action,
        "midway_in_primitive": e.midway_in_primitive,
        "primitive_span_tiles": e.primitive_span_tiles,
        "span_index": e.span_index,
    } for e in binner.events]

    covered_pixels = sum(1 for c in indep_cover if c > 0)
    return {
        "schema": SCHEMA_VERSION,
        "mini_grid": {
            "width": MINI_WIDTH, "height": MINI_HEIGHT,
            "tile_w": MINI_TILE_W, "tile_h": MINI_TILE_H,
            "cols": MINI_COLS, "rows": MINI_ROWS,
            "bucket_capacity": MINI_BUCKET_CAPACITY,
            "max_bucket_capacity": MINI_MAX_BUCKET_CAPACITY,
            "strategy": MINI_STRATEGY,
            "triangle_count": len(build_mini_grid().triangles),
            "covered_pixel_count": covered_pixels,
            "covered_fragment_count": sum(indep_cover),
            "overdraw_extra_count": sum(indep_cover) - covered_pixels,
            "ascii_coverage": ascii_coverage(indep_cover),
            "tiles": tiles_claim,
            "overflow_events": events_claim,
            "invariance": invariance,
            "runs": run_evidence,
        },
        "long_chain_scope": long_chain_scope(),
    }


# ---------------------------------------------------------------------------
# 超长可续读工作链的适用范围（rebin 原序重桶作为参照）
# ---------------------------------------------------------------------------

def make_chain_scene(n: int, width: int = 8, height: int = 8) -> Scene:
    """n 个确定性小三角全部落在唯一瓦内，形成超长单瓦工作链。"""
    s = Scene("chain", width, height,
              description=f"{n} 个图元的单瓦超长工作链")
    for i in range(n):
        # 确定性伪位置（不使用随机源，保证可复算）。
        cx = 1.5 + ((i * 29) % 53) * 0.1
        cy = 1.5 + ((i * 41) % 53) * 0.1
        k = 0.55 + (i % 3) * 0.18
        s.add_triangle(
            (cx, cy - k), (cx + k, cy + k * 0.7),
            (cx - k, cy + k * 0.7),
            (0.2 + (i % 5) * 0.15, 0.55, 0.9 - (i % 4) * 0.18, 1.0),
            label=f"chain-{i}",
        )
    return s


def long_chain_scope(lengths=(1, 2, MINI_BUCKET_CAPACITY, 5, 8, 17, 32, 33, 64, 256),
                     capacity: int = 4, hard_capacity: int = 32) -> dict:
    """扫描工作链长度，比较 continuation 游标续读与 rebin 原序重桶。

    返回结构化范围结论；任何长度下 continuation 都必须像素精确，
    ``n <= hard_capacity`` 时 rebin 也必须精确且两者顺序一致，
    ``n > hard_capacity`` 时 rebin 必须抛 :class:`BufferCapacityError`。
    """
    rows = []
    scope_ok = True
    for n in sorted(set(lengths)):
        scene = make_chain_scene(n)
        ref_target, _ = render_serial_reference(scene)
        ref_rgb = _rgb_bytes(ref_target)

        cont_cfg = BinConfig(
            tile_w=8, tile_h=8, bucket_capacity=capacity,
            strategy="continuation", max_bucket_capacity=hard_capacity)
        cont_res = TileExecutor(
            scene, cont_cfg, ExecuteConfig("shuffle", seed=5)).run()
        cont_binner = cont_res.binner
        key = (0, 0)
        chunks = list(cont_binner.iter_continuation_chunks(key))
        cont_order = cont_binner.resolve_ordered_ids(key)
        cont_ok = (
            cont_res.rgb == ref_rgb
            and cont_res.coverage == ref_target.cover
            and cont_order == list(range(n))
            and not cont_binner.verify_completeness()
            and rule_evidence(cont_res)["ok"]
        )

        reb: dict = {"supported": n <= hard_capacity}
        try:
            rb_cfg = BinConfig(
                tile_w=8, tile_h=8, bucket_capacity=capacity,
                strategy="rebin", max_bucket_capacity=hard_capacity)
            rb_res = TileExecutor(
                scene, rb_cfg, ExecuteConfig("serial")).run()
            rb_order = rb_res.binner.resolve_ordered_ids(key)
            reb.update({
                "ok": rb_res.rgb == ref_rgb and rb_order == list(range(n)),
                "pixel_exact": rb_res.rgb == ref_rgb,
                "ordered_ids_match_continuation": rb_order == cont_order,
                "error": None,
            })
        except BufferCapacityError as e:
            reb.update({"ok": False, "pixel_exact": False,
                        "ordered_ids_match_continuation": False,
                        "error": "BufferCapacityError",
                        "error_message": str(e)[:120]})

        if n <= hard_capacity:
            row_ok = cont_ok and reb.get("ok") and reb.get(
                "ordered_ids_match_continuation")
        else:
            # 适用范围边界：rebin 必须响亮拒绝，continuation 必须仍然精确。
            row_ok = cont_ok and reb.get("error") == "BufferCapacityError"
        scope_ok &= row_ok
        rows.append({
            "n": n,
            "continuation": {
                "ok": cont_ok,
                "chunks": len(chunks),
                "resident_capacity": capacity,
                "ordered_ids_complete": cont_order == list(range(n)),
            },
            "rebin": reb,
            "row_ok": row_ok,
        })

    return {
        "ok": scope_ok,
        "bucket_capacity": capacity,
        "rebin_hard_capacity": hard_capacity,
        "boundary_rule": (
            "n<=hard_capacity: continuation 与 rebin 完全等价；"
            "n>hard_capacity: rebin 抛 BufferCapacityError，"
            "continuation 凭不可变 Scene + 游标仍精确还原"
        ),
        "lengths": rows,
    }


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------

def run_tile_rules_suite(*, outdir: str | Path | None = None,
                         verify_docs: bool = True,
                         write_files: bool = True):
    """瓦片规则校验总入口，风格与 ``run_check_suite`` 一致。

    返回 ``(ok, report, lines)``。``outdir`` 为 None 时使用每次新建的系统
    临时净目录；校验本身从不读取该目录或 ``out/`` 下的历史材料。
    """
    lines: list[str] = []
    issues, claims = evaluate_mini_grid()
    chain = claims["long_chain_scope"]

    for iss in issues:
        tag = "PASS" if iss.ok else "FAIL"
        lines.append(f"[{tag}] {iss.check_id}: {iss.msg}")
    chain_ok = chain["ok"]
    lines.append(
        f"[{'PASS' if chain_ok else 'FAIL'}] long-chain-scope: "
        f"{len(chain['lengths'])} 个链长（硬上限 {chain['rebin_hard_capacity']}）"
        "下续读/重桶归属与像素核对")

    doc_issues: list[Issue] = []
    if verify_docs:
        from .maintenance import check_documentation
        doc_issues = check_documentation()
        for di in doc_issues:
            lines.append(f"[{'PASS' if di.ok else 'FAIL'}] docs: {di.msg}")

    all_issues = issues + [
        Issue("long-chain-scope", "overflow", chain_ok, "超长工作链适用范围")
    ] + doc_issues
    ok = all(i.ok for i in all_issues)

    report = {
        "ok": ok,
        "claims": claims,
        "issues": [i.as_dict() for i in all_issues],
    }

    if write_files:
        target = Path(outdir) if outdir else Path(
            tempfile.mkdtemp(prefix="tile-rules-clean-"))
        target.mkdir(parents=True, exist_ok=True)
        (target / "tile_rules_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            Path("tile_rules_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
        report["report_path"] = str((target / "tile_rules_report.json").resolve())
        lines.append(f"报告 -> {report['report_path']}")
    lines.append("瓦片规则校验总体: " + ("RULES PASS" if ok else "RULES FAIL"))
    return ok, report, lines


# 常见命名别名，方便不同入口/测试习惯。
def check_tile_rules(**kw):
    return run_tile_rules_suite(**kw)


verify_tile_rules = run_tile_rules_suite
tile_rules_check = run_tile_rules_suite


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m tile_raster.tile_rules",
        description="瓦片规则校验：边界/归属/覆盖集合/溢出状态/换序一致性")
    p.add_argument("--out", default="tile_rules_out",
                   help="证据与报告目录（默认 ./tile_rules_out，建议传净目录）")
    p.add_argument("--clean", action="store_true",
                   help="先删除并重建 --out（仅删除该显式目录）")
    p.add_argument("--no-docs", action="store_true",
                   help="跳过文档声明核对")
    p.add_argument("--json", default=None, help="额外把报告写到该 JSON 路径")
    args = p.parse_args(argv)

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


if __name__ == "__main__":
    import sys

    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
