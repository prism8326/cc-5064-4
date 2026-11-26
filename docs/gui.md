# 瓦片光栅调试视窗使用说明

## 启动（图形入口）

```bash
# 仓库根目录 /workspace
python3 -m tile_raster gui
# 等价：python3 run_gui.py
```

浏览器访问 <http://127.0.0.1:8765/>。

无头 / 容器 / 远程主机：

```bash
python3 -m tile_raster gui --no-browser --host 0.0.0.0 --port 8765
```

仅使用 Python 标准库，不需要安装 tkinter、PIL、numpy 或任何前端依赖；
页面是原生 HTML/CSS/JavaScript，图像以标准库编码的 PNG data URL 返回。

## 界面组成

### 参数栏

| 控件 | 含义 |
| --- | --- |
| 场景 | paper / dense / giant / empty / tiny / depth-order / midway |
| 宽 × 高 | 输出分辨率（服务端有上下限校验，非法输入返回中文错误） |
| 瓦片边长 | 正方形瓦片像素尺寸（也可在 API 中分别给 `tile_w/tile_h`） |
| 候选桶容量 | 每瓦片驻留候选 seq 数 `bucket_capacity` |
| 额外缓冲容量 | 溢出实验室的有界 LRU 快照槽数 `SnapshotCache` |
| 溢出策略 | `continuation` 分批续绘 / `rebin` 倍增重分桶 |
| 工作组顺序 | `serial` 行优先 / `shuffle` 固定种子打乱 |
| 种子 | shuffle 与确定性场景使用 |
| 注入“无可信底图”故障 | 让实验室演示 `OrderUnrecoverableError` |

切换场景会自动填入该场景的规范参数（例如 midway 会填 tile=24、cap=4）。

### 图像区

1. **最终图像**：完全由 `TileExecutor` 逐瓦片执行后写回的 PNG。
2. **覆盖图**：每像素真实覆盖/overdraw 次数的热力图（数据来自 `RasterTarget.cover`）。
3. **计算负载图**：每瓦片真实 covered fragment 数热力图
   （数据来自 `TileDiagnostic.load_fragments = TileStats.covered`）。

三张图都叠加半透明白色瓦片网格，方便核对跨瓦图元。

### KPI 与选项卡

顶部 KPI 展示三角形数、非空瓦片、溢出瓦片、片元覆盖、深度拒绝、混合次数、
分箱/处理/写回/量化耗时等。

- **瓦片诊断**：每瓦片状态（`immediate` / `overflow-continuation` /
  `overflow-rebin` / `empty`）、分配 id 数、溢出 seq、续绘批数、rebin 次数、
  真实片元负载与耗时。
- **缓冲诊断**：容量、硬上限、存储生命周期、溢出与 `midway_in_primitive` 事件、
  最大跨箱图元、最重瓦片等中文提示（`diagnostics.buffer_diagnosis`）。
- **溢出实验室**：A/B 瓦片的不同溢出序号、共享 bridge seq、跨瓦纸边缘探针的串行 vs
  瓦片混色读次数、serial/shuffle/恢复的像素一致性、有界缓存压力与每瓦重读次数、
  投机 episode；故障注入时显示 `OrderUnrecoverableError`。
- **一致性 / 基准**：触发 `/api/check`（252 配置矩阵 + 容量证明）与 `/api/bench`，
  在页面内展示结构化结果。

### 异常提示

- 参数非法：顶部红色横幅显示中文 `InvalidParameter`（HTTP 400）。
- 超过桶硬上限：`BufferCapacityError`。
- 可信底图不可用且次序已无法恢复：`OrderUnrecoverableError`（实验室 HTTP 422），
  界面明确报错而**不会显示伪造的正确结果**。

### 运行状态与覆盖图 / 计算负载异常核对

参数栏点击“渲染瓦片管线”后，图像区上方的**运行状态条**会经历
`运行状态：…渲染中… → PASS / 异常`，无需看终端即可确认管线是否跑完：

- PASS 状态会回显本次全部瓦片参数（场景、宽高、`tile_w/tile_h`、
  `bucket_capacity`、`strategy`、`work_order(seed)`），以及覆盖像素数、
  真实片元数与溢出瓦数。
- 每张图标题右侧有规则角标：
  - **覆盖图**：核对全帧/每瓦覆盖集合与串行参考、以及
    `sum(cover) == TileStats.covered`；不一致时显示“覆盖图异常 ×N”。
  - **计算负载**：核对每瓦覆盖计数和是否等于
    `TileDiagnostic.load_fragments`；不一致时显示“计算负载异常 ×N”。
  - **最终图像**：核对片元归属（像素唯一属于 `y//tile_h, x//tile_w` 瓦）
    与工作组顺序排列；异常时显示“片元归属异常”。
- 状态条右侧 chips 给出瓦网格、覆盖像素、片元、溢出瓦；任何异常都会在
  下方红条列出 `scope/check_id` 与实测值（覆盖集合或负载差额）。这些核对
  全部来自后端 `rule_evidence`，与 `python -m tile_raster tile-rules`
  使用同一份代码，不会出现界面与 CLI 两套结论。

调整瓦片参数并核对的推荐动作：拖动“瓦片尺寸（tile）”与“候选桶容量”，
分别用 `continuation` / `rebin`、`serial` / `shuffle` 渲染同一场景，
确认三张图与角标始终一致、溢出瓦数随 cap 变化。
“一致性 / 基准”选项卡中的“运行瓦片规则校验”按钮调用 `/api/tile-rules`，
在视窗内直接给出小网格覆盖集合、换序归属与超长链范围结果。

> CLI（`render/check/tile-rules/...`）只是无界面辅助；上述参数拖动、
> 角标、状态条与选项卡交互必须在光栅调试视窗中完成，CLI 不能替代。

## HTTP API（供脚本调用）

| 方法/路径 | 作用 |
| --- | --- |
| `GET /api/health` | 健康检查 |
| `POST /api/render` | 渲染，返回三张 PNG data URL、metrics、diagnosis |
| `POST /api/lab` | 溢出/重读实验；业务致命错误时 HTTP 422 |
| `POST /api/check` | 一致性与容量自检 |
| `POST /api/bench` | dense/giant/empty/paper 基准 |

示例：

```bash
curl -s -X POST http://127.0.0.1:8765/api/render \
  -H 'Content-Type: application/json' \
  -d '{"scene":"midway","tile":24,"bucket_capacity":4}' | python3 -m json.tool
```

## 推荐观察路径

1. 选 `paper`，cap=6：看到 A=(1,1)、B=(2,1) 两个相邻瓦片在 seq 8 / 14 溢出，
   paper#777（seq 15、16）横跨两块；打开“溢出实验室”看重读与恢复。
2. 选 `midway`，保持 tile24/cap4：在缓冲诊断事件表中看到
   `midway_in_primitive=是`、跨度第 2/3 瓦；切换 continuation/rebin 并分别渲染，
   最终图像与负载一致，缓冲诊断中的成本项不同。
3. 选 `giant`，tile16：负载图显示超大三角覆盖全部 48 个瓦片，缓冲诊断记录最大跨箱 48。
4. 选 `empty`：覆盖图与负载图全空，covered/pixel tests 为 0。
