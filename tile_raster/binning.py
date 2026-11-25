"""保守瓦片分箱与候选桶容量管理。

容量模型与存储生命周期
----------------------
* 几何的唯一权威所有者是 :class:`~tile_raster.scene.Scene`（整个渲染期间不可变）。
* 分箱器只在 :class:`TileBin` 中保存图元序号 ``seq``（整数），不复制几何。
* 每个瓦片有容量为 ``bucket_capacity`` 的驻留候选桶。第 ``C+1`` 个候选到达时
  记录 ``overflow_seq``（即“该瓦片在某提交序号溢出”），随后按策略处理：

  - ``continuation``（分批续绘）：封存当前前缀并记录 ``continuation_cursor``，
    执行期从权威 Scene 重新扫描、分批补绘；任何几何都不会从 Scene 丢失。
  - ``rebin``（重分桶）：以倍增容量重建该瓦片桶，重新扫描 Scene、按序回填，
    旧桶槽位释放；容量超过 ``max_bucket_capacity`` 时响亮报错。

* 所有追加都经过容量检查并计数；溢出产生 :class:`OverflowEvent`，绝不静默丢几何。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import tile_span_for_triangle, iter_covered_tiles
from .raster import BufferCapacityError
from .scene import Scene


@dataclass
class BinConfig:
    tile_w: int = 16
    tile_h: int = 16
    bucket_capacity: int = 8
    strategy: str = "continuation"  # "continuation" | "rebin"
    max_bucket_capacity: int = 8192
    rebin_growth: int = 2

    def validate(self) -> None:
        if self.tile_w <= 0 or self.tile_h <= 0:
            raise ValueError("瓦片宽高必须为正整数")
        if self.bucket_capacity <= 0:
            raise ValueError("bucket_capacity 必须为正整数")
        if self.strategy not in ("continuation", "rebin"):
            raise ValueError("strategy 只能是 continuation 或 rebin")
        if self.max_bucket_capacity < self.bucket_capacity:
            raise ValueError("max_bucket_capacity 不能小于 bucket_capacity")


@dataclass
class OverflowEvent:
    seq: int
    submission: int
    paper_id: int
    label: str
    tx: int
    ty: int
    tile_index: int
    arriving_count: int
    configured_capacity: int
    primitive_span_tiles: int
    span_index: int
    midway_in_primitive: bool
    strategy: str
    action: str  # "defer-continuation" | "rebin"
    old_capacity: int = 0
    new_capacity: int = 0


@dataclass
class TileBin:
    tx: int
    ty: int
    capacity: int
    ids: list[int] = field(default_factory=list)
    overflow_seq: int | None = None
    overflow_submission: int | None = None
    continuation_cursor: int | None = None
    deferred_count: int = 0
    rebins: int = 0
    rebin_copies: int = 0
    span_tiles_seen: int = 0

    @property
    def tile_index(self) -> int:
        return -1  # 由 Binner 填充上下文


@dataclass
class BinningReport:
    cols: int
    rows: int
    tile_w: int
    tile_h: int
    tile_count_grid: int
    nonempty_tiles: int
    total_bin_appends: int
    capacity_checks: int
    overflow_events: list[OverflowEvent]
    max_primitive_span: int
    max_primitive_span_seq: int
    max_tile_list_length: int
    max_tile_list_coord: tuple[int, int] | None
    per_tile_lengths: dict[str, int]
    strategy: str
    rebin_count: int = 0
    rebin_copies: int = 0
    continuation_tiles: int = 0

    def as_dict(self) -> dict:
        return {
            "cols": self.cols,
            "rows": self.rows,
            "tile_w": self.tile_w,
            "tile_h": self.tile_h,
            "tile_count_grid": self.tile_count_grid,
            "nonempty_tiles": self.nonempty_tiles,
            "total_bin_appends": self.total_bin_appends,
            "capacity_checks": self.capacity_checks,
            "overflow_event_count": len(self.overflow_events),
            "overflow_events": [e.__dict__.copy() for e in self.overflow_events],
            "max_primitive_span": self.max_primitive_span,
            "max_primitive_span_seq": self.max_primitive_span_seq,
            "max_tile_list_length": self.max_tile_list_length,
            "max_tile_list_coord": self.max_tile_list_coord,
            "per_tile_lengths": self.per_tile_lengths,
            "strategy": self.strategy,
            "rebin_count": self.rebin_count,
            "rebin_copies": self.rebin_copies,
            "continuation_tiles": self.continuation_tiles,
        }


class TileBinner:
    def __init__(self, scene: Scene, config: BinConfig):
        config.validate()
        self.scene = scene
        self.config = config
        self.cols = (scene.width + config.tile_w - 1) // config.tile_w
        self.rows = (scene.height + config.tile_h - 1) // config.tile_h
        self.bins: dict[tuple[int, int], TileBin] = {}
        self.capacity_checks = 0
        self.total_appends = 0
        self.events: list[OverflowEvent] = []
        self.max_span = 0
        self.max_span_seq = -1
        # 每个图元的保守瓦片范围，执行/校验时复用，保证一致。
        self.spans: list[tuple[int, int, int, int, int]] = []

    def get_bin(self, tx: int, ty: int) -> TileBin:
        key = (tx, ty)
        b = self.bins.get(key)
        if b is None:
            b = TileBin(tx=tx, ty=ty, capacity=self.config.bucket_capacity)
            self.bins[key] = b
        return b

    def bin_scene(self) -> "TileBinner":
        cfg = self.config
        for tri in self.scene.triangles:
            span = tile_span_for_triangle(
                tri.xs(), tri.ys(), self.scene.width, self.scene.height,
                cfg.tile_w, cfg.tile_h, self.cols, self.rows,
            )
            self.spans.append(span)
            n_tiles = span[4]
            if n_tiles > self.max_span:
                self.max_span = n_tiles
                self.max_span_seq = tri.seq
            span_index = 0
            appended_here_before = 0
            for tx, ty in iter_covered_tiles(span):
                span_index += 1
                b = self.get_bin(tx, ty)
                b.span_tiles_seen += 1
                self._append(b, tri, n_tiles, span_index, appended_here_before)
                appended_here_before += 1
        return self

    def _append(self, b: TileBin, tri, span_tiles: int, span_index: int,
                appended_before_in_primitive: int) -> None:
        cfg = self.config
        self.capacity_checks += 1

        # 已在分批续绘模式下封存的瓦片：后续候选只计数，不占桶。
        if cfg.strategy == "continuation" and b.continuation_cursor is not None:
            b.deferred_count += 1
            return

        if len(b.ids) < b.capacity:
            b.ids.append(tri.seq)
            self.total_appends += 1
            return

        # 驻留桶已满：这是该瓦片的“提交序号溢出”时刻。
        if b.overflow_seq is None:
            b.overflow_seq = tri.seq
            b.overflow_submission = tri.submission

        midway = span_tiles > 1 and appended_before_in_primitive > 0
        arriving = len(b.ids) + 1

        if cfg.strategy == "continuation":
            if b.continuation_cursor is None:
                b.continuation_cursor = tri.seq
                ev = OverflowEvent(
                    seq=tri.seq, submission=tri.submission, paper_id=tri.paper_id,
                    label=tri.label, tx=b.tx, ty=b.ty,
                    tile_index=b.ty * self.cols + b.tx,
                    arriving_count=arriving,
                    configured_capacity=cfg.bucket_capacity,
                    primitive_span_tiles=span_tiles, span_index=span_index,
                    midway_in_primitive=midway, strategy=cfg.strategy,
                    action="defer-continuation", old_capacity=b.capacity,
                )
                self.events.append(ev)
            b.deferred_count += 1
            return

        # rebin：倍增容量并从权威 Scene 重扫回填当前前缀。
        old_cap = b.capacity
        all_ids = self._scan_ids_for_tile(b.tx, b.ty)
        if len(all_ids) > cfg.max_bucket_capacity:
            raise BufferCapacityError(
                f"瓦片 ({b.tx},{b.ty}) 需要 {len(all_ids)} 个槽位，超过硬上限 "
                f"{cfg.max_bucket_capacity}；拒绝静默丢弃几何"
            )
        prefix_ids = [sid for sid in all_ids if sid <= tri.seq]
        new_cap = min(cfg.max_bucket_capacity,
                      max(len(prefix_ids), b.capacity * cfg.rebin_growth))
        if new_cap < len(prefix_ids):
            raise BufferCapacityError(
                f"瓦片 ({b.tx},{b.ty}) 重分桶后仍无法容纳当前前缀"
            )
        b.ids = prefix_ids
        copied = len(prefix_ids)
        b.rebins += 1
        b.rebin_copies += copied
        b.capacity = new_cap
        # 重分桶后当前 tri 必然已在 ids 中。
        if tri.seq not in b.ids:
            raise BufferCapacityError("重分桶后仍丢失当前图元，这是程序错误")
        self.events.append(OverflowEvent(
            seq=tri.seq, submission=tri.submission, paper_id=tri.paper_id,
            label=tri.label, tx=b.tx, ty=b.ty,
            tile_index=b.ty * self.cols + b.tx,
            arriving_count=arriving,
            configured_capacity=cfg.bucket_capacity,
            primitive_span_tiles=span_tiles, span_index=span_index,
            midway_in_primitive=midway, strategy=cfg.strategy,
            action="rebin", old_capacity=old_cap, new_capacity=b.capacity,
        ))

    def _scan_ids_for_tile(self, tx: int, ty: int) -> list[int]:
        """从权威 Scene 保守扫描属于某瓦片的全部图元，严格按 seq 排序。"""
        out: list[int] = []
        for i, tri in enumerate(self.scene.triangles):
            span = self.spans[i] if i < len(self.spans) else tile_span_for_triangle(
                tri.xs(), tri.ys(), self.scene.width, self.scene.height,
                self.config.tile_w, self.config.tile_h, self.cols, self.rows,
            )
            tx0, ty0, tx1, ty1, _ = span
            if tx0 <= tx < tx1 and ty0 <= ty < ty1:
                out.append(tri.seq)
        return out

    def resolve_ordered_ids(self, key: tuple[int, int]) -> list[int]:
        """返回瓦片最终完整、严格按提交序号排列的图元 id。

        * rebin 模式直接用增长后的桶；
        * continuation 模式用权威 Scene 扫描补全（执行期分批，不一次性渲染）。
        """
        b = self.bins[key]
        if self.config.strategy == "rebin":
            return list(b.ids)
        if b.continuation_cursor is None:
            return list(b.ids)
        return self._scan_ids_for_tile(b.tx, b.ty)

    def iter_continuation_chunks(self, key: tuple[int, int]):
        """分批续绘：依次产出需要补绘的图元 seq 块（每块 <= bucket_capacity）。

        第一批为已存储前缀之后的内容；每批都重新扫描权威 Scene，内存有界。
        """
        b = self.bins[key]
        if self.config.strategy != "continuation" or b.continuation_cursor is None:
            return
        chunk_cap = self.config.bucket_capacity
        next_seq = b.continuation_cursor
        n = len(self.scene.triangles)
        while next_seq < n:
            chunk: list[int] = []
            exhausted = True
            for i in range(next_seq, n):
                tri = self.scene.triangles[i]
                span = self.spans[i]
                tx0, ty0, tx1, ty1, _ = span
                if tx0 <= b.tx < tx1 and ty0 <= b.ty < ty1:
                    chunk.append(tri.seq)
                    if len(chunk) >= chunk_cap:
                        next_seq = i + 1
                        exhausted = False
                        break
            if chunk:
                yield chunk
            if exhausted:
                break
            if not chunk:
                break

    def report(self) -> BinningReport:
        lengths: dict[str, int] = {}
        max_len = 0
        max_coord = None
        rebin_count = 0
        rebin_copies = 0
        cont_tiles = 0
        for (tx, ty), b in sorted(self.bins.items()):
            if self.config.strategy == "rebin":
                final_len = len(b.ids)
            elif b.continuation_cursor is not None:
                final_len = len(self._scan_ids_for_tile(tx, ty))
            else:
                final_len = len(b.ids)
            lengths[f"{tx},{ty}"] = final_len
            if final_len > max_len:
                max_len = final_len
                max_coord = (tx, ty)
            rebin_count += b.rebins
            rebin_copies += b.rebin_copies
            if b.continuation_cursor is not None:
                cont_tiles += 1
        return BinningReport(
            cols=self.cols, rows=self.rows,
            tile_w=self.config.tile_w, tile_h=self.config.tile_h,
            tile_count_grid=self.cols * self.rows,
            nonempty_tiles=len(self.bins),
            total_bin_appends=self.total_appends,
            capacity_checks=self.capacity_checks,
            overflow_events=list(self.events),
            max_primitive_span=self.max_span,
            max_primitive_span_seq=self.max_span_seq,
            max_tile_list_length=max_len,
            max_tile_list_coord=max_coord,
            per_tile_lengths=lengths,
            strategy=self.config.strategy,
            rebin_count=rebin_count,
            rebin_copies=rebin_copies,
            continuation_tiles=cont_tiles,
        )

    def verify_completeness(self) -> list[str]:
        """强校验：每个图元保守范围内的每个瓦片都恰好有该 seq 一次。

        返回错误信息列表；空列表表示通过。
        """
        errors: list[str] = []
        for i, tri in enumerate(self.scene.triangles):
            span = self.spans[i]
            for tx, ty in iter_covered_tiles(span):
                key = (tx, ty)
                if key not in self.bins:
                    errors.append(f"seq={tri.seq} 漏箱 ({tx},{ty})")
                    continue
                ids = self.resolve_ordered_ids(key)
                if ids.count(tri.seq) != 1:
                    errors.append(
                        f"seq={tri.seq} 在瓦片 ({tx},{ty}) 出现 {ids.count(tri.seq)} 次"
                    )
        return errors
