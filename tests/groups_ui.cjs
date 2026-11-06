const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_GROUPS_URL;
const source = process.env.MINDEX_GROUPS_SOURCE;
const secondSource = process.env.MINDEX_GROUPS_SECOND_SOURCE;
const secondGroup = process.env.MINDEX_GROUPS_SECOND_GROUP;
const emptyGroup = process.env.MINDEX_GROUPS_EMPTY_GROUP;
const music = process.env.MINDEX_GROUPS_MUSIC;
const musicGroup = process.env.MINDEX_GROUPS_MUSIC_GROUP;
const sourceGroup = process.env.MINDEX_GROUPS_SOURCE_GROUP;
(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1280,height:1000}});
  // 四路并行回归时页面与模型服务争用 CPU：默认等待按满载情况设置，显式 timeout 的断言不受影响。
  page.setDefaultTimeout(90000);
  const errors = [];
  const resolutions = [];
  page.on('request', (request) => { if (request.url().endsWith('/api/admin/groups/resolve')) resolutions.push(request.url()); });
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route('**/api/settings/voices/catalog*', (route) => route.fulfill({json:{groups:[],library:[],default_voice:null,clone:{supported:false},error:null}}));
  async function go(path) { await page.goto(base + path); await page.waitForLoadState('networkidle'); }
  async function chooseGroups(control, ids) {
    if (await control.locator('select[multiple]').count()) {
      const select = control.locator('select[multiple]');
      await select.selectOption(ids);
      return;
    }
    await control.locator('.group-options input').first().waitFor();
    for (const value of await control.locator('input:checked').evaluateAll((nodes) => nodes.map((node) => node.value))) {
      if (!ids.includes(value)) await control.locator(`input[value="${value}"]`).uncheck();
    }
    for (const id of ids) await control.locator(`input[value="${id}"]`).check();
  }
  // 组筛选默认全选：全部实际组勾选且“全部选择”同步勾选（values 为空 = 不限）。
  async function assertAllSelected(selector, name) {
    await page.waitForFunction((sel) => {
      const select = document.querySelector(sel);
      if (!select) return false;
      const real = [...select.options].filter((option) => option.value && option.value !== '__all__');
      return real.length > 0 && real.every((option) => option.selected) &&
        [...select.selectedOptions].some((option) => option.value === '__all__');
    }, selector).catch(() => { throw new Error(`${name}：组筛选应默认全选`); });
  }
  async function assertGroupText(name) {
    const result = await page.locator('.group-picker').evaluateAll((nodes) => nodes.filter((node) => node.getClientRects().length).map((node) => ({
      text: node.textContent,
      caption: node.querySelector('.group-caption')?.textContent,
      native: node.querySelectorAll('select, input[type=text], textarea').length,
      listbox: node.classList.contains('group-picker-listbox'),
      overflow: [...node.querySelectorAll('.group-option, .group-option>span')].some((entry) => entry.scrollWidth > entry.clientWidth + 1)
    })));
    for (const item of result) {
      assert(!/\uE000z\d+\uE001/.test(item.text), `${name}: exposed translation token ${item.text}`);
      assert(item.caption?.trim(), `${name}: missing group label`);
      assert.equal(item.native, item.listbox ? 1 : 0, `${name}: group picker has unexpected controls`);
      assert.equal(item.overflow, false, `${name}: group label should wrap naturally`);
    }
  }
  async function audit(name) {
    await assertGroupText(name + '-initial');
    for (const language of ['en', 'zh']) {
      await page.evaluate((lang) => MindexI18n.setLanguage(lang), language);
      for (const width of [1280, 390, 320]) {
        await page.setViewportSize({width,height:1000});
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
        await assertGroupText(`${name}-${language}-${width}`);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `${name} ${language} ${width}: horizontal overflow`);
        // 任务创建侧栏：素材组筛选与「可用素材」列表框上下相邻，应同宽。
        assert(await page.evaluate(() => {
          const form = document.getElementById('create-form');
          const label = document.getElementById('create-sources')?.closest('label');
          const picker = form?.querySelector(':scope > .group-picker');
          if (!picker || !label) return true;
          return Math.abs(picker.getBoundingClientRect().width - label.getBoundingClientRect().width) < 1;
        }), `${name} ${language} ${width}: 素材组筛选应与可用素材列表框同宽`);
      }
    }
    await page.screenshot({path:process.env.MINDEX_GROUPS_OUTPUT + '/' + name + '.png',fullPage:true});
    await page.setViewportSize({width:1280,height:1000});
  }
  try {
    await go('/admin/groups');
    assert.deepEqual(await page.locator('.admin-tabs a').allTextContents(), ['素材','音乐库','管理']);
    await page.locator(`#groups-members input[value="${source}"]`).waitFor();
    assert.equal(await page.locator('#groups-results option').count(), 2, '添加成员区默认显示全部素材');
    await page.locator('[data-group-id=ungrouped-source]').click();
    await page.waitForFunction(()=>document.querySelector('#groups-name').disabled);
    assert(await page.locator('#groups-delete').isDisabled());
    assert(await page.locator('#groups-add').isDisabled());
    await page.locator(`[data-group-id="${sourceGroup}"]`).click();
    await page.waitForFunction(()=>!document.querySelector('#groups-name').disabled);
    await page.locator('#groups-new').click();
    await page.locator('#groups-name').fill('浏览器原文 Browser');
    await page.locator('#groups-description').fill('说明保持原文');
    await page.locator('#groups-form button[type=submit]').click();
    await page.locator('#groups-list button', {hasText:'浏览器原文 Browser (0)'}).waitFor();
    await page.locator('#groups-results').selectOption(source);
    await page.locator('#groups-query').fill('第二');
    assert.equal(await page.locator('#groups-results option:checked').count(), 1, '搜索不能丢失已选素材');
    await page.locator('#groups-select-visible').click();
    assert.equal(await page.locator('#groups-results option:checked').count(), 2);
    await page.locator('#groups-query').fill('');
    await page.locator('#groups-add').click();
    await page.locator('#groups-list button', {hasText:'浏览器原文 Browser (2)'}).waitFor();
    const group = await page.evaluate(() => MindexGroupContext.group_id);
    // 组信息首行按顶部对齐：两个字段横向并排且标签行齐平（说明多行时不再下沉）。
    const fields = await page.locator('#groups-form>label').evaluateAll((nodes) => nodes.map((node) => { const r = node.getBoundingClientRect(); return {x:r.x,right:r.right,top:r.top}; }));
    assert(fields[1].x >= fields[0].right && Math.abs(fields[0].top - fields[1].top) < 2, '组信息默认紧凑排列在同一行且顶部对齐');
    await audit('groups-management');
    assert((await page.locator('#groups-member-title').textContent()).includes('（2）'), '语言切换后仍显示当前组的成员数量');
    assert.equal(await page.locator('#groups-description').inputValue(), '说明保持原文');
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.locator(`#groups-list button[data-group-id="${group}"]`).click();
    assert.equal(await page.locator('#groups-members input').count(), 2, '重新打开组仍能看到全部成员');
    await page.locator(`#groups-members input[value="${source}"]`).check();
    await page.locator(`#groups-members input[value="${secondSource}"]`).check();
    await page.locator('#groups-remove').click();
    await page.locator('#groups-list button', {hasText:'浏览器原文 Browser (0)'}).waitFor();
    await page.locator('#groups-kind').selectOption('music');
    await page.locator('#groups-results option').first().waitFor();
    assert.equal(await page.locator('#groups-results option').count(), 1, '音乐组添加区默认展示曲库');
    await page.locator(`#groups-list button[data-group-id="${musicGroup}"]`).click();
    await page.locator('#groups-results').selectOption(music);
    await page.locator('#groups-add').click();
    await page.locator(`#groups-members input[value="${music}"]`).waitFor();
    await page.locator(`#groups-members input[value="${music}"]`).check();
    await page.locator('#groups-remove').click();
    await page.locator('#groups-list button', {hasText:'音乐原文 Music (0)'}).waitFor();
    await go('/admin');
    await page.locator('.source-item', {hasText:'测试素材.mp4'}).click();
    const membership = page.locator('#source-detail [data-group-membership]');
    const sourceSaved = page.waitForResponse((r) => r.url().includes('/group-memberships/source/') && r.request().method() === 'PUT');
    await chooseGroups(membership, [group, sourceGroup]);
    await sourceSaved;
    await audit('groups-source-detail');
    await page.reload();
    await page.waitForLoadState('networkidle');
    assert.deepEqual((await membership.locator('input:checked').evaluateAll((nodes) => nodes.map((n) => n.value))).sort(), [group, sourceGroup].sort());
    // 素材 Tab 左侧列表组筛选：与检索页同款多选列表框，默认全选（=不限），选择后按组过滤左侧素材列表。
    const sourceFilter = page.locator('#source-group-filter .group-picker');
    assert(await sourceFilter.evaluate((node) => node.classList.contains('group-picker-listbox')), '素材页左侧组筛选应为多选列表框');
    await assertAllSelected('#source-group-filter select[multiple]', '素材页组筛选');
    const filteredSources = page.waitForResponse((r) => r.url().includes('source_group_ids='));
    await chooseGroups(sourceFilter, [sourceGroup]);
    await (await filteredSources).json();
    await page.waitForFunction(() => document.querySelectorAll('#source-list .source-item').length === 1);
    assert((await page.locator('#source-list .source-item').first().textContent()).includes('测试素材'), '组筛选应只显示组内素材');
    await chooseGroups(sourceFilter, [emptyGroup]);
    await page.waitForFunction(() => document.querySelectorAll('#source-list .source-item').length === 0);
    assert.equal(await page.locator('#source-list .empty').count(), 1, '空组应显示空态而不是扩大到全库');
    await chooseGroups(sourceFilter, []);
    await page.waitForFunction(() => document.querySelectorAll('#source-list .source-item').length === 2, null, {timeout: 10000});
    await go('/admin/music?id=' + music);
    const musicMembership = page.locator('#music-view [data-group-membership]');
    const musicSaved = page.waitForResponse((r) => r.url().includes('/group-memberships/music/') && r.request().method() === 'PUT');
    await chooseGroups(musicMembership, [musicGroup]);
    await musicSaved;
    assert.equal(await page.locator('main > .group-picker').count(), 0, '音乐组筛选不能插在页面标题与 Tab 上方');
    assert.equal(await page.locator('main').evaluate((main) => main.firstElementChild.matches('.studio-intro')), true);
    // 音乐库左侧组筛选：多选列表框样式，默认全选（=不限），选择后过滤左侧曲目列表。
    const musicFilter = page.locator('.template-panel #music-group-filter .group-picker');
    assert(await musicFilter.evaluate((node) => node.classList.contains('group-picker-listbox')), '音乐库组筛选应为多选列表框');
    await assertAllSelected('.template-panel #music-group-filter select[multiple]', '音乐库组筛选');
    const filteredTracks = page.waitForResponse((r) => r.url().includes('music_group_ids='));
    await chooseGroups(musicFilter, [musicGroup]);
    await (await filteredTracks).json();
    await page.waitForFunction(() => document.querySelectorAll('#music-list .job-item').length === 1);
    await audit('groups-music-detail');
    resolutions.length = 0;
    await go('/search');
    // 检索页组筛选默认全选：值为空（不限），素材联动列出全部素材。
    const searchFilter = page.locator('.material-filters .group-picker');
    await assertAllSelected('.material-filters .group-picker select[multiple]', '检索页组筛选');
    assert.deepEqual(await page.evaluate(() => searchGroups.values()), [], '默认全选应视为不限（空值）');
    await page.waitForFunction(() => document.getElementById('source').options.length === 3);
    await chooseGroups(searchFilter, ['__all__']);
    await page.waitForFunction(() => document.getElementById('source').options.length === 3);
    await assertAllSelected('.material-filters .group-picker select[multiple]', '检索页组筛选点击全部选择后');
    await chooseGroups(searchFilter, [group]);
    await page.waitForFunction(() => !document.getElementById('source').disabled);
    assert.deepEqual(await page.locator('#source option:not([value="__all__"])').evaluateAll((nodes) => nodes.map((node) => node.value)), [source]);
    assert(await page.locator('.material-filters').evaluate((node) => node.firstElementChild.classList.contains('group-picker')));
    await chooseGroups(page.locator('.group-picker'), [group, secondGroup]);
    await page.waitForFunction(() => document.getElementById('source').options.length === 3);
    await chooseGroups(page.locator('.group-picker'), [emptyGroup]);
    await page.waitForFunction(() => document.getElementById('source').disabled && document.getElementById('source').options.length === 1);
    await chooseGroups(page.locator('.group-picker'), [group]);
    await page.waitForFunction(() => !document.getElementById('source').disabled);
    await page.locator('#query').fill('红色');
    await page.locator('#granularity').selectOption('shot');
    const response = page.waitForResponse((r) => r.url().endsWith('/api/search/text'));
    await page.locator('#search-button').click();
    const search = await (await response).json();
    assert(search.results.length > 0);
    assert(search.results.every((r) => r.source_id === source));
    assert.deepEqual(await page.locator('.group-picker select option:checked').evaluateAll((nodes) => nodes.map((node) => node.value)), [group]);
    assert.equal(resolutions.length, 0, '切换、组合和重选素材组均应使用目录缓存');
    // 外部改动使缓存失效；新成员范围不能继续沿用旧结果。
    await page.evaluate(async ({group, source}) => {
      await MindexApi.json(`/api/admin/groups/${group}/members/remove`, 'POST', {member_ids:[source]});
      window.dispatchEvent(new CustomEvent('mindex:agent-mutation', {detail:{groups:true}}));
    }, {group, source});
    await page.waitForFunction(() => document.getElementById('source').disabled && document.getElementById('source').options.length === 1);
    await page.evaluate(async ({group, source}) => {
      await MindexApi.json(`/api/admin/groups/${group}/members`, 'POST', {member_ids:[source]});
      window.dispatchEvent(new CustomEvent('mindex:groups-changed'));
    }, {group, source});
    await page.waitForFunction(() => !document.getElementById('source').disabled && document.getElementById('source').options.length === 2);
    await audit('groups-search');
    await page.locator('#filter-reset').click();
    // 重置回到默认全选（=不限），而不是清空勾选。
    await assertAllSelected('.material-filters .group-picker select[multiple]', '检索页组筛选重置后');
    assert.deepEqual(await page.evaluate(() => searchGroups.values()), [], '重置后应为不限（空值）');
    await page.waitForFunction(() => document.querySelectorAll('#source option').length === 3);
    await go('/studio?job=' + process.env.MINDEX_GROUPS_JOB);
    await assertAllSelected('#create-form .group-picker select[multiple]', '任务创建组筛选');
    await chooseGroups(page.locator('#create-form > .group-picker'), [sourceGroup]);
    await page.waitForFunction(() => !document.getElementById('create-sources').disabled && document.getElementById('create-sources').options.length === 2);
    assert.deepEqual(await page.locator('#create-sources option:not([value="__all__"])').evaluateAll((nodes) => nodes.map((node) => node.value)), [source]);
    assert(await page.locator('#create-form').evaluate((node) => !!(node.querySelector('.group-picker').compareDocumentPosition(node.querySelector('label:has(#create-sources)')) & Node.DOCUMENT_POSITION_FOLLOWING)));
    await chooseGroups(page.locator('#create-form > .group-picker'), [emptyGroup]);
    await page.waitForFunction(() => document.getElementById('create-sources').disabled && document.getElementById('create-sources').options.length === 1);
    await chooseGroups(page.locator('#create-form > .group-picker'), [sourceGroup]);
    await page.waitForFunction(() => !document.getElementById('create-sources').disabled);
    await page.locator('#job-meta > summary').click();
    assert.equal(await page.locator('#job-meta-body select, #job-meta-body .group-picker, #job-meta-body button').count(), 0, '元信息只读');
    assert.equal(resolutions.length, 0, '任务创建切换素材组也应使用目录缓存');
    await audit('groups-studio');
    await page.evaluate(() => openStage(order()[0], 'content'));
    assert.equal(await page.locator('#stage-panel .recall-editor .group-picker').count(), 0, '文案与查询不显示素材组筛选');
    await page.locator('#stage-panel [data-recall-filter=source_ids]').waitFor();
    for (const language of ['zh', 'en']) {
      await page.evaluate((lang) => MindexI18n.setLanguage(lang), language);
      await page.setViewportSize({width:1920,height:1000});
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
      const rows = await page.locator('#stage-panel .editor-stretch-row').evaluateAll((rows) => rows.map((row) => {
        const nodes = [...row.querySelectorAll(':scope>.form-field,:scope>.recall-person-options>.form-field,:scope>.recall-return-options>.form-field,:scope>.visual-rewrite')];
        const r = row.getBoundingClientRect();
        return {name:row.className,left:r.left,right:r.right,width:r.width,scrollWidth:row.scrollWidth,
          fields:nodes.map((node) => {const r=node.getBoundingClientRect();return {top:r.top,left:r.left,right:r.right,width:r.width};})};
      }));
      assert.equal(rows.length, 4);
      for (const row of rows) {
        assert(row.fields.length >= 3);
        assert(row.scrollWidth <= row.width + 1, `${language}: 宽屏不应出现行内滚动 ${row.name}`);
        assert(Math.abs(row.fields[0].left-row.left)<2 && Math.abs(row.fields.at(-1).right-row.right)<2, `${language}: 控件应伸展填满整行 ${row.name}`);
        const fields=row.fields.filter((_,i)=>!row.name.includes('shot-editor-fields')||i<4);
        assert(fields.every((field)=>Math.abs(field.top-fields[0].top)<2), `${language}: 控件应保持同行 ${row.name}`);
      }
      if (language === 'zh') await page.locator('#stage-panel').screenshot({path:process.env.MINDEX_GROUPS_OUTPUT+'/editor-wide.png'});
      await page.setViewportSize({width:1280,height:1000});
    }
    await audit('groups-shot-recall');
    for (const kind of ['aigc', 'tool']) {
      await page.locator('[data-shot-source=kind]').selectOption(kind);
      const selector = kind === 'aigc' ? '.aigc-prompt-row' : '.tool-upload-options';
      await page.locator(selector).waitFor();
      await page.setViewportSize({width:1920,height:1000});
      const layout = await page.locator(selector).evaluate((row) => {
        const r=row.getBoundingClientRect();
        return {width:r.width,scrollWidth:row.scrollWidth,fields:[...row.children].map((node)=>{const r=node.getBoundingClientRect();return {top:r.top,left:r.left,right:r.right};})};
      });
      assert.equal(layout.fields.length, kind === 'aigc' ? 3 : 2);
      assert(layout.fields.every((f)=>Math.abs(f.top-layout.fields[0].top)<2), `${kind}: 控件应同行`);
      assert(layout.scrollWidth<=layout.width+1);
      await page.locator('#stage-panel').screenshot({path:process.env.MINDEX_GROUPS_OUTPUT+'/source-'+kind+'-wide.png'});
      await audit('groups-source-'+kind);
    }
    await page.evaluate(() => { state.drafts.delete(draftKey(order()[0])); openStage(order()[0], 'tts'); });
    await page.locator('.music-editor > label select').first().selectOption('library');
    // 分镜选曲的音乐组筛选（复选框样式）同样默认全选。
    await page.waitForFunction(() => {
      const box = document.querySelector('.music-library-picker .group-picker');
      const real = [...box.querySelectorAll('input[type=checkbox]:not([data-group-all])')];
      return real.length > 0 && real.every((input) => input.checked);
    });
    await chooseGroups(page.locator('.music-library-picker .group-picker'), ['__all__']);
    assert.equal(await page.locator('.music-library-picker .group-picker input[type=checkbox]:not([data-group-all]):not(:checked)').count(), 0, '音乐组全部选择应勾选所有实际组');
    await chooseGroups(page.locator('.music-library-picker .group-picker'), [musicGroup]);
    const suggested = page.waitForResponse((r) => r.url().endsWith('/music-library/recommend'));
    await page.locator('.music-library-picker button').first().click();
    const recommendation = await (await suggested).json();
    assert.deepEqual(recommendation.items.map((item) => item.id), [music]);
    await audit('groups-shot-music');
    assert.deepEqual(errors, []);
    console.log('管理、素材与音乐归属、检索、任务范围，中英文及 1280/390/320px 布局通过');
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exit(1); });
