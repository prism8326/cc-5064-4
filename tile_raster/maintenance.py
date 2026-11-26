"""执行约定的“维护校验”：核对文档声明与代码/执行证据是否仍然一致。

核对内容：

* ``docs/tile-rules.md`` 中由标记包裹的机器可读声明（小网格覆盖集合、溢出
  状态、换序不变量、超长链适用范围）必须与当前代码复算结果**完全一致**；
* 必备约定主题（分箱 / 瓦片边界 / 片元归属 / 两种溢出策略 / 净目录入口）
  不能从文档中消失；
* ``README.md`` 与 ``docs/gui.md`` 必须保留光栅调试视窗启动入口、瓦片参数
  调整与“覆盖图 / 计算负载异常”核对说明。

更新声明块：``python -m tile_raster.maintenance --update``。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BEGIN = "<!-- TILE-RULES-CLAIMS:BEGIN -->"
END = "<!-- TILE-RULES-CLAIMS:END -->"

REQUIRED_TERMS = (
    "分箱", "瓦片边界", "片元归属", "continuation", "rebin", "净目录",
)
README_REQUIRED = (
    "python3 -m tile_raster gui", "docs/gui.md", "tile-rules",
)
GUI_DOC_REQUIRED = (
    "运行状态", "异常", "覆盖图", "计算负载", "tile",
)


def find_repo_root(start: Path | None = None) -> Path | None:
    """从包位置/给定位置向上找含 pyproject.toml 与 docs/ 的仓库根。"""
    here = (start or Path(__file__)).resolve()
    candidates = [here, *here.parents]
    for c in candidates:
        if (c / "pyproject.toml").is_file() and (c / "docs").is_dir():
            return c
    # 退而求其次：包的上一级（源码布局）。
    pkg_parent = Path(__file__).resolve().parents[1]
    if (pkg_parent / "docs").is_dir():
        return pkg_parent
    return None


def compute_claims() -> dict:
    from .tile_rules import evaluate_mini_grid

    _, claims = evaluate_mini_grid()
    return claims


def render_claims_block(claims: dict) -> str:
    body = json.dumps(claims, ensure_ascii=False, indent=2, sort_keys=False)
    return (
        f"{BEGIN}\n"
        "<!-- 本块由 `python -m tile_raster maintenance --update` 生成，"
        "请勿手改；维护校验会逐字段复算。 -->\n"
        "```json\n"
        f"{body}\n"
        "```\n"
        f"{END}"
    )


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_doc_claims(text: str) -> dict | None:
    if BEGIN not in text or END not in text:
        return None
    block = text.split(BEGIN, 1)[1].split(END, 1)[0]
    m = _FENCE_RE.search(block)
    raw = m.group(1) if m else block.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 容错：截取首个 { 到末个 }。
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            return json.loads(raw[i:j + 1])
        raise


def update_documentation(root: Path | None = None) -> Path:
    root = root or find_repo_root()
    if root is None:
        raise FileNotFoundError("找不到含 docs/ 的仓库根，无法更新文档声明块")
    doc = root / "docs" / "tile-rules.md"
    claims = compute_claims()
    block = render_claims_block(claims)
    if doc.exists():
        text = doc.read_text(encoding="utf-8")
        if BEGIN in text and END in text:
            pre = text.split(BEGIN, 1)[0]
            post = text.split(END, 1)[1]
            text = pre + block + post
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    else:
        doc.parent.mkdir(parents=True, exist_ok=True)
        text = _new_doc_skeleton(block)
    doc.write_text(text, encoding="utf-8")
    return doc


def _new_doc_skeleton(block: str) -> str:
    return f"""# 瓦片规则：分箱、瓦片边界与片元归属

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

{block}

## 6. 图形视窗核对

`python3 -m tile_raster gui` 启动调试视窗后，调整“瓦片尺寸 / 候选桶容量 /
溢出策略 / 工作组顺序”，点击“渲染瓦片管线”。页面给出可见运行状态，并把
后端 `rule_evidence.anomalies` 标成“覆盖图异常 / 计算负载异常 / 归属或
溢出异常”角标；CLI 只是无界面辅助，不能替代视窗里的交互核对。详见
[`docs/gui.md`](gui.md)。
"""


def check_documentation(root: Path | None = None):
    """返回 Issue 列表；空失败项即文档与实现一致。"""
    from .tile_rules import Issue

    root = root or find_repo_root()
    if root is None or not (root / "docs").is_dir():
        return [Issue("docs-locate", "docs", True,
                      "未找到 docs/ 仓库目录，跳过文档维护核对",
                      severity="warn")]

    issues: list[Issue] = []

    def add(cid, ok, msg, **kw):
        issues.append(Issue(cid, "docs", bool(ok), msg, **kw))

    doc_path = root / "docs" / "tile-rules.md"
    if not doc_path.is_file():
        add("tile-rules-doc-exists", False,
            "缺少 docs/tile-rules.md（可运行 "
            "`python -m tile_raster maintenance --update` 生成）")
        return issues
    text = doc_path.read_text(encoding="utf-8")

    missing_terms = [t for t in REQUIRED_TERMS if t not in text]
    add("tile-rules-terms", not missing_terms,
        "瓦片规则文档包含分箱/瓦片边界/片元归属/溢出策略/净目录约定"
        if not missing_terms else f"文档缺少必备主题词 {missing_terms}",
        actual=missing_terms)

    try:
        doc_claims = extract_doc_claims(text)
    except Exception as e:  # JSON 损坏等
        doc_claims = None
        add("claims-parse", False, f"文档声明块无法解析: {type(e).__name__}: {e}")
    if doc_claims is not None:
        canonical = compute_claims()
        same = doc_claims == canonical
        add("claims-match-code", same,
            "文档中的小网格覆盖集合/溢出状态/链范围与代码复算完全一致"
            if same else
            "文档声明已过期：运行 `python -m tile_raster maintenance --update` 刷新",
            expected="canonical claims",
            actual="doc claims differ" if not same else None)

    readme = root / "README.md"
    if readme.is_file():
        rtext = readme.read_text(encoding="utf-8")
        miss = [t for t in README_REQUIRED if t not in rtext]
        add("readme-entry", not miss,
            "README 保留 GUI 启动入口、tile-rules 与文档链接"
            if not miss else f"README 缺少 {miss}", actual=miss)

    gui_doc = root / "docs" / "gui.md"
    if gui_doc.is_file():
        gtext = gui_doc.read_text(encoding="utf-8")
        miss = [t for t in GUI_DOC_REQUIRED if t not in gtext]
        add("gui-doc-status", not miss,
            "docs/gui.md 说明了运行状态与覆盖/负载异常核对"
            if not miss else f"gui.md 缺少 {miss}", actual=miss)
    else:
        add("gui-doc-status", False, "缺少 docs/gui.md")

    scripts = [
        root / "scripts" / "small_grid_reference.py",
        root / "scripts" / "check_tile_rules.py",
    ]
    missing_scripts = [str(p.relative_to(root)) for p in scripts if not p.is_file()]
    add("reference-scripts", not missing_scripts,
        "小网格参考绘制脚本与瓦片规则校验脚本存在"
        if not missing_scripts else f"缺少脚本 {missing_scripts}",
        actual=missing_scripts)
    return issues


def run_maintenance_suite(*, update: bool = False):
    """维护校验入口，返回 (ok, report, lines)。"""
    lines: list[str] = []
    root = find_repo_root()
    updated = None
    if update:
        updated = update_documentation(root)
        lines.append(f"已刷新文档声明块 -> {updated}")
    issues = check_documentation(root)
    for i in issues:
        lines.append(f"[{'PASS' if i.ok else 'FAIL'}] {i.check_id}: {i.msg}")

    sample_report = None
    try:
        from .samples import verify_legacy_samples
        sample_ok, sample_report, sample_lines = verify_legacy_samples()
        lines.extend(sample_lines)
    except Exception as e:  # 旧样例校验自身异常也算失败，不许伪报
        sample_ok = False
        lines.append(f"[FAIL] legacy-samples: {type(e).__name__}: {e}")

    ok = all(i.ok for i in issues) and sample_ok
    report = {
        "ok": ok,
        "repo_root": str(root) if root else None,
        "updated": str(updated) if updated else None,
        "issues": [i.as_dict() for i in issues],
        "legacy_samples": sample_report,
    }
    lines.append("维护校验总体: " + ("MAINTAINED" if ok else "NEEDS MAINTENANCE"))
    return ok, report, lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m tile_raster.maintenance",
        description="文档/样例维护校验：文档声明必须与代码复算一致")
    p.add_argument("--update", action="store_true",
                   help="用当前代码复算结果刷新 docs/tile-rules.md 声明块")
    p.add_argument("--json", default=None)
    args = p.parse_args(argv)
    ok, report, lines = run_maintenance_suite(update=args.update)
    print("\n".join(lines))
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
