// 选题挖掘页（/studio/topics）浏览器回归：由 tests/test_topics_ui.py 启动隔离服务（脚本化模型、临时数据目录）。
// MINDEX_TOPICS_URL 为服务地址；MINDEX_TOPICS_SCENARIO 选择场景（逗号分隔，缺省全部）。
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");

const base = process.env.MINDEX_TOPICS_URL;
const wanted = new Set((process.env.MINDEX_TOPICS_SCENARIO || "").split(",").filter(Boolean));
const scenarios = [];
const scenario = (name, run) => scenarios.push({ name, run });

async function createProfile(page, { name, domain, taboos = [] }) {
  await page.locator(await page.locator("#profile-new").isVisible() ? "#profile-new" : "#profile-new-empty").click();
  assert.equal(await page.locator("#pf-language,#pf-region").count(), 0, "领域档案不提供语言地区限制");
  await page.fill("#pf-name", name);
  await page.fill("#pf-domain", domain);
  if (taboos.length) await page.fill("#pf-taboos", taboos.join("\n"));
  await page.click("#pf-save");
  await page.waitForFunction((n) => [...document.querySelectorAll("#profile-select option")]
    .some((o) => o.selected && o.textContent.startsWith(n)), name);
}

scenario("material_scope", async (page) => {
  await page.goto(`${base}/studio/topics`);
  await createProfile(page, { name: "范围测试", domain: "科技" });
  const groupSelect = page.locator("#topic-material-scope .group-picker select");
  const sources = page.locator("#topic-material-sources");
  await page.waitForFunction(() => document.querySelector("#topic-material-sources").options.length === 2);
  const response = await page.request.get(`${base}/api/admin/groups?type=source`);
  const groups = (await response.json()).items;
  const chosen = groups.find((g) => g.name === "选题素材组").id;
  const empty = groups.find((g) => g.name === "空素材组").id;
  await groupSelect.selectOption(chosen);
  await page.waitForFunction(() => document.querySelector("#topic-material-sources").options.length === 1);
  assert.deepEqual(await sources.evaluate((node) => [...node.options].map((o) => o.value)), ["material-a"]);
  await page.click("#topic-material-save");
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("素材范围已保存"));
  await page.locator("#topic-material-current-members summary").filter({hasText: "1"}).waitFor();
  assert.match(await page.locator("#topic-material-current").textContent(), /选题素材组/);
  await page.locator("#topic-material-current-members summary").click();
  assert.deepEqual(await page.locator("#topic-material-current li").allTextContents(), ["material-a"]);
  await page.reload();
  await page.waitForFunction((id) => document.querySelector("#topic-material-scope .group-picker select")?.selectedOptions[0]?.value === id, chosen);
  // 恢复流程先选中组、再异步 resolve 回填素材列表；等列表就绪后再断言，避免竞态。
  await page.waitForFunction(() => document.querySelectorAll("#topic-material-sources option").length === 1);
  assert.deepEqual(await sources.evaluate((node) => [...node.options].map((o) => o.value)), ["material-a"]);
  await page.locator("#topic-material-current-members summary").filter({hasText: "1"}).waitFor();
  assert.match(await page.locator("#topic-material-current").textContent(), /选题素材组/);
  await groupSelect.selectOption(empty);
  await page.waitForFunction(() => document.querySelector("#topic-material-sources").disabled && document.querySelector("#topic-material-sources").options.length === 0);
  assert.equal(await sources.locator("option").count(), 0);
  await page.click("#topic-material-save");
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("素材范围已保存"));
  await page.locator("#topic-material-current-members summary").filter({hasText: "0"}).waitFor();
  assert.match(await page.locator("#topic-material-current").textContent(), /空素材组/);
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({width, height: 900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.doesNotMatch(await page.locator("#topic-material-scope").textContent(), /[\uE000\uE001]/);
  }
  await page.click("[data-language-toggle]");
  assert.match(await page.textContent("#topic-material-save"), /Save material scope/);
  assert.match(await page.locator("#topic-material-current h3").textContent(), /Currently saved material scope/);
});

scenario("profiles", async (page) => {
  await page.goto(`${base}/studio/topics`);
  // 标签顺序：创作任务 → 选题挖掘 → 文案模板 → 风格模板 → 交付模板，当前页高亮
  const tabs = await page.$$eval(".studio-tabs a", (links) => links.map((a) => [a.getAttribute("href"), a.getAttribute("aria-current")]));
  assert.deepEqual(tabs, [["/studio", null], ["/studio/topics", "page"], ["/studio/templates", null], ["/studio/brands", null], ["/studio/delivery-templates", null]]);
  await page.locator("#topics-empty").waitFor();
  assert.equal(await page.isVisible("#topics-bar"), false);
  await createProfile(page, { name: "财经号", domain: "金融", taboos: ["不荐股", "不预测涨跌"] });
  assert.ok(await page.isVisible("#topics-workspace"));
  assert.match(await page.textContent("#profile-summary"), /未开启每日定时/);
  // 编辑：权重和不为 1 时拒绝，改正后保存；定时摘要更新
  await page.click("#profile-edit");
  assert.equal(await page.inputValue("#pf-taboos"), "不荐股\n不预测涨跌");
  await page.fill("#pf-w-timeliness", "0.5");
  assert.match(await page.textContent("#pf-weights-sum"), /1\.25/);
  await page.click("#pf-save");
  assert.match(await page.textContent("#pf-error"), /权重之和必须为 1/);
  await page.fill("#pf-w-timeliness", "0.25");
  await page.check("#pf-s-enabled");
  await page.fill("#pf-s-time", "07:30");
  await page.click("#pf-save");
  await page.waitForFunction(() => /07:30/.test(document.querySelector("#profile-summary").textContent));
  await page.locator("#schedule-status", { hasText: "没有运行定时调度" }).waitFor();  // 测试服务不启动调度器
  assert.doesNotMatch(await page.textContent("#schedule-status"), /后台运行中/);  // Web 端没有 MindexDesktop
  // 第二份档案，切换后刷新页面仍保持选择（localStorage 与地址栏）
  await createProfile(page, { name: "历史号", domain: "历史" });
  const options = await page.$$eval("#profile-select option", (o) => o.map((x) => x.textContent));
  assert.deepEqual(options, ["财经号 · 金融", "历史号 · 历史"]);
  await page.selectOption("#profile-select", { label: "财经号 · 金融" });
  assert.match(page.url(), /profile=tp_/);
  await page.goto(`${base}/studio/topics`);
  await page.waitForFunction(() => document.querySelector("#profile-select").selectedOptions[0]?.textContent === "财经号 · 金融");
  // 语言切换：静态文案切到英文，档案名（用户内容）不变
  await page.click("[data-language-toggle]");
  assert.equal(await page.textContent("#studio-topics-tab, .studio-tab[aria-current=page]"), "Topic discovery");
  assert.equal(await page.$eval("#profile-select", (s) => s.selectedOptions[0].textContent), "财经号 · 金融");
  await page.click("[data-language-toggle]");
  // 删除当前档案后切到剩下的一份
  page.once("dialog", (dialog) => dialog.accept());
  await page.click("#profile-delete");
  await page.waitForFunction(() => document.querySelector("#profile-select").options.length === 1);
  assert.equal(await page.$eval("#profile-select", (s) => s.selectedOptions[0].textContent), "历史号 · 历史");
});

scenario("sources", async (page) => {
  await page.goto(`${base}/studio/topics`);
  await createProfile(page, { name: "财经号", domain: "金融" });
  await page.locator("#sources-section[open]").waitFor();  // 新档案没有数据源时默认展开
  assert.match(await page.textContent("#source-list"), /还没有数据源/);
  const kinds = await page.$$eval("#sf-kind option", (o) => o.map((x) => x.value));
  assert.equal(kinds[0], "rss");
  assert.ok(kinds.includes("local"));
  assert.ok(await page.isVisible("#sf-third-party"));
  await page.fill("#sf-name", "财经要闻");
  await page.fill("#sf-field-url", "https://news.example.com/feed.xml");
  await page.click("#sf-save");
  await page.locator(".source-row", { hasText: "财经要闻" }).waitFor();
  await page.fill("#sf-name", "坏地址");
  await page.fill("#sf-field-url", "https://down.example.com/");
  await page.click("#sf-save");
  await page.locator(".source-row", { hasText: "坏地址" }).waitFor();
  // 本地素材库：没有网络请求，不显示第三方提示
  await page.selectOption("#sf-kind", "local");
  assert.equal(await page.isVisible("#sf-third-party"), false);
  assert.ok(await page.isChecked("#sf-field-library"));
  await page.fill("#sf-name", "本地素材库");
  await page.click("#sf-save");
  await page.locator(".source-row", { hasText: "本地素材库" }).waitFor();
  assert.match(await page.textContent("#sources-summary"), /3 个，3 个启用/);
  // 试抓：显示前几条，不入库
  await page.locator(".source-row", { hasText: "财经要闻" }).getByRole("button", { name: "试抓" }).click();
  await page.locator("#source-test", { hasText: "试抓成功，共 2 条" }).waitFor();
  assert.match(await page.textContent("#source-test"), /央行宣布降息/);
  // 立即抓取：失败来源只记错误
  await page.click("#items-section > summary");
  await page.click("#fetch-run");
  await page.locator("#fetch-status", { hasText: "1 个来源失败" }).waitFor();
  assert.equal(await page.locator("#item-list .item-row").count(), 2);
  assert.match(await page.locator(".source-row", { hasText: "坏地址" }).textContent(), /HTTP 404/);
  const link = page.locator("#item-list a.item-title").first();
  assert.equal(await link.getAttribute("target"), "_blank");
  assert.match(await link.getAttribute("rel"), /noopener/);
  // 热榜：DailyHotApi 预设只显示服务地址与榜单；Brave 需要密钥，保存后只显示掩码
  await page.selectOption("#sf-kind", "hotlist");
  assert.ok(await page.isVisible("#sf-field-url"));
  assert.equal(await page.isVisible("#sf-field-base_url"), false);
  await page.selectOption("#sf-field-preset", "dailyhot");
  assert.equal(await page.isVisible("#sf-field-url"), false);
  assert.equal(await page.inputValue("#sf-field-boards"), "weibo\nzhihu\n36kr");
  await page.fill("#sf-field-base_url", "http://hot.local:6688");
  await page.fill("#sf-field-boards", "weibo");
  await page.fill("#sf-name", "今日热榜");
  await page.click("#sf-save");
  const hot = page.locator(".source-row", { hasText: "今日热榜" });
  await hot.waitFor();
  assert.match(await hot.textContent(), /http:\/\/hot\.local:6688 · weibo/);
  await hot.getByRole("button", { name: "试抓" }).click();
  await page.locator("#source-test", { hasText: "试抓成功，共 1 条" }).waitFor();
  await page.selectOption("#sf-kind", "brave");
  assert.ok(await page.isVisible("#sf-secret"));
  await page.fill("#sf-name", "Brave");
  await page.click("#sf-save");
  await page.locator("#sf-error", { hasText: "API Key" }).waitFor();
  await page.fill("#sf-secret", "brave-key-123456");
  await page.click("#sf-save");
  const brave = page.locator(".source-row", { hasText: "Brave" });
  await brave.waitFor();
  await brave.getByRole("button", { name: "编辑" }).click();
  assert.match(await page.getAttribute("#sf-secret", "placeholder"), /••••3456/);
  await page.click("#sf-cancel");
  page.once("dialog", (dialog) => dialog.accept());
  await brave.getByRole("button", { name: "删除" }).click();
  await page.waitForFunction(() => ![...document.querySelectorAll(".source-row")].some((r) => r.textContent.includes("Brave")));
  page.once("dialog", (dialog) => dialog.accept());
  await hot.getByRole("button", { name: "删除" }).click();
  await page.waitForFunction(() => ![...document.querySelectorAll(".source-row")].some((r) => r.textContent.includes("今日热榜")));
  // 日历：导入内置样例后显示事件数
  assert.equal(await page.isVisible("#calendar-import"), false);
  await page.selectOption("#sf-kind", "calendar");
  await page.fill("#sf-name", "纪念日");
  await page.click("#sf-save");
  const cal = page.locator(".source-row", { hasText: "纪念日" });
  await cal.waitFor();
  assert.match(await cal.textContent(), /已导入 0 个事件/);
  await page.locator("#calendar-import:not([hidden])").waitFor();
  await page.waitForFunction(() => document.querySelectorAll("#calendar-sample option").length === 4);
  await page.click("#calendar-run");
  assert.match(await page.textContent("#calendar-status"), /请选择文件或样例/);
  await page.selectOption("#calendar-sample", "history");
  await page.click("#calendar-run");
  await page.locator("#calendar-status", { hasText: "已导入 12 个事件" }).waitFor();
  await page.locator(".source-row", { hasText: "已导入 12 个事件" }).waitFor();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator(".source-row", { hasText: "纪念日" }).getByRole("button", { name: "删除" }).click();
  await page.locator("#calendar-import[hidden]").waitFor({ state: "attached" });
  await page.selectOption("#sf-kind", "rss");
  // 编辑、停用、删除
  await page.locator(".source-row", { hasText: "坏地址" }).getByRole("button", { name: "编辑" }).click();
  assert.ok(await page.isDisabled("#sf-kind"));
  assert.equal(await page.inputValue("#sf-field-url"), "https://down.example.com/");
  await page.fill("#sf-name", "备用地址");
  await page.click("#sf-save");
  await page.locator(".source-row", { hasText: "备用地址" }).waitFor();
  await page.locator(".source-row", { hasText: "备用地址" }).getByRole("button", { name: "停用" }).click();
  await page.locator(".source-row.is-disabled", { hasText: "备用地址" }).waitFor();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator(".source-row", { hasText: "备用地址" }).getByRole("button", { name: "删除" }).click();
  await page.waitForFunction(() => document.querySelectorAll(".source-row").length === 2);
});

scenario("cards", async (page) => {
  await page.goto(`${base}/studio/topics`);
  await createProfile(page, { name: "财经号", domain: "金融", taboos: ["不荐股"] });
  await page.fill("#sf-name", "要闻");
  await page.fill("#sf-field-url", "https://news.example.com/feed.xml");
  await page.click("#sf-save");
  await page.locator(".source-row", { hasText: "要闻" }).waitFor();
  assert.match(await page.textContent("#run-status"), /还没有挖掘过/);
  await page.click("#run-start");
  await page.locator("#run-status", { hasText: "最近一次挖掘" }).waitFor({ timeout: 60000 });
  assert.match(await page.textContent("#run-status"), /产出 2 个选题，2 个已写大纲/);
  await page.waitForFunction(() => document.querySelectorAll("#card-list .topic-item").length === 2);
  const scores = await page.$$eval("#card-list .topic-score-badge", (nodes) => nodes.map((n) => Number(n.textContent)));
  assert.deepEqual(scores, [...scores].sort((a, b) => b - a));
  await page.locator("#card-list .topic-item").first().click();
  await page.locator("#card-view:not([hidden])").waitFor();
  assert.match(page.url(), /card=tc_/);
  assert.equal(await page.locator("#card-scores .topic-score").count(), 5);
  assert.match(await page.textContent("#coverage-summary"), /关键画面命中 2\/3/);
  assert.equal(await page.locator(".coverage-row.is-gap").count(), 1);
  assert.match(await page.textContent(".coverage-row.is-gap"), /紫色天空/);
  const clip = page.locator(".coverage-row.is-hit .coverage-clip").first();
  assert.match(await clip.getAttribute("href"), /^\/admin\?source=.+&segment=.+/);
  await page.waitForFunction(() => [...document.querySelectorAll(".coverage-clip img")].every((img) => img.complete && img.naturalWidth > 0));
  assert.match(await page.inputValue("#card-outline"), /^开场：/);
  assert.equal(await page.inputValue("#card-template"), "hot");
  // 素材采集清单：两张卡片都缺的画面合并计数；导出 CSV；标记已采集
  await page.click("#gaps-section > summary");
  const gap = page.locator(".gap-row", { hasText: "紫色天空" });
  await gap.waitFor();
  assert.match(await gap.textContent(), /出现 2 次/);
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#gaps-export")]);
  const csv = require("node:fs").readFileSync(await download.path(), "utf8");
  assert.ok(csv.startsWith("\ufeff画面,出现次数"), csv.slice(0, 40));
  assert.match(csv, /紫色天空,2,待采集/);
  await gap.getByRole("button", { name: "标记已采集" }).click();
  await page.waitForFunction(() => !document.querySelector(".gap-row"));
  await page.selectOption("#gaps-status", "done");
  await page.locator(".gap-row.is-done", { hasText: "紫色天空" }).waitFor();
  assert.ok((await page.locator("#card-items .item-row").count()) >= 1);
  // 编辑大纲与模板：显示未保存，保存后消失
  await page.fill("#card-outline", "开场：自己写的大纲。");
  await page.selectOption("#card-template", "science");
  assert.ok(await page.isVisible("#card-dirty"));
  await page.click("#card-save");
  await page.locator("#card-dirty").waitFor({ state: "hidden" });
  await page.reload();
  await page.locator("#card-view:not([hidden])").waitFor();
  assert.equal(await page.inputValue("#card-outline"), "开场：自己写的大纲。");
  assert.equal(await page.inputValue("#card-template"), "science");
  // 重新生成大纲（确认覆盖）、重新打分
  page.once("dialog", (dialog) => dialog.accept());
  await page.click("#card-outline-run");
  await page.waitForFunction(() => document.querySelector("#card-outline").value.startsWith("开场：选题"));
  await page.click("#card-score");
  await page.locator("#notice", { hasText: "已重新打分" }).waitFor();
  // 忽略后从新选题列表消失，在已忽略里能恢复
  const title = await page.textContent("#card-title");
  await page.click("#card-dismiss");
  await page.waitForFunction(() => document.querySelectorAll("#card-list .topic-item").length === 1);
  await page.selectOption("#card-status", "dismissed");
  await page.locator("#card-list .topic-item", { hasText: title }).click();
  assert.equal(await page.textContent("#card-dismiss"), "恢复");
  await page.click("#card-dismiss");
  await page.waitForFunction(() => document.querySelectorAll("#card-list .topic-item").length === 0);
  // 再挖一次没有新条目：失败原因显示在任务条上
  await page.check("#run-skip-fetch");
  await page.click("#run-start");
  await page.locator("#run-status", { hasText: "没有可用的候选条目" }).waitFor({ timeout: 30000 });
  assert.ok(await page.isVisible("#run-retry"));
});

scenario("job", async (page) => {
  await page.goto(`${base}/studio/topics`);
  await createProfile(page, { name: "财经号", domain: "金融", taboos: ["不荐股"] });
  await page.fill("#sf-name", "要闻");
  await page.fill("#sf-field-url", "https://news.example.com/feed.xml");
  await page.click("#sf-save");
  await page.locator(".source-row", { hasText: "要闻" }).waitFor();
  await page.click("#run-start");
  await page.locator("#run-status", { hasText: "最近一次挖掘" }).waitFor({ timeout: 60000 });
  await page.locator("#card-list .topic-item").first().click();
  await page.locator("#card-view:not([hidden])").waitFor();
  const title = await page.textContent("#card-title");
  assert.match(await page.textContent("#card-job-note"), /参考文稿/);
  await Promise.all([page.waitForURL(/\/studio\?job=/), page.click("#card-job")]);
  const job = new URL(page.url()).searchParams.get("job");
  await page.locator("#job-topic:not([hidden])").waitFor();
  await page.locator("#job-topic", { hasText: "档案：财经号" }).waitFor();
  assert.match(await page.textContent("#job-topic"), new RegExp(title.slice(0, 6)));
  assert.equal(await page.inputValue("#script-template"), "hot");
  assert.match(await page.textContent("#script-references"), /选题来源资料/);
  assert.match(await page.getAttribute("#studio-topics-tab", "href"), new RegExp(`job=${job}`));
  // 回到选题：卡片已采纳，可打开任务
  await page.goto(`${base}/studio/topics`);
  await page.selectOption("#card-status", "adopted");
  await page.locator("#card-list .topic-item").first().click();
  await page.locator("#card-job-link:not([hidden])").waitFor();
  assert.equal(await page.getAttribute("#card-job-link", "href"), `/studio?job=${job}`);
  assert.equal(await page.textContent("#card-job"), "再建一份任务");
  // 改档案禁区 → 任务页提示"档案已更新"，同步后消失
  await page.click("#profile-edit");
  await page.fill("#pf-taboos", "不荐股\n不谈加密货币");
  await page.click("#pf-save");
  await page.locator("#notice", { hasText: "已保存领域档案" }).waitFor();
  await page.goto(`${base}/studio?job=${job}`);
  await page.locator("#job-topic", { hasText: "档案已更新" }).waitFor();
  await page.locator("#job-topic").getByRole("button", { name: "同步" }).click();
  await page.locator("#notice", { hasText: "已同步账号档案" }).waitFor();
  await page.waitForFunction(() => !/档案已更新/.test(document.querySelector("#job-topic").textContent));
});

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const item of scenarios) {
      if (wanted.size && !wanted.has(item.name)) continue;
      const context = await browser.newContext({ viewport: { width: 1360, height: 1000 } });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      try {
        await item.run(page);
      } catch (error) {
        await page.screenshot({ path: `/tmp/mindex-topics-${item.name}.png`, fullPage: true }).catch(() => {});
        throw error;
      }
      assert.deepEqual(errors, [], `${item.name}: 页面脚本错误`);
      await context.close();
      console.log(`topics ui ${item.name} ok`);
    }
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
