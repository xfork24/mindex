const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
(async () => {
 const browser = await chromium.launch({headless: true});
 try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const base = process.env.MINDEX_RUNTIME_URL;
  await page.goto(base + '/settings');
  await page.waitForFunction(() => document.querySelector('#agent-runtime-status').textContent.length > 0);
  const old = await (await page.request.post(base + '/api/agent/sessions', {data: {}})).json();
  assert.equal(old.runtime, 'builtin');
  await page.selectOption('#agent-runtime', 'dsh');
  await page.locator('#agent-runtime-form button').click();
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('仅影响新会话'));
  const fresh = await (await page.request.post(base + '/api/agent/sessions', {data: {}})).json();
  assert.equal(fresh.runtime, 'dsh');
  assert.equal((await (await page.request.get(base + '/api/agent/sessions/' + old.id)).json()).runtime, 'builtin');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#agent-runtime').value === 'dsh');
  await page.route('**/api/settings/agent-runtime', route => route.fulfill({json: {
   runtime: 'dsh', dsh: {ready: false, errors: ['缺少 deepseek-harness-sdk；运行 uv sync --extra dsh']}
  }}));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#agent-runtime-status').textContent.includes('uv sync --extra dsh'));
  assert.equal(await page.locator('#agent-runtime').inputValue(), 'dsh');
  assert.deepEqual(errors, []);
 } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exit(1);});
