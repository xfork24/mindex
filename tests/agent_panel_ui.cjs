// 智能助手面板浏览器回归：只对 serve_agent_fixture.py 的隔离素材库与规则化模型运行。
const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_AGENT_URL;
const job = process.env.MINDEX_AGENT_JOB;
const scenario = process.env.MINDEX_AGENT_SCENARIO || 'basic';
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
const studioUrl = `${base}/studio?job=${job}`;

async function openPanel(page) {
  if (await page.locator('#agent-drawer').isHidden()) await page.locator('.agent-launcher').click();
  await page.locator('#agent-drawer:not([hidden])').waitFor();
}
async function say(page, text) {
  await page.locator('.agent-send:not([disabled])').waitFor();
  await page.locator('.agent-input').fill(text);
  await page.locator('.agent-send').click();
}
async function shotCount(page) {
  return Number((await page.locator('#shot-count').textContent()).match(/\d+/)[0]);
}
async function waitShots(page, n) {
  await page.waitForFunction((count) => /\d+/.test(document.querySelector('#shot-count')?.textContent || '')
    && Number(document.querySelector('#shot-count').textContent.match(/\d+/)[0]) === count, n, {timeout: 15000});
}
async function workspaceShots(page) {
  const data = await (await page.request.get(`${base}/api/studio/jobs/${job}/workspace`)).json();
  const plan = data.plans[data.active_plan];
  return data.versions[plan.board].data.order;
}
const focused = (page, selector) => page.locator(`${selector}[data-agent-focus]`).first().waitFor({timeout: 15000});

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1280, height: 900}});
  const page = await context.newPage();
  const errors = [];
  const watch = (p) => p.on('pageerror', (e) => errors.push(e.message));
  watch(page);
  try {
    if (scenario === 'basic') {
      for (const path of ['/', '/admin', '/persons', '/settings']) {
        await page.goto(base + path);
        await page.locator('.agent-launcher').waitFor();
      }
      await page.goto(studioUrl);
      await page.locator('#job-workspace:not([hidden])').waitFor();
      await openPanel(page);
      assert.equal(await page.locator('.agent-launcher').getAttribute('aria-expanded'), 'true');
      // 疑似密钥粘贴被拦截。
      await page.locator('.agent-input').focus();
      await page.evaluate(() => {
        const data = new DataTransfer();
        data.setData('text/plain', 'sk-abcdefghijklmnopqrstuvwxyz123456');
        document.querySelector('.agent-input').dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
      });
      assert.equal(await page.locator('.agent-input').inputValue(), '');
      assert.equal(await page.locator('.agent-warning').first().isVisible(), true);

      await say(page, '这个任务有几个分镜？');
      await page.locator('.agent-assistant', {hasText: '当前方案共有 3 个分镜'}).waitFor({timeout: 15000});
      await page.locator('.agent-tool summary', {hasText: 'get_workspace_summary'}).waitFor();
      await focused(page, '#storyboard-section');  // 查询也在页面上定位
      const sid = await page.evaluate(() => window.MindexAgent.state.session);
      const logData = await (await page.request.get(`${base}/api/agent/sessions/${sid}/log`)).json();
      const ctx = logData.items.find((e) => e.type === 'user/message' && e.data.source.kind === 'context');
      assert.ok(ctx && ctx.data.content[0].text.includes(job));
      assert.ok(!ctx.data.content[0].text.includes('tab_id'));

      await page.reload();
      await page.locator('#agent-drawer:not([hidden])').waitFor();
      await page.locator('.agent-assistant', {hasText: '当前方案共有 3 个分镜'}).waitFor({timeout: 15000});
      assert.equal(await page.locator('[data-agent-focus]').count(), 0);  // 重放历史不定位
      await page.locator('[data-language=en]').first().click();
      assert.equal(await page.locator('.agent-send').textContent(), 'Send');
      assert.equal(await page.locator('.agent-follow span').textContent(), 'Follow the assistant');
      await page.locator('[data-language=zh]').first().click();
    }

    if (scenario === 'approval') {
      // 同一任务开两个页面：A 中经助手合并，B 自动刷新。
      const other = await context.newPage();
      watch(other);
      await other.goto(studioUrl);
      await waitShots(other, 3);
      await page.goto(studioUrl);
      await page.locator('#job-workspace:not([hidden])').waitFor();
      await openPanel(page);
      await say(page, '合并 a1daad59 和 925281d5');
      const card = page.locator('.agent-approval');
      await card.waitFor({timeout: 15000});
      assert.match(await card.textContent(), /合并/);
      await focused(page, 'tr');  // 确认前先定位到将被合并的分镜
      await card.locator('button', {hasText: '允许'}).click();
      await page.locator('.agent-approval[data-outcome="allowed-once"]').waitFor();
      await waitShots(page, 2);
      await waitShots(other, 2);
      assert.equal(new URL(other.url()).pathname, '/studio');
      await other.close();
    }

    if (scenario === 'cross') {
      // 在设置页发起修改：等待确认时跳到创作页，面板与确认卡片延续。
      await page.goto(`${base}/settings`);
      await openPanel(page);
      await say(page, '合并 a1daad59 和 925281d5');
      await page.waitForURL(/\/studio\?job=/, {timeout: 15000});
      await page.locator('#agent-drawer:not([hidden])').waitFor();
      const card = page.locator('.agent-approval:not([data-outcome])');
      await card.waitFor({timeout: 15000});
      await focused(page, 'tr');
      await card.locator('button', {hasText: '允许'}).click();
      await waitShots(page, 2);
      await page.waitForFunction(() => {
        const row = document.querySelector('tr[data-agent-focus] th');
        return row && row.textContent.includes('a1daad59');
      }, null, {timeout: 15000});
    }

    if (scenario === 'query') {
      await page.goto(studioUrl);
      await page.locator('#job-workspace:not([hidden])').waitFor();
      await openPanel(page);
      await say(page, '查看第 2 镜');
      await page.waitForFunction(() => {
        const row = document.querySelector('tr[data-agent-focus] th');
        return row && row.textContent.includes('925281d5');
      }, null, {timeout: 15000});
      assert.equal(new URL(page.url()).pathname, '/studio');
    }

    if (scenario === 'external') {
      // 模拟外部 MCP：直接改数据并报告改动，页面刷新但不跳转。
      await page.goto(studioUrl);
      await waitShots(page, 3);
      const data = await (await page.request.get(`${base}/api/studio/jobs/${job}/workspace`)).json();
      const plan = data.plans[data.active_plan];
      const shots = data.versions[plan.board].data.order.slice(0, 2).map((sid) => data.versions[plan.shots[sid].content].data);
      const patched = await page.request.patch(`${base}/api/studio/jobs/${job}/plans/${plan.id}`, {data: {revision: data.revision, shots}});
      assert.equal(patched.status(), 200);
      await page.request.post(`${base}/api/agent/changes`, {data: {kind: 'mutation', data: {domain: 'studio', job_id: job}}});
      await page.request.post(`${base}/api/agent/changes`, {data: {kind: 'focus', data: {page: 'settings', role: 'agent'}}});
      await waitShots(page, 2);
      await page.waitForTimeout(1500);
      assert.equal(new URL(page.url()).pathname, '/studio');  // 外部来源的跨页定位不跳转
    }

    if (scenario === 'nofollow') {
      await page.goto(`${base}/settings`);
      await openPanel(page);
      await page.locator('.agent-follow input').uncheck();
      await say(page, '合并 a1daad59 和 925281d5');
      const card = page.locator('.agent-approval');
      await card.waitFor({timeout: 15000});
      await page.waitForTimeout(1500);
      assert.equal(new URL(page.url()).pathname, '/settings');
      await card.locator('button', {hasText: '允许'}).click();
      await page.locator('.agent-approval[data-outcome="allowed-once"]').waitFor();
      await page.waitForTimeout(1000);
      assert.equal(new URL(page.url()).pathname, '/settings');
      assert.equal((await workspaceShots(page)).length, 2);
      await page.reload();
      assert.equal(await page.locator('.agent-follow input').isChecked(), false);  // 选择被记住
    }

    if (scenario === 'navigate') {
      await page.goto(studioUrl);
      await openPanel(page);
      await say(page, '打开设置页的智能助手角色');
      await page.waitForURL(`${base}/settings`, {timeout: 15000});
      await focused(page, '.role-row[data-role="agent"]');
      assert.equal(await page.locator('#role-body-agent').isVisible(), true);
      await page.locator('#agent-drawer:not([hidden])').waitFor();
    }

    if (scenario === 'markdown') {
      await page.goto(studioUrl);
      await openPanel(page);
      await say(page, '列出所有分镜');
      const reply = page.locator('.agent-assistant .agent-text').last();
      await reply.locator('table').waitFor({timeout: 15000});
      assert.equal(await reply.locator('h2').textContent(), '分镜');
      assert.equal(await reply.locator('tbody tr').count(), 2);
      assert.equal(await reply.locator('td strong').textContent(), '蓝色');
      assert.match(await reply.locator('pre code').textContent(), /shot-a1daad59/);
      assert.equal(await reply.locator('script').count(), 0);
      assert.equal(await page.evaluate(() => window.__xss), undefined);
      const links = await reply.locator('a').evaluateAll((items) => items.map((a) => [a.textContent, a.getAttribute('href'), a.target]));
      assert.deepEqual(links.find((l) => l[0] === '文档'), ['文档', 'https://example.com/docs', '_blank']);
      const danger = links.find((l) => l[0] === '危险');
      assert.ok(!danger || !danger[1], `javascript 链接未被清除：${danger}`);
      // 用户消息仍是纯文本。
      await say(page, '**不是粗体**');
      await page.locator('.agent-user', {hasText: '**不是粗体**'}).waitFor();
      assert.equal(await page.locator('.agent-user strong').count(), 0);
    }
    if (scenario === 'media') {
      const external = [];
      page.on('request', (request) => { if (!request.url().startsWith(base)) external.push(request.url()); });
      await page.goto(studioUrl);
      await page.locator('#job-workspace:not([hidden])').waitFor();
      await openPanel(page);
      await say(page, '看看第 1 镜的媒体');
      const block = page.locator('.agent-media').last();
      await block.waitFor({timeout: 15000});
      const tiles = block.locator('.agent-media-tile');
      assert.ok(await tiles.count() >= 2, '召回候选缩略图网格');
      await page.waitForFunction(() => [...document.querySelectorAll('.agent-media-tile img')].every((img) => img.complete && img.naturalWidth > 0));
      // 音频与单镜视频默认折叠，展开前不创建媒体元素。
      const audio = block.locator('details.agent-player-audio');
      assert.equal(await audio.getAttribute('open'), null);
      assert.equal(await audio.locator('audio').count(), 0);
      assert.equal(await block.locator('details.agent-player-video').first().getAttribute('open'), null);
      await audio.locator('summary').click();
      await page.waitForFunction(() => document.querySelector('details.agent-player-audio audio')?.readyState >= 1, null, {timeout: 15000});
      // 大图浏览：打开、左右切换、Esc 关闭。
      const count = await tiles.count();
      await tiles.first().click();
      const viewer = page.locator('dialog.agent-viewer[open]');
      await viewer.waitFor();
      assert.match(await viewer.locator('.agent-viewer-caption').textContent(), new RegExp(`1 / ${count}$`));
      await page.waitForFunction(() => document.querySelector('dialog.agent-viewer[open] video')?.readyState >= 1, null, {timeout: 15000});
      await page.keyboard.press('ArrowRight');
      assert.match(await viewer.locator('.agent-viewer-caption').textContent(), new RegExp(`2 / ${count}$`));
      await page.keyboard.press('ArrowLeft');
      await page.keyboard.press('ArrowLeft');
      assert.match(await viewer.locator('.agent-viewer-caption').textContent(), new RegExp(`${count} / ${count}$`));
      await page.keyboard.press('Escape');
      await page.locator('dialog.agent-viewer[open]').waitFor({state: 'detached', timeout: 5000}).catch(() => {});
      assert.equal(await page.locator('dialog.agent-viewer').evaluate((d) => d.open), false);
      await page.waitForFunction(() => !document.querySelector('dialog.agent-viewer video'));  // 关闭后释放播放器

      // 成片默认展开并可加载。
      await say(page, '看成片');
      const final = page.locator('.agent-media details.agent-player-video', {hasText: '成片'}).last();
      await final.waitFor({timeout: 15000});
      assert.equal(await final.getAttribute('open'), '');
      await page.waitForFunction(() => {
        const videos = [...document.querySelectorAll('.agent-media details.agent-player-video[open] video')];
        return videos.length && videos.at(-1).readyState >= 1;
      }, null, {timeout: 15000});

      // 检索结果：片段缩略图网格。
      await say(page, '找绿色素材');
      await page.locator('.agent-media').last().locator('.agent-media-tile[data-kind="video"]').first().waitFor({timeout: 15000});

      // Markdown 图片：本站图片显示并可点开，外链图片不加载。
      await say(page, '显示参考图');
      const reply = page.locator('.agent-assistant .agent-markdown').last();
      await reply.locator('img').first().waitFor({timeout: 15000});
      assert.equal(await reply.locator('img').count(), 1);
      await reply.locator('img').click();
      await page.locator('dialog.agent-viewer[open] img.agent-viewer-image').waitFor();
      await page.keyboard.press('Escape');

      // 关闭面板时暂停播放。
      await page.evaluate(() => { const a = document.querySelector('details.agent-player-audio audio'); a.muted = true; return a.play(); });
      await page.waitForFunction(() => !document.querySelector('details.agent-player-audio audio').paused);
      await page.locator('.agent-close').click();
      assert.equal(await page.evaluate(() => document.querySelector('details.agent-player-audio audio').paused), true);
      assert.deepEqual(external.filter((u) => !u.startsWith('data:')), []);
    }

    if (scenario === 'resize') {
      await page.goto(studioUrl);
      await openPanel(page);
      const drawerBox = async () => (await page.locator('#agent-drawer').boundingBox()).width;
      assert.equal(Math.round(await drawerBox()), 440);
      const handle = page.locator('.agent-resize');
      assert.equal(await handle.getAttribute('role'), 'separator');
      const box = await handle.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + 200);
      await page.mouse.down();
      await page.mouse.move(box.x - 200, box.y + 200, {steps: 8});
      await page.mouse.up();
      const dragged = Math.round(await drawerBox());
      assert.ok(Math.abs(dragged - 640) <= 6, `拖拽后宽度 ${dragged}`);
      assert.equal(await handle.getAttribute('aria-valuenow'), String(dragged));
      await handle.focus();
      await page.keyboard.press('ArrowRight');
      await page.keyboard.press('ArrowRight');
      assert.equal(Math.round(await drawerBox()), dragged - 48);
      // 刷新与跨页后保持。
      await page.reload();
      await page.locator('#agent-drawer:not([hidden])').waitFor();
      assert.equal(Math.round(await drawerBox()), dragged - 48);
      await page.goto(`${base}/settings`);
      await page.locator('#agent-drawer:not([hidden])').waitFor();
      assert.equal(Math.round(await drawerBox()), dragged - 48);
      // 上下限与双击恢复。
      const settingsHandle = await page.locator('.agent-resize').boundingBox();
      await page.mouse.move(settingsHandle.x + 4, 300);
      await page.mouse.down();
      await page.mouse.move(1270, 300, {steps: 5});
      await page.mouse.up();
      assert.equal(Math.round(await drawerBox()), 320);
      await page.locator('.agent-resize').focus();
      await page.keyboard.press('End');
      assert.equal(Math.round(await drawerBox()), 1024);  // 视口 80%
      await page.locator('.agent-resize').dblclick();
      assert.equal(Math.round(await drawerBox()), 440);
      assert.equal(await page.evaluate(() => localStorage.getItem('mindex.agent.width')), '440');
      // 宽屏时页面右侧留白跟随宽度（1280 视口不触发；放大视口验证）。
      await page.setViewportSize({width: 1600, height: 900});
      await page.locator('.agent-resize').focus();
      await page.keyboard.press('ArrowLeft');
      const margin = await page.evaluate(() => getComputedStyle(document.querySelector('main')).marginRight);
      assert.equal(margin, '464px');
    }

    assert.deepEqual(errors, []);
    console.log('agent panel ui ok:', scenario);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
