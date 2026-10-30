// Run with serve_fixture.py on an isolated library. No real model/service required.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const batchRun=async(page,stage)=>{if(stage==='split'&&!await page.evaluate(()=>plan().script)){await page.locator('#script-text').fill(await page.evaluate(()=>state.data.job.outline||'红色开场。'));await page.evaluate(()=>saveScript());await page.waitForFunction(()=>!!plan().script);}await page.locator('#batch-stage').selectOption(stage==='recall'?'picture':stage);await page.locator('#batch-regenerate').click();};
const openShotStage=(page,shot,n)=>page.evaluate(([i,k])=>{const sid=order()[i];openStage(sid,barStages(sid)[k]);},[shot,n]);
const base = process.env.MINDEX_I18N_TEST_URL || 'http://127.0.0.1:8027';
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/settings/voices/catalog', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({groups:[{label:'系统音色 · Qwen3-TTS',voices:[{voice_id:'vivian',label:'Vivian · 明快、略带锋芒的年轻女声（中文）'}]}],library:[],default_voice:'vivian',clone:{supported:false},error:null})}));
    async function toggle(lang) { await page.evaluate(lang => MindexI18n.setLanguage(lang), lang); }
    async function preserve(selector) {
      await page.evaluate(selector => {window.kept = document.querySelector(selector);}, selector);
      await toggle('en'); await toggle('zh');
      assert.ok(await page.evaluate(selector => window.kept === document.querySelector(selector), selector), selector);
    }
    await page.goto(base + '/');
    assert.equal(await page.locator('html').getAttribute('lang'),'zh-CN');
    await page.locator('#query').fill('中文素材与未提交输入');
    await page.locator('#actor-select').waitFor();
    await page.locator('.multi-search').first().fill('演员');
    await page.evaluate(()=>{document.querySelector('#query').focus();document.querySelector('#query').setSelectionRange(2,5);});
    await toggle('en');
    assert.equal(await page.locator('#query').inputValue(),'中文素材与未提交输入');
    assert.equal(await page.locator('.multi-search').first().inputValue(),'演员');
    assert.deepEqual(await page.evaluate(()=>[document.activeElement.id,document.querySelector('#query').selectionStart,document.querySelector('#query').selectionEnd]),['query',2,5]);
    await preserve('#actor-select');
    await toggle('en');
    await page.locator('[data-query]').first().click();
    assert.match(await page.locator('#query').inputValue(),/close-up/i);
    const peer = await context.newPage(); await peer.goto(base + '/admin');
    assert.equal(await peer.locator('html').getAttribute('lang'),'en');
    await toggle('zh'); await peer.waitForFunction(()=>document.documentElement.lang==='zh-CN');
    await toggle('en'); await page.reload();
    assert.equal(await page.locator('html').getAttribute('lang'),'en');
    // Delayed search response renders in the language selected while waiting.
    let release;
    await page.route('**/api/search/text', async route => {await new Promise(resolve=>{release=resolve});await route.continue();});
    await page.locator('#query').fill('绿色画面'); await page.locator('#search-button').click();
    await page.waitForFunction(()=>document.querySelector('#search-button').disabled);
    await toggle('zh'); await toggle('en'); release();
    await page.locator('.card').first().waitFor();
    assert.match(await page.locator('#result-count').innerText(),/clips/);
    assert.match(await page.locator('.card-title').first().innerText(),/工程测试专用/);
    await page.unroute('**/api/search/text');
    const video = page.locator('.card video').first();
    await video.evaluate(async v=>{if(v.shotPlayer) await v.shotPlayer.ready;await v.play();});
    await preserve('.card video');
    assert.equal(await video.evaluate(v=>v.paused),false);
    await page.locator('.card').first().getByRole('button',{name:'查看详情'}).click();
    await preserve('#detail-dialog video'); assert.equal(await page.locator('#detail-dialog').evaluate(d=>d.open),true);
    await page.locator('#close-dialog').click();
    // Upload inputs, form values and filter controls stay attached.
    await page.goto(base + '/admin');
    await page.locator('.source-item').first().click();
    await page.locator('details.work-panel').waitFor();
    if (!await page.locator('details.work-panel').evaluate(node => node.open)) await page.locator('.work-toolbar').click();
    await page.locator('.work-card').last().waitFor();
    await page.locator('#add-source').click();
    let finishUpload;
    await page.route('**/api/admin/sources/upload',async route=>{await new Promise(resolve=>{finishUpload=resolve});await route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({detail:'无效的文件名'})});});
    await page.locator('#upload-files').setInputFiles({name:'中文文件.mp4',mimeType:'video/mp4',buffer:Buffer.from('fixture')});
    await page.locator('.upload-row').waitFor();await preserve('#upload-files');
    assert.match(await page.locator('.upload-row').innerText(),/中文文件.mp4/);finishUpload();
    await page.locator('.upload-row .source-warning').waitFor();
    await page.locator('#upload-close').click();await preserve('[data-work=dialogue] select');
    await page.locator('#source-detail').getByRole('button',{name:'备注',exact:true}).click();
    await page.locator('#edit-notes').fill('未保存素材备注');await preserve('#edit-notes');
    assert.equal(await page.locator('#edit-notes').inputValue(),'未保存素材备注');await page.locator('#edit-close').click();
    // Settings modal contains dynamic label/input pairs: preserve the inputs and entered credentials.
    await page.goto(base + '/settings');await page.locator('#roles .role-row-head').first().waitFor();
    if(await page.locator('#roles .role-row-head').first().getAttribute('aria-expanded')!=='true')await page.locator('#roles .role-row-head').first().click();
    await page.locator('.new-profile-card').first().click();
    await page.locator('#pf-name').fill('我的中文档案');await page.locator('#pf-api-key').fill('test-key-not-a-secret');
    await page.locator('#pf-model').fill('model-id');await preserve('#pf-extra input');
    assert.equal(await page.locator('#pf-name').inputValue(),'我的中文档案');
    assert.equal(await page.locator('#pf-api-key').inputValue(),'test-key-not-a-secret');
    assert.equal(await page.locator('#profile-dialog').evaluate(n=>n.open),true);
    await page.locator('#profile-close').click();
    await page.goto(base + '/persons');await page.locator('[data-pane=catalogue]').click();await page.locator('#entity-new').click();
    await page.locator('#entity-name').fill('中文人物');await preserve('#entity-name');
    assert.equal(await page.locator('#entity-name').inputValue(),'中文人物');await page.locator('#entity-close').click();
    // Create real fixture storyboard; retain unsaved narration/query and dynamic selects.
    await page.goto(base + '/studio');await page.locator('#create-text').fill('山风吹过山谷，远处的寺庙亮着灯。');await page.locator('#create-button').click();
    await page.locator('#job-workspace').waitFor({state:'visible'});await batchRun(page,'split');
    await page.locator('#shot-strip .shot-card').first().waitFor();
    await page.waitForFunction(()=>document.querySelector('#execution-log').textContent.includes('执行结束'));
    const chineseLog = await page.locator('#execution-log').innerText();await toggle('en');
    assert.ok(!/[\u4e00-\u9fff]/.test(await page.locator('#execution-log').innerText()));
    await toggle('zh');assert.equal(await page.locator('#execution-log').innerText(),chineseLog);
    await page.locator('#shot-strip .shot-card').first().click();
    const narration = page.locator('textarea[aria-label="旁白与字幕"]');await narration.fill('未保存的中文旁白');
    await page.evaluate(()=>{window.keptNarration=document.querySelector('textarea[aria-label="旁白与字幕"]');});
    await toggle('en');assert.equal(await page.locator('textarea[aria-label="Narration and subtitles"]').inputValue(),'未保存的中文旁白');
    assert.ok(await page.evaluate(()=>window.keptNarration.isConnected));
    await toggle('zh');await page.getByRole('button',{name:'取消修改',exact:true}).click();
    await page.locator('#plan-settings').click();await page.locator('#dialog-content select').first().waitFor();await preserve('#dialog-content select');
    assert.ok(await page.locator('#dialog-content select').count()>0);await page.locator('#dialog-close').click();
    // No switch-related fetches or reloads; long English labels fit narrow viewport.
    for (const path of ['/','/admin','/persons','/settings','/studio']) {
      await page.goto(base+path);await page.waitForTimeout(300);let requests=0;const count=()=>requests++;
      page.on('request',count);await toggle('en');await toggle('zh');page.off('request',count);assert.equal(requests,0,path);
      await toggle('en');await page.setViewportSize({width:390,height:844});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),path);
      await page.screenshot({path:`/tmp/mindex-i18n-${path.slice(1)||'search'}-mobile.png`,fullPage:true});
      await page.setViewportSize({width:1440,height:1000});
    }
    const unavailable = await browser.newContext();
    await unavailable.addInitScript(()=>{Object.defineProperty(window,'localStorage',{get(){throw Error('storage unavailable')}})});
    const fallback = await unavailable.newPage();await fallback.goto(base+'/');
    await fallback.locator('[data-language=en]').click();assert.equal(await fallback.locator('html').getAttribute('lang'),'en');
    await fallback.locator('[data-language=zh]').click();assert.equal(await fallback.locator('html').getAttribute('lang'),'zh-CN');
    assert.deepEqual(errors,[]);console.log('PASS: five pages; dynamic search/storyboard/settings; unchanged content; node/input/file/focus/playback retention; no switch requests; delayed response; persistence; cross-tab sync; blocked storage; narrow layout.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
