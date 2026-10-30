/* All application pages and dynamic forms, against a disposable deterministic fixture. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const base=process.env.MINDEX_CONTROL_URL, out=process.env.MINDEX_CONTROL_OUTPUT;
 assert.match(base,/^http:\/\/127\.0\.0\.1:/);
 fs.mkdirSync(out,{recursive:true});
 const fixture=JSON.parse(fs.readFileSync(process.env.MINDEX_CONTROL_FIXTURE,'utf8'));
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[], records=[], issues=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/api/settings/voices/catalog*',route=>route.fulfill({json:{groups:[],library:[],default_voice:null,clone:{supported:false},error:null}}));
 async function audit(name){
  for(const language of ['zh','en']){
   await page.evaluate(lang=>window.MindexI18n?.setLanguage(lang),language);
   for(const width of [1440,1000,800,540,390,320]){
    await page.setViewportSize({width,height:1000});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const result=await page.evaluate(()=>{
     const faults=[], controls=[];
     const visible=n=>n.getClientRects().length && getComputedStyle(n).visibility!=='hidden' && n.getBoundingClientRect().width>2 && n.getBoundingClientRect().height>2;
     if(document.documentElement.scrollWidth>innerWidth+1) faults.push(`page overflow ${document.documentElement.scrollWidth}/${innerWidth}`);
     for(const n of document.querySelectorAll('button,input:not([type=hidden]),textarea,select')){
      if(!visible(n))continue;
      const r=n.getBoundingClientRect();
      const id=n.id||n.className||n.tagName;
      controls.push({id,x:r.x,y:r.y,width:r.width,height:r.height});
      if(!n.closest('.shot-strip,.candidate-strip,.window-viewport,.multi-list,#source-list,.job-panel,.profile-scroller,.tabs') && (r.x < -1 || r.right>innerWidth+1)) faults.push(`${id} outside viewport ${r.x}/${r.right}`);
      if(n.type==='checkbox'||n.type==='radio') if(Math.abs(r.width-16)>1||Math.abs(r.height-16)>1) faults.push(`${id} stretched checkbox ${r.width}/${r.height}`);
      if(n.tagName==='BUTTON' && !n.matches('.role-row-head,.source-item,.job-item,.new-profile-card,.gallery-cell,.gallery-delete,.tool-item,.tab,.clone-mode,.shot-card,.stage-menu-button,.strip-nav,.card-play,.poster-button')){
       const range=document.createRange();range.selectNodeContents(n);const text=range.getBoundingClientRect();
       if(text.width>r.width+1 || text.height>r.height+1)faults.push(`${id} clipped button label`);
      }
     }
     for(const n of document.querySelectorAll('dialog[open]')){
      if(n.scrollWidth>n.clientWidth+1)faults.push(`${n.id} internal horizontal overflow`);
     }
     for(const n of document.querySelectorAll('.form-grid,.template-fields,.tool-fields,.source-options,.recall-primary-options,.recall-scope-options,.recall-filters')){
      const children=[...n.children].filter(visible), r=n.getBoundingClientRect();
      if(visible(n)&&r.width>500 && children.length>=2 && getComputedStyle(n).gridTemplateColumns.split(' ').length<2)faults.push(`${n.id||n.className} unnecessarily stacked`);
     }
     return {controls,faults};
    });
    records.push({name,language,width,controls:result.controls.length});
    issues.push(...result.faults.map(message=>({name,language,width,message})));
    if(language==='zh'&&[1440,390].includes(width))await page.screenshot({path:path.join(out,`${name}-${width}.png`)});
   }
  }
  fs.writeFileSync(path.join(out,'coverage.json'),JSON.stringify({records,issues,errors},null,2));
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>window.MindexI18n?.setLanguage('zh'));
 }
 async function go(route){await page.goto(base+route);await page.waitForLoadState('networkidle');}
 try{
  await go('/');await audit('search');
  for(const id of ['image-tab','video-tab']){await page.locator('#'+id).click();await audit('search-'+id);}
  await go('/admin');await audit('admin');await page.locator('.source-item').first().click();await page.locator('details.work-panel').waitFor();await page.locator('details.work-panel > summary').click();await page.locator('.work-card').first().waitFor();await audit('admin-workspace');
  await page.locator('#add-source').click();await audit('admin-upload');await page.locator('#upload-close').click();
  await go('/admin/music');await audit('music');await page.locator('#music-list .template-item').first().click();await audit('music-editor');
  await go('/persons');await audit('persons');
  await page.locator('#entity-new').click();await audit('person-editor');await page.locator('#entity-close').click();await page.locator('.actor-card').first().click();await page.locator('.role-card > button').first().click();await audit('person-role-editor');await page.locator('#entity-close').click();
  await page.locator('[data-pane=cast]').click();await audit('persons-import');
  await go('/persons?draft='+fixture.draft_id);await page.locator('.entry').first().waitFor();await audit('persons-import-draft');
  await page.locator('[data-pane=processing]').click();await audit('persons-processing');
  await go('/studio?job='+process.env.MINDEX_CONTROL_JOB);await page.locator('#plan-settings').waitFor();await audit('studio');
  await page.locator('#plan-settings').click();await audit('studio-plan');await page.locator('#dialog-close').click();
  await go('/studio/templates');await page.locator('.template-item').first().click();await audit('copy-templates');
  await go('/studio/brands');await page.locator('.template-item').first().click();await audit('style-templates');
  await go('/studio/topics');await audit('topics');await page.locator('#profile-new').click();await audit('topic-profile');await page.locator('#profile-close').click();await page.locator('details.topics-section').evaluateAll(nodes=>nodes.forEach(n=>n.open=true));for(const kind of await page.locator('#sf-kind option').evaluateAll(nodes=>nodes.map(n=>n.value))){await page.locator('#sf-kind').selectOption(kind);await audit('topic-source-'+kind);}
  await go('/studio/delivery-templates');await audit('delivery-reference');
  await go('/settings');await page.locator('.role-row-head').first().waitFor();await audit('settings');
  const count=await page.locator('#roles .role-row').count();
  for(let i=0;i<count;i++){
   const row=page.locator('#roles .role-row').nth(i), head=row.locator('.role-row-head');if(await head.getAttribute('aria-expanded')!=='true')await head.click();
   await row.locator('.new-profile-card').click();await audit('model-profile-'+i);for(const provider of await page.locator('#pf-provider option').evaluateAll(nodes=>nodes.map(n=>n.value))){await page.locator('#pf-provider').selectOption(provider);await audit('model-profile-'+i+'-'+provider);}await page.locator('#profile-close').click();
  }
  for(const section of ['tuning','voices','releases','services','browser','tools']){await page.locator(`[data-section=${section}]`).click();await audit('settings-'+section);}
  await page.locator('#tool-new').click();await audit('settings-tool-editor');
  await page.locator('[data-section=voices]').click();
  if(!await page.locator('#voice-register').isVisible())await page.locator('[data-section=voices]').click();
  await page.locator('#voice-register').click();await audit('settings-voice-register');await page.locator('#clone-close').click();
  await go('/assets/shared/login.html');await page.locator('input[name=token]').waitFor();await audit('login');
  for(const [name,file] of [['review-film',fixture.film],['review-labels',fixture.labels]]){await page.goto(require('node:url').pathToFileURL(file).href);await audit(name);}
 }finally{
  fs.writeFileSync(path.join(out,'coverage.json'),JSON.stringify({records,issues,errors},null,2));
  await browser.close();
 }
 assert.deepEqual(errors,[],'Browser exceptions');
 assert.deepEqual(issues,[],'Control layout violations');
 console.log(`Passed ${records.length} layout checks across all pages and dynamic forms.`);
})().catch(error=>{console.error(error);process.exit(1);});
