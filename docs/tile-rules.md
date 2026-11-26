# 瓦片规则：分箱、瓦片边界与片元归属

> 本文的机器可读声明由可执行校验守护：
>
> ```bash
> python3 -m tile_raster tile-rules          # 含文档核对
> python3 -m tile_raster maintenance         # 只做维护/文档核对
> python3 -m tile_raster mini-grid --out "$(mktemp -d)"
> ```
>
> 校验必须能在**净目录**中运行：所有覆盖集合与溢出状态都从代码现场复算，
> 不读取 `out/` 下的历史产物或任何手改数据。

## 1. 坐标与瓦片边界

屏幕原点在左上，x 向右、y 向下，像素中心为 `(x+0.5, y+0.5)`。瓦片 `(tx,ty)`
覆盖半开矩形：

```text
x ∈ [tx*tw, min((tx+1)*tw, W))
y ∈ [ty*th, min((ty+1)*th, H))
```

因此每个像素 `(x,y)` 的片元归属唯一确定为
`tile_index = (y // th) * cols + (x // tw)`。边缘残瓦使用同一公式，不存在
两不管或双重归属像素。

## 2. 分箱是保守超集，覆盖集合才是事实

`geometry.tile_span_for_triangle` 对顶点 bbox 外扩 1 像素并钳进瓦片网格，
所以候选桶可能包含实际没有覆盖任何采样点的 seq；**候选集合 ⊇ 真实覆盖
集合**。真实覆盖集合只来自 `draw_triangle` 通过 top-left 覆盖判定后对
`RasterTarget.cover` 的递增。核对关系：

- 每瓦 `sum(cover)` == `TileDiagnostic.load_fragments`
  （= `TileStats.covered`，真实着色片元数，含 overdraw）；
- 各瓦覆盖集合按归属划分、互不相交，其计数总和 == `total_stats.covered`；
- 覆盖**像素集合**大小 ≤ 覆盖**片元数**，差值即 overdraw。

## 3. 并行执行约定（示例不得假设串行瓦序）

- **分箱/溢出事件顺序**是确定的：按 seq 遍历图元，跨度内按 ty→tx 落桶，
  与工作组执行顺序无关。
- **工作组（瓦片）执行顺序**允许 `serial`（行优先）或固定种子 `shuffle`。
  每块瓦有私有 color/depth/cover 目标，提交时只写自己的不相交矩形，因此
  换序不得改变最终像素、覆盖集合或逐像素最后写入 seq 的归属。
- **瓦内提交顺序**必须严格按 seq：溢出后 continuation 用游标从权威
  `Scene` 原序续读，rebin 按原序重扫回填；两条路径的
  `resolve_ordered_ids()` 必须相同。

## 4. 溢出处理与容量（显式，不藏在默认配置里）

每个瓦片有显式 `bucket_capacity=C`。第 `C+1` 个候选到达时记录该瓦
`overflow_seq`，随后二选一：

- `continuation`：封存 C 项前缀与游标，后续候选只计 deferred；执行期
  `iter_continuation_chunks` 从不可变 `Scene` 按 seq 原序、每批 ≤ C 续绘，
  常驻槽位 O(C)，链再长也不丢几何。
- `rebin`：容量倍增并从 `Scene` 按原序重扫；该瓦全量 id 数超过显式
  `max_bucket_capacity` 时抛 `BufferCapacityError`，绝不静默丢几何。

适用范围（由 `long_chain_scope` 逐链长执行核对）：硬上限内两种策略的
顺序、覆盖、像素完全等价；超过 rebin 硬上限后 rebin 响亮报错，而游标
续读仍能精确还原——代价是重扫与 replay 片元。若瓦内已把半透明层混进
不可拆分的目标且没有可信底图，则属于次序不可恢复，必须报
`OrderUnrecoverableError`（见 README 与溢出实验室），不在“可续读链”
承诺范围内。

## 5. 小网格参考（8×8 / tile4 / cap2 / max64）

参考绘制脚本：

```bash
python3 -m tile_raster mini-grid --out out/mini_grid_ref
# 或：python3 scripts/small_grid_reference.py --out <净目录>
```

ASCII 图中数字是真实覆盖次数，`|` 与行间分隔是瓦片边界。下列声明即文档
化的覆盖集合与溢出状态，可由 `tile-rules` / `maintenance` 一键复算：

<!-- TILE-RULES-CLAIMS:BEGIN -->
<!-- 本块由 `python -m tile_raster maintenance --update` 生成，请勿手改；维护校验会逐字段复算。 -->
```json
{
  "schema": "tile-rules-claims/v1",
  "mini_grid": {
    "width": 8,
    "height": 8,
    "tile_w": 4,
    "tile_h": 4,
    "cols": 2,
    "rows": 2,
    "bucket_capacity": 2,
    "max_bucket_capacity": 64,
    "strategy": "continuation",
    "triangle_count": 7,
    "covered_pixel_count": 44,
    "covered_fragment_count": 48,
    "overdraw_extra_count": 4,
    "ascii_coverage": ". . 1 1 | 1 1 . 1\n. . 1 1 | 1 1 1 1\n. . . . | . 1 1 1\n. . . 1 | 1 1 1 1\n-----------------\n. . 1 1 | 1 1 1 1\n. 1 1 1 | 1 2 2 1\n1 1 1 1 | 1 2 2 1\n. . . . | 1 1 1 1",
    "tiles": [
      {
        "index": 0,
        "tx": 0,
        "ty": 0,
        "rect": [
          0,
          0,
          4,
          4
        ],
        "coverage_set": [
          [
            2,
            0
          ],
          [
            2,
            1
          ],
          [
            3,
            0
          ],
          [
            3,
            1
          ],
          [
            3,
            3
          ]
        ],
        "cover_multiplicity": [],
        "covered_pixel_count": 5,
        "cover_sum": 5,
        "load_fragments": 5,
        "status": "overflow-continuation",
        "ordered_ids": [
          0,
          1,
          2
        ],
        "overflow_seq": 2,
        "continuation_chunks": [
          [
            2
          ]
        ]
      },
      {
        "index": 1,
        "tx": 1,
        "ty": 0,
        "rect": [
          4,
          0,
          8,
          4
        ],
        "coverage_set": [
          [
            4,
            0
          ],
          [
            4,
            1
          ],
          [
            4,
            3
          ],
          [
            5,
            0
          ],
          [
            5,
            1
          ],
          [
            5,
            2
          ],
          [
            5,
            3
          ],
          [
            6,
            1
          ],
          [
            6,
            2
          ],
          [
            6,
            3
          ],
          [
            7,
            0
          ],
          [
            7,
            1
          ],
          [
            7,
            2
          ],
          [
            7,
            3
          ]
        ],
        "cover_multiplicity": [],
        "covered_pixel_count": 14,
        "cover_sum": 14,
        "load_fragments": 14,
        "status": "overflow-continuation",
        "ordered_ids": [
          0,
          1,
          2
        ],
        "overflow_seq": 2,
        "continuation_chunks": [
          [
            2
          ]
        ]
      },
      {
        "index": 2,
        "tx": 0,
        "ty": 1,
        "rect": [
          0,
          4,
          4,
          8
        ],
        "coverage_set": [
          [
            0,
            6
          ],
          [
            1,
            5
          ],
          [
            1,
            6
          ],
          [
            2,
            4
          ],
          [
            2,
            5
          ],
          [
            2,
            6
          ],
          [
            3,
            4
          ],
          [
            3,
            5
          ],
          [
            3,
            6
          ]
        ],
        "cover_multiplicity": [],
        "covered_pixel_count": 9,
        "cover_sum": 9,
        "load_fragments": 9,
        "status": "immediate",
        "ordered_ids": [
          0
        ],
        "overflow_seq": null,
        "continuation_chunks": []
      },
      {
        "index": 3,
        "tx": 1,
        "ty": 1,
        "rect": [
          4,
          4,
          8,
          8
        ],
        "coverage_set": [
          [
            4,
            4
          ],
          [
            4,
            5
          ],
          [
            4,
            6
          ],
          [
            4,
            7
          ],
          [
            5,
            4
          ],
          [
            5,
            5
          ],
          [
            5,
            6
          ],
          [
            5,
            7
          ],
          [
            6,
            4
          ],
          [
            6,
            5
          ],
          [
            6,
            6
          ],
          [
            6,
            7
          ],
          [
            7,
            4
          ],
          [
            7,
            5
          ],
          [
            7,
            6
          ],
          [
            7,
            7
          ]
        ],
        "cover_multiplicity": [
          [
            5,
            5,
            2
          ],
          [
            5,
            6,
            2
          ],
          [
            6,
            5,
            2
          ],
          [
            6,
            6,
            2
          ]
        ],
        "covered_pixel_count": 16,
        "cover_sum": 20,
        "load_fragments": 20,
        "status": "overflow-continuation",
        "ordered_ids": [
          0,
          3,
          4,
          5,
          6
        ],
        "overflow_seq": 4,
        "continuation_chunks": [
          [
            4,
            5
          ],
          [
            6
          ]
        ]
      }
    ],
    "overflow_events": [
      {
        "tile": [
          0,
          0
        ],
        "seq": 2,
        "action": "defer-continuation",
        "midway_in_primitive": false,
        "primitive_span_tiles": 2,
        "span_index": 1
      },
      {
        "tile": [
          1,
          0
        ],
        "seq": 2,
        "action": "defer-continuation",
        "midway_in_primitive": true,
        "primitive_span_tiles": 2,
        "span_index": 2
      },
      {
        "tile": [
          1,
          1
        ],
        "seq": 4,
        "action": "defer-continuation",
        "midway_in_primitive": false,
        "primitive_span_tiles": 1,
        "span_index": 1
      }
    ],
    "invariance": {
      "pixel_exact_all_modes": true,
      "coverage_exact_all_modes": true,
      "coverage_sets_order_invariant": true,
      "winner_attribution_order_invariant": true,
      "owner_partition": true,
      "independent_reference_matches": true
    },
    "runs": {
      "continuation-serial": {
        "pixel_exact_vs_serial": true,
        "coverage_exact_vs_serial": true,
        "rule_evidence_ok": true,
        "anomaly_count": 0
      },
      "continuation-shuffle": {
        "pixel_exact_vs_serial": true,
        "coverage_exact_vs_serial": true,
        "rule_evidence_ok": true,
        "anomaly_count": 0
      },
      "rebin-serial": {
        "pixel_exact_vs_serial": true,
        "coverage_exact_vs_serial": true,
        "rule_evidence_ok": true,
        "anomaly_count": 0
      },
      "rebin-shuffle": {
        "pixel_exact_vs_serial": true,
        "coverage_exact_vs_serial": true,
        "rule_evidence_ok": true,
        "anomaly_count": 0
      }
    }
  },
  "long_chain_scope": {
    "ok": true,
    "bucket_capacity": 4,
    "rebin_hard_capacity": 32,
    "boundary_rule": "n<=hard_capacity: continuation 与 rebin 完全等价；n>hard_capacity: rebin 抛 BufferCapacityError，continuation 凭不可变 Scene + 游标仍精确还原",
    "lengths": [
      {
        "n": 1,
        "continuation": {
          "ok": true,
          "chunks": 0,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": true,
          "ok": true,
          "pixel_exact": true,
          "ordered_ids_match_continuation": true,
          "error": null
        },
        "row_ok": true
      },
      {
        "n": 2,
        "continuation": {
          "ok": true,
          "chunks": 0,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": true,
          "ok": true,
          "pixel_exact": true,
          "ordered_ids_match_continuation": true,
          "error": null
        },
        "row_ok": true
      },
      {
        "n": 5,
        "continuation": {
          "ok": true,
          "chunks": 1,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": true,
          "ok": true,
          "pixel_exact": true,
          "ordered_ids_match_continuation": true,
          "error": null
        },
        "row_ok": true
      },
      {
        "n": 8,
        "continuation": {
          "ok": true,
          "chunks": 1,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": true,
          "ok": true,
          "pixel_exact": true,
          "ordered_ids_match_continuation": true,
          "error": null
        },
        "row_ok": true
      },
      {
        "n": 17,
        "continuation": {
          "ok": true,
          "chunks": 4,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": true,
          "ok": true,
          "pixel_exact": true,
          "ordered_ids_match_continuation": true,
          "error": null
        },
        "row_ok": true
      },
      {
        "n": 32,
        "continuation": {
          "ok": true,
          "chunks": 7,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": true,
          "ok": true,
          "pixel_exact": true,
          "ordered_ids_match_continuation": true,
          "error": null
        },
        "row_ok": true
      },
      {
        "n": 33,
        "continuation": {
          "ok": true,
          "chunks": 8,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": false,
          "ok": false,
          "pixel_exact": false,
          "ordered_ids_match_continuation": false,
          "error": "BufferCapacityError",
          "error_message": "瓦片 (0,0) 需要 33 个槽位，超过硬上限 32；拒绝静默丢弃几何"
        },
        "row_ok": true
      },
      {
        "n": 64,
        "continuation": {
          "ok": true,
          "chunks": 15,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": false,
          "ok": false,
          "pixel_exact": false,
          "ordered_ids_match_continuation": false,
          "error": "BufferCapacityError",
          "error_message": "瓦片 (0,0) 需要 64 个槽位，超过硬上限 32；拒绝静默丢弃几何"
        },
        "row_ok": true
      },
      {
        "n": 256,
        "continuation": {
          "ok": true,
          "chunks": 63,
          "resident_capacity": 4,
          "ordered_ids_complete": true
        },
        "rebin": {
          "supported": false,
          "ok": false,
          "pixel_exact": false,
          "ordered_ids_match_continuation": false,
          "error": "BufferCapacityError",
          "error_message": "瓦片 (0,0) 需要 256 个槽位，超过硬上限 32；拒绝静默丢弃几何"
        },
        "row_ok": true
      }
    ]
  }
}
```
<!-- TILE-RULES-CLAIMS:END -->

## 6. 图形视窗核对

`python3 -m tile_raster gui` 启动调试视窗后，调整“瓦片尺寸 / 候选桶容量 /
溢出策略 / 工作组顺序”，点击“渲染瓦片管线”。页面给出可见运行状态，并把
后端 `rule_evidence.anomalies` 标成“覆盖图异常 / 计算负载异常 / 归属或
溢出异常”角标；CLI 只是无界面辅助，不能替代视窗里的交互核对。详见
[`docs/gui.md`](gui.md)。
