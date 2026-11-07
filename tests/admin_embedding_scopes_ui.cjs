// Run only against serve_upload_fixture.py with temporary data and fixture models.
// 两组嵌入发布互相独立：更新shot/moment嵌入（scope=shot_moment）与 更新scene场景嵌入（scope=scene）。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_EMBEDDING_SCOPES_URL || "http://127.0.0.1:18439";
const video =
  process.env.MINDEX_EMBEDDING_SCOPES_VIDEO || "/tmp/mindex-embedding-scopes/工程样本.mp4";
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } }),
      errors = [],
      requests = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("request", (r) => {
      if (r.url().endsWith("/publish") && r.method() === "POST") requests.push(r.postDataJSON());
    });
    const response = await page.request.post(base + "/api/admin/sources/upload", {
      multipart: {
        file: {
          name: "embedding-scopes.mp4",
          mimeType: "video/mp4",
          buffer: fs.readFileSync(video),
        },
      },
    });
    assert.ok(response.ok(), await response.text());
    const sid = (await response.json()).source_id;
    await page.goto(base + "/admin?source=" + sid);
    const card = (name) => page.locator(`[data-work=${name}]`);
    // 工作面板默认收起，卡片不展开就不渲染；每次 reload 后都要重新展开。
    const expandWork = async () => {
      const panel = page.locator("details.work-panel");
      if (!(await panel.evaluate((node) => node.open))) await panel.locator("summary").click();
      await page.locator("[data-work=shots]").waitFor();
    };
    await expandWork();
    // 页面重载会重置内存里的勾选状态，因此每次处理前都重新取消「完成后更新检索」，
    // 全部发布都由按钮手动触发，POST 序列才可断言；每步之间 reload 避开轮询重渲染。
    const settle = (name) =>
      page.waitForFunction(
        async ({ sid, name }) => {
          const work = (await (await fetch(`/api/admin/sources/${sid}`)).json()).work[name];
          return work.task?.status === "complete";
        },
        { sid, name },
        { timeout: 120000 },
      );
    const waitPublish = (name, field) =>
      page.waitForFunction(
        async ({ sid, name, field }) => {
          const work = (await (await fetch(`/api/admin/sources/${sid}`)).json()).work[name];
          return work[field]?.status === "complete";
        },
        { sid, name, field },
        { timeout: 120000 },
      );
    const process = async (name) => {
      await expandWork();
      await card(name).locator("label.check > input[type=checkbox]").uncheck();
      await card(name).getByRole("button", { name: "开始处理", exact: true }).click();
      await settle(name);
      await page.reload();
    };
    // 工作卡片每 2 秒轮询重渲染，动作性点击会被反复 detach；页面内等按钮可用后直接派发 click。
    const clickButton = async (name, label) => {
      await card(name)
        .getByRole("button", { name: label, exact: true })
        .waitFor({ timeout: 15000 });
      await page.waitForFunction(
        async ({ name, label }) => {
          const scope = document.querySelector(`[data-work=${name}]`);
          const btn = [...scope.querySelectorAll("button")].find(
            (b) => b.textContent.trim() === label,
          );
          return btn && !btn.disabled;
        },
        { name, label },
        { timeout: 15000 },
      );
      for (let attempt = 0; attempt < 5; attempt++) {
        try {
          await card(name)
            .getByRole("button", { name: label, exact: true })
            .evaluate((el) => el.click());
          return;
        } catch (e) {
          if (attempt === 4) throw e;
        }
      }
    };
    const publish = async (name, label, field, scope) => {
      await expandWork();
      // 页面队列偶发吞掉派发的 click；确认 POST 真正发出，未发出则重试点击。
      for (let attempt = 0; attempt < 3; attempt++) {
        await clickButton(name, label);
        try {
          const response = await page.waitForResponse(
            (r) => {
              if (!r.url().endsWith("/publish") || r.request().method() !== "POST") return false;
              try {
                const body = r.request().postDataJSON();
                return body && body.work === name && body.scope === scope;
              } catch {
                return false;
              }
            },
            { timeout: 10000 },
          );
          assert.ok(response.ok(), `${name}/${scope} 发布请求被拒绝: ${response.status()}`);
          break;
        } catch (e) {
          if (attempt === 2) throw new Error(`发布 POST 未发出: ${name} ${label}`);
        }
      }
      await waitPublish(name, field);
      await page.reload();
    };
    await process("shots");
    await process("analysis");
    // scene 覆盖依赖分析产出的场景段，因此 scene 按钮在分析完成后才可用。
    await publish("shots", "更新shot/moment嵌入", "publish_task", "shot_moment");
    await publish("shots", "更新scene场景嵌入", "scene_publish_task", "scene");
    await publish("analysis", "更新shot/moment嵌入", "publish_task", "shot_moment");
    await publish("analysis", "更新scene场景嵌入", "scene_publish_task", "scene");
    await expandWork();
    await card("analysis")
      .getByRole("button", { name: "更新scene场景嵌入", exact: true })
      .waitFor();
    console.log("captured publish POSTs:", JSON.stringify(requests));
    assert.deepEqual(requests, [
      { work: "shots", scope: "shot_moment" },
      { work: "shots", scope: "scene" },
      { work: "analysis", scope: "shot_moment" },
      { work: "analysis", scope: "scene" },
    ]);
    // 对白卡也有两个发布按钮；人物卡没有。
    assert.equal(
      await card("dialogue")
        .getByRole("button", { name: "更新shot/moment嵌入", exact: true })
        .count(),
      1,
    );
    assert.equal(
      await card("dialogue")
        .getByRole("button", { name: "更新scene场景嵌入", exact: true })
        .count(),
      1,
    );
    assert.equal(
      await card("persons")
        .getByRole("button", { name: "更新shot/moment嵌入", exact: true })
        .count(),
      0,
    );
    assert.equal(
      await card("persons").getByRole("button", { name: "更新scene场景嵌入", exact: true }).count(),
      0,
    );
    const work = (await (await page.request.get(base + `/api/admin/sources/${sid}`)).json()).work;
    assert.ok(work.analysis.search.current);
    assert.ok(work.analysis.scene_search.current);
    assert.ok(work.analysis.scene_search.indexed_segments > 0);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: "/tmp/mindex-embedding-scopes-ui.png", fullPage: true });
    console.log("Embedding scopes UI passed: independent buttons, scopes, coverage and tasks.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
