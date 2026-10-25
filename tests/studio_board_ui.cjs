// 分镜工作台（顶部分镜条 + 阶段浮层 + 下方阶段详情）真实浏览器回归。
// 由 tests/test_studio_board_ui.py 启动进程内隔离服务（三镜、已完整构建），经 MINDEX_STUDIO_URL / MINDEX_STUDIO_JOB 传入。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require(process.env.MINDEX_PLAYWRIGHT_MODULE || "playwright");
const base = process.env.MINDEX_STUDIO_URL, job = process.env.MINDEX_STUDIO_JOB;
assert.match(base || "", /^http:\/\/127\.0\.0\.1:/, "只允许本机隔离服务");
const out = process.env.MINDEX_BOARD_OUTPUT || "artifacts/studio-board";
fs.mkdirSync(out, { recursive: true });
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ headless: true });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(20000);
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(`${base}/studio?job=${job}`);
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    const cards = page.locator("#shot-strip .shot-card");
    await cards.nth(2).waitFor();
    assert.equal(await cards.count(), 3);
    assert.equal(await page.locator("table.shot-matrix").count(), 0, "表格已移除");
    assert.equal(await page.locator("#collapse-all-rows").count(), 0, "展开所有行已移除");
    // 没有打开阶段：显示提示，详情面板隐藏。
    assert.ok(await page.locator("#stage-placeholder").isVisible());
    assert.ok(await page.locator("#stage-panel").isHidden());

    // 卡片：状态点数量 = 可打开阶段数；完整构建后显示分镜渲染并嵌入静音、不自动播放的视频。
    const model = await page.evaluate(() => order().map(sid => ({ sid, stage: cardStage(sid), stages: barStages(sid) })));
    for (const [i, m] of model.entries()) {
      assert.equal(await cards.nth(i).locator(".stage-dot").count(), m.stages.length);
      assert.equal(m.stage, "render", `分镜 ${i + 1} 卡片应显示分镜渲染`);
    }
    const video = cards.first().locator(".card-media video");
    await video.waitFor();
    // 分镜渲染：带配音的预览视频（不静音、不自动播放），叠加该镜字幕，不单独显示音频。
    assert.deepEqual(await video.evaluate(v => [v.muted, v.autoplay, v.preload, v.controls]), [false, false, "metadata", true]);
    assert.match(await video.evaluate(v => v.currentSrc || v.src), /preview\.mp4/);
    assert.equal(await cards.first().locator(".card-media audio").count(), 0);
    // 字幕：叠加层文字与该镜时间线字幕一致；原生字幕轨隐藏（全屏时才显示原生字幕）。
    const subtitle = await page.evaluate(() => ver(plan().shots[order()[0]].render).data.event.subtitle);
    await video.evaluate((v, t) => { v.currentTime = Math.min(t, (v.duration || 1) - 0.05); }, (subtitle.start + subtitle.end) / 2);
    const caption = cards.first().locator(".render-subtitle");
    await caption.waitFor();
    assert.equal(await caption.innerText(), subtitle.text);
    assert.equal(await video.evaluate(v => v.textTracks[0].mode), "hidden");
    // 样本画面没有黑边：字幕底边与画面底边对齐（容差 2px），且不拦截点击。
    await page.waitForFunction(() => document.querySelector("#shot-strip .shot-card .card-media video")?.dataset.pictureBottom);
    const geometry = await video.evaluate(v => {
      const c = v.parentElement.querySelector(".render-subtitle"), vr = v.getBoundingClientRect(), cr = c.getBoundingClientRect();
      const scale = Math.min(vr.width / v.videoWidth, vr.height / v.videoHeight);
      return { picBottom: vr.top + (vr.height + v.videoHeight * scale) / 2, captionBottom: cr.bottom, events: getComputedStyle(c).pointerEvents };
    });
    assert.ok(Math.abs(geometry.picBottom - geometry.captionBottom) <= 2, JSON.stringify(geometry));
    assert.equal(geometry.events, "none");
    // 黑边检测：上下各 12.5% 黑边（2.39:1 素材 pad 进 16:9 的近似）→ 画面底边约在 87.5%；全黑帧检测不出。
    const bars = await page.evaluate(() => {
      const canvas = document.createElement("canvas");
      canvas.width = 320; canvas.height = 180;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#000"; ctx.fillRect(0, 0, 320, 180);
      ctx.fillStyle = "#3a6"; ctx.fillRect(0, 22.5, 320, 135);
      const letterbox = pictureBottom(canvas, 320, 180);
      ctx.fillStyle = "#000"; ctx.fillRect(0, 0, 320, 180);
      return { letterbox, dark: pictureBottom(canvas, 320, 180) };
    });
    assert.ok(Math.abs(bars.letterbox - 0.875) <= 0.03, JSON.stringify(bars));
    assert.equal(bars.dark, null);
    await page.screenshot({ path: `${out}/board-initial.png`, fullPage: false });

    // 悬停卡片（媒体区之外）120ms 后展开阶段浮层；详情区不变。
    const popover = page.locator("#stage-popover");
    await cards.nth(1).locator(".shot-card-head").hover();
    await popover.waitFor({ state: "visible" });
    assert.equal(await popover.locator("[role=menuitem]").count(), model[1].stages.length);
    assert.equal(await cards.nth(1).getAttribute("aria-expanded"), "true");
    assert.ok(await page.locator("#stage-panel").isHidden(), "悬停不打开详情");
    // 浮层贴在卡片下方，且不被分镜条裁剪（在视口中可见）。
    const cardBox = await cards.nth(1).boundingBox(), popBox = await popover.boundingBox();
    assert.ok(Math.abs(popBox.y - (cardBox.y + cardBox.height + 4)) <= 2, "浮层位于卡片正下方");
    await page.screenshot({ path: `${out}/board-popover.png` });
    // 移入浮层保持展开；离开两者后收起。
    await popover.locator("[role=menuitem]").first().hover();
    await sleep(400);
    assert.ok(await popover.isVisible(), "移入浮层保持展开");
    await page.mouse.move(5, 5);
    await popover.waitFor({ state: "hidden" });

    // 点击卡片空白处：直接打开卡片显示的阶段，浮层收起。
    await cards.first().locator(".shot-card-head").click();
    const heading = page.locator("#stage-panel h3");
    await heading.waitFor();
    assert.match(await heading.innerText(), /^分镜 1 · 分镜渲染/);
    assert.ok(await popover.isHidden());
    assert.equal(await cards.first().getAttribute("aria-current"), "true");
    assert.match(await cards.first().getAttribute("class"), /is-open/);
    // 详情区全宽：与分镜工作台同宽。
    const area = await page.locator("#shot-stage-area").boundingBox(), board = await page.locator("#storyboard-board").boundingBox();
    assert.ok(Math.abs(area.width - board.width) <= 2, "详情区铺满分镜条下方");

    // 悬停分镜 2，点击浮层中的「配音」：打开分镜 2 的配音。
    await cards.nth(1).locator(".shot-card-head").hover();
    await popover.waitFor({ state: "visible" });
    await popover.locator("[data-stage-option=tts]").click();
    await page.waitForFunction(() => document.querySelector("#stage-panel h3")?.textContent.startsWith("分镜 2 · 配音"));
    assert.ok(await popover.isHidden());
    await page.screenshot({ path: `${out}/board-tts.png` });

    // 点击卡片里的媒体：不打开详情，也不展开浮层。
    await page.mouse.move(5, 5);
    await cards.nth(2).locator(".card-media video").click();
    await sleep(400);
    assert.match(await heading.innerText(), /^分镜 2 · 配音/);
    assert.ok(await popover.isHidden(), "媒体区悬停不展开浮层");
    await page.evaluate(() => document.querySelectorAll("video,audio").forEach(v => v.pause()));

    // 关闭详情：回到提示。
    await page.locator("#stage-panel .detail-heading button").click();
    assert.ok(await page.locator("#stage-placeholder").isVisible());

    // 键盘：Tab 聚焦卡片展开浮层；下方向键进入列表；Enter 打开并把焦点移到详情；Esc 收起并还焦点。
    await page.mouse.move(5, 5);
    await page.locator("#batch-regenerate").focus();
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.dataset.shotId), model[0].sid);
    await popover.waitFor({ state: "visible" });
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute("role")), "menuitem");
    await page.keyboard.press("ArrowDown");
    const option = await page.evaluate(() => document.activeElement.dataset.stageOption);
    await page.keyboard.press("Enter");
    await page.waitForFunction(o => selection()?.stage === o, option);
    assert.equal(await page.evaluate(() => document.activeElement.id), "stage-panel");
    await page.locator("#batch-regenerate").focus();
    await page.keyboard.press("Tab");
    await popover.waitFor({ state: "visible" });
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Escape");
    await popover.waitFor({ state: "hidden" });
    assert.equal(await page.evaluate(() => document.activeElement.dataset.shotId), model[0].sid);
    // 卡片上 Enter 打开卡片显示的阶段。
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => selection()?.stage === "render" && selection()?.sid === order()[0]);
    // 详情区的分镜渲染同样只有带字幕的视频，没有音频播放器。
    await page.locator("#stage-panel video track").waitFor({ state: "attached" });
    assert.equal(await page.locator("#stage-panel audio").count(), 0);
    await page.locator("#stage-panel video").scrollIntoViewIfNeeded();
    await page.locator("#stage-panel video").evaluate(v => { v.currentTime = Math.min(0.5, (v.duration || 1) / 2); });
    await sleep(500);
    await page.locator("#stage-panel video").screenshot({ path: `${out}/render-subtitle.png` });

    // 选片原片区间：缩略图 + 播放按钮，点击后加载 source-playlist 并静音播放。
    const playlist = await page.evaluate(async () => {
      const sid = order()[0], box = document.createElement("div");
      box.className = "card-media";
      document.body.append(box);
      await cardMediaSpec(sid, "select").build(box);
      const play = box.querySelector(".card-play");
      const hasPoster = !!box.querySelector("img");
      play.click();
      const video = box.querySelector("video");
      await video.shotPlayer.ready;
      const result = { hasPoster, muted: video.muted, items: video.shotPlayer.items.length, src: video.getAttribute("src") || "" };
      document.querySelectorAll("video").forEach(v => v.pause());
      box.remove();
      return result;
    });
    assert.ok(playlist.hasPoster, "选片卡片显示缩略图");
    assert.ok(playlist.muted && playlist.items >= 1 && playlist.src.startsWith("/api/admin/shots/"), JSON.stringify(playlist));

    // 窄屏：分镜条可横向滚动（按钮、滚轮），浮层跟随卡片；任何宽度、两种语言都没有页面横向滚动。
    for (const lang of ["zh", "en"]) {
      await page.evaluate(l => MindexI18n.setLanguage(l), lang);
      for (const width of [1440, 1000, 800, 540, 390, 320]) {
        await page.setViewportSize({ width, height: 900 });
        await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
        assert.ok(overflow <= 1, `${lang} ${width}px 页面横向溢出 ${overflow}px`);
        const widths = await cards.evaluateAll(list => list.map(c => c.getBoundingClientRect().width));
        const strip = await page.locator("#shot-strip").evaluate(s => s.clientWidth);
        for (const w of widths) assert.ok(w >= Math.min(219, strip - 1), `${width}px 卡片宽 ${w}`);
        if (width === 540) await page.screenshot({ path: `${out}/board-540-${lang}.png` });
      }
    }
    await page.evaluate(() => MindexI18n.setLanguage("zh"));
    // 宽屏下把分镜条限制到约 1 张卡片宽，检查两端按钮（窄于 700px 时按钮隐藏，改用滑动与滚轮）。
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.locator(".shot-strip-wrap").evaluate(w => { w.style.width = "330px"; });
    await page.locator("#shot-strip").evaluate(s => { s.style.scrollBehavior = "auto"; s.scrollLeft = 0; });
    await page.evaluate(() => updateStripNav());
    await page.locator("#shot-strip").scrollIntoViewIfNeeded();
    const next = page.locator("#strip-next");
    assert.ok(await next.isVisible(), "分镜溢出时显示下一组按钮");
    const at = await page.evaluate(() => [document.getElementById("shot-strip").scrollLeft, document.getElementById("strip-prev").disabled]);
    assert.ok(await page.locator("#strip-prev").isDisabled(), "在起点时上一组按钮禁用 " + JSON.stringify(at));
    await next.click();
    await page.waitForFunction(() => document.getElementById("shot-strip").scrollLeft > 0);
    await page.locator("#shot-strip").evaluate(s => { s.scrollLeft = 0; });
    const stripBox = await page.locator("#shot-strip").boundingBox();
    await page.mouse.move(stripBox.x + stripBox.width / 2, stripBox.y + 20);
    await page.mouse.wheel(0, 150);
    await page.waitForFunction(() => document.getElementById("shot-strip").scrollLeft > 0);
    await page.locator("#shot-strip").evaluate(s => { s.scrollLeft = 0; });
    await cards.first().locator(".shot-card-head").hover();
    await popover.waitFor({ state: "visible" });
    await page.locator("#shot-strip").evaluate(s => { s.scrollLeft = 40; });
    await sleep(100);
    const moved = await page.evaluate(() => {
      const pop = document.getElementById("stage-popover"), card = document.querySelector(".shot-card");
      return { hidden: pop.hidden, pop: pop.getBoundingClientRect().left, card: card.getBoundingClientRect().left,
        strip: document.getElementById("shot-strip").getBoundingClientRect().left };
    });
    assert.ok(moved.hidden || Math.abs(moved.pop - Math.max(moved.card, moved.strip, 8)) <= 2, JSON.stringify(moved));
    await page.locator("#shot-strip").evaluate(s => { s.scrollLeft = s.scrollWidth; });
    await sleep(100);
    assert.ok(await popover.isHidden(), "卡片滚出可视区域后浮层收起");
    assert.ok(await page.locator("#strip-prev").isEnabled() && await next.isDisabled(), "滚到末尾：上一组可用、下一组禁用");
    await page.locator(".shot-strip-wrap").evaluate(w => { w.style.width = ""; });
    await page.setViewportSize({ width: 540, height: 900 });
    assert.ok(await next.isHidden(), "窄屏隐藏两端按钮");
    await page.close();

    // 触屏（hover:none）：「阶段 ▾」按钮可见；点按钮展开，点阶段打开；点卡片直接打开卡片显示的阶段。
    const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    const mobile = await touch.newPage();
    mobile.on("pageerror", e => errors.push(e.message));
    await mobile.goto(`${base}/studio?job=${job}`);
    await mobile.evaluate(() => MindexI18n.setLanguage("zh"));
    const first = mobile.locator("#shot-strip .shot-card").first();
    await first.waitFor();
    const menu = first.locator(".stage-menu-button");
    assert.equal(await menu.evaluate(b => getComputedStyle(b).opacity), "1");
    await menu.tap();
    const mobilePop = mobile.locator("#stage-popover");
    await mobilePop.waitFor({ state: "visible" });
    await mobilePop.locator("[data-stage-option=content]").tap();
    await mobile.waitForFunction(() => document.querySelector("#stage-panel h3")?.textContent.startsWith("分镜 1 · 文案与查询"));
    await mobile.locator("#shot-strip .shot-card").nth(1).locator(".shot-card-head").tap();
    await mobile.waitForFunction(() => document.querySelector("#stage-panel h3")?.textContent.startsWith("分镜 2 · 分镜渲染"));
    assert.ok(await mobilePop.isHidden());
    await mobile.screenshot({ path: `${out}/board-mobile.png`, fullPage: true });
    await touch.close();

    assert.deepEqual(errors, []);
    console.log("studio board UI checks passed");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
