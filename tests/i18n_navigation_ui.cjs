const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_I18N_TEST_URL || "http://127.0.0.1:8027";
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await context.addInitScript(() => localStorage.setItem("mindex.language", "en"));
    for (const path of [
      "/",
      "/admin",
      "/admin/music",
      "/persons",
      "/settings",
      "/studio",
      "/studio/templates",
      "/studio/brands",
      "/studio/topics",
      "/assets/shared/login.html",
    ]) {
      let release;
      await page.route("**/assets/shared/messages.js*", async (route) => {
        await new Promise((resolve) => {
          release = resolve;
        });
        await route.continue();
      });
      await page.goto(base + path, { waitUntil: "commit" });
      await page.waitForFunction(
        () => document.body && document.documentElement.hasAttribute("data-i18n-pending"),
      );
      assert.equal(await page.locator("html").getAttribute("lang"), "en", path);
      assert.equal(
        await page.locator("body").evaluate((node) => getComputedStyle(node).visibility),
        "hidden",
        path,
      );
      assert.ok(release, path);
      release();
      await page.waitForFunction(() => !document.documentElement.hasAttribute("data-i18n-pending"));
      assert.equal(
        await page.locator("body").evaluate((node) => getComputedStyle(node).visibility),
        "visible",
        path,
      );
      const untranslated = await page
        .locator("[data-i18n]")
        .evaluateAll((nodes) =>
          nodes
            .flatMap((n) =>
              JSON.parse(n.dataset.i18n).map(([index]) => n.childNodes[index]?.textContent || ""),
            )
            .filter((text) => /[一-鿿]/.test(text)),
        );
      assert.deepEqual(untranslated, [], path);
      await page.unroute("**/assets/shared/messages.js*");
    }
    // Ordinary top-level and sub-tab navigation uses the same early bootstrap.
    await page.goto(base + "/studio");
    await page.locator("#studio-templates-tab").click();
    await page.waitForURL("**/studio/templates");
    await page.waitForFunction(() => !document.documentElement.hasAttribute("data-i18n-pending"));
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.locator('.page-nav a[href="/settings"]').click();
    await page.waitForURL("**/settings");
    await page.waitForFunction(() => !document.documentElement.hasAttribute("data-i18n-pending"));
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.evaluate(() => {
      const box = document.createElement("div");
      box.id = "diagnostic-probe";
      document.body.append(box);
      MindexI18n.setText(
        box,
        MindexI18n.msg("ui.e5d4ec45ff67", {
          p0: MindexI18n.system(
            '上游 recall 不兼容：参数 召回权重（视觉）：原值 3，当前 1；参数 素材可见性指纹：原值 "old"，当前 "new"',
          ),
        }),
      );
    });
    const probe = page.locator("#diagnostic-probe");
    assert.ok(!/[一-鿿]/.test(await probe.innerText()));
    assert.match(await probe.innerText(), /Recall weight \(visual\)/);
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    assert.match(await probe.innerText(), /素材可见性指纹/);
    await page.evaluate(() => MindexI18n.setLanguage("en"));
    assert.ok(!/[一-鿿]/.test(await probe.innerText()));
    await page.addScriptTag({ url: base + "/assets/shared/shot-player.js" });
    await page.evaluate(() => {
      const video = document.createElement("video");
      document.body.append(video);
      const player = new ShotPlayer(video);
      player.container.id = "player-probe";
      player.fail(Error("镜头解码失败"));
      MindexFeedback.show("notice", MindexI18n.msg("ui.player_decode_failed"));
    });
    assert.equal(
      await page.locator("#player-probe button").first().getAttribute("aria-label"),
      "Play video",
    );
    assert.equal(await page.locator("#player-probe").getAttribute("title"), "Shot decode failed");
    assert.equal(await page.locator("#notice").getAttribute("title"), "Shot decode failed");
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    assert.equal(
      await page.locator("#player-probe button").first().getAttribute("aria-label"),
      "播放视频",
    );
    assert.equal(await page.locator("#player-probe").getAttribute("title"), "镜头解码失败");
    assert.equal(await page.locator("#notice").getAttribute("title"), "镜头解码失败");
    console.log(
      "PASS: ten pages hide untranslated English startup under delayed catalog loading; top/sub-tab navigation stays English",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
