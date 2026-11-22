"use strict";

const state = {
  data: null,
  plan: null, // last plan payload returned by /api/plan
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function toast(message, kind = "error") {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 6000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch (_) { payload = null; }
  if (!res.ok) {
    let detail = payload && payload.detail;
    if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
    throw new Error(detail || `请求失败 (${res.status})`);
  }
  return payload;
}

/* ------------------------------------------------------------ rendering */
function stateClass(slice) {
  if (slice.status === "running") return "running";
  if (slice.draft || slice.status === "created") return "created";
  if (slice.paused) return "paused";
  if (slice.orders.length) return "busy";
  return "idle";
}

function renderSlots() {
  const layout = state.data.layout;
  const occupied = new Map();
  for (const slice of layout.slices) {
    for (let s = slice.interval.start; s <= slice.interval.end; s++) occupied.set(s, slice);
  }
  const planNew = new Map();
  const planRemove = new Set();
  const batch = state.data.batch;
  if (batch && batch.state !== "abandoned") {
    for (const p of batch.plan.placements) {
      if (!layout.slices.some((s) => s.id === p.slice_id)) {
        for (let s = p.interval.start; s <= p.interval.end; s++) planNew.set(s, p);
      }
    }
    for (const r of batch.plan.removals) planRemove.add(r.slice_id);
  }

  const strip = $("#slot-strip");
  strip.innerHTML = "";
  for (let s = 0; s < layout.slot_count; s++) {
    const el = document.createElement("div");
    if (s >= layout.slot_count) continue;
    const slice = occupied.get(s);
    const pending = planNew.get(s);
    const willRemove = slice && planRemove.has(slice.id);
    if (slice && !(willRemove && pending)) {
      el.className = "slot " + stateClass(slice);
      if (willRemove) el.classList.add("planned-remove");
      const tag = willRemove ? '<span class="tag remove">待拆</span>' : "";
      el.innerHTML = `<span class="idx">槽位 ${s}</span>
        <span class="sid">${esc(slice.id)}</span>
        <span class="sspec">${esc(slice.spec)}</span>${tag}`;
    } else if (pending) {
      el.className = "slot created planned-new";
      el.innerHTML = `<span class="idx">槽位 ${s}</span>
        <span class="sid">${esc(pending.slice_id)}</span>
        <span class="sspec">${esc(pending.spec)} · 方案</span>
        <span class="tag new">新</span>`;
    } else {
      el.className = "slot free";
      el.innerHTML = `<span class="idx">槽位 ${s}</span><span class="sid">空闲</span>`;
    }
    strip.appendChild(el);
  }
  // Extra slots beyond current count are omitted; note when slot_count < 8.
  const note = $("#slot-note");
  if (layout.slot_count < 8) {
    note.hidden = false;
    note.textContent = `当前仅启用 0–${layout.slot_count - 1} 共 ${layout.slot_count} 个槽位。`;
  } else {
    note.hidden = true;
  }
}

function renderTable() {
  const tbody = $("#slice-table tbody");
  tbody.innerHTML = "";
  const batch = state.data.batch;
  const planRemove = new Set(batch && batch.state !== "abandoned" ? batch.plan.removals.map(r => r.slice_id) : []);
  for (const slice of state.data.layout.slices) {
    const tr = document.createElement("tr");
    const statusCls = stateClass(slice);
    const statusLabel = slice.status === "created" ? "本批已建" :
      slice.status === "busy" ? "已接单" :
      slice.status === "running" ? "运行中" : "空闲";
    const accept = slice.can_accept
      ? '<span class="badge yes">可接单</span>'
      : `<span class="badge no" title="${esc(slice.accept_block_reason || "")}">不接单</span>`;
    const paused = slice.paused ? ' <span class="badge warn">已暂停</span>' : "";
    const removeBtn = slice.removable
      ? `<button class="small ghost" data-remove="${esc(slice.id)}">移除</button>` : "";
    tr.innerHTML = `
      <td><b>${esc(slice.id)}</b>${planRemove.has(slice.id) ? ' <span class="badge warn">待拆</span>' : ""}</td>
      <td>${esc(slice.spec)}</td>
      <td>${esc(slice.interval.start === slice.interval.end
        ? "槽位 " + slice.interval.start
        : `槽位 ${slice.interval.start}–${slice.interval.end}`)}</td>
      <td><span class="badge ${statusCls}">${statusLabel}</span>${paused}</td>
      <td>${accept}</td>
      <td>${slice.order_count}</td>
      <td>${removeBtn}</td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        state.data = await api("/api/external/remove", {
          method: "POST", body: { slice_id: btn.dataset.remove },
        });
        renderAll();
      } catch (err) { toast(err.message); }
    });
  });
  $$("#order-slice option, #ext-remove-slice option").forEach((o) => {
    if (o.value) o.remove();
  });
  for (const slice of state.data.layout.slices) {
    const add = (sel) => {
      const opt = document.createElement("option");
      opt.value = slice.id;
      opt.textContent = `${slice.id} · ${slice.spec} · ${slice.interval.start}–${slice.interval.end}`;
      $(sel).appendChild(opt);
    };
    add("#order-slice");
    if (slice.status === "idle" || slice.status === "busy") add("#ext-remove-slice");
  }
}

function renderFault() {
  const banner = $("#fault-banner");
  const fault = state.data.layout.fault;
  if (!fault) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.textContent =
    `已注入驱动故障：${fault.mode}，跳过 ${fault.skip} 项，剩余 ${fault.remaining}/${fault.count} 次 · ${fault.message}`;
}

function pillFor(batch) {
  const el = $("#batch-pill");
  if (!batch) {
    el.className = "pill";
    el.textContent = "尚未规划";
    return;
  }
  const labels = {
    planned: "方案待确认", applying: "调整中", failed: "执行失败",
    delivered: "已交付", abandoned: "已放弃",
  };
  el.className = "pill state-" + batch.state;
  el.textContent = `${batch.id} · ${labels[batch.state] || batch.state}`;
}

function fmtInterval(iv) {
  return iv.start === iv.end ? `槽位 ${iv.start}` : `槽位 ${iv.start}–${iv.end}`;
}

function renderPlanResult() {
  const box = $("#plan-result");
  const batch = state.data.batch;
  if (!state.plan) { box.innerHTML = ""; return; }
  const plan = state.plan;
  if (!plan.ok) {
    box.innerHTML = `<div class="plan-box bad">
      <div class="plan-title bad">无解：暂时放不下两块新切片</div>
      <div>${esc(plan.error || "未知原因")}</div>
      ${(plan.detail || []).map((d) => `<div class="hint">${esc(d)}</div>`).join("")}
    </div>`;
    return;
  }
  const newItems = plan.placements.map((p) =>
    `<li><span class="tag-new">新建</span> ${esc(p.slice_id)} · ${esc(p.spec)}
      → ${esc(fmtInterval(p.interval))}（互不重叠，需求 ${p.index + 1}）</li>`).join("");
  const removeItems = plan.removals.length
    ? plan.removals.map((r) =>
      `<li><span class="tag-remove">拆除</span> ${esc(r.slice_id)} · ${esc(r.spec)}
        @ ${esc(fmtInterval(r.interval))}（空闲）</li>`).join("")
    : `<li><span class="tag-keep">无需拆除任何空闲切片</span></li>`;
  const alt = plan.alternatives > 0
    ? `<div class="hint">另有 ${plan.alternatives} 个拆除数量相同、占用槽位数相同的等价方案，已选最靠前者。</div>`
    : "";
  box.innerHTML = `<div class="plan-box ok">
    <div class="plan-title ok">方案可行 · 拆除 ${plan.removed_count} 块空闲切片（最少）</div>
    <ul class="plan-list">${newItems}${removeItems}</ul>
    ${alt}
    <div class="hint">运行中切片不移动；暂停与执行将在“确认方案”后进行。</div>
  </div>`;
}

function renderBatch() {
  const batch = state.data.batch;
  pillFor(batch);
  const summary = $("#batch-summary");
  const actions = $("#batch-actions");
  const events = $("#batch-events");
  actions.innerHTML = "";
  events.innerHTML = "";

  if (!batch) {
    summary.className = "batch-summary muted";
    summary.textContent = "还没有调整批次。先在上方生成方案。";
    return;
  }
  summary.className = "batch-summary";

  const plan = batch.plan;
  const actual = batch.actual;
  const rows = [];
  rows.push(["批次", `${batch.id} · ${batch.state}`]);
  rows.push(["新切片", batch.slice_ids.join("、")]);
  if (plan.ok) {
    rows.push(["计划位置", plan.placements.map((p) => `${p.slice_id}→${fmtInterval(p.interval)}`).join("；")]);
    rows.push(["拟拆除", plan.removals.length ? plan.removals.map((r) => r.slice_id).join("、") : "无"]);
  }
  if (batch.error) rows.push(["最近问题", batch.error]);
  if (actual) {
    const created = actual.created.filter((c) => c.state === "created").map((c) => c.slice_id);
    const missing = actual.created.filter((c) => c.state === "missing").map((c) => c.slice_id);
    const pendingRem = actual.remaining_removals.filter((r) => r.state === "still-present").map((r) => r.slice_id);
    if (created.length) rows.push(["实际已建", created.join("、")]);
    if (missing.length) rows.push(["尚未建成", missing.join("、")]);
    if (pendingRem.length) rows.push(["待拆仍存在", pendingRem.join("、")]);
    rows.push(["自由槽位", actual.free_slots.length ? actual.free_slots.join("、") : "无"]);
    rows.push(["与方案一致", actual.layout_matches_plan ? "是" : "否"]);
  }
  summary.innerHTML = rows.map(([k, v]) =>
    `<div class="kv"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("");

  const add = (label, cls, fn, disabled = false) => {
    const btn = document.createElement("button");
    btn.textContent = label;
    btn.className = cls;
    btn.disabled = disabled;
    btn.addEventListener("click", fn);
    actions.appendChild(btn);
    return btn;
  };

  if (batch.state === "planned") {
    const confirmBtn = add("确认方案", "primary", () => act("/api/batch/confirm", {}), !plan.ok);
    confirmBtn.title = "确认时若布局冲突或待拆切片已接单，将退回重算";
    const holdBtn = add("确认并暂停（暂缓驱动）", "", () => act("/api/batch/confirm?hold=true", {}), !plan.ok);
    holdBtn.title = "进入调整窗口：待拆切片暂停接单，其余空闲切片照常接单；可先测试接单再继续执行";
    add("放弃批次", "ghost", () => act("/api/batch/abandon", {}));
  } else if (batch.state === "applying") {
    add("继续调用驱动", "primary", () => act("/api/batch/apply", {}));
    add("放弃并回收本批切片", "danger", () => act("/api/batch/abandon", {}));
  } else if (batch.state === "failed") {
    add("重试（只补未完成的改动）", "primary", () => act("/api/batch/retry", {}));
    add("放弃并回收本批切片", "danger", () => act("/api/batch/abandon", {}));
  } else if (batch.state === "delivered") {
    add("规划下一批", "ghost", () => {
      state.plan = null;
      $("#plan-result").innerHTML = "";
      toast("请在上方填写规格并生成新方案。", "info");
    });
  } else if (batch.state === "abandoned") {
    add("规划下一批", "ghost", () => {
      state.plan = null;
      $("#plan-result").innerHTML = "";
    });
  }

  events.innerHTML = batch.events.slice().reverse().map((e) =>
    `<div class="event k-${esc(e.kind)}">
      <span class="t">${esc(e.at.slice(11, 19))}</span>
      <span><span class="k">${esc(e.kind)}</span>${esc(e.message)}</span>
    </div>`).join("");
}

function fillSpecSelects() {
  const specs = state.data.layout.specs;
  const names = specs.map((s) => s.name);
  $$(".spec-select").forEach((sel) => {
    const prev = sel.value;
    sel.innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (names.includes(prev)) sel.value = prev;
  });
  const ext = $("#ext-spec");
  const prevExt = ext.value;
  ext.innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (names.includes(prevExt)) ext.value = prevExt;
  const layout = state.data.layout;
  $("#slot-range").textContent = `0–${layout.slot_count - 1}`;
  $("#slot-count").value = String(layout.slot_count);
  $$("#ext-start, #ext-end").forEach((i) => { i.max = layout.slot_count - 1; });
}

/* --------------------------------------------------------------- import */
function defaultImportJson() {
  return JSON.stringify(state.data.default_layout, null, 2);
}

$("#import-example-btn").addEventListener("click", () => {
  $("#import-json").value = defaultImportJson();
  $("#import-error").textContent = "";
});

$("#import-btn").addEventListener("click", async () => {
  const errEl = $("#import-error");
  errEl.textContent = "";
  let payload;
  try {
    payload = JSON.parse($("#import-json").value);
  } catch (err) {
    errEl.textContent = "JSON 解析失败：" + err.message;
    return;
  }
  try {
    state.data = await api("/api/layout/import", { method: "POST", body: payload });
    state.plan = null;
    $("#plan-result").innerHTML = "";
    renderAll();
    toast("布局已导入。", "ok");
  } catch (err) {
    errEl.textContent = err.message;
    toast("导入失败：" + err.message);
  }
});

function renderAll() {
  renderSlots();
  renderTable();
  renderFault();
  renderPlanResult();
  renderBatch();
  fillSpecSelects();
}

async function act(path, body) {
  try {
    state.data = await api(path, { method: "POST", body });
    if (path.startsWith("/api/plan")) {
      state.plan = state.data.batch ? state.data.batch.plan : null;
      if (state.plan && state.plan.ok) {
        toast(`方案已生成：拆除 ${state.plan.removed_count} 块空闲切片。确认后才会执行。`, "ok");
      } else if (state.plan) {
        toast(`无解：${state.plan.error}`, "error");
      }
    }
    renderAll();
    if (path.startsWith("/api/batch") && state.data.batch) {
      const b = state.data.batch;
      if (b.state === "delivered") toast("两块新切片已配齐并交付，现在可以接单。", "ok");
      else if (b.state === "failed") toast(b.error || "批次失败，请核对实际改动后重试。", "error");
      else if (b.state === "applying") {
        toast("已进入调整窗口：待拆切片暂停接单，其余空闲切片照常接单。", "info");
      } else if (b.state === "planned" && path.startsWith("/api/batch/confirm") && b.error) {
        toast("确认时发现冲突，已退回重算：" + b.error, "info");
      }
    }
  } catch (err) {
    toast(err.message);
  }
}

/* -------------------------------------------------------------- wiring */
$("#plan-btn").addEventListener("click", () => {
  const specs = $$(".spec-select").map((s) => s.value);
  const names = $$(".spec-name").map((s) => s.value.trim());
  if (!specs[0] || !specs[1]) { toast("请为两块新切片选择规格"); return; }
  act("/api/plan", {
    new_slices: [
      { spec: specs[0], name: names[0] || null },
      { spec: specs[1], name: names[1] || null },
    ],
  });
});

$("#reset-btn").addEventListener("click", async () => {
  try {
    state.data = await api("/api/layout/reset", { method: "POST" });
    state.plan = null;
    $("#plan-result").innerHTML = "";
    renderAll();
    toast("已恢复示例布局。", "ok");
  } catch (err) { toast(err.message); }
});

$("#slot-count").addEventListener("change", async () => {
  const count = Number($("#slot-count").value);
  const current = state.data.layout;
  const tooLarge = current.slices.find((s) => s.interval.end >= count);
  if (tooLarge) {
    toast(`切片 ${tooLarge.id} 占用槽位 ${tooLarge.interval.end}，超出新卡范围。`);
    $("#slot-count").value = current.slot_count;
    return;
  }
  const importPayload = {
    slot_count: count,
    specs: current.specs.map((s) => ({
      name: s.name,
      allowed_intervals: s.allowed_intervals.filter((iv) => iv.end < count),
    })).filter((s) => s.allowed_intervals.length),
    slices: current.slices.map((s) => ({
      id: s.id, spec: s.spec,
      interval: s.interval,
      kind: s.status === "running" ? "running" : "idle",
    })),
  };
  try {
    state.data = await api("/api/layout/import", { method: "POST", body: importPayload });
    state.plan = null;
    $("#plan-result").innerHTML = "";
    renderAll();
  } catch (err) {
    toast(err.message);
    $("#slot-count").value = current.slot_count;
  }
});

$("#order-btn").addEventListener("click", async () => {
  const sliceId = $("#order-slice").value || null;
  try {
    state.data = await api("/api/order", {
      method: "POST",
      body: { slice_id: sliceId, force: $("#order-force").checked },
    });
    renderAll();
    toast("订单已派发。", "ok");
  } catch (err) { toast(err.message); }
});

$("#ext-add-btn").addEventListener("click", async () => {
  try {
    state.data = await api("/api/external/add-idle", {
      method: "POST",
      body: {
        spec: $("#ext-spec").value,
        interval: { start: Number($("#ext-start").value), end: Number($("#ext-end").value) },
      },
    });
    renderAll();
    toast("已新增空闲切片。", "ok");
  } catch (err) { toast(err.message); }
});

$("#ext-remove-btn").addEventListener("click", async () => {
  const sliceId = $("#ext-remove-slice").value;
  if (!sliceId) { toast("没有可移除的空闲切片"); return; }
  try {
    state.data = await api("/api/external/remove", { method: "POST", body: { slice_id: sliceId } });
    renderAll();
    toast("已移除空闲切片。", "ok");
  } catch (err) { toast(err.message); }
});

$("#fault-btn").addEventListener("click", async () => {
  try {
    state.data = await api("/api/fault", {
      method: "POST",
      body: {
        mode: $("#fault-mode").value,
        skip: Number($("#fault-skip").value),
        count: Number($("#fault-count").value),
        message: "模拟驱动错误：设备忙，请重试",
      },
    });
    renderAll();
    toast("已注入驱动故障。", "info");
  } catch (err) { toast(err.message); }
});

$("#fault-clear-btn").addEventListener("click", async () => {
  state.data = await api("/api/fault/clear", { method: "POST" });
  renderAll();
  toast("故障已清除。", "ok");
});

$("#selftest-btn").addEventListener("click", async () => {
  const btn = $("#selftest-btn");
  btn.disabled = true;
  try {
    const result = await api("/api/selftest", { method: "POST" });
    renderSelftest(result);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
});

function renderSelftest(result) {
  const box = $("#selftest-result");
  const s = result.summary;
  let html = `<div class="selftest-summary ${result.passed ? "pass" : "fail"}">
    ${result.passed ? "全部通过" : "存在失败项"}：${s.passed}/${s.total} 个场景通过</div>`;
  for (const sc of result.scenarios) {
    const steps = sc.steps.map((st) =>
      `<div class="step ${st.passed ? "pass" : "fail"}">
        <span class="mark">${st.passed ? "✓" : "✗"}</span>
        <span>${esc(st.name)}${st.detail ? `<div class="detail">${esc(st.detail)}</div>` : ""}</span>
      </div>`).join("");
    html += `<div class="scenario ${sc.passed ? "pass" : "fail"}">
      <div class="scenario-head"><b>${esc(sc.title)}</b>
        <span class="badge ${sc.passed ? "yes" : "warn"}">${sc.passed ? "通过" : "失败"}</span></div>
      <div class="intent">${esc(sc.intent)}</div>
      ${sc.error ? `<div class="step fail"><span class="mark">✗</span><span>${esc(sc.error)}</span></div>` : ""}
      ${steps}
    </div>`;
  }
  box.innerHTML = html;
}

async function boot() {
  try {
    state.data = await api("/api/state");
    renderAll();
    $("#import-json").value = defaultImportJson();
  } catch (err) {
    toast("无法加载状态：" + err.message);
  }
}

boot();
