// 隔离服务 --progress-fixture --reject-blue-analysis；工程内容拒绝与独立列表滚动验收。
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_UPLOAD_TEST_URL || "http://127.0.0.1:8019";
const fixture = process.env.MINDEX_UPLOAD_TEST_VIDEO;
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } }),
      errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(base + "/admin");
    await page.getByRole("button", { name: "添加新素材" }).click();
    await page.locator("#upload-files").setInputFiles(fixture);
    await page.locator("#upload-results").getByText("登记完成", { exact: false }).waitFor();
    await page.locator("#upload-close").click();
    await page
      .locator("[data-work=shots]")
      .getByRole("button", { name: "开始处理", exact: true })
      .click();
    await page.waitForFunction(() =>
      document
        .querySelector("[data-work=shots] .work-task-progress[data-phase=publish]")
        ?.textContent.includes("检索已更新"),
    );
    await page
      .locator("[data-work=analysis]")
      .getByRole("button", { name: "开始处理", exact: true })
      .click();
    await page.waitForFunction(() =>
      document
        .querySelector("[data-work=analysis] .work-task-progress[data-phase=publish]")
        ?.textContent.includes("检索已更新"),
    );
    const analysis = page.locator("[data-work=analysis]");
    assert.ok(
      (await analysis.locator(".work-summary").textContent()).includes("已跳过 2 个敏感片段"),
    );
    const sid = await page.evaluate(() => new URLSearchParams(location.search).get("source"));
    const source = await (await page.request.get(base + `/api/admin/sources/${sid}`)).json();
    assert.equal(source.work.analysis.status, "complete");
    assert.equal(source.work.analysis.result.skipped, 2);
    await page
      .locator(".segment-card")
      .nth(1)
      .getByRole("button", { name: "详情", exact: true })
      .click();
    await page
      .locator("#analysis-content")
      .getByText("模型内容安全策略拒绝，已跳过此片段", { exact: true })
      .waitFor();
    await page
      .locator("#analysis-content")
      .getByText("工程测试：系统检测到输入或生成内容可能包含不安全或敏感内容", { exact: true })
      .waitFor();
    await page.locator("#analysis-close").click();
    await page.reload();
    await analysis.waitFor();
    // Extend only the rendered list to exercise a full 48-item page, leaving
    // the isolated database and API state unchanged.
    await page.evaluate(() => {
      const list = document.getElementById("source-list"),
        item = list.querySelector(".source-item");
      for (let i = 1; i < 48; i++) {
        const copy = item.cloneNode(true);
        copy.querySelector("strong").textContent = `滚动工程素材 ${i}`;
        list.append(copy);
      }
      window.scrollTo(0, 0);
    });
    const bounds = async () =>
      page.evaluate(() => {
        const panel = document.querySelector(".source-panel"),
          list = document.getElementById("source-list");
        return {
          bottom: panel.getBoundingClientRect().bottom,
          top: panel.getBoundingClientRect().top,
          height: innerHeight,
          scroll: list.scrollTop,
          range: list.scrollHeight - list.clientHeight,
          page: scrollY,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
    await page.waitForFunction(
      () =>
        Math.abs(
          document.querySelector(".source-panel").getBoundingClientRect().bottom - innerHeight,
        ) < 2,
    );
    let before = await bounds();
    assert.ok(before.range > 500);
    assert.equal(before.overflow, false);
    const list = page.locator("#source-list");
    await list.hover();
    await page.mouse.wheel(0, 400);
    await page.waitForFunction(() => document.getElementById("source-list").scrollTop > 100);
    const scrolled = await bounds();
    assert.equal(scrolled.page, before.page);
    await page.evaluate(() => {
      const l = document.getElementById("source-list");
      l.scrollTop = l.scrollHeight;
    });
    await list.hover();
    await page.mouse.wheel(0, 500);
    await page.waitForTimeout(100);
    assert.equal((await bounds()).page, before.page);
    await page.evaluate(() => window.scrollTo(0, 330));
    await page.waitForFunction(
      () =>
        Math.abs(document.querySelector(".source-panel").getBoundingClientRect().top) < 2 &&
        Math.abs(
          document.querySelector(".source-panel").getBoundingClientRect().bottom - innerHeight,
        ) < 2,
    );
    await page.screenshot({ path: "/tmp/mindex-skip-scroll-desktop.png", fullPage: false });
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.waitForFunction(
      () =>
        Math.abs(
          document.querySelector(".source-panel").getBoundingClientRect().bottom - innerHeight,
        ) < 2,
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => window.scrollTo(0, 0));
    assert.equal((await bounds()).overflow, false);
    assert.ok(
      await page.evaluate(
        () => document.getElementById("source-list").clientHeight <= innerHeight / 2 + 2,
      ),
    );
    await page.screenshot({ path: "/tmp/mindex-skip-scroll-mobile.png", fullPage: false });
    await page.getByRole("button", { name: "English", exact: true }).click();
    assert.ok(
      (await analysis.locator(".work-summary").textContent()).includes("Skipped 2 sensitive clips"),
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: rejected shot continues, publishes, shows reason/count after reload; independent list scrolling and no page scroll at edge; sidebar fills viewport; resize/mobile/English; no page errors.",
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
