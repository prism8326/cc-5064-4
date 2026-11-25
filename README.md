# 计算式瓦片光栅渲染器 · tile_raster

一个**零第三方依赖**（仅 Python 3.11 标准库）的计算式瓦片光栅器与配套**瓦片光栅调试视窗**。

它不是“先渲染整屏再切块显示”：每个瓦片的最终像素都由瓦片执行器
（`TileExecutor` → 同一个 `draw_triangle`）在本地紧凑目标上逐像素做真实覆盖判定、
属性插值、深度测试/写入与半透明混合后产生，再跨距写回全帧。覆盖热力图与计算负载图
同样来自真实的逐像素/逐瓦片计数，而不是候选桶列表长度。

---

## 图形入口（主要交付方式）

```bash
# 在 /workspace 下
python3 -m tile_raster gui
# 或使用根目录便捷启动器
python3 run_gui.py
```

启动后浏览器打开：

```
http://127.0.0.1:8765/
```

容器 / 无头 / 远程环境（不自动拉起浏览器）：

```bash
python3 -m tile_raster gui --no-browser --host 0.0.0.0 --port 8765
```

视窗里可以调整场景、宽高、瓦片尺寸、候选桶容量、额外缓冲槽、溢出策略
（分批续绘 / 重分桶）、工作组顺序（serial / 固定种子 shuffle）与故障注入，
并查看：

- **最终图像**（瓦片执行器实际产出，标准库编码 PNG）
- **覆盖图**：每像素真实 cover / overdraw 次数热力图
- **计算负载图**：每瓦片真实 covered fragment 数热力图
- 每瓦片诊断表、缓冲/容量诊断、溢出实验室、一致性矩阵与基准
- 参数错误、容量硬上限、`OrderUnrecoverableError` 等异常会在页面顶部红条提示

> 命令行（`render / check / bench / lab`）只是无界面辅助，见文末。
> 视窗使用说明另见 [`docs/gui.md`](docs/gui.md)。

---

## 30 秒自检

```bash
# 27 个 unittest（分箱 / 像素一致性 / 溢出实验室 / HTTP API）
python3 -m unittest discover -s tests -v

# 一键性质自检：252 个像素一致性配置 + 策略对比 + 中途溢出 + 容量增长证明
python3 -m tile_raster check

# 无界面出图（final / coverage / load 三张 PNG + metrics.json）
python3 -m tile_raster render --scene paper --outdir out/paper
```

最近一次结果：`Ran 27 tests ... OK`；`check` 输出 `总体: ALL PASS`
（252 个配置全部逐字节一致）。

---

## 需求 → 实现 / 证据映射

| 要求 | 实现位置 | 可执行证据 |
| --- | --- | --- |
| 变换后三角形保守分配到覆盖瓦片 | `geometry.tile_span_for_triangle` / `iter_covered_tiles`，`binning.TileBinner` | `tests/test_binning.py`、`verify_completeness()` |
| 瓦片内真实覆盖、插值、深度、混合 | `raster.draw_triangle`（1/256 子像素定点边函数、重心插值、深度缓冲、straight alpha） | `tests/test_raster_consistency.py` |
| 两个相邻瓦片在**不同提交序号**溢出 | `scene.make_paper_overflow_scene`：A=(1,1) `overflow_seq=8`，B=(2,1) `=14`（96×72/tile24/cap6） | `tests/test_overflow_lab.py` |
| 一张纸对两块都有真实贡献 | paper#777 的 seq 15、16 同时在 A/B，探针混色读与像素校验 | lab `bridge_seqs_in_tile=[15,16]` |
| 有空间的瓦片立即完成，其余按序补绘 | `executor.TileExecutor` + `binning.iter_continuation_chunks` | 瓦片状态 `immediate` / `overflow-continuation` |
| 混色不可拆分时从**可信底图**重算 | `executor.TrustedBase`（首张半透明前的 color/depth/cover 全帧快照），`overflow_lab` 恢复+全 seq 重播 | `recovery_pixel_exact=True` |
| 可信底图也不可用必须**明确报错** | `raster.OrderUnrecoverableError`（中文错误），HTTP 422，界面红条 | `--fault`、`test_fault_..._reported_not_wrong_pixels` |
| 限制额外缓冲容量 | `overflow_lab.SnapshotCache(capacity)` 有界 LRU，hits/misses/evictions | `test_bounded_extra_buffer_reread_counts` |
| 打乱工作组完成顺序 | `ExecuteConfig(work_order="shuffle", seed=...)`，确定性 shuffle | shuffle 与 serial 像素逐字节一致 |
| 沿跨瓦纸边对比串行混色 vs 每块重读 | `overflow_lab.run_overflow_lab`：48 个边缘探针，串行/瓦片均 52 次 dst 混色读 | `blend_dst_reads_on_probes=52` |
| 跨多瓦图元**中途**顶爆候选桶，比较续绘/重分桶 | `scene.make_midway_scene`：跨 3 瓦大桥在第 2 瓦 (1,0) 触发，`OverflowEvent.midway_in_primitive` | `tests/test_midway_overflow.py`、check 中途溢出行 |
| 分批续绘 vs 重分桶，严格按提交次序 | 两策略 `resolve_ordered_ids()` 完全相同；像素都等于串行参考 | `test_strategies_resolve_same_full_order` |
| 分箱范围保守且有界，超大/极小都不漏 | 顶点 bbox 外扩 1px、屏幕裁剪、钳到瓦片网格 | giant 跨满 48 箱；tiny/subpixel 完整性 0 错误 |
| 列表容量 / 硬上限 / 生命周期清楚，不静默丢几何 | `BinConfig.bucket_capacity/max_bucket_capacity`，权威几何只在不可变 `Scene`，桶仅存 seq | `test_hard_cap_raises_instead_of_silent_loss` |
| 容量检查覆盖“列表增长最剧烈”输入 | `benchmarks.capacity_growth_proof`：400 三角单瓦，400/400 次检查，容量链 8→512 | `test_capacity_checks_cover_worst_growth_input` |
| 简单串行参考路径 | `raster.render_serial_reference`，与瓦片路径调用同一个 `draw_triangle` | 全部像素一致性断言 |
| 不同瓦片尺寸 / 执行顺序像素一致性 | `benchmarks.consistency_suite`：7 场景 × tile{8,16,24} × cap{2,6,64} × 2 策略 × 2 顺序 = 252 | `python -m tile_raster check` |
| dense / giant / empty 场景与分箱、处理、写回成本 | `benchmarks.benchmark_scenes` | `python -m tile_raster bench` |
| 记录超大三角跨箱数 | giant 128×96/tile16 → 8×6=48，最大 span=48 | growth 报告 `giant_max_span` |
| 图形调试视窗 + 覆盖图 + 计算负载图 + 异常提示 | `tile_raster/web/`、`service.py`、`server.py` | jsdom 运行时冒烟 `UI_SMOKE_PASS` |

---

## 管线与正确性要点

1. **变换**：`geometry.Mat3` / `compose_affine` 提供平移、缩放、旋转与复合；
   `Scene.transformed_copy` 可对整场景施加变换。
2. **保守分箱**：对变换后三角形顶点 bbox 向外扩 1 像素（覆盖子像素取样与舍入），
   裁剪到屏幕并钳制进瓦片网格，因此范围**保守且有界**。每个三角形在其 span 的
   每个瓦片候选桶中恰好出现一次 seq（`verify_completeness` 强校验）。
3. **光栅与填充规则**：1/256 子像素定点边函数，左上（top-left）填充约定保证共享边
   不重复覆盖、不留缝。已用随机四边形实验与 `test_quad_seam_no_double_coverage`
   验证：相邻三角形零双重覆盖，轴对齐四边形 union 面积精确。
4. **属性与深度**：重心权重插值 RGB/A 与 z；默认 `z < depth` 通过，材质可关深度测试、
   控制深度写。每个瓦片有独立紧凑 depth/cover 缓冲。
5. **混合**：straight alpha，`out = src*a + dst*(1-a)`。瓦片路径与串行参考使用同一函数、
   同一 seq 顺序与同一浮点运算，因此结果可逐字节相等（不只是“视觉接近”）。
6. **写回**：瓦片目标按 `gy * screen_w + x0 + lx` 的跨距正确复制进全帧，支持边缘残瓦。

### 容量模型与存储生命周期

- 几何的**唯一权威所有者**是不可变语义的 `Scene`；`TileBin.ids` 只保存 `Triangle.seq`
  整数，不复制顶点/颜色。
- 每瓦片候选桶初始容量 `bucket_capacity`。第 `C+1` 个候选到达时记录该瓦片的
  `overflow_seq / overflow_submission`，随后：
  - **continuation（分批续绘，默认）**：封存当前前缀与 `continuation_cursor`，
    后续候选只计 `deferred_count` 不占桶；执行期用 `iter_continuation_chunks`
    从权威 `Scene` 重扫，按每批 ≤ C 个 seq **按提交顺序补绘**。额外内存有界。
  - **rebin（重分桶）**：容量按 `rebin_growth` 倍增，从 `Scene` 重扫并只回填
    `seq <= 当前 seq` 的前缀（未来图元仍正常追加，避免重复），旧槽释放并累计
    `rebin_copies`。所需槽位超过 `max_bucket_capacity` 时抛 `BufferCapacityError`。
- 任何路径都不会在溢出后静默丢弃几何：要么续绘/重分桶容纳，要么响亮报错。

### 次序不可恢复与可信底图

当一块区域已经把多个半透明层混合成“最终颜色”后，较早的图元无法再插入正确次序。
实验室 `speculative_tile_episode` 故意提前混色跨瓦纸并制造这种倒置：

- 若有 `TrustedBase`（第一张半透明图元之前捕获的整屏 color/depth/cover）或有界
  `SnapshotCache` 中命中快照，则恢复底图并按全部 ordered seq 重播，像素重新与串行
  参考逐字节一致；
- 若可信底图不可用且额外缓存未命中，返回/抛出中文 `OrderUnrecoverableError`
  （界面红条、HTTP 422、CLI 退出码 2），**不输出伪造的“正确”像素**。

---

## 专门的“跨瓦中途溢出”场景

`midway`（96×48，tile=24，cap=4）：4 个小三角严格只落在瓦片 R=(1,0) 并把桶**恰好填满**，
随后的 `midway-bridge` 横跨 `(0,0) (1,0) (2,0)` 三个瓦片。按 ty→tx 处理到跨度内第 2 个
瓦片 R 时该桶溢出，因此两种策略都记录同一个事件：

```text
midway_in_primitive=True, tile=(1,0), span_index=2/3, seq=4
continuation：1 个续绘批，deferred=1
rebin       ：倍增并复制 5 个前缀项
两种策略最终 ordered ids 相同，serial/shuffle 像素都与串行参考逐字节一致
```

在视窗中选择“跨瓦中途溢出 (midway, tile24/cap4)”即可以规范参数复现；
`python -m tile_raster check` 也会输出该证据行。

---

## 覆盖图与计算负载图的真实来源

- `coverage`：每个像素在 `draw_triangle` 内通过 top-left 覆盖判定后递增的真实
  cover/overdraw 计数（`RasterTarget.cover`），`coverage_rgb()` 据此上色并叠瓦片边界。
- 负载图：每个 `TileDiagnostic.load_fragments = TileStats.covered`，即该瓦片执行器
  实际着色的片元数；另有 `pixel_tests / depth_rejected / shaded / blend_fragments /
  blend_dst_reads / replay_fragments` 等真实计数。
- 候选桶长度、`total_bin_appends` 只用于分箱诊断，**不**参与覆盖图或负载图。

---

## 基准样例（本机，repeat=3，毫秒；数值随机器变化）

```text
dense  160x120 tile16 tris=220 bin=1.31  proc=25.26 wb=4.39  covered=18560  tests=57994 span=9
giant  256x192 tile32 tris=3   bin=0.14  proc=69.24 wb=6.53  covered=84427  tests=123648 span=48
paper  96x72   tile24 tris=23  bin=0.06  proc=5.55  wb=1.03  covered=6697   tests=11820 span=12
empty  320x240 tile32 tris=0   bin=0.001 proc=0.04  wb=11.92 covered=0      tests=0     span=0
```

`bin_ms / process_ms / writeback_ms / quantize_ms` 分别测量分箱、瓦片处理、全帧写回与
PNG/量化成本。运行 `python3 -m tile_raster bench --repeat 5 --out out/bench.json` 复现。

---

## 目录结构

```text
tile_raster/
  geometry.py       仿射变换、定点边函数、保守瓦片跨度
  scene.py          Vertex/Material/Triangle/Scene 与全部内置场景工厂
  raster.py         draw_triangle 真实片元管线 + 串行参考；异常类型
  binning.py        保守分箱、容量/溢出事件、continuation/rebin、完整性校验
  executor.py       TileExecutor、TrustedBase、瓦片诊断、RenderResult/热力图
  overflow_lab.py   跨瓦纸边缘重读、LRU 快照缓存、可信底图恢复/故障
  benchmarks.py     252 配置一致性矩阵、策略对比、容量增长证明、基准
  diagnostics.py    缓冲/容量中文诊断
  imaging.py        标准库 PPM/PNG 编码与热力 LUT
  service.py        参数校验 + render/lab/check/bench 的 JSON-safe 负载
  server.py         ThreadingHTTPServer 与静态资源/API 路由
  cli.py            gui/render/check/bench/lab
  web/              index.html / styles.css / app.js（原生 JS + Canvas）
tests/              27 个 unittest（含真实 HTTP 服务测试）
docs/gui.md         调试视窗使用说明
run_gui.py          根目录图形启动器
```

---

## 命令行（仅辅助）

```bash
python3 -m tile_raster gui [--host 127.0.0.1] [--port 8765] [--no-browser]
python3 -m tile_raster render --scene paper --tile 24 --bucket-capacity 6 \
    --strategy continuation --work-order shuffle --seed 7 --outdir out/paper
python3 -m tile_raster check [--json out/check.json]
python3 -m tile_raster bench --repeat 5 [--out out/bench.json]
python3 -m tile_raster lab --bucket-capacity 6 --extra 1 --seed 7 [--fault]
```

---

## 验证方式与环境限制

- 全部算法/API 由 `tests/` 的 27 个 unittest 与 `check` 的 252 配置矩阵守护。
- Web 后端经真实 `ThreadingHTTPServer` 测试（健康检查、静态首页、render、参数错误 400、
  故障 lab 422）。
- 前端逻辑用 jsdom 加载真实 `app.js` 并对接真实后端做过运行时冒烟
  （KPI、瓦片表、PNG data URL、lab A8/B14、rebin 渲染、非法参数与 fault 红条，
  结果 `UI_SMOKE_PASS`）。
- 当前交付环境没有可用的真实显示器/浏览器，因此未做像素级人工浏览器视觉回归；
  已通过 HTTP/PNG 签名校验、直接查看解码后的 PNG，以及 jsdom 运行时冒烟替代。
