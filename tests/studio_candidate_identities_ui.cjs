// Run only against serve_fixture.py with candidate-fixture.json seeded under /tmp.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
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
const base = process.env.MINDEX_CANDIDATE_URL || "http://127.0.0.1:18340";
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
const fixture = JSON.parse(
  fs.readFileSync("/tmp/mindex-candidate-identities-20261019/candidate-fixture.json"),
);
const out = "artifacts/candidate-identities";
fs.mkdirSync(out, { recursive: true });
let browser;
let page;
(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  const records = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base + "/studio");
  await page.locator("#create-text").fill("验收演员");
  await page.locator("#create-button").click();
  await page.locator("#job-workspace").waitFor();
  await batchRun(page, "split");
  await page.waitForFunction(() => !busy() && document.querySelector("#shot-strip .shot-card"));
  await batchRun(page, "recall");
  await page.waitForFunction(
    () =>
      !busy() &&
      Object.values(state.data.versions).some(
        (v) => v.stage === "recall" && v.status === "complete",
      ),
  );
  await openShotStage(page, 0, 1);
  await page.locator(".candidate-picker .detail-candidate").first().waitFor();
  for (const lang of ["zh", "en"]) {
    for (const width of [1440, 1000, 800, 540, 390, 320]) {
      await page.evaluate((l) => MindexI18n.setLanguage(l), lang);
      await page.setViewportSize({ width, height: 1000 });
      await page.locator(".candidate-picker .detail-candidate").first().scrollIntoViewIfNeeded();
      await page.waitForFunction(() =>
        [...document.querySelectorAll(".evidence-chip img")].every(
          (n) => n.complete && n.naturalWidth > 0,
        ),
      );
      const cards = await page.locator(".candidate-picker .detail-candidate").evaluateAll((nodes) =>
        nodes.map((n) => {
          const preview = n.querySelector(".candidate-image").getBoundingClientRect();
          const rank = n.querySelector(".candidate-rank").getBoundingClientRect();
          return {
            chips: [...n.querySelectorAll(".evidence-chip")].map((c) => ({
              text: c.textContent,
              src: c.querySelector("img")?.getAttribute("src"),
              loaded: c.querySelector("img")?.complete,
              href: c.getAttribute("href"),
            })),
            rank: n.querySelector(".candidate-rank").textContent,
            offset: [rank.x - preview.x, rank.y - preview.y],
            metadata: n.querySelector(".candidate-source").textContent,
            time: n.querySelector(".candidate-time").textContent,
            separateLines:
              n.querySelector(".candidate-time").getBoundingClientRect().top >=
              n.querySelector(".candidate-source").getBoundingClientRect().bottom,
            rankInImage: n.querySelector(".candidate-image .candidate-rank") !== null,
          };
        }),
      );
      assert.ok(cards.length);
      for (const [i, c] of cards.entries()) {
        assert.equal(c.chips.length, 4, "all four distinct pairs, no duplicate frame tags");
        assert.equal(new Set(c.chips.map((c) => c.text)).size, 4);
        assert.deepEqual(
          new Set(c.chips.map((c) => c.src)),
          new Set(fixture.refs.map((r) => r.url)),
        );
        assert.ok(c.chips.every((c) => c.href.startsWith("/persons?evidence=")));
        assert.equal(c.rank, String(i + 1));
        assert.deepEqual(c.offset, [6, 6]);
        assert.ok(c.rankInImage);
        assert.equal(
          c.metadata,
          "工程测试专用_颜色样本.mp4",
          "movie name has no rank, time or separator",
        );
        assert.match(c.time, /^\d+\.\d{3}s–\d+\.\d{3}s$/);
        assert.ok(c.separateLines, "movie and time on separate lines");
      }
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1),
        false,
      );
      records.push({ lang, width, cards });
      if ([1440, 320].includes(width)) {
        await page.screenshot({
          path: path.join(out, `recall-${lang}-${width}.png`),
          fullPage: true,
        });
      }
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.evaluate(() => MindexI18n.setLanguage("zh"));
  await page
    .locator(".detail-candidate")
    .first()
    .getByRole("button", { name: "预览", exact: true })
    .click();
  await page.locator("#play-dialog").waitFor();
  await page.locator("#play-close").click();
  await page
    .locator(".detail-candidate")
    .first()
    .getByRole("button", { name: "设为参考锚点", exact: true })
    .click();
  await page.waitForFunction(() => plan().anchors[order()[0]]?.mode === "manual");
  assert.equal(
    await page.locator(".detail-candidate").first().getAttribute("class"),
    "detail-candidate chosen-candidate",
  );
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(out, "coverage.json"), JSON.stringify({ records, errors }, null, 2));
  console.log(
    "PASS: 12 layouts; four deduplicated actor-role tags per candidate, matched reference URLs, rank at image top-left, preview and anchor selection.",
  );
  await browser.close();
})().catch(async (e) => {
  console.error(e);
  await page?.screenshot({ path: path.join(out, "failure.png"), fullPage: true });
  await browser?.close();
  process.exit(1);
});
