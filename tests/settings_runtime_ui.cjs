const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
(async () => {
 const browser = await chromium.launch({headless: true});
 try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const base = process.env.MINDEX_RUNTIME_URL;
  await Promise.all([
   page.waitForResponse(r => r.url() === base + '/api/settings/agent-runtime' && r.ok()),
   page.goto(base + '/settings')
  ]);
  // 就绪时状态文案为空；以实际请求与角色卡片确认页面已加载。
  const agent = page.locator('.role-row[data-role="agent"]');
  await agent.locator('.role-row-head').click();
  await agent.locator('.new-profile-card').click();
  await page.locator('#pf-name').fill('百万上下文回归');
  await page.locator('#pf-base-url').fill('http://127.0.0.1:1/v1');
  await page.locator('#pf-model').fill('fixture');
  await page.getByLabel('模型上下文容量', {exact: false}).fill('1048576');
  await page.getByLabel('单次回复输出上限', {exact: false}).fill('4096');
  const saved = page.waitForResponse(r => r.url() === base + '/api/settings/profiles' && r.request().method() === 'POST');
  await page.locator('#pf-save').click();
  const profile = await (await saved).json();
  assert.equal(profile.config.context_window, 1048576);
  assert.equal(profile.config.max_tokens, 4096);
  await page.locator('#profile-dialog').waitFor({state: 'hidden'});
  const card = page.locator(`.profile-item[data-profile-id="${profile.id}"]`);
  await card.getByRole('button', {name: '编辑', exact: true}).click();
  assert.equal(await page.locator('#pf-context_window').inputValue(), '1048576');
  assert.equal(await page.locator('#pf-max_tokens').inputValue(), '4096');
  await page.keyboard.press('Escape');
  const old = await (await page.request.post(base + '/api/agent/sessions', {data: {}})).json();
  assert.equal(old.runtime, 'builtin');
  await page.selectOption('#agent-runtime', 'dsh');
  await Promise.all([
   page.waitForResponse(r => r.url() === base + '/api/settings/agent-runtime' && r.request().method() === 'PUT' && r.ok()),
   page.locator('#agent-runtime-form button').click()
  ]);
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
