// Use serve_upload_fixture.py --progress-fixture --embedding-delay 4 with an isolated data directory.
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_UPLOAD_TEST_URL || "http://127.0.0.1:8018";
const fixture = process.env.MINDEX_UPLOAD_TEST_VIDEO;
(async () => {
  assert.ok(fixture, "MINDEX_UPLOAD_TEST_VIDEO must reference the isolated progress fixture");
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } }),
      errors = [],
      counts = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("response", async (response) => {
      if (/\/api\/admin\/sources\/[a-f0-9]+$/.test(new URL(response.url()).pathname)) {
        const source = await response.json().catch(() => null),
          p = source?.work?.shots?.task?.progress;
        if (p?.stage === "embedding") counts.push(p.completed);
      }
    });
    await page.goto(base + "/admin");
    await page.getByRole("button", { name: "添加新素材" }).click();
    await page.locator("#upload-files").setInputFiles(fixture);
    await page.locator("#upload-results").getByText("登记完成", { exact: false }).waitFor();
    await page.locator("#upload-close").click();
    const shots = page.locator("[data-work=shots]");
    await shots.getByRole("button", { name: "开始处理", exact: true }).click();
    const bar = shots.getByRole("progressbar", { name: "分镜处理检索更新进度" });
    await shots
      .locator(".work-task-progress[data-phase=publish]")
      .filter({ hasText: "嵌入片段 0/3 个片段" })
      .waitFor();
    await page.waitForFunction(
      () =>
        document.querySelector("[data-work=shots] .work-progress[data-phase=publish]")?.value === 1,
    );
    assert.equal(await bar.getAttribute("max"), "3");
    assert.ok(
      (await shots.locator(".work-task-progress[data-phase=publish]").textContent()).includes(
        "1/3 个片段",
      ),
    );
    // The active published coverage remains unchanged while vectors are building.
    assert.ok((await shots.locator(".muted").first().textContent()).includes("0/3"));
    await page.reload();
    await page.waitForFunction(
      () =>
        document.querySelector("[data-work=shots] .work-progress[data-phase=publish]")?.value >= 1,
    );
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: "/tmp/mindex-embedding-progress-mobile.png", fullPage: true });
    await page.waitForFunction(
      () =>
        document.querySelector("[data-work=shots] .work-progress[data-phase=publish]")?.value === 2,
    );
    await page.waitForFunction(() =>
      document
        .querySelector("[data-work=shots] .work-task-progress[data-phase=publish]")
        ?.textContent.includes("检索已更新 3/3 个片段"),
    );
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({ path: "/tmp/mindex-embedding-progress-desktop.png", fullPage: true });
    assert.ok((await shots.locator(".muted").first().textContent()).includes("已更新"));
    assert.ok(
      counts.includes(0) && counts.includes(1) && counts.includes(2),
      JSON.stringify(counts),
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: persisted API progress -> polling -> 0/3, 1/3, 2/3, published 3/3; reload; mobile; no page errors.",
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
