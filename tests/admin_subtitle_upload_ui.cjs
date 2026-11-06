// 仅对隔离的 serve_upload_fixture.py 素材库运行。
const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const base = process.env.MINDEX_SUBTITLE_TEST_URL || 'http://127.0.0.1:18439';
const video = process.env.MINDEX_SUBTITLE_TEST_VIDEO || '/tmp/mindex-subtitle-upload-20261018/工程样本.mp4';
(async () => {
  const browser = await chromium.launch({headless: true});
  const errors = [], requests = [];
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    // 四路并行回归时页面与模型服务争用 CPU：默认等待按满载情况设置，显式 timeout 的断言不受影响。
    page.setDefaultTimeout(90000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {if (request.method() === 'POST' && request.url().endsWith('/dialogue')) requests.push(request.postDataJSON());});
    const uploaded = await page.request.post(base + '/api/admin/sources/upload', {
      multipart: {file: {name: '字幕工程素材.mp4', mimeType: 'video/mp4', buffer: fs.readFileSync(video)}}
    });
    assert.equal(uploaded.status(), 201);
    const sid = (await uploaded.json()).source_id;
    const shots = await page.request.post(`${base}/api/admin/sources/${sid}/shots`, {data: {publish: false}});
    const shotId = (await shots.json()).id;
    await page.goto(`${base}/admin?source=${sid}`);
    await page.waitForFunction(async url => (await (await fetch(url)).json()).status === 'complete', `${base}/api/admin/source-tasks/${shotId}`);
    await page.reload();
    const dialogue = page.locator('[data-work=dialogue]');
    await dialogue.getByLabel('对白处理方式').selectOption('subtitle');
    const input = dialogue.getByLabel('上传字幕文件', {exact: true});
    // 字幕方式 + 占位项未选定时，上传文字与选择框同排可见。
    await input.waitFor();
    assert.ok((await input.getAttribute('accept')).includes('.ass'));
    await input.setInputFiles({name: '错误.srt', mimeType: 'text/plain', buffer: Buffer.from('broken')});
    await dialogue.getByRole('status').filter({hasText: '字幕文件格式无效'}).waitFor();
    assert.equal(await dialogue.getByRole('button', {name: '开始处理', exact: true}).isDisabled(), false);
    let releaseUpload;
    const hold = new Promise(resolve => {releaseUpload = resolve;});
    await page.route(`**/api/admin/sources/${sid}/subtitles`, async route => {await hold;await route.continue();});
    await input.setInputFiles({name: '中文对白.srt', mimeType: 'text/plain', buffer: Buffer.from('1\n00:00:00,200 --> 00:00:01,400\n<i>你好</i>，牛车水！\n\n')});
    await dialogue.getByRole('status').filter({hasText: '正在上传字幕文件'}).waitFor();
    assert.equal(await dialogue.getByRole('button', {name: '开始处理', exact: true}).isDisabled(), true);
    releaseUpload();
    await dialogue.getByLabel('对白字幕轨道').locator('option').filter({hasText: '中文对白.srt'}).waitFor({state: 'attached'});
    assert.ok((await dialogue.getByLabel('对白字幕轨道').inputValue()).startsWith('file:'));
    // 选定字幕（轨道或已上传文件）后上传控件收起；仅占位项“请选择文本字幕”时可见。
    assert.equal(await input.isHidden(), true);
    const source = await (await page.request.get(`${base}/api/admin/sources/${sid}`)).json();
    assert.equal(source.channels.dialogue.status, 'not_processed');
    await page.reload();
    assert.equal(await dialogue.getByLabel('对白处理方式').inputValue(), 'subtitle');
    assert.ok((await dialogue.getByLabel('对白字幕轨道').inputValue()).startsWith('file:'));
    await dialogue.getByLabel('完成后更新检索').uncheck();
    await dialogue.getByRole('button', {name: '开始处理', exact: true}).click();
    await dialogue.locator('.work-status').filter({hasText: '处理完成'}).waitFor();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].method, 'subtitle');
    assert.ok(requests[0].subtitle_file && !('subtitle_stream' in requests[0]));
    const result = await (await page.request.get(`${base}/api/admin/sources/${sid}`)).json();
    assert.equal(result.work.dialogue.result.utterances, 1);
    assert.equal(result.work.dialogue.result.subtitle_file.name, '中文对白.srt');
    await page.locator('.segment-card').first().getByRole('button', {name: '详情', exact: true}).click();
    await page.locator('#analysis-content').getByText('你好，牛车水！', {exact: false}).waitFor();
    await page.locator('#analysis-close').click();
    await page.screenshot({path: '/tmp/mindex-subtitle-upload-desktop.png', fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: '/tmp/mindex-subtitle-upload-mobile.png', fullPage: true});
    await page.getByRole('button', {name: 'English', exact: true}).click();
    await dialogue.getByLabel('Dialogue processing method').waitFor();
    assert.equal(await dialogue.getByLabel('Upload subtitle file', {exact: true}).isHidden(), true);
    assert.deepEqual(errors, []);
    console.log('PASS: invalid upload recovery; pending upload disables start; file selected and restored; subtitle task request; timed dialogue; segment details; desktop/mobile; English; no page errors.');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode = 1;});
