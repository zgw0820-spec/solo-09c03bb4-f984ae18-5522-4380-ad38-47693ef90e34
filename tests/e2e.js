// Browser end-to-end test of the console.  Requires a running server on
// $BASE_URL (default http://127.0.0.1:8000) and Playwright's Chromium.
const { chromium } = require("playwright");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8000";
// Point Playwright at a system/standalone Chrome if one is provided.
const EXECUTABLE = process.env.CHROME_PATH || undefined;

function assert(cond, message) {
  if (!cond) throw new Error("断言失败：" + message);
  console.log("  ✓ " + message);
}

async function reset(page) {
  await page.click("#reset-btn");
  await page.waitForTimeout(350);
}

async function pickSpecs(page, a, b) {
  await page.selectOption(".spec-select[data-index='0']", a);
  await page.selectOption(".spec-select[data-index='1']", b);
}

async function main() {
  const browser = await chromium.launch({
    executablePath: EXECUTABLE,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1800 } });
  page.on("dialog", async (dialog) => { await dialog.accept("雪地里的狗（模拟描述）"); });
  const errors = [];
  page.on("pageerror", (e) => errors.push("pageerror: " + String(e)));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    // 409/422 responses are intentional in this suite (refused order, invalid
    // import); only JS-level errors matter here.
    if (text.startsWith("Failed to load resource:")) return;
    errors.push(text);
  });

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.click("#reset-btn");
  await page.waitForTimeout(400);
  console.log("场景 1：页面加载与布局渲染");
  assert((await page.locator(".slot").count()) === 8, "渲染 8 个槽位");
  assert((await page.locator("#slice-table tbody tr").count()) === 4, "示例布局有 4 块切片");

  console.log("场景 2：规划 → 确认 → 交付");
  await reset(page);
  await pickSpecs(page, "2g.20gb", "2g.20gb");
  await page.click("#plan-btn");
  await page.waitForSelector(".plan-box.ok", { timeout: 5000 });
  const planText = await page.locator(".plan-box").innerText();
  assert(planText.includes("方案可行"), "显示可行方案");
  assert(planText.includes("拆除"), "显示拆除数量");
  assert((await page.locator(".slot.planned-new").count()) >= 2, "方案槽位高亮");
  await page.getByRole("button", { name: "确认方案" }).click();
  await page.waitForFunction(
    () => document.querySelector("#batch-pill").textContent.includes("已交付"),
    null, { timeout: 5000 },
  );
  const summary = await page.locator("#batch-summary").innerText();
  assert(summary.includes("与方案一致") && summary.includes("是"), "核对实际改动一致");

  console.log("场景 3：无解方案给出原因");
  await reset(page);
  await pickSpecs(page, "7g.80gb", "1g.10gb");
  await page.click("#plan-btn");
  await page.waitForSelector(".plan-box.bad", { timeout: 5000 });
  const bad = await page.locator(".plan-box").innerText();
  assert(bad.includes("无解") && bad.includes("运行中切片"), "说明无解原因");
  const confirmDisabled = await page
    .getByRole("button", { name: "确认方案" }).isDisabled();
  assert(confirmDisabled, "无解方案不可确认");

  console.log("场景 4：调整窗口内接单");
  await reset(page);
  await pickSpecs(page, "2g.20gb", "2g.20gb");
  await page.click("#plan-btn");
  await page.waitForSelector(".plan-box.ok");
  // Shrink footprint: choose 1g+1g so only idle-1 is removed and idle-2 stays.
  await pickSpecs(page, "1g.10gb", "1g.10gb");
  await page.click("#plan-btn");
  await page.waitForSelector(".plan-box.ok");
  await page.getByRole("button", { name: "确认并暂停（暂缓驱动）" }).click();
  await page.waitForFunction(
    () => document.querySelector("#batch-pill").textContent.includes("调整中"),
    null, { timeout: 5000 },
  );
  assert(true, "进入调整窗口");
  // Collect paused slice ids from the table (only planned removals are paused).
  const pausedIds = await page.locator("#slice-table tbody tr").evaluateAll((rows) =>
    rows.filter((r) => r.textContent.includes("已暂停"))
      .map((r) => r.querySelector("td b").textContent.trim()),
  );
  assert(pausedIds.length === 1, "只有一块切片被暂停：" + pausedIds.join(","));
  // Order to a slice that is not being removed must succeed.
  const idleOptions = await page.locator("#order-slice option").evaluateAll(
    (opts) => opts.map((o) => o.value).filter(Boolean),
  );
  const freeCandidate = idleOptions.find(
    (id) => !pausedIds.includes(id) && id.startsWith("idle"),
  );
  assert(Boolean(freeCandidate), "存在未被暂停的空闲切片");
  if (freeCandidate) {
    await page.selectOption("#order-slice", freeCandidate);
    await page.click("#order-btn");
    await page.waitForTimeout(350);
    const toast = await page.locator("#toast").innerText();
    assert(toast.includes("订单已派发"), "非待拆空闲切片可接单");
  }
  // Ordering the paused slice must be refused.
  await page.selectOption("#order-slice", pausedIds[0]);
  await page.click("#order-btn");
  await page.waitForTimeout(350);
  const rejectToast = await page.locator("#toast").innerText();
  assert(rejectToast.includes("暂停"), "待拆切片拒绝接单：" + rejectToast.slice(0, 60));
  await page.getByRole("button", { name: "继续调用驱动" }).click();
  await page.waitForFunction(
    () => document.querySelector("#batch-pill").textContent.includes("已交付"),
    null, { timeout: 5000 },
  );
  assert(true, "继续驱动后交付");

  console.log("场景 5：驱动在生效后报错 → 重试");
  await reset(page);
  await pickSpecs(page, "1g.10gb", "1g.10gb");
  await page.click("#plan-btn");
  await page.waitForSelector(".plan-box.ok");
  await page.fill("#fault-skip", "0");
  await page.fill("#fault-count", "1");
  await page.selectOption("#fault-mode", "before_apply");
  await page.click("#fault-btn");
  await page.waitForTimeout(250);
  await page.getByRole("button", { name: "确认方案" }).click();
  await page.waitForFunction(
    () => document.querySelector("#batch-pill").textContent.includes("执行失败"),
    null, { timeout: 5000 },
  );
  assert(true, "批次进入执行失败");
  await page.getByRole("button", { name: /重试/ }).click();
  await page.waitForFunction(
    () => document.querySelector("#batch-pill").textContent.includes("已交付"),
    null, { timeout: 5000 },
  );
  assert(true, "重试后交付");

  console.log("场景 6：确认时冲突退回重算");
  await reset(page);
  await pickSpecs(page, "1g.10gb", "1g.10gb");
  await page.click("#plan-btn");
  await page.waitForSelector(".plan-box.ok");
  // External removal changes the layout between planning and confirming.
  const removable = await page.locator("#ext-remove-slice option").evaluateAll(
    (opts) => opts.map((o) => o.value).filter(Boolean),
  );
  await page.selectOption("#ext-remove-slice", removable[removable.length - 1]);
  await page.click("#ext-remove-btn");
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: "确认方案" }).click();
  await page.waitForTimeout(500);
  const pill = await page.locator("#batch-pill").innerText();
  assert(pill.includes("方案待确认"), "冲突后退回 planned");
  const events = await page.locator("#batch-events").innerText();
  assert(/conflict/i.test(events), "记录冲突事件");

  console.log("场景 7：网页自测");
  await page.click("#selftest-btn");
  await page.waitForSelector(".selftest-summary", { timeout: 20000 });
  const st = await page.locator(".selftest-summary").innerText();
  assert(st.includes("全部通过"), "自测全部通过：" + st.trim());
  const failedScenarios = await page.locator(".scenario.fail").count();
  assert(failedScenarios === 0, "没有失败场景");

  console.log("场景 8：图像描述作业与生成拥塞");
  await reset(page);
  // Occupy the only 1g slice with unrelated external work.  Jobs encode on the
  // 3g slice and generate on 1g, so generate is congested.
  await page.selectOption("#order-slice", "idle-2");
  await page.click("#order-btn");
  await page.waitForTimeout(250);
  for (const prompt of ["雪地里的狗", "海边的猫"]) {
    await page.fill("#job-prompt", prompt);
    await page.selectOption("#job-encode-spec", "3g.40gb");
    await page.selectOption("#job-generate-spec", "1g.10gb");
    await page.click("#job-submit-btn");
    await page.waitForTimeout(300);
  }
  const jobRows = await page.locator("#job-table tbody tr").count();
  assert(jobRows === 2, "两单作业均已提交");
  const firstJobText = await page.locator("#job-table tbody tr").first().innerText();
  assert(firstJobText.includes("编码中"), "第一单编码开工（开工才领切片）");
  assert(firstJobText.includes("idle-1"), "编码领取 3g 切片 idle-1");
  const secondJobText = await page.locator("#job-table tbody tr").nth(1).innerText();
  assert(secondJobText.includes("排队"), "第二单排队等待暂存位");
  // Complete the encode of the first job; 1g generate is busy, so the result
  // must be buffered off-card and the encode slice returned.
  await page.getByRole("button", { name: "编码完工" }).first().click();
  await page.waitForTimeout(400);
  const bufferText = await page.locator("#job-buffer").innerText();
  assert(bufferText.includes("job-001"), "生成全忙时编码结果暂存卡外：" + bufferText);
  const rowAfterEncode = await page.locator("#job-table tbody tr").first().innerText();
  assert(rowAfterEncode.includes("已编码待生成"), "已完成编码等待生成");
  assert(rowAfterEncode.includes("已归还"), "编码切片已归还");

  console.log("场景 9：生成切片腾出 → 恢复派单");
  // Release idle-2: generate takes the buffered result and the next job starts
  // encoding in parallel on the freed encode slice.
  await page.evaluate(async () => {
    await fetch("/api/order/release", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slice_id: "idle-2" }),
    });
  });
  await page.reload({ waitUntil: "networkidle" });
  const afterRelease = await page.evaluate(async () => {
    const res = await fetch("/api/state");
    return res.json();
  });
  const states = afterRelease.jobs.jobs.map((j) => j.state);
  assert(states[0] === "generating", "腾出后生成开工并取走暂存结果");
  assert(states[1] === "encoding", "下一单在空闲编码切片上并行开工");
  assert(afterRelease.jobs.buffer.job === null, "暂存位已清空");

  console.log("场景 10：重复完工回报只结算一次");
  const jobId = afterRelease.jobs.jobs[0].id;
  const dup1 = await page.evaluate(async (id) => {
    const res = await fetch(`/api/jobs/${id}/complete`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage: "encode" }),
    });
    return res.json();
  }, jobId);
  const dupJob = dup1.jobs.jobs.find((j) => j.id === jobId);
  assert(dupJob.duplicate_reports === 1, "重复回报被忽略并计数");
  assert(dupJob.state === "generating", "重复回报不改变状态");

  console.log("场景 11：最多 8 槽位校验");
  await page.locator("#import-card details").evaluate((el) => { el.open = true; });
  await page.click("#import-example-btn");
  const json = await page.inputValue("#import-json");
  const mutated = json.replace('"slot_count": 8', '"slot_count": 9');
  await page.fill("#import-json", mutated);
  await page.click("#import-btn");
  await page.waitForTimeout(400);
  const importErr = await page.locator("#import-error").innerText();
  assert(importErr.length > 0, "拒绝超过 8 个槽位：" + importErr.slice(0, 40));

  assert(errors.length === 0, "无浏览器控制台错误" + (errors.length ? "：" + errors.join(" | ") : ""));

  await page.screenshot({ path: "/tmp/console-final.png", fullPage: true });
  await browser.close();
  console.log("\n浏览器端到端测试全部通过。");
}

main().catch((err) => {
  console.error("\n测试失败：", err.message);
  process.exit(1);
});
