// 临时素材库专用浏览器验收，必须由 serve_upload_fixture.py 提供服务。
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_UPLOAD_TEST_URL || "http://127.0.0.1:8018";
const fixture =
  process.env.MINDEX_UPLOAD_TEST_VIDEO || "/tmp/mindex-upload-verification/工程样本.mp4";
(async () => {
  const browser = await chromium.launch({ headless: true });
  let page;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(base + "/admin");
    await page.getByRole("button", { name: "添加新素材" }).click();
    await page.locator("#upload-files").setInputFiles([
      { name: "失败.mp4", mimeType: "video/mp4", buffer: Buffer.from("broken") },
      {
        name: "工程样本.mp4",
        mimeType: "video/mp4",
        buffer: require("node:fs").readFileSync(fixture),
      },
      {
        name: "重复素材.mov",
        mimeType: "video/quicktime",
        buffer: require("node:fs").readFileSync(fixture),
      },
    ]);
    await page
      .locator("#upload-results")
      .getByText("重复文件，已定位已有素材", { exact: false })
      .waitFor();
    assert.equal(await page.locator(".upload-row").count(), 3);
    assert.ok(
      await page
        .locator(".upload-row")
        .first()
        .textContent()
        .then((t) => t.includes("失败")),
    );
    await page.locator("#upload-close").click();
    await page.locator(".work-panel").waitFor();
    if (!(await page.locator(".work-panel").evaluate((element) => element.open)))
      await page.locator(".work-toolbar h3").click();
    await page.waitForFunction(() => document.querySelectorAll(".work-status").length === 4);
    assert.equal(await page.locator(".work-card").count(), 4);
    assert.equal(await page.locator(".work-status").filter({ hasText: "待处理" }).count(), 4);
    const sid = await page.evaluate(() => new URLSearchParams(location.search).get("source"));
    const shots = page.locator("[data-work=shots]"),
      dialogue = page.locator("[data-work=dialogue]"),
      analysis = page.locator("[data-work=analysis]"),
      persons = page.locator("[data-work=persons]");
    assert.ok(await analysis.getByRole("button", { name: "开始处理", exact: true }).isDisabled());
    await shots.getByRole("button", { name: "开始处理", exact: true }).click();
    await shots.locator(".work-status").filter({ hasText: "处理完成" }).waitFor();
    await page.waitForFunction(() =>
      document
        .querySelector("[data-work=shots] .work-task-progress[data-phase=publish]")
        .textContent.includes("已更新"),
    );
    await dialogue.getByLabel("对白处理方式").selectOption("subtitle");
    await dialogue.getByRole("button", { name: "分析对白", exact: true }).click();
    await dialogue.getByText("请选择字幕轨道或上传字幕文件。", { exact: true }).waitFor();
    await dialogue.getByLabel("对白处理方式").selectOption("asr");
    await dialogue.getByRole("button", { name: "分析对白", exact: true }).click();
    await dialogue.locator(".work-status").filter({ hasText: "无音轨" }).waitFor();
    await analysis.getByRole("button", { name: "开始处理", exact: true }).click();
    await analysis.locator(".work-status").filter({ hasText: "处理完成" }).waitFor();
    await persons.getByRole("button", { name: "配置人物" }).click();
    await page.locator("#person-setup").getByText("请选择关联作品", { exact: true }).waitFor();
    assert.ok(await page.locator("#person-start").isDisabled());
    const entity = async (value) =>
      await (await page.request.post(base + "/api/persons/entities", { data: value })).json();
    const work = await entity({ kind: "work", name: "浏览器工程作品" }),
      actor = await entity({ kind: "actor", name: "浏览器工程演员" });
    const role = await entity({
      kind: "role",
      name: "浏览器工程角色",
      work_ids: [work.id],
      actor_ids: [actor.id],
    });
    // 使用与现有人物库相同的参考图上传 API。
    const ref = await page.request.post(base + "/api/persons/references", {
      multipart: {
        actor_id: actor.id,
        role_id: role.id,
        file: {
          name: "reference.png",
          mimeType: "image/png",
          buffer: Buffer.from(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX2kAAAAASUVORK5CYII=",
            "base64",
          ),
        },
      },
    });
    assert.equal(ref.status(), 201, await ref.text());
    await page.locator("#person-close").click();
    await persons.getByRole("button", { name: "配置人物" }).click();
    await page.locator("#person-work").selectOption(work.id);
    await page.waitForFunction(() => !document.querySelector("#person-start").disabled);
    await page.locator("#person-start").click();
    await persons.locator(".work-status").filter({ hasText: "处理完成" }).waitFor();
    await page.waitForFunction(() =>
      document
        .querySelector("[data-work=persons] .work-task-progress[data-phase=publish]")
        .textContent.includes("已更新"),
    );
    await page
      .locator(".segment-card")
      .first()
      .getByRole("button", { name: "详情", exact: true })
      .click();
    await page.locator("#analysis-content").getByText("LLM 分析结果", { exact: true }).waitFor();
    await page.locator("#analysis-close").click();
    await page.screenshot({ path: "/tmp/mindex-upload-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: "/tmp/mindex-upload-mobile.png", fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.reload();
    await page.locator(".work-panel").waitFor();
    if (!(await page.locator(".work-panel").evaluate((element) => element.open)))
      await page.locator(".work-toolbar h3").click();
    await page.locator(".work-card").last().waitFor();
    assert.equal(await page.locator(".work-card").count(), 4);
    assert.deepEqual(errors, []);
    const search = await page.request.post(base + "/api/search/text", {
      data: { text: "绿色画面", source_id: sid },
    });
    assert.equal(search.status(), 200);
    assert.ok((await search.json()).results.length);
    console.log(
      "PASS: sequential upload with failure and duplicate; four tasks; prerequisites; subtitles/ASR; person setup; publishing; retrieval; details; reload; desktop/mobile; no page errors.",
    );
  } catch (error) {
    if (page) {
      console.error(await page.locator("body").innerText());
      await page.screenshot({ path: "/tmp/mindex-upload-failure.png", fullPage: true });
    }
    throw error;
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
