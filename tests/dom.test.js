// DOM-level test of the console using jsdom.  Runs the real app.js against a
// mocked fetch backed by the live FastAPI server's JSON, exercising rendering
// and click handlers without a browser.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("/tmp/node_modules/jsdom");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8000";
const APP = path.resolve(__dirname, "../app");

function assert(cond, message) {
  if (!cond) throw new Error("断言失败：" + message);
  console.log("  ✓ " + message);
}

async function main() {
  const html = fs.readFileSync(path.join(APP, "templates/index.html"), "utf8");
  const dom = new JSDOM(html, { url: BASE + "/", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;

  const calls = [];
  window.fetch = async (url, opts = {}) => {
    calls.push({ url, opts });
    const res = await fetch(new URL(url, BASE), {
      method: opts.method || "GET",
      headers: opts.headers,
      body: opts.body,
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, json: async () => JSON.parse(text), text: async () => text };
  };
  window.setTimeout = setTimeout;
  window.clearTimeout = clearTimeout;
  window.console = console;

  const appJs = fs.readFileSync(path.join(APP, "static/app.js"), "utf8");
  // app.js uses browser-only fetch/URL; give it the globals it expects.
  window.eval(
    "window.__testHooks = {};\n" +
    appJs.replace("boot();", "window.__testHooks.boot = boot; window.__testHooks.act = act;")
  );
  await window.__testHooks.boot();

  await new Promise((r) => setTimeout(r, 300));
  const doc = window.document;
  const q = (s) => doc.querySelector(s);
  const qa = (s) => Array.from(doc.querySelectorAll(s));

  // Start from the sample layout so repeated runs are independent.
  await window.__testHooks.act("/api/layout/reset", {});
  await new Promise((r) => setTimeout(r, 200));

  console.log("场景 1：初始渲染");
  assert(qa(".slot").length === 8, "渲染 8 个槽位");
  assert(qa("#slice-table tbody tr").length >= 4, "切片表有数据");
  assert(q("#import-json").value.includes("slot_count"), "导入框预填示例");

  console.log("场景 2：规划并渲染方案");
  qa(".spec-select").forEach((sel, i) => {
    sel.value = i === 0 ? "2g.20gb" : "2g.20gb";
    sel.dispatchEvent(new window.Event("change"));
  });
  q("#plan-btn").click();
  await new Promise((r) => setTimeout(r, 400));
  assert(q(".plan-box.ok"), "显示可行方案");
  assert(qa(".slot.planned-new").length >= 2, "方案槽位预览高亮");
  assert(q(".plan-box").textContent.includes("最少"), "标注最少拆除");

  console.log("场景 3：无解渲染");
  qa(".spec-select").forEach((sel, i) => { sel.value = i === 0 ? "7g.80gb" : "1g.10gb"; });
  q("#plan-btn").click();
  await new Promise((r) => setTimeout(r, 400));
  assert(q(".plan-box.bad"), "显示无解");
  assert(q(".plan-box").textContent.includes("运行中切片"), "给出原因");
  const confirmBtn = qa("#batch-actions button").find((b) => b.textContent.includes("确认方案"));
  assert(confirmBtn && confirmBtn.disabled, "无解不可确认");

  console.log("场景 4：调整窗口 + 接单");
  await window.__testHooks.act("/api/layout/reset", {});
  qa(".spec-select").forEach((sel) => { sel.value = "1g.10gb"; });
  q("#plan-btn").click();
  await new Promise((r) => setTimeout(r, 400));
  const holdBtn = qa("#batch-actions button").find((b) => b.textContent.includes("确认并暂停"));
  assert(holdBtn, "存在确认并暂停按钮");
  holdBtn.click();
  await new Promise((r) => setTimeout(r, 400));
  assert(q("#batch-pill").textContent.includes("调整中"), "进入调整窗口");
  const pausedCount = qa("#slice-table tbody tr").filter((tr) => tr.textContent.includes("已暂停")).length;
  assert(pausedCount === 1, "只有一块切片被暂停");
  const orderSelect = q("#order-slice");
  const idleId = Array.from(orderSelect.options).map((o) => o.value)
    .find((v) => v && v.startsWith("idle") && v !== "idle-1");
  if (idleId) {
    orderSelect.value = idleId;
    q("#order-btn").click();
    await new Promise((r) => setTimeout(r, 350));
    assert(q("#toast").textContent.includes("订单已派发"), "非待拆空闲切片可接单");
  }

  console.log("场景 5：自测面板渲染");
  q("#selftest-btn").click();
  await new Promise((r) => setTimeout(r, 2500));
  assert(q(".selftest-summary.pass"), "自测全部通过");
  assert(qa(".scenario").length === 8, "渲染 8 个自测场景");

  console.log("场景 6：导入校验错误展示");
  // Abandon the held batch first, otherwise layout import is (rightly) blocked.
  const abandonBtn = qa("#batch-actions button").find((b) => b.textContent.includes("放弃"));
  if (abandonBtn) {
    abandonBtn.click();
    await new Promise((r) => setTimeout(r, 300));
  }
  const json = q("#import-json").value.replace('"slot_count": 8', '"slot_count": 9');
  q("#import-json").value = json;
  q("#import-btn").click();
  await new Promise((r) => setTimeout(r, 400));
  assert(q("#import-error").textContent.length > 0, "展示导入错误");

  dom.window.close();
  console.log("\njsdom DOM 测试全部通过。");
}

main().catch((e) => { console.error("\n测试失败：", e.message); process.exit(1); });
