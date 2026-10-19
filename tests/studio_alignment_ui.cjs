// Real workflow and geometry checks against the isolated deterministic fixture.
const assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require(process.env.MINDEX_PLAYWRIGHT_MODULE||'playwright');
const base=process.env.MINDEX_ALIGNMENT_URL||'http://127.0.0.1:18341';
assert.match(base,/^http:\/\/127\.0\.0\.1:/);
const out='artifacts/studio-alignment';fs.mkdirSync(out,{recursive:true});
let browser,page;
(async()=>{
 browser=await chromium.launch({headless:true});page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.setDefaultTimeout(30000);const errors=[],records=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/studio');await page.locator('[data-language=zh]').click();
 await page.locator('#create-text').fill('红色开场画面讲述一个故事，绿色树林中光线渐变，人物回望，远处寺庙亮着灯。');
 await page.locator('#create-button').click();await page.locator('#job-workspace').waitFor();
 await page.locator('[data-stage=split]').click();await page.waitForFunction(()=>!busy()&&document.querySelector('.cell-trigger'));
 const row=page.locator('.shot-summary-row').first();
 await row.locator('.cell-trigger').nth(3).click();
 assert.equal(await page.locator('.candidate-picker').count(),0,'no repeated anchors even before selection exists');
 assert.equal(await page.getByText('新选片的参考锚点',{exact:true}).count(),0);
 for(const stage of ['recall','tts','select']){
  await page.locator(`[data-stage=${stage}]`).click();
  await page.waitForFunction(s=>!busy()&&Object.values(state.data.versions).some(v=>v.stage===s&&v.status==='complete'),stage);
 }
 await page.locator('#collapse-all-rows').click();
 for(const lang of ['zh','en'])for(const width of [1440,1000,800,540,390,320]){
  await page.evaluate(l=>MindexI18n.setLanguage(l),lang);await page.setViewportSize({width,height:1000});
  const rows=await page.locator('.shot-summary-row').evaluateAll(rows=>rows.map(row=>[...row.querySelectorAll('.cell-expand')].map(n=>n.getBoundingClientRect().bottom)));
  for(const ys of rows)assert.ok(Math.max(...ys)-Math.min(...ys)<=1,'five summary bottom actions aligned');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  records.push({group:'summary-bottom',lang,width,rows});
 }
 // Each expanded stage still provides its version selector and reachable controls.
 for(let stage=0;stage<4;stage++){
  await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>MindexI18n.setLanguage('zh'));
  await row.locator('.cell-trigger').nth(stage).click();
  const host=page.locator('.shot-detail-panel .version-history');await host.locator('select').waitFor();
  for(const lang of ['zh','en'])for(const width of [1440,1000,800,540,390,320]){
   await page.evaluate(l=>MindexI18n.setLanguage(l),lang);await page.setViewportSize({width,height:1000});
   const controls=await host.locator('select,button').evaluateAll(nodes=>nodes.map(n=>{const r=n.getBoundingClientRect();return {tag:n.tagName,text:n.textContent,y:r.y,h:r.height,center:r.y+r.height/2};}));
   assert.equal(controls.length,3);
   for(const c of controls)assert.equal(c.h,32);
   for(let i=0;i<controls.length;i++)for(let j=i+1;j<controls.length;j++)if(Math.abs(controls[i].y-controls[j].y)<32)assert.ok(Math.abs(controls[i].center-controls[j].center)<=1);
   if(stage===3)assert.equal(await page.locator('.candidate-picker').count(),0);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   records.push({group:'history-'+stage,lang,width,controls});
  }
  await page.setViewportSize({width:1440,height:1000});await page.evaluate(()=>MindexI18n.setLanguage('zh'));
  await host.locator('button').first().click();await page.locator('#work-dialog[open]').waitFor();await page.locator('#dialog-close').click();
 }
 await page.locator('.manual-range-host .fixed-window').waitFor();
 const slider=page.locator('.manual-range-host .fixed-window');await slider.focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('ArrowLeft');
 const response=page.waitForResponse(r=>r.url().endsWith('/range-confirm')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'保存当前窗口',exact:true}).click();assert.equal((await response).status(),200);
 await page.waitForFunction(()=>!busy());await page.locator('#notice').filter({hasText:'当前窗口已保存'}).waitFor();
 assert.equal(await page.locator('.candidate-picker').count(),0);await page.locator('.manual-range-host .window-viewport').waitFor();
 const history=page.locator('.shot-detail-panel .version-history');
 const selected=await history.locator('select').inputValue();
 await history.locator('button').nth(1).click();await page.waitForFunction(()=>!busy());
 assert.equal(await page.evaluate(()=>plan().shots[order()[0]].select),selected);
 for(const width of [1440,320]){await page.setViewportSize({width,height:1000});await page.locator('.shot-detail-panel').scrollIntoViewIfNeeded();await page.screenshot({path:`${out}/select-${width}.png`});}
 await page.setViewportSize({width:1440,height:1000});await row.locator('.cell-trigger').nth(1).click();
 await page.locator('.candidate-picker').waitFor();await page.locator('.candidate-picker').getByRole('button',{name:'预览',exact:true}).first().click();
 await page.locator('#play-dialog').waitFor();await page.locator('#play-close').click();
 assert.deepEqual(errors,[]);fs.writeFileSync(`${out}/final-coverage.json`,JSON.stringify({records,errors,actions:['select-before-generation','four-stages','history-detail','window-keyboard','window-save','select-history','recall-preview']},null,2));
 console.log('PASS: 60 alignment layouts; no duplicate select picker; history details and selection, window save, recall preview.');
 await browser.close();
})().catch(async e=>{console.error(e);await page?.screenshot({path:`${out}/failure.png`});await browser?.close();process.exit(1);});
