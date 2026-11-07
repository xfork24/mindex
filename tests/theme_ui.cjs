/* 主题三态切换：默认跟随系统、循环 auto→light→dark、持久化、跨页生效、多语言标签。 */
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    // 四路并行回归时页面与模型服务争用 CPU：默认等待按满载情况设置，显式 timeout 的断言不受影响。
    page.setDefaultTimeout(45000);
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const base = process.env.MINDEX_THEME_URL;
    await page.goto(base + "/");
    const toggle = page.locator("[data-theme-toggle]");
    await toggle.waitFor();
    const state = () =>
      page.evaluate(() => ({
        mode: document.querySelector("[data-theme-toggle]").dataset.mode,
        attr: document.documentElement.getAttribute("data-theme") || "",
        stored: localStorage.getItem("mindex.theme") || "",
      }));
    // 默认跟随系统（无显式主题属性，未写存储）。
    assert.deepEqual(await state(), { mode: "auto", attr: "", stored: "" });
    // 循环：auto → light → dark → auto；深色写入 data-theme，全部持久化。
    await toggle.click();
    assert.deepEqual(await state(), { mode: "light", attr: "", stored: "light" });
    await toggle.click();
    assert.deepEqual(await state(), { mode: "dark", attr: "dark", stored: "dark" });
    await toggle.click();
    assert.deepEqual(await state(), { mode: "auto", attr: "", stored: "auto" });
    // 显式深色在跨页导航与刷新后保持（auto → light → dark）。
    await toggle.click();
    await toggle.click();
    assert.equal((await state()).mode, "dark");
    await page.goto(base + "/settings");
    await page.locator("#agent-runtime").waitFor();
    assert.equal(
      await page.evaluate(() => document.documentElement.getAttribute("data-theme")),
      "dark",
    );
    await page.reload();
    await page.locator("#agent-runtime").waitFor();
    assert.equal(
      await page.evaluate(() => document.documentElement.getAttribute("data-theme")),
      "dark",
    );
    // 多语言标签随语言联动。
    await page.evaluate(() => MindexI18n.setLanguage("en"));
    assert.equal(
      await page.locator("[data-theme-toggle]").getAttribute("aria-label"),
      "Theme: dark",
    );
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    assert.equal(
      await page.locator("[data-theme-toggle]").getAttribute("aria-label"),
      "主题：深色",
    );
    assert.deepEqual(errors, []);
    console.log("Theme cycle, persistence, cross-page and i18n labels passed");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
