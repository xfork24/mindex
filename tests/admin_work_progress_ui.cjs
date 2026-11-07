// 临时素材库：四项真实任务及独立发布；受控状态另行验证运行中、等待及失败渲染。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_UPLOAD_TEST_URL || "http://127.0.0.1:8018";
const fixture = process.env.MINDEX_UPLOAD_TEST_VIDEO;
(async () => {
  assert.ok(fixture);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } }),
      errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const response = await page.request.post(base + "/api/admin/sources/upload", {
      multipart: {
        file: { name: "工程样本.mp4", mimeType: "video/mp4", buffer: fs.readFileSync(fixture) },
      },
    });
    assert.ok(response.ok(), await response.text());
    const sid = (await response.json()).source_id;
    const source = async () =>
      await (await page.request.get(base + `/api/admin/sources/${sid}`)).json();
    await page.goto(base + `/admin?source=${sid}`);
    const card = (name) => page.locator(`[data-work=${name}]`);
    const complete = async (name, type = "task") => {
      await page.waitForFunction(
        async ({ sid, name, type }) => {
          const s = await (await fetch(`/api/admin/sources/${sid}`)).json();
          return s.work[name][type]?.status === "complete";
        },
        { sid, name, type },
        { timeout: 60000, polling: 500 },
      );
      await page.waitForFunction(
        ({ name, type }) => {
          const row = document.querySelector(
            `[data-work=${name}] .work-task-progress[data-phase=publish]`,
          );
          return row?.textContent.includes("完成") && row.textContent.includes("检索已更新");
        },
        { name, type },
        { timeout: 60000 },
      );
      const task = (await source()).work[name][type];
      assert.equal(task.status, "complete", name + " " + type);
      assert.equal(task.publication.status, "complete");
      assert.equal(task.progress.phase, "publish");
      assert.equal(task.progress.completed, task.progress.total);
      assert.ok(task.progress.total > 0);
      const bar = card(name).locator(".work-progress[data-phase=publish]");
      assert.equal(await bar.getAttribute("max"), String(task.progress.total));
      assert.equal(await bar.getAttribute("value"), String(task.progress.completed));
      return task;
    };
    if (!(await source()).work.shots.task) {
      await card("shots").getByRole("button", { name: "开始处理", exact: true }).click();
    }
    await complete("shots");
    await card("dialogue").getByLabel("对白处理方式").selectOption("subtitle");
    await card("dialogue")
      .getByLabel("上传字幕文件", { exact: true })
      .setInputFiles({
        name: "进度.srt",
        mimeType: "text/plain",
        buffer: Buffer.from("1\n00:00:00,000 --> 00:00:05,900\n工程字幕对白\n"),
      });
    await card("dialogue")
      .getByLabel("对白字幕轨道")
      .locator("option")
      .filter({ hasText: "进度.srt" })
      .waitFor({ state: "attached" });
    await card("dialogue").getByRole("button", { name: "开始处理", exact: true }).click();
    const dialogue = await complete("dialogue");
    assert.equal(dialogue.progress.processing_progress.unit, "files");
    assert.equal(dialogue.progress.processing_progress.completed, 1);
    await card("analysis").getByRole("button", { name: "开始处理", exact: true }).click();
    await card("analysis").locator(".work-progress[data-phase=process]").waitFor();
    const analysis = await complete("analysis");
    assert.equal(analysis.progress.processing_progress.unit, "scenes");
    const entity = async (data) => {
      const r = await page.request.post(base + "/api/persons/entities", { data });
      assert.ok(r.ok(), await r.text());
      return r.json();
    };
    const work = await entity({ kind: "work", name: "进度验收作品" }),
      actor = await entity({ kind: "actor", name: "进度验收演员" });
    await entity({
      kind: "role",
      name: "进度验收角色",
      work_ids: [work.id],
      actor_ids: [actor.id],
    });
    const ref = await page.request.post(base + "/api/persons/references", {
      multipart: {
        actor_id: actor.id,
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
    await card("persons").getByRole("button", { name: "配置并处理" }).click();
    await page.locator("#person-work").selectOption(work.id);
    await page.waitForFunction(() => !document.querySelector("#person-start").disabled);
    await page.locator("#person-start").click();
    const persons = await complete("persons");
    assert.equal(persons.progress.processing_progress.unit, "shots");
    for (const name of ["shots", "dialogue", "analysis", "persons"]) {
      const prior = (await source()).work[name].publish_task?.id;
      await card(name).getByRole("button", { name: "更新检索", exact: true }).click();
      await page.waitForFunction(
        async ({ sid, name, prior }) => {
          const s = await (await fetch(`/api/admin/sources/${sid}`)).json();
          return (
            s.work[name].publish_task?.id &&
            s.work[name].publish_task.id !== prior &&
            s.work[name].publish_task.status === "complete"
          );
        },
        { sid, name, prior },
        { timeout: 60000, polling: 500 },
      );
      await complete(name, "publish_task");
      assert.equal(await card(name).locator(".work-task-progress[data-phase=publish]").count(), 1);
      assert.equal(await card(name).locator(".work-progress[data-phase=process]").count(), 1);
    }
    await page.reload();
    await card("persons").waitFor();
    for (const name of ["shots", "dialogue", "analysis", "persons"])
      await complete(name, "publish_task");
    await page.screenshot({ path: "/tmp/mindex-work-progress-desktop.png", fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: "/tmp/mindex-work-progress-mobile.png", fullPage: true });
    // Controlled snapshots exercise long-running states without sleeping models
    // or changing the real source/task rows.
    const saved = await source();
    const show = async (states) =>
      page.evaluate(
        ({ saved, states }) => {
          const s = structuredClone(saved);
          for (const [name, task] of Object.entries(states)) {
            s.work[name].task = task;
            s.work[name].publish_task = null;
          }
          const target = document.createElement("div");
          document.querySelector(".work-panel").replaceWith(target);
          renderWork(s, target);
          clearTimeout(workPoll);
        },
        { saved, states },
      );
    const task = (progress, status = "running") => ({
      status,
      progress,
      created_at: "2026-10-18 00:00:00",
    });
    await show({
      shots: task({ stage: "split", completed: 15, total: 405, unit: "shots" }),
      dialogue: task({ stage: "asr", completed_chunks: 2, total_chunks: 8 }),
      analysis: task({ stage: "scene_grouping", completed: 12, total: 32, unit: "segments" }),
      persons: task({ stage: "matching", completed_shots: 5, total_shots: 20 }),
    });
    for (const [name, done, total] of [
      ["shots", 15, 405],
      ["dialogue", 2, 8],
      ["analysis", 12, 32],
      ["persons", 5, 20],
    ]) {
      assert.ok(
        (
          await card(name).locator(".work-task-progress[data-phase=process]").textContent()
        ).includes(`${done}/${total}`),
      );
      assert.equal(
        await card(name).locator(".work-progress[data-phase=process]").getAttribute("value"),
        String(done),
      );
    }
    await show(
      Object.fromEntries(
        ["shots", "dialogue", "analysis", "persons"].map((name) => [
          name,
          task({
            stage: name === "persons" ? "person_index" : "embedding",
            phase: "publish",
            completed: 15,
            total: 405,
            unit: name === "persons" ? "shots" : "segments",
            processing_progress: { stage: "scenes", completed: 3, total: 3, unit: "scenes" },
          }),
        ]),
      ),
    );
    for (const name of ["shots", "dialogue", "analysis", "persons"]) {
      assert.equal(await card(name).locator(".work-progress").count(), 2);
      assert.equal(
        await card(name).locator(".work-progress[data-phase=publish]").getAttribute("value"),
        "15",
      );
    }
    await show({
      shots: task({ stage: "detect" }),
      dialogue: task({ stage: "subtitle", completed: 0, total: 1, unit: "tracks" }),
      analysis: task({ stage: "index_wait", phase: "publish" }),
      persons: task({ stage: "matching", completed_shots: 5, total_shots: 20 }, "failed"),
    });
    assert.equal(await card("shots").locator("progress").getAttribute("value"), null);
    assert.equal(
      await card("analysis").locator("progress[data-phase=publish]").getAttribute("value"),
      null,
    );
    assert.ok(
      (
        await card("analysis").locator(".work-task-progress[data-phase=publish]").textContent()
      ).includes("等待检索更新"),
    );
    assert.equal(await card("dialogue").locator("progress").getAttribute("max"), "1");
    assert.equal(await card("persons").locator("progress").getAttribute("value"), "5");
    assert.deepEqual(errors, []);
    console.log(
      "PASS: all four real processing tasks + four independent updates; durable separate phase bars; reload; desktop/mobile; controlled running counts, lock wait and failure; no page errors.",
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
