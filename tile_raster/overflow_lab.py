"""半透明彩纸溢出实验与跨瓦片纸边对比。

完整复现题目描述的场景：

* 两个相邻瓦片 A/B 在不同提交序号溢出，同一张彩纸对两块都有贡献；
* 有界额外缓冲（LRU 可信底图快照池，容量 ``extra_buffer_capacity``）；
* 工作组按指定顺序（串行或打乱）完成；
* 注入“晚到图元已被提前混色”的次序险情：单一颜色缓冲无法恢复中间次序时，
  若可信底图仍可读则从底图重算并严格按提交序号重播；若底图也不存在则抛出
  :class:`OrderUnrecoverableError`，由界面明确提示，绝不输出错误像素。
* 沿跨瓦片纸边探针比较串行混色序列与每块重读次数。
"""
from __future__ import annotations

import random
from collections import OrderedDict
from dataclasses import dataclass

from .binning import BinConfig, TileBinner
from .executor import ExecuteConfig, TileExecutor, TrustedBase
from .raster import (
    OrderUnrecoverableError,
    RasterTarget,
    TileStats,
    draw_triangle,
    render_serial_reference,
)
from .scene import Scene


class SnapshotCache:
    """容量有界的额外缓冲：缓存瓦片可信底图快照（LRU）。"""

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("extra_buffer_capacity 必须 >= 1")
        self.capacity = capacity
        self._d: "OrderedDict[tuple[int,int], tuple]" = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key):
        if key in self._d:
            self._d.move_to_end(key)
            self.hits += 1
            return self._d[key]
        self.misses += 1
        return None

    def put(self, key, snapshot) -> None:
        if key in self._d:
            self._d.move_to_end(key)
            self._d[key] = snapshot
            return
        self._d[key] = snapshot
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)
            self.evictions += 1

    def has(self, key) -> bool:
        return key in self._d

    def keys(self):
        return list(self._d.keys())


@dataclass
class SpecTileOutcome:
    key: tuple[int, int]
    recovered: bool
    initial_cache_hit: bool
    recovery_cache_hit: bool | None
    base_rereads: int
    early_seqs: list[int]
    max_blended_seq: int
    inversion_next_seq: int | None
    wasted_fragments: int
    replay_fragments: int
    blend_dst_reads: int
    error: str | None = None


def _tile_target(scene: Scene, key: tuple[int, int], tw: int, th: int) -> RasterTarget:
    tx, ty = key
    x0, y0 = tx * tw, ty * th
    x1, y1 = min(scene.width, x0 + tw), min(scene.height, y0 + th)
    return RasterTarget.create(
        x0, y0, x1, y1, bg=scene.background, bg_alpha=scene.background_alpha
    )


def speculative_tile_episode(
    scene: Scene,
    binner: TileBinner,
    key: tuple[int, int],
    base: TrustedBase | None,
    cache: SnapshotCache,
    early_seqs: list[int],
    *,
    trusted_base_available: bool = True,
    trace=None,
) -> tuple[RasterTarget | None, SpecTileOutcome]:
    """对单个瓦片执行“提前混色 -> 次序检测 -> 底图重算”的完整事件。"""
    tw, th = binner.config.tile_w, binner.config.tile_h
    target = _tile_target(scene, key, tw, th)
    stats = TileStats()
    base_rereads = 0

    # 1) 取得可信底图快照（先走有界额外缓冲）。
    snap = cache.get(key)
    initial_hit = snap is not None
    if snap is None and base is not None and trusted_base_available:
        snap = base.read_snapshot(key, target)  # type: ignore[attr-defined]
        cache.put(key, snap)
    if snap is not None:
        target.restore(snap)
    # 若没有可信底图，则从清空目标开始；不会把空状态伪装成可信快照放入缓存。

    ordered_ids = binner.resolve_ordered_ids(key)
    early_set = set(early_seqs)
    early_in_tile = [s for s in ordered_ids if s in early_set]

    # 2) 险情：把指定的“晚到彩纸”提前混色。
    max_blended = -1
    wasted = 0
    for seq in early_in_tile:
        before = stats.covered
        draw_triangle(scene.triangles[seq], target, stats, trace=trace)
        wasted += stats.covered - before
        max_blended = max(max_blended, seq)

    # 3) 次序守卫：按提交序号扫描，若下一个应画序号小于已混最大序号，
    #    说明较早图元无法插入已混好的半透明颜色之下。
    inversion_next = None
    drawn = set(early_in_tile)
    for seq in ordered_ids:
        if seq in drawn:
            continue
        if max_blended >= 0 and seq < max_blended:
            inversion_next = seq
            break

    recovery_hit = None
    if inversion_next is not None:
        # 4) 只能从可信底图重算。先尝试额外缓冲里的快照。
        snap2 = cache.get(key)
        recovery_hit = snap2 is not None
        if snap2 is None:
            if base is None or not trusted_base_available:
                err = (
                    f"瓦片 {key} 在 seq={inversion_next} 处发生混合次序倒置："
                    f"seq>{max_blended} 的半透明结果已经混入唯一颜色缓冲，"
                    f"中间次序无法恢复，且可信底图不可用（额外缓冲也未命中）。"
                    f"拒绝继续，以免产生错误像素。"
                )
                outcome = SpecTileOutcome(
                    key=key, recovered=False, initial_cache_hit=initial_hit,
                    recovery_cache_hit=False, base_rereads=base_rereads,
                    early_seqs=early_in_tile, max_blended_seq=max_blended,
                    inversion_next_seq=inversion_next,
                    wasted_fragments=wasted, replay_fragments=0,
                    blend_dst_reads=stats.blend_dst_reads, error=err,
                )
                return None, outcome
            snap2 = base.read_snapshot(key, target)
            base_rereads += 1
            cache.put(key, snap2)

        target.restore(snap2)
        stats = TileStats()  # 重算工作量单独计量
        # 5) 严格按提交序号重播该瓦片全部图元，保证深度与混合次序。
        for seq in ordered_ids:
            draw_triangle(scene.triangles[seq], target, stats,
                          trace=trace, count_as_replay=True)

    outcome = SpecTileOutcome(
        key=key, recovered=inversion_next is not None,
        initial_cache_hit=initial_hit,
        recovery_cache_hit=recovery_hit,
        base_rereads=base_rereads,
        early_seqs=early_in_tile,
        max_blended_seq=max_blended,
        inversion_next_seq=inversion_next,
        wasted_fragments=wasted,
        replay_fragments=stats.replay_fragments if inversion_next is not None else 0,
        blend_dst_reads=stats.blend_dst_reads,
    )
    return target, outcome


# TrustedBase 增加按 key 读取紧凑快照的辅助（避免循环导入问题，直接打猴子方法
# 不如在类里实现；这里通过包装函数实现同样逻辑）。
def _read_snapshot(self: TrustedBase, key, target: RasterTarget):
    tx, ty = key
    tw = target.x1 - target.x0
    th = target.y1 - target.y0
    color: list[float] = []
    depth: list[float] = []
    cover: list[int] = []
    for ly in range(th):
        gy = target.y0 + ly
        base_row = gy * self.width + target.x0
        for lx in range(tw):
            gi = base_row + lx
            color.extend(self.color[gi * 4:gi * 4 + 4])
            depth.append(self.depth[gi])
            cover.append(self.cover[gi])
    return color, depth, cover


TrustedBase.read_snapshot = _read_snapshot  # type: ignore[attr-defined]


def find_edge_probes(scene: Scene, tile_w: int, tile_h: int,
                     border_x: int = 48, y_range=(28, 48),
                     band: int = 2) -> list[tuple[int, int]]:
    """A/B 分界线两侧的候选探针像素（之后用串行轨迹确认被跨瓦纸覆盖）。"""
    pts = []
    for x in range(border_x - band, border_x + band):
        for y in range(y_range[0], y_range[1]):
            if 0 <= x < scene.width and 0 <= y < scene.height:
                pts.append((x, y))
    return pts


def _trace_summary(trace, probes):
    out = {}
    for p in probes:
        events = trace.get(p, [])
        out[f"{p[0]},{p[1]}"] = [
            {
                "seq": e.seq, "paper_id": e.paper_id, "action": e.action,
                "depth_passed": e.depth_passed,
            }
            for e in events
        ]
    return out


def run_overflow_lab(
    scene: Scene,
    *,
    tile: int = 24,
    bucket_capacity: int = 6,
    extra_buffer_capacity: int = 1,
    seed: int = 20261125,
    recovery_order: str = "shuffle",
    tile_a=(1, 1),
    tile_b=(2, 1),
    bridge_paper_id: int = 777,
    trusted_base_available: bool = True,
) -> dict:
    """运行完整溢出对比实验，返回 JSON 可序列化报告。"""
    bin_cfg = BinConfig(tile_w=tile, tile_h=tile,
                        bucket_capacity=bucket_capacity,
                        strategy="continuation")
    binner = TileBinner(scene, bin_cfg).bin_scene()

    # 1) 串行参考 + 探针轨迹
    probes = find_edge_probes(scene, tile, tile)
    serial_trace = {p: [] for p in probes}
    ref_target, ref_stats = render_serial_reference(scene, trace=serial_trace)

    bridge_seqs = [t.seq for t in scene.triangles
                   if t.paper_id == bridge_paper_id]
    # 仅保留确实被跨瓦纸覆盖的探针
    edge_probes = [
        p for p in probes
        if any(e.paper_id == bridge_paper_id for e in serial_trace.get(p, []))
    ]
    if not edge_probes:
        edge_probes = probes  # 兜底：仍然给出探针

    # 2) 安全瓦片执行：串行 / 打乱工作组
    safe_results = {}
    for order in ("serial", "shuffle"):
        ex_cfg = ExecuteConfig(work_order=order, seed=seed)
        tr = {p: [] for p in edge_probes}
        res = TileExecutor(scene, bin_cfg, ex_cfg).run(trace=tr)
        safe_results[order] = (res, tr)

    serial_edge = {
        p: [e for e in serial_trace[p] if e.paper_id == bridge_paper_id or True]
        for p in edge_probes
    }

    # 探针级统计
    def probe_stats(trace):
        blends = 0
        seqs = []
        for p in edge_probes:
            evs = trace.get(p, [])
            blends += sum(1 for e in evs if e.action == "blend")
            seqs.append([e.seq for e in evs if e.depth_passed is not False])
        return blends, seqs

    serial_blends, serial_seqs = probe_stats(serial_trace)
    tile_probe = {}
    pixel_order_matches = True
    for order, (res, tr) in safe_results.items():
        blends, seqs = probe_stats(tr)
        # 与串行逐像素比较通过深度的 seq 序列
        for i, p in enumerate(edge_probes):
            if seqs[i] != serial_seqs[i]:
                pixel_order_matches = False
        tile_probe[order] = {
            "blend_dst_reads_on_probes": blends,
            "seq_sequences": seqs,
        }

    # 3) 取安全执行产出的可信底图，做乱序险情实验
    base = safe_results["serial"][0].trusted_base
    overflow_keys = [
        (tx, ty) for (tx, ty), b in sorted(binner.bins.items())
        if b.overflow_seq is not None
    ]
    # 确保 A/B 在实验集合中且排在前面
    focus = []
    for k in (tile_a, tile_b):
        if k in binner.bins and k not in focus:
            focus.append(k)
    for k in overflow_keys:
        if k not in focus:
            focus.append(k)

    # 阶段 A：按提交/串行次序借出额外缓冲快照并注入提前混色
    cache = SnapshotCache(extra_buffer_capacity)
    episodes: list[SpecTileOutcome] = []
    dirty_targets: dict[tuple[int, int], RasterTarget] = {}
    for key in focus:
        target, oc = speculative_tile_episode(
            scene, binner, key, base, cache, bridge_seqs,
            trusted_base_available=trusted_base_available,
        )
        episodes.append(oc)
        if target is not None:
            dirty_targets[key] = target

    fatal_error = next((oc.error for oc in episodes if oc.error), None)

    # 恢复后瓦片像素必须与串行参考对应区域完全一致。
    recovery_pixel_exact = True
    recovery_checked = 0
    for key, target in dirty_targets.items():
        if target is None:
            continue
        recovery_checked += 1
        tx, ty = key
        for ly in range(target.h):
            gy = target.y0 + ly
            for lx in range(target.w):
                gx = target.x0 + lx
                ri = gy * ref_target.w + gx
                ti = ly * target.w + lx
                for c4 in range(4):
                    a = target.color[ti * 4 + c4]
                    b = ref_target.color[ri * 4 + c4]
                    if _q(a) != _q(b):
                        recovery_pixel_exact = False

    def cache_pressure(order: str) -> dict:
        """有界额外缓冲 + 指定工作组完成顺序下的每块重读。"""
        comp = list(focus)
        if order == "shuffle":
            random.Random(seed + 1).shuffle(comp)
        cachep = SnapshotCache(extra_buffer_capacity)
        reread: dict[str, int] = {}
        hits: dict[str, bool] = {}
        if trusted_base_available:
            for key in focus:  # issue 顺序固定为提交顺序
                snap = base.read_snapshot(
                    key, _tile_target(scene, key, tile, tile))
                cachep.put(key, snap)
            survivors = [list(k) for k in cachep.keys()]
            for key in comp:
                got = cachep.get(key)
                ks = f"{key[0]},{key[1]}"
                if got is None:
                    reread[ks] = reread.get(ks, 0) + 1
                    hits[ks] = False
                    snap = base.read_snapshot(
                        key, _tile_target(scene, key, tile, tile))
                    cachep.put(key, snap)
                else:
                    reread[ks] = 0
                    hits[ks] = True
        else:
            survivors = []
        return {
            "completion_order": [list(k) for k in comp],
            "survivors_after_issue": survivors,
            "hits": cachep.hits,
            "misses": cachep.misses,
            "evictions": cachep.evictions,
            "base_rereads_by_tile": reread,
            "recovery_cache_hit_by_tile": hits,
            "total_base_rereads": sum(reread.values()),
        }

    pressure = {o: cache_pressure(o) for o in ("serial", "shuffle")}

    # 4) 像素校验：安全瓦片结果与串行参考逐字节一致
    safe_res = safe_results["serial"][0]
    pixel_exact = safe_res.rgb == _ref_rgb_bytes(ref_target)
    shuffled_exact = safe_results["shuffle"][0].rgb == safe_res.rgb

    ab_info = {}
    for name, key in (("A", tile_a), ("B", tile_b)):
        b = binner.bins.get(key)
        ids = set(binner.resolve_ordered_ids(key)) if b else set()
        ab_info[name] = {
            "tile": list(key),
            "overflow_seq": b.overflow_seq if b else None,
            "overflow_submission": b.overflow_submission if b else None,
            "final_id_count": len(ids),
            "bridge_seqs_in_tile": [s for s in bridge_seqs if s in ids],
        }

    report = {
        "tile": tile,
        "bucket_capacity": bucket_capacity,
        "extra_buffer_capacity": extra_buffer_capacity,
        "bridge_paper_id": bridge_paper_id,
        "bridge_seqs": bridge_seqs,
        "A_B": ab_info,
        "edge_probe_count": len(edge_probes),
        "edge_probes": [list(p) for p in edge_probes],
        "serial": {
            "blend_dst_reads_on_probes": serial_blends,
            "total_blend_fragments": ref_stats.blend_fragments,
        },
        "tiled": tile_probe,
        "probe_seq_order_matches": pixel_order_matches,
        "safe_pixel_exact_vs_serial": pixel_exact,
        "shuffled_pixel_exact": shuffled_exact,
        "recovery_pixel_exact": recovery_pixel_exact,
        "recovery_checked_tiles": recovery_checked,
        "speculative_episodes": [
            {k: (list(v) if isinstance(v, tuple) else v)
             for k, v in oc.__dict__.items()}
            for oc in episodes
        ],
        "cache_pressure": pressure,
        "trusted_base_available": trusted_base_available,
        "fatal_error": fatal_error,
    }
    return report


def _q(v: float) -> int:
    v = 0.0 if v < 0 else 1.0 if v > 1 else v
    return int(v * 255.0 + 0.5)


def _ref_rgb_bytes(target: RasterTarget) -> bytes:
    out = bytearray(target.w * target.h * 3)
    for i in range(target.w * target.h):
        for c in range(3):
            v = target.color[i * 4 + c]
            v = 0.0 if v < 0 else 1.0 if v > 1 else v
            out[i * 3 + c] = int(v * 255.0 + 0.5)
    return bytes(out)
