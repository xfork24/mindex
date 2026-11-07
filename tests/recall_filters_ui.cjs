// Isolated acceptance: serve_fixture.py with at least three indexed sources.
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
const base = process.env.MINDEX_RECALL_URL || "http://127.0.0.1:18338";
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
const out = process.env.MINDEX_RECALL_OUTPUT || "artifacts/recall-filters";
fs.mkdirSync(out, { recursive: true });
const widths = [1440, 1000, 800, 540, 390, 320];
const records = [];
const errors = [];
let browser;
let page;
async function audit(group, selector) {
  for (const lang of ["zh", "en"]) {
    await page.evaluate((l) => MindexI18n.setLanguage(l), lang);
    for (const width of widths) {
      await page.setViewportSize({ width, height: 1000 });
      const r = await page.locator(selector).evaluate((host) => {
        const controls = [...host.querySelectorAll("input,select,button")]
          .filter((n) => n.getClientRects().length)
          .map((n) => {
            const r = n.getBoundingClientRect();
            const s = getComputedStyle(n);
            return {
              tag: n.tagName,
              type: n.type,
              multiple: n.multiple,
              id: n.id || n.dataset.recallFilter || n.className,
              x: r.x,
              y: r.y,
              w: r.width,
              h: r.height,
              resize: s.resize,
            };
          });
        const file = [...host.querySelectorAll("input[type=file]")].find(
          (n) => n.getClientRects().length,
        );
        const button = host.querySelector(".recall-upload-button");
        const f = file?.getBoundingClientRect();
        const b = button?.getBoundingClientRect();
        const panel = host
          .querySelector(".search-media-input:not([hidden]) .upload")
          ?.getBoundingClientRect();
        return {
          mediaPanel: panel ? { x: panel.x, y: panel.y, w: panel.width, h: panel.height } : null,
          controls,
          overflow: document.documentElement.scrollWidth > innerWidth + 1,
          upload: file
            ? {
                x: f.x,
                y: f.y,
                w: f.width,
                h: f.height,
                button: b ? { x: b.x, y: b.y, w: b.width, h: b.height } : null,
              }
            : null,
        };
      });
      assert.equal(r.overflow, false, `${group}/${lang}/${width} page overflow`);
      for (const c of r.controls) {
        if ((c.tag === "INPUT" && c.type !== "file") || (c.tag === "SELECT" && !c.multiple)) {
          assert.equal(c.h, 32, `${group} ${c.id}`);
        }
        if (c.type === "file") {
          assert.equal(c.h, 40);
        }
      }
      if (r.upload?.button) {
        assert.equal(r.upload.button.h, 40);
        if (Math.abs(r.upload.y - r.upload.button.y) < 40) {
          assert.ok(Math.abs(r.upload.y - r.upload.button.y) <= 1, "upload row alignment");
        }
      }
      const fields = r.controls.filter(
        (c) => c.tag === "INPUT" && !["file", "search"].includes(c.type),
      );
      for (let i = 0; i < fields.length; i++) {
        for (let j = i + 1; j < fields.length; j++) {
          if (Math.abs(fields[i].y - fields[j].y) < 32) {
            assert.ok(Math.abs(fields[i].y - fields[j].y) <= 1, "filter alignment");
          }
        }
      }
      records.push({ group, lang, width, ...r });
      if ([1440, 320].includes(width)) {
        await page.screenshot({
          path: path.join(out, `${group}-${lang}-${width}.png`),
          fullPage: true,
        });
      }
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.evaluate(() => MindexI18n.setLanguage("zh"));
}
async function picked(selector) {
  return page
    .locator(selector)
    .evaluate((n) => [...n.selectedOptions].map((o) => o.value).filter((v) => v !== "__all__"));
}
(async () => {
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(20000);
  page.on("pageerror", (e) => errors.push(e.message));
  const status = await (await page.request.get(base + "/api/status")).json();
  assert.ok(status.sources.length >= 3, "seed at least three sources");
  const ids = status.sources.slice(0, 2).map((s) => s.id);
  await page.goto(base);
  await page.locator("#source option").nth(3).waitFor({ state: "attached" });
  await page.locator("#source").selectOption(ids);
  const sourceSearch = page.locator(".material-filters .multi-search");
  await sourceSearch.fill(status.sources[0].name.slice(0, 3));
  assert.deepEqual(new Set(await picked("#source")), new Set(ids));
  await page.locator("#refresh").click();
  await page.waitForFunction(() => !document.querySelector("#refresh").disabled);
  assert.deepEqual(new Set(await picked("#source")), new Set(ids));
  assert.ok(await sourceSearch.inputValue());
  await page.locator("#query").fill("红蓝绿画面");
  await page.locator("#granularity").selectOption("shot");
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/api/search/text") && r.method() === "POST",
  );
  await page.locator("#search-button").click();
  assert.deepEqual(new Set((await request).postDataJSON().filters.source_ids), new Set(ids));
  await page.waitForFunction(() => !document.querySelector("#search-button").disabled);
  const list = page.locator(".material-filters .multi-list");
  await list.scrollIntoViewIfNeeded();
  const before = await list.boundingBox();
  await page.mouse.move(before.x + before.width - 3, before.y + before.height - 3);
  await page.mouse.down();
  await page.mouse.move(before.x + before.width - 3, before.y + before.height + 65, { steps: 8 });
  await page.mouse.up();
  assert.ok((await list.boundingBox()).height > before.height + 30, "source list resize");
  await sourceSearch.fill("");
  await page.locator("#source").selectOption("__all__");
  assert.equal((await picked("#source")).length, status.sources.length);
  await page.locator("#filter-reset").click();
  assert.deepEqual(await picked("#source"), []);
  assert.equal(await sourceSearch.inputValue(), "");
  await audit("search-text", "#search-form");
  const mediaBoxes = {};
  for (const mode of ["image", "video"]) {
    await page.locator("#" + mode + "-tab").click();
    await audit("search-" + mode, "#search-form");
    mediaBoxes[mode] = await page.locator("#" + mode + "-drop").boundingBox();
  }
  assert.equal(mediaBoxes.image.height, mediaBoxes.video.height, "matching media upload panels");
  for (const a of records.filter((r) => r.group === "search-image")) {
    const b = records.find(
      (r) => r.group === "search-video" && r.lang === a.lang && r.width === a.width,
    );
    assert.deepEqual(a.mediaPanel, b.mediaPanel, `media panel geometry ${a.lang}/${a.width}`);
    assert.deepEqual(a.upload, b.upload, `file geometry ${a.lang}/${a.width}`);
  }
  await page.goto(base + "/studio");
  await page.locator("#create-text").fill("红色开场画面。蓝色结尾画面。");
  await page.locator("#create-button").click();
  await page.locator("#job-workspace").waitFor();
  await batchRun(page, "split");
  await page.waitForFunction(() => !busy() && document.querySelector("#shot-strip .shot-card"));
  await page.locator("#shot-strip .shot-card").first().click();
  const source = "[data-recall-filter=source_ids]";
  await page
    .locator(source + " option")
    .nth(3)
    .waitFor({ state: "attached" });
  await page.locator(source).selectOption(ids);
  for (const [key, value] of [
    ["scene", "室内"],
    ["emotion", "平静"],
    ["shot_size", "近景"],
    ["min_duration", "0.5"],
    ["max_duration", "10"],
  ]) {
    await page.locator(`[data-recall-filter=${key}]`).fill(value);
  }
  await audit("studio-text", ".recall-editor");
  await page.getByRole("button", { name: "保存此镜", exact: true }).click();
  await page.waitForFunction(() => !hasDirtyDraft());
  assert.deepEqual(new Set(await picked(source)), new Set(ids));
  assert.equal(await page.locator("[data-recall-filter=scene]").inputValue(), "室内");
  await page.locator("[data-recall-filter=scene]").fill("取消的场景");
  await page.getByRole("button", { name: "取消修改", exact: true }).click();
  assert.equal(await page.locator("[data-recall-filter=scene]").inputValue(), "室内");
  await batchRun(page, "recall");
  await page.waitForFunction(
    () =>
      !busy() &&
      Object.values(state.data.versions).some(
        (v) => v.stage === "recall" && v.status === "complete",
      ),
  );
  const saved = await page.evaluate(
    () =>
      Object.values(state.data.versions)
        .filter((v) => v.stage === "recall" && v.status === "complete")
        .at(-1).data,
  );
  assert.ok(saved.results.length);
  assert.ok(saved.results.every((r) => ids.includes(r.source_id)));
  for (const mode of ["image", "video"]) {
    await page.locator(".recall-editor>label select").first().selectOption(mode);
    assert.deepEqual(new Set(await picked(source)), new Set(ids));
    await audit("studio-" + mode, ".recall-editor");
    const payload =
      mode === "image"
        ? {
            name: "reference.png",
            mimeType: "image/png",
            buffer: Buffer.from(
              "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
              "base64",
            ),
          }
        : path.join("/tmp/mindex-recall-filters-20261019", "蓝绿素材.mp4");
    await page.locator(".recall-upload-row input").setInputFiles(payload);
    await page.locator(".recall-upload-button").click();
    await page.locator(".query-preview-host " + (mode === "image" ? "img" : "video")).waitFor();
    await page.getByRole("button", { name: "保存此镜", exact: true }).click();
    await page.waitForFunction(() => !hasDirtyDraft());
    assert.deepEqual(new Set(await picked(source)), new Set(ids));
    assert.equal(await page.locator("[data-recall-filter=emotion]").inputValue(), "平静");
  }
  assert.deepEqual(errors, []);
  fs.writeFileSync(
    path.join(out, "coverage.json"),
    JSON.stringify({ records, errors }, null, 2) + "\n",
  );
  console.log(
    `PASS: ${records.length} layouts; multi-source search, refresh, reset, resize, save/cancel, text recall, image/video uploads, no page errors.`,
  );
  await browser.close();
})().catch(async (e) => {
  console.error(e);
  if (page) {
    await page.screenshot({ path: path.join(out, "failure.png"), fullPage: true });
  }
  await browser?.close();
  process.exit(1);
});
