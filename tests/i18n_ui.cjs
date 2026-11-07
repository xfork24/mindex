// Run with serve_fixture.py on an isolated library. No real model/service required.
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const batchRun = async (page, stage) => {
  if (stage === "split" && !(await page.evaluate(() => plan().script))) {
    await page
      .locator("#script-text")
      .fill(await page.evaluate(() => state.data.job.outline || "红色开场。"));
    await page.evaluate(() => saveScript());
    await page.waitForFunction(() => !!plan().script);
  }
  await page.locator("#batch-stage").selectOption(stage === "recall" ? "picture" : stage);
  await page.locator("#batch-regenerate").click();
};
const openShotStage = (page, shot, n) =>
  page.evaluate(
    ([i, k]) => {
      const sid = order()[i];
      openStage(sid, barStages(sid)[k]);
    },
    [shot, n],
  );
const base = process.env.MINDEX_I18N_TEST_URL || "http://127.0.0.1:8027";
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage(),
      errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/settings/voices/catalog", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          groups: [
            {
              label: "系统音色 · Qwen3-TTS",
              voices: [{ voice_id: "vivian", label: "Vivian · 明快、略带锋芒的年轻女声（中文）" }],
            },
          ],
          library: [],
          default_voice: "vivian",
          clone: { supported: false },
          error: null,
        }),
      }),
    );
    async function toggle(lang) {
      await page.evaluate((lang) => MindexI18n.setLanguage(lang), lang);
    }
    async function preserve(selector) {
      await page.evaluate((selector) => {
        window.kept = document.querySelector(selector);
      }, selector);
      await toggle("en");
      await toggle("zh");
      assert.ok(
        await page.evaluate(
          (selector) => window.kept === document.querySelector(selector),
          selector,
        ),
        selector,
      );
    }
    await page.goto(base + "/search");
    assert.equal(await page.locator("html").getAttribute("lang"), "zh-CN");
    await page.locator("#query").fill("中文素材与未提交输入");
    await page.locator("#actor-select").waitFor();
    await page.locator(".multi-search").first().fill("演员");
    await page.evaluate(() => {
      document.querySelector("#query").focus();
      document.querySelector("#query").setSelectionRange(2, 5);
    });
    await toggle("en");
    assert.equal(await page.locator("#query").inputValue(), "中文素材与未提交输入");
    assert.equal(await page.locator(".multi-search").first().inputValue(), "演员");
    assert.deepEqual(
      await page.evaluate(() => [
        document.activeElement.id,
        document.querySelector("#query").selectionStart,
        document.querySelector("#query").selectionEnd,
      ]),
      ["query", 2, 5],
    );
    await preserve("#actor-select");
    await toggle("en");
    await page.locator("[data-query]").first().click();
    assert.match(await page.locator("#query").inputValue(), /close-up/i);
    const peer = await context.newPage();
    await peer.goto(base + "/admin");
    assert.equal(await peer.locator("html").getAttribute("lang"), "en");
    await toggle("zh");
    await peer.waitForFunction(() => document.documentElement.lang === "zh-CN");
    await toggle("en");
    await page.reload();
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    // Delayed search response renders in the language selected while waiting.
    let release;
    await page.route("**/api/search/text", async (route) => {
      await new Promise((resolve) => {
        release = resolve;
      });
      await route.continue();
    });
    await page.locator("#query").fill("绿色画面");
    await page.locator("#search-button").click();
    await page.waitForFunction(() => document.querySelector("#search-button").disabled);
    await toggle("zh");
    await toggle("en");
    release();
    await page.locator(".card").first().waitFor();
    assert.match(await page.locator("#result-count").innerText(), /clips/);
    assert.match(await page.locator(".card-title").first().innerText(), /工程测试专用/);
    await page.unroute("**/api/search/text");
    const video = page.locator(".card video").first();
    await video.evaluate(async (v) => {
      if (v.shotPlayer) await v.shotPlayer.ready;
      await v.play();
    });
    await preserve(".card video");
    assert.equal(await video.evaluate((v) => v.paused), false);
    await page.locator(".card").first().getByRole("button", { name: "查看详情" }).click();
    await preserve("#detail-dialog video");
    assert.equal(await page.locator("#detail-dialog").evaluate((d) => d.open), true);
    await page.locator("#close-dialog").click();
    // Upload inputs, form values and filter controls stay attached.
    await page.goto(base + "/admin");
    await page.locator(".source-item").first().click();
    await page.locator("details.work-panel").waitFor();
    if (!(await page.locator("details.work-panel").evaluate((node) => node.open)))
      await page.locator(".work-toolbar").click();
    await page.locator(".work-card").last().waitFor();
    await page.locator("#add-source").click();
    let finishUpload, markUploadStarted;
    const uploadStarted = new Promise((resolve) => {
      markUploadStarted = resolve;
    });
    await page.route("**/api/uploads", async (route) => {
      await new Promise((release) => {
        finishUpload = release;
        markUploadStarted();
      });
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: "无效的文件名" }),
      });
    });
    await page.locator("#upload-files").setInputFiles({
      name: "中文文件.mp4",
      mimeType: "video/mp4",
      buffer: Buffer.from("fixture"),
    });
    await uploadStarted;
    await page.locator(".upload-row").waitFor();
    await preserve("#upload-files");
    assert.match(await page.locator(".upload-row").innerText(), /中文文件.mp4/);
    finishUpload();
    await page.locator(".upload-row .source-warning").waitFor();
    await page.locator("#upload-close").click();
    await preserve("[data-work=dialogue] select");
    await page.locator("#source-detail").getByRole("button", { name: "备注", exact: true }).click();
    await page.locator("#edit-notes").fill("未保存素材备注");
    await preserve("#edit-notes");
    assert.equal(await page.locator("#edit-notes").inputValue(), "未保存素材备注");
    await page.locator("#edit-close").click();
    // Settings modal contains dynamic label/input pairs: preserve the inputs and entered credentials.
    await page.goto(base + "/settings");
    await page.locator("#roles .role-row-head").first().waitFor();
    // 部署设置章节：与「检索调参」同一套 role-row 折叠，默认折叠，展开状态跨刷新保留。
    assert.equal(
      (
        await page.locator('section[aria-label="部署设置"] .section-heading h2').textContent()
      ).trim(),
      "部署设置",
      "章节标题应为部署设置",
    );
    const deploymentRow = page.locator("#deployment-row");
    const deploymentHead = deploymentRow.locator(".role-row-head");
    assert.equal(
      await deploymentRow.evaluate((node) => node.classList.contains("is-open")),
      false,
      "部署后端应默认折叠",
    );
    assert.equal(
      await page.locator("#deployment-body").evaluate((node) => node.hidden),
      true,
      "折叠时内容应隐藏",
    );
    assert.equal(await deploymentHead.getAttribute("aria-expanded"), "false");
    await deploymentHead.click();
    assert.equal(
      await deploymentHead.getAttribute("aria-expanded"),
      "true",
      "点击标题应展开部署后端",
    );
    // 折叠头右侧摘要显示当前后端组合。
    assert.match(
      (await page.locator("#deployment-summary").textContent()).trim(),
      /[\w-]+\s·\s[\w-]+/,
      "摘要应显示后端组合",
    );
    // 后端概要常显（不再折叠），标题保留。
    assert.equal(
      await page.locator("details.deployment-summary").count(),
      0,
      "后端概要不应再包在可折叠项里",
    );
    assert.equal(
      (await page.locator(".deployment-label").textContent()).trim(),
      "后端概要",
      "后端概要标题应保留",
    );
    assert.equal(await page.locator("#deployment-status").isVisible(), true, "后端概要应常显");
    // 部署字段规则由异步加载的脚本渲染：必须已渲染且不含 i18n 占位 token（\uE000z<id>\uE001）。
    await page.waitForFunction(
      () => document.querySelectorAll("#deployment-field-rules li").length > 0,
    );
    const deploymentRules = await page.locator("#deployment-field-rules li").allTextContents();
    assert(
      deploymentRules.every((text) => !text.includes("\uE000")),
      `部署字段规则泄漏 i18n 占位 token：${JSON.stringify(deploymentRules)}`,
    );
    assert(
      deploymentRules.some((text) => /只读|保存后重启|迁移/.test(text)),
      `部署字段规则缺少可读说明：${JSON.stringify(deploymentRules)}`,
    );
    // 每列标签与值右边缘对齐（dd 的默认左缩进必须归零）。
    const offsets = await page.evaluate(() =>
      [...document.querySelectorAll("#deployment-status > div")].map((row) => {
        const label = row.querySelector("dt").getBoundingClientRect();
        const value = row.querySelector("dd").getBoundingClientRect();
        return {
          delta: Math.round(Math.abs(label.right - value.right)),
          margin: Math.round(
            parseFloat(getComputedStyle(row.querySelector("dd")).marginInlineStart) || 0,
          ),
        };
      }),
    );
    assert(
      offsets.every((item) => item.delta <= 1),
      `后端概要列上下未对齐：${JSON.stringify(offsets)}`,
    );
    assert(
      offsets.every((item) => item.margin === 0),
      `后端概要值仍有默认左缩进：${JSON.stringify(offsets)}`,
    );
    // 部署配置默认折叠：展开后编辑器可见，再收起。
    const deploymentConfig = page.locator("details.deployment-config");
    assert.equal(await deploymentConfig.evaluate((node) => node.open), false, "部署配置应默认折叠");
    await deploymentConfig.locator("> summary").click();
    assert.equal(await page.locator("#deployment-editor").isVisible(), true, "展开后编辑器应可见");
    await deploymentConfig.locator("> summary").click();
    assert.equal(await page.locator("#deployment-editor").isVisible(), false, "折叠后编辑器应隐藏");
    // 字段规则自身可折叠：展开可见，整区折叠时随之隐藏。
    const deploymentRulesPanel = page.locator("details.deployment-rules");
    await deploymentRulesPanel.locator("> summary").click();
    assert.equal(
      await page.locator("#deployment-field-rules").isVisible(),
      true,
      "展开后字段规则应可见",
    );
    await deploymentHead.click();
    assert.equal(
      await page.locator("#deployment-status").isVisible(),
      false,
      "折叠后后端概要应隐藏",
    );
    assert.equal(
      await page.locator("#deployment-field-rules").isVisible(),
      false,
      "折叠后字段规则应隐藏",
    );
    for (const path of ["/search", "/admin", "/persons", "/settings", "/"]) {
      await page.goto(base + path);
      await page.waitForTimeout(300);
      let requests = 0;
      const count = () => requests++;
      page.on("request", count);
      await toggle("en");
      await toggle("zh");
      page.off("request", count);
      assert.equal(requests, 0, path);
      await toggle("en");
      await page.setViewportSize({ width: 390, height: 844 });
      // 任何页面都不得把 i18n 占位 token 泄漏到可见文本（用户会看到 z201 这类代号）。
      assert.ok(
        !(await page.evaluate(() => document.body.textContent.includes("\uE000"))),
        `${path} 泄漏 i18n 占位 token`,
      );
      assert.ok(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        path,
      );
      await page.screenshot({
        path: `/tmp/mindex-i18n-${path.slice(1) || "search"}-mobile.png`,
        fullPage: true,
      });
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    const unavailable = await browser.newContext();
    await unavailable.addInitScript(() => {
      Object.defineProperty(window, "localStorage", {
        get() {
          throw Error("storage unavailable");
        },
      });
    });
    const fallback = await unavailable.newPage();
    await fallback.goto(base + "/search");
    await fallback.locator("[data-language-toggle]").click();
    assert.equal(await fallback.locator("html").getAttribute("lang"), "en");
    await fallback.locator("[data-language-toggle]").click();
    assert.equal(await fallback.locator("html").getAttribute("lang"), "zh-CN");
    assert.deepEqual(errors, []);
    console.log(
      "PASS: five pages; dynamic search/storyboard/settings; unchanged content; node/input/file/focus/playback retention; no switch requests; delayed response; persistence; cross-tab sync; blocked storage; narrow layout.",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
