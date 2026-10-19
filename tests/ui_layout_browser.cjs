// Isolated UI acceptance. Start serve_fixture.py, seed_layout_fixture.py and a /tmp HTTP server.
// MINDEX_LAYOUT_URL, MINDEX_LAYOUT_DATA and MINDEX_PLAYWRIGHT_MODULE override local defaults.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_LAYOUT_URL || 'http://127.0.0.1:18329';
assert.match(base, /^http:\/\/127\.0\.0\.1:/, 'acceptance must use an isolated local server');
const root = process.env.MINDEX_LAYOUT_DATA || '/tmp/mindex-ui-layout-fixture';
const reviewBase=process.env.MINDEX_LAYOUT_REVIEW_URL || 'http://127.0.0.1:18331';
const reviewURL=file=>reviewBase+'/'+path.relative(fs.realpathSync('/tmp'),file).split(path.sep).join('/');
const fixture = JSON.parse(fs.readFileSync(path.join(root, 'layout-fixture.json')));
const out = process.env.MINDEX_LAYOUT_OUTPUT || 'artifacts/ui-layout/after';
fs.mkdirSync(out, {recursive:true});
const widths = [1440,1000,800,540,390,320];
const previous=process.env.MINDEX_LAYOUT_RESUME&&fs.existsSync(path.join(out,'coverage.json'))?JSON.parse(fs.readFileSync(path.join(out,'coverage.json'))):null;
const records = previous?.records||[], problems = previous?.problems||[], errors = previous?.errors||[];
let page, browser;
const voiceCatalog = {groups:[{label:'系统音色',voices:[{voice_id:'vivian',label:'Vivian · 工程测试音色 Long fixture voice'}]}],library:[],default_voice:'vivian',clone:{supported:true,backend:'openai'},error:null};
async function audit(group, languages=['zh','en'], screenshots=true) {
  if(previous&&records.some(r=>r.group===group)){await page.setViewportSize({width:1440,height:900});if(await page.locator('[data-language]').count())await page.evaluate(()=>MindexI18n.setLanguage('zh'));return;}
  for (const lang of languages) {
    if (await page.locator('[data-language]').count()) await page.evaluate(l=>MindexI18n.setLanguage(l),lang);
    for (const width of widths) {
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
      const result = await page.evaluate(()=>{
        const controls=[], issues=[];
        const visible=n=>n.getClientRects().length && getComputedStyle(n).visibility!=='hidden' && !n.closest('template');
        const describe=n=>n.id || n.className || n.tagName.toLowerCase()+':'+(n.textContent||'').slice(0,30);
        const intrinsic='option,svg,svg *,input,textarea,.sr-only';
        const scrollers='.matrix-scroll,.candidate-strip,.profile-scroller,.multi-list,.source-shots,.json-view,#source-list,.job-panel,.tabs,.window-viewport';
        if(document.documentElement.scrollWidth>innerWidth+1)issues.push('page overflow '+document.documentElement.scrollWidth);
        for(const n of document.querySelectorAll('button,input:not([type=hidden]),textarea,select,a.quiet')) {
          if(!visible(n))continue;
          const r=n.getBoundingClientRect(),s=getComputedStyle(n),size=parseFloat(s.getPropertyValue('--control-size'))||32;
          const type=n.tagName.toLowerCase();
          controls.push({id:describe(n),tag:type,type:n.type,x:r.x,y:r.y,width:r.width,height:r.height,fontSize:s.fontSize,radius:s.borderRadius,borderColor:s.borderColor,borderWidth:s.borderWidth,lineHeight:s.lineHeight,padding:s.padding,resize:s.resize,disabled:n.disabled||false});
          if(n.multiple&&type==='select'&&n.closest('.multi-list')&&getComputedStyle(n.closest('.multi-list')).resize!=='vertical')issues.push(describe(n)+' list not resizable');
          if(type==='textarea'&&s.resize!=='both')issues.push(describe(n)+' textarea resize '+s.resize);
          if((n.type==='checkbox'||n.type==='radio')&&(Math.abs(r.height-16)>1||Math.abs(r.width-16)>1))issues.push(describe(n)+' checkbox '+r.width+'x'+r.height);
          if((type==='input'&&!['checkbox','radio','file','range','hidden'].includes(n.type)||type==='select'&&!n.multiple)&&Math.abs(r.height-size)>1)issues.push(describe(n)+' height '+r.height+' expected '+size);
          if(type==='select'&&n.multiple&&r.height<48)issues.push(describe(n)+' collapsed multiselect '+r.height);
          const custom=n.matches('.tab,.clone-mode,.source-item,.job-item,.poster-button,.gallery-cell,.role-row-head,.new-profile-card,.cell-trigger,.detail-candidate,.candidate-thumb');
          if(type==='button'&&!custom){
            if(r.height<size-1)issues.push(describe(n)+' small button '+r.height);
            if(innerWidth>540&&r.height>size+1)issues.push(describe(n)+' tall button '+r.height+' expected '+size);
            const range=document.createRange();range.selectNodeContents(n);const rr=range.getBoundingClientRect();
            if(rr.height>r.height+1||rr.width>r.width+1)issues.push(describe(n)+' clipped button text');
          }
          const d=n.closest('dialog[open]');
          if(d&&!n.closest(scrollers)){const dr=d.getBoundingClientRect();if(r.left<dr.left-1||r.right>dr.right+1)issues.push(describe(n)+' overflows dialog horizontally');}
        }
        for(const d of document.querySelectorAll('dialog[open]')) {
          const r=d.getBoundingClientRect();if(r.top<0||r.bottom>innerHeight+1||r.left<0||r.right>innerWidth+1)issues.push(d.id+' outside viewport');
          if(d.scrollWidth>d.clientWidth+1)issues.push(d.id+' internal horizontal overflow '+d.scrollWidth+'/'+d.clientWidth);
          const close=d.querySelector(':scope>button');const heading=d.querySelector(':scope>h2,:scope>h3');
          if(close&&heading){const c=close.getBoundingClientRect(),h=heading.getBoundingClientRect();if(c.left<h.right&&c.right>h.left&&c.top<h.bottom&&c.bottom>h.top)issues.push(d.id+' close overlaps heading');}
        }
        // Bottom alignment of single-line labelled fields sharing each actual flex/grid row.
        let maxAlignment=0;
        for(const host of document.querySelectorAll('.fields,.filter-grid,.form-grid,.weight-grid,.search-row,.form-bottom,.plan-bar')) {
          if(!visible(host))continue;
          const items=[];
          for(const child of host.children){
            let n=child.matches('button,input,select')?child:child.matches('label')?child.querySelector('input:not([type=checkbox],[type=radio],[type=file]),select:not([multiple])'):null;
            if(!n||!visible(n))continue;const r=n.getBoundingClientRect();if(r.height>40)continue;items.push({y:r.y,h:r.height,c:r.y+r.height/2,id:describe(n)});
          }
          for(let i=0;i<items.length;i++)for(let j=i+1;j<items.length;j++){
            const a=items[i],b=items[j];if(a.h!==b.h||Math.abs(a.y-b.y)>=Math.min(a.h,b.h))continue;
            const delta=Math.abs(a.c-b.c);maxAlignment=Math.max(delta,maxAlignment);if(delta>1)issues.push(describe(host)+' alignment '+a.id+'/'+b.id+' '+delta);
          }
        }
        return {scrollWidth:document.documentElement.scrollWidth,controlCount:controls.length,controls,issues,maxAlignment};
      });
      records.push({group,language:lang,width,...result});
      problems.push(...result.issues.map(issue=>({group,lang,width,issue})));
      if(screenshots&&[1440,320].includes(width)&&lang===languages.at(-1))await page.screenshot({path:path.join(out,`${group}-${lang}-${width}.png`),fullPage:true});
    }
  }
  const modal=page.locator('dialog[open]');if(await modal.count()){
       const last=modal.last().locator('button:not([disabled]),input:not([type=hidden],[disabled]),select:not([disabled]),textarea:not([disabled])').last();
       if(await last.isVisible()){await last.scrollIntoViewIfNeeded();const bounds=await last.boundingBox();if(bounds.y<0||bounds.y+bounds.height>900)problems.push({group,issue:'last modal control cannot be reached'});}
       await modal.last().evaluate(n=>n.scrollTop=0);
  }
  console.log('AUDIT',group);
  fs.writeFileSync(path.join(out,'coverage.json'),JSON.stringify({records,problems,errors}));
  await page.setViewportSize({width:1440,height:900});
  if(await page.locator('[data-language]').count())await page.evaluate(()=>MindexI18n.setLanguage('zh'));
}
async function goto(route){await page.goto(base+route);await page.waitForLoadState('networkidle');}
async function close(id){await page.locator(id).click();}
async function boundedResize(selector,dx,dy){
 const n=page.locator(selector).first();await n.scrollIntoViewIfNeeded();const before=await n.boundingBox();
 await page.mouse.move(before.x+before.width-3,before.y+before.height-3);await page.mouse.down();await page.mouse.move(before.x+before.width+dx-3,before.y+before.height+dy-3,{steps:8});await page.mouse.up();
 const after=await n.boundingBox();return {before,after};
}
(async()=>{
 browser=await chromium.launch({headless:true});page=await browser.newPage({viewport:{width:1440,height:900}});page.on('pageerror',e=>errors.push(e.message));
 page.setDefaultTimeout(15000);
 await page.route('**/api/settings/voices/catalog*',route=>route.fulfill({json:voiceCatalog}));
 if(!process.env.MINDEX_LAYOUT_SUPPLEMENT_ONLY){
 await goto('/');await audit('search-initial');
 await page.locator('#query').fill('绿色画面');await page.locator('#search-button').click();await page.locator('.card').first().waitFor();await audit('search-results');
 await page.locator('.card button').first().click();await audit('search-detail');await close('#close-dialog');
 const resized=await boundedResize('#query',-100,50);assert.ok(resized.after.width<resized.before.width-50&&resized.after.height>resized.before.height+25,'textarea must resize in both directions');
 const select=page.locator('#actor-select');await select.selectOption(fixture.actors[0]);const listSize=await boundedResize('#person-controls .multi-list',0,70);assert.ok(listSize.after.height>listSize.before.height+30);
 await page.locator('#person-controls .multi-search').first().fill('00');assert.equal(await select.inputValue(),fixture.actors[0]);
 await page.locator('#filter-reset').click();assert.equal(await page.locator('#person-controls .multi-search').first().inputValue(),'');assert.equal(await page.locator('#person-controls .multi-list').first().evaluate(n=>n.getBoundingClientRect().height),listSize.after.height);
 for(const mode of ['image','video']){await page.locator(`#${mode}-tab`).click();await audit(`search-${mode}`);}
 await page.locator('#text-tab').click();let release;await page.route('**/api/search/text',async route=>{await new Promise(r=>release=r);await route.fulfill({status:503,json:{detail:'布局测试：检索服务暂时不可用。 '.repeat(20)}})});
 await page.locator('#query').fill('工程错误状态');await page.locator('#search-button').click();await page.waitForFunction(()=>document.querySelector('#search-button').disabled);await audit('search-loading',undefined,false);release();await page.locator('#notice.error').waitFor();await audit('search-error');await page.unroute('**/api/search/text');
 await goto('/admin');assert.equal(await page.locator('header #reload').count(),1);await audit('admin-initial');
 await page.locator('.source-item').filter({hasText:'工程测试专用'}).first().click();await page.locator('.work-card').last().waitFor();await audit('admin-workspace');
 await page.locator('[data-work=dialogue] select').first().selectOption('subtitle');await audit('admin-subtitle');
 await page.locator('#source-detail').getByRole('button',{name:'备注',exact:true}).click();await page.locator('#edit-name').fill('长素材名称 '.repeat(20));await page.locator('#edit-notes').fill('长备注文本\n'.repeat(30));await audit('admin-edit');await close('#edit-close');
 await page.locator('.poster-button').first().click();await audit('admin-play');await page.locator('#play-video').evaluate(v=>v.play());await close('#play-close');await page.waitForFunction(()=>document.querySelector('#play-video').paused);
 await page.locator('.segment-card').first().getByRole('button',{name:'详情',exact:true}).click();await page.locator('.analysis-section').first().waitFor();await audit('admin-analysis');await close('#analysis-close');
 await page.locator('#add-source').click();await audit('admin-upload');await close('#upload-close');
 await page.locator('[data-work=persons]').getByRole('button',{name:'配置并处理'}).click();await page.locator('#person-work option').last().waitFor({state:'attached'});await audit('admin-person-setup');await close('#person-close');
 await goto('/admin?source='+fixture.paged_source_id);await page.locator('.segment-card').last().waitFor();await audit('admin-many-long');assert.ok(await page.locator('#segment-next').isEnabled());await page.locator('#segment-next').click();await page.waitForFunction(()=>document.querySelector('#segment-page').textContent.includes('2 /'));await page.locator('#moments-tab').click();await audit('admin-empty-segments');
 await goto('/persons?draft='+fixture.draft_id);await page.locator('.entry').last().waitFor();await audit('persons-cast-candidates');await page.locator('.entry input').first().fill('修改后的候选演员');await page.locator('#import-work').selectOption(fixture.work_id);await page.locator('#import-save').click();await page.locator('#notice').filter({hasText:'草稿'}).waitFor();
 await page.locator('[data-pane=catalogue]').click();await page.locator('.actor-card').last().waitFor();await audit('persons-catalogue');await page.locator('.actor-card').first().click();await page.locator('.role-card').first().waitFor();await audit('persons-expanded-roles');
 await page.locator('#entity-new').click();await page.locator('#entity-name').fill('新演员名称');await audit('persons-entity');await page.locator('#entity-kind').selectOption('role');await audit('persons-role-fields');await page.locator('#entity-kind').selectOption('actor');await page.locator('#entity-name').fill('新增验收演员 '+Date.now());await page.locator('#entity-save').click();await page.waitForFunction(()=>!document.querySelector('#entity-dialog').open);
 await page.locator('.actor-card').first().getByRole('button',{name:'关联分镜',exact:true}).click();await audit('persons-linked');await close('#linked-close');
 await page.locator('#capture-source').selectOption(fixture.source_id);await page.locator('#capture-time').fill('2');await page.locator('#capture-preview').click();await page.locator('#capture-canvas').waitFor();await audit('persons-capture');
 await page.locator('[data-pane=processing]').click();await page.locator('#review-source').selectOption(fixture.source_id);await page.locator('#review-refresh').click();await page.locator('#evidence-list .person-card').first().waitFor();await audit('persons-processing-review');
 await page.locator('#evidence-list .person-card').first().getByRole('button',{name:'核对整个分镜',exact:true}).click();await audit('persons-single-review');await close('#review-close');
 await page.locator('#review-select-all').click();await page.waitForFunction(()=>!document.querySelector('#review-batch').disabled);await page.locator('#review-batch').click();await audit('persons-batch-review');await close('#review-close');
 await goto('/settings');await page.locator('#roles .role-row-head').last().waitFor();await audit('settings-initial');
  // All six roles and every available provider preset, using the real dynamic editor.
 for(let i=0;i<6;i++){
  const head=page.locator('#roles .role-row-head').nth(i);if(await head.getAttribute('aria-expanded')!=='true')await head.click();
  await page.locator('#roles .role-row').nth(i).locator('.new-profile-card').click();
  const role=await page.locator('#pf-role').inputValue();
  const providers=await page.locator('#pf-provider option').evaluateAll(ns=>ns.map(n=>n.value));
  for(const provider of providers){await page.locator('#pf-provider').selectOption(provider);await audit(`settings-${role}-${provider}`,undefined,['self','minimax'].includes(provider));}
  // Backend switches can change the voice field from list to manual input.
  for(const backend of await page.locator('#pf-backend option').evaluateAll(ns=>ns.map(n=>n.value))){await page.locator('#pf-backend').selectOption(backend);await audit(`settings-${role}-backend-${backend}`,undefined,false);}
  await close('#profile-close');
 }
 await page.locator('[data-section=tuning]').click();await audit('settings-tuning');await page.locator('[data-section=voices]').click();await page.locator('#voice-register').click();await audit('settings-voice-register');
 for(const mode of ['url','upload']){await page.locator(`.clone-mode[data-mode=${mode}]`).click();await audit('settings-voice-'+mode);}
 await close('#clone-close');
 await goto('/studio');await audit('studio-initial');await page.locator('#create-text').fill('山风吹过山谷，远处的寺庙亮着灯。');await page.locator('#create-button').click();await page.locator('#job-workspace').waitFor();
 await page.locator('[data-stage=split]').click();await page.locator('#matrix-body .cell-trigger').first().waitFor();await page.waitForFunction(()=>document.querySelector('#execution-log').textContent.includes('执行结束'));await audit('studio-board');
 for(const [id,name] of [['new-plan','new'],['copy-plan','copy'],['rename-plan','rename'],['plan-settings','parameters'],['run-all','preflight']]){await page.locator('#'+id).click();await page.locator('#work-dialog').waitFor();await audit('studio-dialog-'+name);await close('#dialog-close');}
 await page.locator('#matrix-body .cell-trigger').first().click();await audit('studio-text-editor');
 for(const mode of ['image','video','text']){await page.locator('.recall-editor select').first().selectOption(mode);await audit('studio-query-'+mode);}
 await page.getByRole('button',{name:'取消修改',exact:true}).click();
 for(const stage of ['recall','tts','select','render']){await page.locator(`[data-stage=${stage}]`).click();await page.waitForFunction(stage=>!busy()&&Object.values(state.data.versions).some(v=>v.stage===stage&&v.status==='complete')&&state.data.builds.at(-1)?.status!=='running',stage);}
 for(let i=1;i<5;i++){await page.locator('#matrix-body .shot-summary-row').first().locator('.cell-trigger').nth(i).click();await audit('studio-stage-'+i);}
 await page.locator('#matrix-body .shot-summary-row').first().locator('.cell-trigger').nth(3).click();

 // Verify the real manual window, save, candidate preview and version details.
 await page.locator('.window-viewport').waitFor();await audit('studio-window');const slider=page.locator('.fixed-window');await slider.focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('ArrowLeft');await page.getByRole('button',{name:'保存当前窗口',exact:true}).click();await page.waitForFunction(()=>!busy());assert.equal(await page.locator('.manual-range-host .window-viewport').count(),1);assert.equal(await page.locator('.candidate-picker').count(),0);await page.locator('#matrix-body .shot-summary-row').first().locator('.cell-trigger').nth(1).click();await page.locator('.candidate-picker button').filter({hasText:'预览'}).first().click();await audit('studio-candidate-player');await close('#play-close');
 await page.locator('.shot-detail-panel').getByRole('button',{name:'查看完整版本',exact:true}).click();await audit('studio-version-detail');await close('#dialog-close');
 await page.locator('#collapse-all-rows').click();await page.locator('#run-all').click();await page.locator('#dialog-content').getByRole('button',{name:'确认构建',exact:true}).click();await page.locator('#final-video').waitFor({timeout:60000});await page.waitForFunction(()=>!busy());await audit('studio-final');
 await page.locator('#final-history>details>summary').click();await audit('studio-final-history');await page.locator('#final-history').getByRole('button',{name:'复制为新方案',exact:true}).first().click();await audit('studio-restore-plan');await close('#dialog-close');
 await page.locator('#artifacts-section>summary').click();await audit('studio-artifacts');
 for(const [kind,url] of [['film',reviewURL(fixture.film)],['labels',reviewURL(fixture.labels)]]){
  await page.goto(url);await audit('review-'+kind,['zh']);
  if(kind==='film'){await page.locator('#next').click();await audit('review-film-page2',['zh'],false);await page.locator('[data-filter=scene]').click();await audit('review-film-empty',['zh'],false);await page.locator('[data-filter=all]').click();}
  else {await page.locator('details summary').first().click();await audit('review-labels-candidates',['zh']);await page.locator('article button').first().click();await audit('review-labels-player',['zh']);await page.locator('#player-video').evaluate(v=>v.play());await close('#close-player');await page.waitForFunction(()=>document.querySelector('#player-video').paused);await page.locator('#export').click();await audit('review-labels-error',['zh'],false);}
 }
 }
 // Controlled response branches use the actual page renderers and actions, with deterministic data.
 await goto('/settings');
 const settings=await page.request.get(base+'/api/settings').then(r=>r.json());
 const rich=structuredClone(settings);rich.embedding_status={active_index_id:'fixture-index',matches_index:true};
 for(const role of rich.roles){role.active_profile_id=101;role.profiles=Array.from({length:6},(_,i)=>({id:101+i,role:role.role,provider:'self',name:'配置档案 Long profile title '.repeat(5),config:{...role.effective,api_key:undefined},api_key:{has_key:true,hint:'••••test'}}));}
 await page.route('**/api/settings',route=>route.fulfill({json:rich}));await page.reload();await page.locator('#roles .role-row-head').last().waitFor();
 for(const head of await page.locator('#roles .role-row-head').all())if(await head.getAttribute('aria-expanded')!=='true')await head.click();
 await audit('settings-many-profiles');
 await page.locator('#roles .profile-actions').first().getByRole('button',{name:'编辑',exact:true}).click();await audit('settings-profile-key-clear');await close('#profile-close');
 await page.route('**/api/settings/profiles/*/test',route=>route.fulfill({json:{ok:false,detail:'工程测试连接失败 '.repeat(30)}}));await page.locator('#roles .profile-actions').first().getByRole('button',{name:'测试连接',exact:true}).click();await page.locator('.test-result.error').first().waitFor();await audit('settings-profile-test-error');
 await page.unroute('**/api/settings');await page.unroute('**/api/settings/profiles/*/test');
 voiceCatalog.clone.ref_text=true;await page.reload();await page.locator('#roles .role-row-head').last().waitFor();
 if(await page.locator('[data-section=voices]').getAttribute('aria-expanded')!=='true')await page.locator('[data-section=voices]').click();
 await page.locator('#voice-clone').click();await audit('settings-clone-reference-text');await close('#clone-close');
 await page.route('**/api/settings/voices/catalog*',route=>route.fulfill({json:{...voiceCatalog,groups:[],library:[],voice_kind:'clone'}}));
 const tts=page.locator('#roles .role-row-head').nth(4);if(await tts.getAttribute('aria-expanded')!=='true')await tts.click();await page.locator('#roles .role-row').nth(4).locator('.new-profile-card').click();await page.locator('#pf-voice').waitFor();await audit('settings-empty-cloned-voice-list');await close('#profile-close');
 await page.route('**/api/settings/voices/catalog*',route=>route.fulfill({status:503,json:{detail:'工程目录获取失败'}}));await page.locator('#roles .role-row').nth(4).locator('.new-profile-card').click();await page.locator('#pf-voice').waitFor();await audit('settings-manual-voice-fallback');await close('#profile-close');
 await goto('/persons');await page.locator('[data-pane=processing]').click();await page.locator('#review-source').selectOption(fixture.source_id);
 for(const status of ['accepted','rejected']){await page.locator('#review-status').selectOption(status);await page.waitForFunction(()=>document.querySelector('#evidence-list').children.length>0);await audit('persons-empty-'+status);}
 fs.writeFileSync(path.join(out,'coverage.json'),JSON.stringify({records,problems,errors}));
 assert.deepEqual(errors,[],'browser script errors');assert.deepEqual(problems,[],'layout violations');
 console.log(`PASS: ${records.length} viewport/state/language cases, ${records.reduce((n,r)=>n+r.controlCount,0)} measured controls.`);
})().catch(error=>{console.error(error);fs.writeFileSync(path.join(out,'coverage.json'),JSON.stringify({records,problems,errors}));process.exitCode=1}).finally(async()=>browser?.close());
