// Acceptance against an isolated serve_fixture.py library only.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_HEADER_URL || "http://127.0.0.1:18341";
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
const out = process.env.MINDEX_HEADER_OUTPUT || "artifacts/header-navigation";
const paths = ["/search", "/", "/admin", "/persons", "/settings"];
const labels = {
  zh: ["视频检索", "创作", "素材管理", "人物库", "设置"],
  en: ["Video search", "Studio", "Media library", "People", "Settings"],
};
fs.mkdirSync(out, { recursive: true });
let browser;
let page;
(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const records = [];
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // Click through every tab in both languages, including the current tab.
  for (const lang of ["zh", "en"]) {
    await page.goto(base);
    await page.evaluate((l) => window.MindexI18n.setLanguage(l), lang);
    for (const current of paths) {
      await page.locator(`.page-nav a[href="${current}"]`).click();
      await page.waitForURL(base + current);
      await page.locator(".page-nav [aria-current=page]").waitFor();
      assert.equal(await page.locator("html").getAttribute("lang"), lang === "zh" ? "zh-CN" : "en");
      // 自动回归只覆盖桌面/窄屏的可用导航；像素、配色和全页溢出留给手动视觉矩阵。
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        const tabs = await page.locator(".page-nav a").evaluateAll((nodes) =>
          nodes.map((node) => ({
            href: node.getAttribute("href"),
            text: node.textContent,
            selected: node.getAttribute("aria-current"),
            visible:
              node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().height > 0,
          })),
        );
        assert.deepEqual(
          tabs.map((t) => t.href),
          paths,
        );
        assert.deepEqual(
          tabs.map((t) => t.text),
          labels[lang],
        );
        assert.equal(tabs.filter((t) => t.selected).length, 1);
        assert.equal(tabs.find((t) => t.selected).href, current);
        assert.ok(
          tabs.every((t) => t.visible),
          `${current} ${lang} ${width}: 导航不可见`,
        );
        records.push({ current, lang, width, tabs });
      }
    }
  }
  // Refresh lists, refresh a selected source, delayed/error responses and retry.
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base + "/admin");
  await page.evaluate(() => window.MindexI18n.setLanguage("zh"));
  await page.locator(".source-item").first().waitFor();
  let response = page.waitForResponse(
    (r) => r.url().includes("/api/admin/sources?") && r.request().method() === "GET",
  );
  await page.locator("#reload").click();
  assert.equal((await response).status(), 200);
  await page.waitForFunction(() => !document.querySelector("#reload").disabled);
  await page.locator(".source-item").first().click();
  await page.locator("details.work-panel").waitFor();
  if (!(await page.locator("details.work-panel").evaluate((node) => node.open))) {
    await page.locator(".work-toolbar").click();
  }
  await page.locator(".work-card").last().waitFor();
  const source = await page.evaluate(() => state.source.id);
  // 素材详情请求带 ?detail=work / ?detail=summary 后缀，按路径前缀匹配。
  const isSource = (u) => new RegExp("/api/admin/sources/" + source + "(\\?|$)").test(u);
  response = page.waitForResponse((r) => isSource(r.url()));
  await page.locator("#reload").click();
  assert.equal((await response).status(), 200);
  await page.waitForFunction(() => !document.querySelector("#reload").disabled);
  assert.equal(await page.evaluate(() => state.source.id), source);
  let release;
  const failRoute = async (route) => {
    await new Promise((r) => (release = r));
    await route.fulfill({ status: 503, json: { detail: "工程刷新失败" } });
  };
  await page.route("**/api/admin/sources/" + source + "*", failRoute);
  await page.locator("#reload").click();
  await page.waitForFunction(() => document.querySelector("#reload").disabled);
  release();
  await page.locator("#admin-notice.error").waitFor();
  assert.match(await page.locator("#admin-notice").textContent(), /工程刷新失败/);
  await page.waitForFunction(() => !document.querySelector("#reload").disabled);
  await page.unroute("**/api/admin/sources/" + source + "*", failRoute);
  response = page.waitForResponse((r) => isSource(r.url()));
  await page.locator("#reload").click();
  assert.equal((await response).status(), 200);
  await page.waitForFunction(() => !document.querySelector("#reload").disabled);
  assert.equal(await page.evaluate(() => state.source.id), source);
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    `${out}/coverage.json`,
    JSON.stringify(
      {
        records,
        errors,
        refresh: ["list", "selected-source", "disabled-during-request", "error-recovery", "retry"],
      },
      null,
      2,
    ),
  );
  console.log(
    "PASS: navigation at desktop/mobile widths, language persistence, refresh success/error/retry.",
  );
  await browser.close();
})().catch(async (e) => {
  console.error(e);
  await page?.screenshot({ path: `${out}/failure.png` });
  await browser?.close();
  process.exit(1);
});
