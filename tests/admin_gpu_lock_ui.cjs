// Run only against the temporary serve_upload_fixture.py library.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const base = process.env.MINDEX_GPU_LOCK_TEST_URL || 'http://127.0.0.1:18437';
const video = process.env.MINDEX_GPU_LOCK_TEST_VIDEO || '/tmp/mindex-gpu-lock-ui-18437/工程样本.mp4';
(async () => {
  const browser = await chromium.launch({headless: true});
  const errors = [];
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(base + '/settings');
    const lock = page.locator('#enforce-gpu-lock');
    await page.waitForFunction(() => document.querySelector('#roles')?.children.length);
    assert.equal(await lock.isChecked(), false);
    await lock.check();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('处理设置已保存'));
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#enforce-gpu-lock').checked);
    await lock.uncheck();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('处理设置已保存'));
    assert.equal((await (await page.request.get(base + '/api/settings')).json()).business.enforce_gpu_lock, false);
    const uploaded = await page.request.post(base + '/api/admin/sources/upload', {
      multipart: {file: {name: '并行工程样本.mp4', mimeType: 'video/mp4', buffer: fs.readFileSync(video)}}
    });
    assert.ok([200, 201].includes(uploaded.status()));
    const sid = (await uploaded.json()).source_id;
    const shotTask = await page.request.post(`${base}/api/admin/sources/${sid}/shots`, {data: {publish: false}});
    assert.equal(shotTask.status(), 202);
    const id = (await shotTask.json()).id;
    await assertTask(id, 'complete');
    await page.goto(`${base}/admin?source=${sid}`);
    const analysis = page.locator('[data-work=analysis]');
    await analysis.getByLabel('完成后更新检索').uncheck();
    await analysis.getByRole('button', {name: '开始处理', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-work=analysis] .work-task-progress')?.textContent.includes('运行中'));
    assert.equal(await analysis.getByRole('button', {name: '开始处理', exact: true}).isDisabled(), true);
    assert.equal(await page.locator('[data-work=dialogue]').getByRole('button', {name: '开始处理', exact: true}).isDisabled(), false);
    assert.equal(await page.locator('[data-work=shots]').getByRole('button', {name: '拆分镜', exact: true}).isDisabled(), false);
    const dialogue = await page.request.post(`${base}/api/admin/sources/${sid}/dialogue`, {data: {publish: false}});
    assert.equal(dialogue.status(), 202, await dialogue.text());
    await page.screenshot({path: '/tmp/mindex-gpu-lock-desktop.png', fullPage: true});
    await page.request.put(base + '/api/settings/business', {data: {enforce_gpu_lock: true}});
    await page.reload();
    await page.locator('[data-work=analysis] .work-task-progress').waitFor();
    assert.equal(await page.locator('[data-work=shots]').getByRole('button', {name: '拆分镜', exact: true}).isDisabled(), true);
    await page.setViewportSize({width: 390, height: 844});
    await page.goto(base + '/settings');
    await page.waitForFunction(() => document.querySelector('#enforce-gpu-lock').checked);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: '/tmp/mindex-gpu-lock-mobile.png', fullPage: true});
    assert.deepEqual(errors, []);
    console.log('PASS: default false; persisted toggle; different work starts during analysis; same work disabled; enforced lock disables others; narrow settings; no page errors.');
    async function assertTask(taskId, expected) {
      await page.waitForFunction(async ({url, expected}) => {
        const task = await (await fetch(url)).json();
        return task.status === expected;
      }, {url: `${base}/api/admin/source-tasks/${taskId}`, expected}, {timeout: 20000});
    }
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
