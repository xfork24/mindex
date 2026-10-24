// 创作页“取消执行”“删除方案”浏览器回归：只对 test_studio_cancel_browser_ui.py 启动的隔离服务运行。
// 服务端已提交一次配音整列重做，配音桩卡在第一个分镜上，直到本脚本写入放行文件。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_STUDIO_URL;
const job = process.env.MINDEX_STUDIO_JOB;
const release = process.env.MINDEX_STUDIO_RELEASE;
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
assert.ok(job && release);
const api = `${base}/api/studio/jobs/${job}`;

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await (await browser.newContext({viewport: {width: 1280, height: 900}})).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  try {
    await page.goto(`${base}/studio?job=${job}`);
    await page.locator('#job-workspace:not([hidden])').waitFor();
    const cancel = page.locator('#cancel-run');

    // 1. 构建执行中：显示可用的“取消执行”，删除方案被锁定。
    await page.locator('#cancel-run:not([hidden]):not([disabled])').waitFor({timeout: 15000});
    assert.equal((await cancel.textContent()).trim(), '取消执行');
    assert.ok(await cancel.getAttribute('title'));
    assert.equal(await page.locator('#delete-plan').isDisabled(), true);

    // 2. 点击后显示“正在取消…”且不可重复点击；服务端已登记取消请求。
    await cancel.click();
    await page.waitForFunction(() => {
      const n = document.querySelector('#cancel-run');
      return n.textContent.includes('正在取消') && n.disabled;
    }, null, {timeout: 10000});
    const running = (await (await page.request.get(`${api}/workspace`)).json()).builds.at(-1);
    assert.equal(running.status, 'running');
    const logs = await (await page.request.get(`${api}/builds/${running.id}/logs`)).json();
    assert.ok(logs.items.some((e) => e.message.includes('已请求取消')));

    // 3. 放行当前节点：构建结束为已取消，按钮随轮询隐藏，页面显示“已取消”。
    fs.writeFileSync(release, 'go');
    await page.locator('#cancel-run[hidden]').waitFor({state: 'attached', timeout: 20000});
    const done = (await (await page.request.get(`${api}/workspace`)).json()).builds.at(-1);
    assert.equal(done.status, 'cancelled');
    assert.equal(done.nodes[0].status, 'complete');
    assert.ok(done.nodes.slice(1).every((n) => n.status === 'blocked' && n.error === '已取消'));
    await page.waitForFunction(() => document.body.innerText.includes('已取消'), null, {timeout: 10000});

    // 4. 只剩一个方案：“删除方案”禁用并说明原因。
    await page.waitForFunction(() => document.querySelector('#plan-select').options.length === 1);
    assert.equal(await page.locator('#delete-plan').isDisabled(), true);
    assert.ok(await page.locator('#delete-plan').getAttribute('title'));

    // 5. 新建第二个方案后可删除：取消确认不删除；确认后删除并切回剩余方案。
    const first = (await (await page.request.get(`${api}/workspace`)).json()).active_plan;
    const created = await page.request.post(`${api}/plans`, {data: {name: '浏览器副本', copy_from: first}});
    assert.ok(created.ok(), await created.text());
    const second = (await created.json()).active_plan;
    assert.notEqual(second, first);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#plan-select')?.options.length === 2, null,
      {timeout: 15000});
    await page.locator('#delete-plan:not([disabled])').waitFor({timeout: 10000});
    let prompt = '';
    page.once('dialog', (d) => { prompt = d.message(); d.dismiss(); });
    await page.locator('#delete-plan').click();
    assert.match(prompt, /浏览器副本/);
    assert.equal(Object.keys((await (await page.request.get(`${api}/workspace`)).json()).plans).length, 2);
    page.once('dialog', (d) => d.accept());
    await page.locator('#delete-plan').click();
    await page.waitForFunction(() => document.querySelector('#plan-select').options.length === 1, null,
      {timeout: 10000});
    const after = await (await page.request.get(`${api}/workspace`)).json();
    assert.deepEqual(Object.keys(after.plans), [first]);
    assert.equal(after.active_plan, first);
    assert.ok(after.builds.some((b) => b.id === done.id && b.status === 'cancelled'));  // 构建记录保留
    await page.locator('#delete-plan[disabled]').waitFor({timeout: 10000});
    assert.deepEqual(errors, []);
    console.log('Studio cancel and delete-plan browser flow works.');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
