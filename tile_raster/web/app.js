"use strict";

const $ = (id) => document.getElementById(id);
let lastParams = {};

const SCENE_INFO = {
  paper: { w: 96, h: 72, tile: 24, cap: 6, desc: "半透明彩纸：A=(1,1)、B=(2,1) 在不同提交序号溢出，paper#777 横跨两块。" },
  dense: { w: 160, h: 120, tile: 16, cap: 128, desc: "220 个密集小三角集中在中心小区域，带顶点色与随机深度。" },
  giant: { w: 128, h: 96, tile: 16, cap: 64, desc: "覆盖全屏 / 横跨所有列的超大三角形，跨箱数=网格瓦数。" },
  empty: { w: 128, h: 96, tile: 32, cap: 16, desc: "空场景：纯背景，所有片元与负载计数应为 0。" },
  tiny: { w: 64, h: 48, tile: 16, cap: 8, desc: "极小与亚像素三角形：必须被保守分箱覆盖。" },
  "depth-order": { w: 64, h: 48, tile: 16, cap: 8, desc: "深度写入 + 半透明重叠，检验提交次序保证。" },
  midway: { w: 96, h: 48, tile: 24, cap: 4, desc: "大桥三角跨 3 瓦，在跨度第 2 瓦 (1,0) 中途顶爆桶；请用 tile24/cap4 观察 midway 事件。" },
};

function showError(msg, trace) {
  $("error-text").textContent = msg;
  const pre = $("error-trace");
  if (trace) { pre.textContent = trace; pre.classList.remove("hidden"); }
  else { pre.textContent = ""; pre.classList.add("hidden"); }
  $("error-banner").classList.remove("hidden");
}
function clearError() { $("error-banner").classList.add("hidden"); }
$("error-close").onclick = clearError;

async function api(path, body) {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, opt);
  let data = null;
  try { data = await resp.json(); } catch (e) { throw new Error("服务返回了无法解析的响应: " + e); }
  if (!resp.ok || data.ok === false) {
    const err = new Error(data.error || ("HTTP " + resp.status));
    err.payload = data;
    throw err;
  }
  return data;
}

// ---- 控件联动 ----
$("tile").addEventListener("input", (e) => $("tile-val").textContent = e.target.value + "px");
$("bucket").addEventListener("input", (e) => $("cap-val").textContent = e.target.value);
$("extra").addEventListener("input", (e) => $("extra-val").textContent = e.target.value);
$("scene").addEventListener("change", () => {
  const info = SCENE_INFO[$("scene").value];
  if (!info) return;
  $("width").value = info.w; $("height").value = info.h;
  $("tile").value = info.tile; $("tile-val").textContent = info.tile + "px";
  $("bucket").value = info.cap; $("cap-val").textContent = info.cap;
  $("scene-desc").textContent = info.desc;
});
$("scene").dispatchEvent(new Event("change"));

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("tab-" + t.dataset.tab).classList.add("active");
  });
});

function gatherParams() {
  const tile = parseInt($("tile").value, 10);
  return {
    scene: $("scene").value,
    width: parseInt($("width").value, 10),
    height: parseInt($("height").value, 10),
    tile_w: tile, tile_h: tile,
    bucket_capacity: parseInt($("bucket").value, 10),
    extra_buffer_capacity: parseInt($("extra").value, 10),
    strategy: $("strategy").value,
    work_order: $("order").value,
    seed: parseInt($("seed").value, 10) || 0,
    fault_no_base: $("fault").checked,
  };
}

function fmt(n) { return (n ?? 0).toLocaleString("zh-CN"); }

function renderKpis(m) {
  const t = m.timings_ms, tot = m.total, b = m.binning;
  const k = [
    { l: "分箱耗时", v: t.bin.toFixed(2), s: "ms", c: "acc" },
    { l: "瓦片处理", v: t.process.toFixed(2), s: "ms", c: "" },
    { l: "写回+量化", v: (t.writeback + t.quantize).toFixed(2), s: "ms", c: "" },
    { l: "像素测试", v: fmt(tot.pixel_tests), s: "edge×3", c: "" },
    { l: "覆盖片元", v: fmt(tot.covered), s: "真实工作量", c: "good" },
    { l: "混合片元", v: fmt(tot.blend_fragments), s: "dst读 " + fmt(tot.blend_dst_reads), c: "" },
    { l: "深度拒绝", v: fmt(tot.depth_rejected), s: "", c: "" },
    { l: "溢出瓦片/事件", v: b.overflow_event_count, s: "续绘瓦 " + b.continuation_tiles, c: "warn" },
    { l: "最大跨箱", v: b.max_primitive_span, s: "瓦片", c: "warn" },
    { l: "最长桶", v: b.max_tile_list_length, s: "@" + (b.max_tile_list_coord || "-"), c: "warn" },
  ];
  $("kpis").innerHTML = k.map((x) => `
    <div class="kpi ${x.c}">
      <div class="k-label">${x.l}</div>
      <div class="k-value">${x.v}<span style="font-size:11px;color:var(--muted)"> ${x.s}</span></div>
    </div>`).join("");
}

function renderTileTable(m) {
  const rows = [...m.tiles].sort((a, b) => a.tile_index - b.tile_index);
  const maxLoad = Math.max(1, ...rows.map((r) => r.load_fragments));
  $("tile-table").querySelector("tbody").innerHTML = rows.map((r) => {
    const pct = Math.round(100 * r.load_fragments / maxLoad);
    return `<tr>
      <td>#${r.tile_index}</td>
      <td>(${r.tx},${r.ty})</td>
      <td><span class="badge ${r.status}">${r.status}</span></td>
      <td>${r.assigned_ids}</td>
      <td>${r.overflow_seq ?? "—"}</td>
      <td>${r.continuation_passes || "—"}</td>
      <td>${r.rebins || "—"}</td>
      <td>${r.base_rereads || "—"}</td>
      <td class="loadcell"><div class="loadbar" style="transform:scaleX(${pct / 100})"></div><span>${fmt(r.load_fragments)}</span></td>
      <td>${fmt(r.pixel_tests)}</td>
      <td>${fmt(r.blend_dst_reads)}</td>
      <td>${r.depth_rejected}</td>
      <td>${r.exec_ms.toFixed(2)}</td>
    </tr>`;
  }).join("");
}

function renderDiag(payload) {
  const d = payload.diagnosis;
  $("diag-list").innerHTML = d.lines.map((l) =>
    `<li class="${l.level}">${l.msg}</li>`).join("");
  const evs = payload.metrics.binning.overflow_events || [];
  if (!evs.length) {
    $("bin-events").innerHTML = `<p class="placeholder">当前参数下没有候选桶溢出事件（可减小桶容量触发）。</p>`;
    return;
  }
  const shown = evs.slice(0, 80);
  $("bin-events").innerHTML = `
    <table><thead><tr><th>seq</th><th>paper</th><th>label</th><th>tile</th>
      <th>到达数</th><th>跨瓦数</th><th>跨度中?</th><th>动作</th><th>容量变化</th></tr></thead>
    <tbody>${shown.map((e) => `<tr>
      <td>${e.seq}</td><td>${e.paper_id}</td><td style="text-align:left">${e.label}</td>
      <td>(${e.tx},${e.ty})</td><td>${e.arriving_count}</td>
      <td>${e.primitive_span_tiles}</td>
      <td>${e.midway_in_primitive ? "是" : ""}</td>
      <td>${e.action}</td>
      <td>${e.old_capacity}→${e.new_capacity || "封存"}</td>
    </tr>`).join("")}</tbody></table>
    ${evs.length > 80 ? `<p class="hint">仅显示前 80 条，共 ${evs.length} 条。</p>` : ""}`;
}

async function doRender() {
  clearError();
  const btn = $("btn-render");
  btn.disabled = true; btn.textContent = "渲染中…";
  try {
    const p = gatherParams();
    lastParams = p;
    const data = await api("/api/render", p);
    $("img-final").src = data.image_png;
    $("img-coverage").src = data.coverage_png;
    $("img-load").src = data.load_png;
    renderKpis(data.metrics);
    renderTileTable(data.metrics);
    renderDiag(data);
  } catch (e) {
    showError(e.message, e.payload?.traceback);
  } finally {
    btn.disabled = false; btn.textContent = "▶ 渲染瓦片管线";
  }
}
$("btn-render").onclick = doRender;

// ---- 溢出实验室 ----
function yn(b) { return b ? `<span class="lab-ok">是</span>` : `<span class="lab-bad">否</span>`; }

function renderLab(lab) {
  $("lab-empty").classList.add("hidden");
  const box = $("lab-content");
  box.classList.remove("hidden");
  const ab = lab.A_B;
  const cp = lab.cache_pressure;
  const pressureCard = (name, p) => `
    <div class="lab-card">
      <h4>额外缓冲重读 · ${name}</h4>
      <p class="hint">完成顺序 ${JSON.stringify(p.completion_order)} · 容量 ${lab.extra_buffer_capacity}
         · issue 后幸存 ${JSON.stringify(p.survivors_after_issue)} · LRU 淘汰 ${p.evictions}</p>
      <table><tbody>
        <tr><td>缓存命中/未命中</td><td>${p.hits} / ${p.misses}</td></tr>
        <tr><td>base 重读总数</td><td><b>${p.total_base_rereads}</b></td></tr>
        ${Object.entries(p.base_rereads_by_tile).map(([k, v]) =>
          `<tr><td>瓦片 ${k}</td><td>${v ? "重读 1 次" : `<span class='lab-ok'>缓存命中</span>`}</td></tr>`).join("")}
      </tbody></table>
    </div>`;

  box.innerHTML = `
    <div class="lab-grid">
      <div class="lab-card">
        <h4>相邻瓦片溢出序号（桶容量 ${lab.bucket_capacity}）</h4>
        <table><tbody>
          <tr><td>A ${JSON.stringify(ab.A.tile)}</td>
              <td>溢出 seq=<b class="lab-bad">${ab.A.overflow_seq}</b>
                  (提交#${ab.A.overflow_submission}) · 共 ${ab.A.final_id_count} id ·
                  bridge 纸 seq ${JSON.stringify(ab.A.bridge_seqs_in_tile)}</td></tr>
          <tr><td>B ${JSON.stringify(ab.B.tile)}</td>
              <td>溢出 seq=<b class="lab-bad">${ab.B.overflow_seq}</b>
                  (提交#${ab.B.overflow_submission}) · 共 ${ab.B.final_id_count} id ·
                  bridge 纸 seq ${JSON.stringify(ab.B.bridge_seqs_in_tile)}</td></tr>
        </tbody></table>
        <p class="hint">两个序号不同，且 paper#${lab.bridge_paper_id} 同时出现在 A/B 两桶。</p>
      </div>
      <div class="lab-card">
        <h4>跨瓦纸边探针 · 串行混色 vs 分块重读</h4>
        <table><tbody>
          <tr><td>边缘探针像素</td><td>${lab.edge_probe_count}</td></tr>
          <tr><td>串行路径 dst 混色读</td><td><b>${lab.serial.blend_dst_reads_on_probes}</b></td></tr>
          <tr><td>瓦片 serial 混色读</td><td>${lab.tiled.serial.blend_dst_reads_on_probes}</td></tr>
          <tr><td>瓦片 shuffle 混色读</td><td>${lab.tiled.shuffle.blend_dst_reads_on_probes}</td></tr>
          <tr><td>探针 seq 次序一致</td><td>${yn(lab.probe_seq_order_matches)}</td></tr>
          <tr><td>瓦片像素 == 串行参考</td><td>${yn(lab.safe_pixel_exact_vs_serial)}</td></tr>
          <tr><td>打乱顺序像素一致</td><td>${yn(lab.shuffled_pixel_exact)}</td></tr>
          <tr><td>底图重算后像素一致</td><td>${yn(lab.recovery_pixel_exact)} (${lab.recovery_checked_tiles} 瓦)</td></tr>
        </tbody></table>
      </div>
      ${pressureCard("serial 完成序", cp.serial)}
      ${pressureCard("shuffle 完成序", cp.shuffle)}
    </div>
    <div class="lab-card">
      <h4>乱序提前混色 → 检测 → 可信底图重算（每瓦事件）</h4>
      ${lab.speculative_episodes.map((e) => `
        <div class="ep">
          瓦片 ${JSON.stringify(e.key)}：提前混色 seq ${JSON.stringify(e.early_seqs)}，
          最大已混 seq=${e.max_blended_seq}，次序倒置点 seq=${e.inversion_next_seq}；
          浪费片元 <b>${e.wasted_fragments}</b>，整序重播片元 <b>${e.replay_fragments}</b>，
          重播混色 dst 读 ${e.blend_dst_reads}，base 重读 ${e.base_rereads}，
          恢复结果 ${e.recovered ? "<span class='lab-ok'>已从可信底图恢复且像素精确</span>" : "<span class='lab-bad'>未恢复</span>"}
          ${e.error ? `<div class="lab-bad">${e.error}</div>` : ""}
        </div>`).join("")}
    </div>`;
}

async function doLab() {
  clearError();
  const btn = $("btn-lab");
  btn.disabled = true; btn.textContent = "实验运行中…";
  try {
    const p = gatherParams();
    const data = await api("/api/lab", {
      bucket_capacity: p.bucket_capacity,
      extra_buffer_capacity: p.extra_buffer_capacity,
      seed: p.seed,
      fault_no_base: p.fault_no_base,
    });
    renderLab(data.lab);
    document.querySelector('.tab[data-tab="lab"]').click();
  } catch (e) {
    if (e.payload && e.payload.lab) renderLab(e.payload.lab);
    showError(e.message, e.payload?.traceback);
    document.querySelector('.tab[data-tab="lab"]').click();
  } finally {
    btn.disabled = false; btn.textContent = "⚗ 运行彩纸溢出/重读实验";
  }
}
$("btn-lab").onclick = doLab;

// ---- 自检 / 基准 ----
async function doCheck() {
  const out = $("check-out");
  out.textContent = "运行中（252 个像素一致性配置 + 容量/中途溢出证明）…";
  try {
    const r = await api("/api/check", {});
    out.textContent = r.lines.join("\n") +
      `\n\n一致性配置数：${r.consistency_checks}` +
      `\n失败用例：${r.failed_cases.length ? JSON.stringify(r.failed_cases, null, 2) : "无"}` +
      `\n容量证明：${JSON.stringify(r.growth, null, 2)}`;
  } catch (e) {
    out.textContent = "自检失败：" + e.message;
  }
}
$("btn-check").onclick = doCheck;
$("btn-check2").onclick = doCheck;

async function doBench() {
  const box = $("bench-out");
  box.innerHTML = `<p class="hint">基准运行中…</p>`;
  try {
    const r = await api("/api/bench", {});
    box.innerHTML = Object.entries(r.benchmarks).map(([name, b]) => `
      <div class="bench-card">
        <h4>${name} · ${b.width}×${b.height} tile${b.tile}</h4>
        <div class="row"><span>三角形</span><b>${b.triangles}</b></div>
        <div class="row"><span>分箱 ms</span><b>${b.bin_ms}</b></div>
        <div class="row"><span>处理 ms</span><b>${b.process_ms}</b></div>
        <div class="row"><span>写回 ms</span><b>${b.writeback_ms}</b></div>
        <div class="row"><span>量化 ms</span><b>${b.quantize_ms}</b></div>
        <div class="row"><span>覆盖片元</span><b>${fmt(b.covered_fragments)}</b></div>
        <div class="row"><span>像素测试</span><b>${fmt(b.pixel_tests)}</b></div>
        <div class="row"><span>最大跨箱</span><b>${b.max_primitive_span}</b></div>
      </div>`).join("");
  } catch (e) {
    box.innerHTML = `<p class="lab-bad">基准失败：${e.message}</p>`;
  }
}
$("btn-bench").onclick = doBench;

// ---- 健康检查 + 首次自动渲染 ----
api("/api/health").then(() => {
  $("health").textContent = "● 本地管线在线";
  $("health").classList.add("ok");
  doRender();
}).catch((e) => {
  $("health").textContent = "○ 服务未连接";
  showError("无法连接本地渲染服务：" + e.message);
});
