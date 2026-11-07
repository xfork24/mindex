// Real workflow and geometry checks against the isolated deterministic fixture.
const assert = require("node:assert/strict"),
  fs = require("node:fs");
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
const base = process.env.MINDEX_ALIGNMENT_URL || "http://127.0.0.1:18341";
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
const out = "artifacts/studio-alignment";
fs.mkdirSync(out, { recursive: true });
let browser, page;
(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(30000);
  const errors = [],
    records = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base + "/studio");
  await page.locator("[data-language=zh]").click();
  await page
    .locator("#create-text")
    .fill("红色开场画面讲述一个故事，绿色树林中光线渐变，人物回望，远处寺庙亮着灯。");
  await page.locator("#create-button").click();
  await page.locator("#job-workspace").waitFor();
  await batchRun(page, "split");
  await page.waitForFunction(() => !busy() && document.querySelector("#shot-strip .shot-card"));
  await openShotStage(page, 0, 3);
  assert.equal(
    await page.locator(".candidate-picker").count(),
    0,
    "no repeated anchors even before selection exists",
  );
  assert.equal(await page.getByText("新选片的参考锚点", { exact: true }).count(), 0);
  for (const stage of ["recall", "tts", "select"]) {
    await batchRun(page, stage);
    await page.waitForFunction(
      (s) =>
        !busy() &&
        Object.values(state.data.versions).some((v) => v.stage === s && v.status === "complete"),
      stage,
    );
  }
  for (const lang of ["zh", "en"])
    for (const width of [1440, 1000, 800, 540, 390, 320]) {
      await page.evaluate((l) => MindexI18n.setLanguage(l), lang);
      await page.setViewportSize({ width, height: 1000 });
      // 分镜卡片不溢出分镜条：内容宽度不超过卡片本身。
      const rows = await page
        .locator("#shot-strip .shot-card")
        .evaluateAll((cards) => cards.map((card) => [card.scrollWidth, card.clientWidth]));
      for (const [scroll, client] of rows)
        assert.ok(scroll <= client + 1, "shot card content fits its card");
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1),
        false,
      );
      records.push({ group: "card-fit", lang, width, rows });
    }
  // Each expanded stage still provides its version selector and reachable controls.
  for (let stage = 0; stage < 4; stage++) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    await openShotStage(page, 0, stage);
    const host = page.locator(".shot-detail-panel .version-history");
    await host.locator("select").waitFor();
    for (const lang of ["zh", "en"])
      for (const width of [1440, 1000, 800, 540, 390, 320]) {
        await page.evaluate((l) => MindexI18n.setLanguage(l), lang);
        await page.setViewportSize({ width, height: 1000 });
        const controls = await host.locator("select,button").evaluateAll((nodes) =>
          nodes.map((n) => {
            const r = n.getBoundingClientRect();
            return {
              tag: n.tagName,
              text: n.textContent,
              y: r.y,
              h: r.height,
              center: r.y + r.height / 2,
            };
          }),
        );
        assert.equal(controls.length, 3);
        for (const c of controls) assert.equal(c.h, 32);
        for (let i = 0; i < controls.length; i++)
          for (let j = i + 1; j < controls.length; j++)
            if (Math.abs(controls[i].y - controls[j].y) < 32)
              assert.ok(Math.abs(controls[i].center - controls[j].center) <= 1);
        if (stage === 3) assert.equal(await page.locator(".candidate-picker").count(), 0);
        assert.equal(
          await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1),
          false,
        );
        records.push({ group: "history-" + stage, lang, width, controls });
      }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    await host.locator("button").first().click();
    await page.locator("#work-dialog[open]").waitFor();
    await page.locator("#dialog-close").click();
  }
  await page.locator(".manual-range-host .fixed-window").waitFor();
  const slider = page.locator(".manual-range-host .fixed-window");
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowLeft");
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/range-confirm") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "保存当前窗口", exact: true }).click();
  assert.equal((await response).status(), 200);
  await page.waitForFunction(() => !busy());
  await page.locator("#notice").filter({ hasText: "当前窗口已保存" }).waitFor();
  assert.equal(await page.locator(".candidate-picker").count(), 0);
  await page.locator(".manual-range-host .window-viewport").waitFor();
  const history = page.locator(".shot-detail-panel .version-history");
  const selected = await history.locator("select").inputValue();
  await history.locator("button").nth(1).click();
  await page.waitForFunction(() => !busy());
  assert.equal(await page.evaluate(() => plan().shots[order()[0]].select), selected);
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.locator(".shot-detail-panel").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${out}/select-${width}.png` });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openShotStage(page, 0, 1);
  await page.locator(".candidate-picker").waitFor();
  await page
    .locator(".candidate-picker")
    .getByRole("button", { name: "预览", exact: true })
    .first()
    .click();
  await page.locator("#play-dialog").waitFor();
  await page.locator("#play-close").click();
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    `${out}/final-coverage.json`,
    JSON.stringify(
      {
        records,
        errors,
        actions: [
          "select-before-generation",
          "four-stages",
          "history-detail",
          "window-keyboard",
          "window-save",
          "select-history",
          "recall-preview",
        ],
      },
      null,
      2,
    ),
  );
  console.log(
    "PASS: 60 alignment layouts; no duplicate select picker; history details and selection, window save, recall preview.",
  );
  await browser.close();
})().catch(async (e) => {
  console.error(e);
  await page?.screenshot({ path: `${out}/failure.png` });
  await browser?.close();
  process.exit(1);
});
