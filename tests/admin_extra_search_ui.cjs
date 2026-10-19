// Run only against serve_upload_fixture.py with temporary data and fixture models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_EXTRA_SEARCH_URL || 'http://127.0.0.1:18439';
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}}), errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', r => {if(r.url().endsWith('/publish') && r.method() === 'POST') requests.push(r.postDataJSON());});
    const response = await page.request.post(base + '/api/admin/sources/upload', {multipart: {file: {
      name: 'extra-search.mp4', mimeType: 'video/mp4', buffer: fs.readFileSync('/tmp/mindex-extra-search-ui/工程样本.mp4')
    }}});
    assert.ok(response.ok(), await response.text());
    const sid = (await response.json()).source_id;
    await page.goto(base + '/admin?source=' + sid);
    const card = name => page.locator(`[data-work=${name}]`);
    if (await card('shots').getByRole('button', {name: '开始处理', exact: true}).count())
      await card('shots').getByRole('button', {name: '开始处理', exact: true}).click();
    const waitTask = async (name, field = 'task') => page.waitForFunction(async ({sid,name,field}) => {
      const s = await (await fetch(`/api/admin/sources/${sid}`)).json();
      return s.work[name][field]?.status === 'complete' && s.work[name][field]?.publication?.status === 'complete';
    }, {sid,name,field}, {timeout: 60000});
    await waitTask('shots');
    await page.reload();
    await card('shots').getByRole('button', {name: '更新shot/scene嵌入', exact: true}).click();
    await waitTask('shots', 'extra_publish_task');
    await page.reload();
    if (await card('analysis').getByRole('button', {name: '开始处理', exact: true}).count())
      await card('analysis').getByRole('button', {name: '开始处理', exact: true}).click();
    await waitTask('analysis');
    await page.reload();
    await card('analysis').getByRole('button', {name: '更新shot/scene嵌入', exact: true}).click();
    await waitTask('analysis', 'extra_publish_task');
    await page.reload();
    await card('analysis').getByRole('button', {name: '更新moment嵌入', exact: true}).click();
    await waitTask('analysis', 'publish_task');
    await page.reload();
    await card('analysis').getByRole('button', {name: '更新shot/scene嵌入', exact: true}).waitFor();
    assert.deepEqual(requests, [{work:'shots',scope:'extra'}, {work:'analysis',scope:'extra'}, {work:'analysis',scope:'moment'}]);
    assert.equal(await card('dialogue').getByRole('button', {name:'更新shot/scene嵌入',exact:true}).count(), 0);
    assert.equal(await card('persons').getByRole('button', {name:'更新shot/scene嵌入',exact:true}).count(), 0);
    const work = (await (await page.request.get(base + `/api/admin/sources/${sid}`)).json()).work;
    assert.ok(work.analysis.search.current);
    assert.ok(work.analysis.extra_search.current);
    assert.ok(work.analysis.extra_search.indexed_segments > 0);
    assert.deepEqual(errors, []);
    await page.screenshot({path:'/tmp/mindex-extra-search-ui.png',fullPage:true});
    console.log('Extra search UI passed: independent buttons, scopes, coverage and tasks.');
  } finally {await browser.close();}
})().catch(e => {console.error(e);process.exit(1);});
