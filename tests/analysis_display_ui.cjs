const assert=require('node:assert/strict');
const {chromium}=require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const base=process.env.MINDEX_ANALYSIS_URL;
 assert.match(base,/^http:\/\/127\.0\.0\.1:/);
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();
  const errors=[];let detailsRequests=0,searchRequests=0;
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(/\/api\/admin\/segments\/[^/]+\/details$/.test(request.url()))detailsRequests++;if(request.url().endsWith('/api/search/text'))searchRequests++;});
  async function choose(language){await page.evaluate(lang=>MindexI18n.setLanguage(lang),language);}
  async function assertSelected(root,language){
   const text=(await root.locator('[data-analysis-text]').allTextContents()).join('\n');
   assert(text.length>0);
   if(language==='en'){
    assert(text.includes('City lights'),text);assert(text.includes('walking'),text);
    assert(!text.includes('城市夜景')&&!text.includes('行走')&&!text.includes('远景'),text);
   }else{
    assert(text.includes('城市夜景'),text);assert(text.includes('行走'),text);
    assert(!text.includes('City lights')&&!text.includes('walking')&&!text.includes('wide shot'),text);
   }
  }
  await page.goto(base+'/admin?source='+process.env.MINDEX_ANALYSIS_SOURCE);
  const card=page.locator(`[data-segment-id="${process.env.MINDEX_ANALYSIS_SHOT}"]`);
  await card.locator('.organization-actions button').first().click();
  const admin=page.locator('#analysis-content');await admin.locator('[data-analysis-text]').first().waitFor();
  for(const language of ['zh','en','zh']){await choose(language);await assertSelected(admin,language);assert((await page.locator('#analysis-title').textContent()).startsWith(language==='en'?'Shot ':'镜头 '));assert((await card.locator('h3').textContent()).startsWith(language==='en'?'Shot ':'镜头 '));assert((await admin.textContent()).includes('对白原文 / Original dialogue'));}
  assert.equal(detailsRequests,1,'Language switching must not request details again');
  const raw=await page.evaluate(async id=>(await (await fetch('/api/admin/segments/'+id+'/details')).json()).llm.annotation,process.env.MINDEX_ANALYSIS_SHOT);
  assert(raw.description.includes(' / '),'Stored analysis must remain bilingual');
  await page.locator('#analysis-close').click();
  await page.locator(`[data-segment-id="${process.env.MINDEX_ANALYSIS_LEGACY}"] .organization-actions button`).first().click();
  await admin.locator('[data-analysis-text]').first().waitFor();
  for(const language of ['en','zh']){await choose(language);assert((await admin.textContent()).includes('旧版中文描述'));}
  await page.goto(base+'/');
  await page.locator('#query').fill('walking');await page.locator('#search-button').click();
  await page.locator('#results .card').first().waitFor();
  await page.locator('#results .card').first().locator('.card-actions button').click();
  const details=page.locator('#detail-content');await details.locator('[data-analysis-text]').first().waitFor();
  await page.evaluate(()=>{window.analysisTestVideo=document.querySelector('#detail-content video');});
  const requestCount=searchRequests;
  for(const language of ['zh','en','zh']){
   await choose(language);await assertSelected(details,language);await assertSelected(page.locator('#results'),language);
   assert((await details.textContent()).includes('对白原文 / Original dialogue'));
   assert((await details.textContent()).includes('用户素材 / User source'));
   assert(await page.evaluate(()=>window.analysisTestVideo===document.querySelector('#detail-content video')),'The player must be preserved');
  }
  assert.equal(searchRequests,requestCount,'Language switching must not rerun search');
  await choose('en');await page.reload();
  await page.locator('#query').fill('walking');await page.locator('#search-button').click();
  await page.locator('#results .card').first().waitFor();
  await page.locator('#results .card').first().locator('.card-actions button').click();
  await details.locator('[data-analysis-text]').first().waitFor();await assertSelected(details,'en');
  // Default moment names, including details, follow the active language without renaming data.
  await page.goto(base+'/admin?source='+process.env.MINDEX_ANALYSIS_SOURCE+'&kind=moment');
  await page.locator('#segment-list .segment-card').first().waitFor();
  assert((await page.locator('#segment-list h3').first().textContent()).startsWith('Moment '));
  await page.locator('#segment-list .organization-actions button').first().click();
  assert((await page.locator('#analysis-title').textContent()).startsWith('Moment '));
  await choose('zh');
  assert((await page.locator('#analysis-title').textContent()).startsWith('精细片段 '));
  // Create tasks through the real form: creation language is frozen, digest language is live.
  await page.goto(base+'/studio');
  for(const lang of ['en','zh']) {
   await choose(lang);
   await page.locator('#create-text').fill('A short video of the harbor.');
   await page.locator('#create-button').click();
   await page.waitForFunction(expected=>document.querySelector('#plan-select')?.selectedOptions[0]?.textContent===expected,lang==='en'?'Default Plan':'默认方案');
   const jobId=new URL(page.url()).searchParams.get('job');
   const saved=await (await page.request.get(base+'/api/studio/jobs/'+jobId)).json();
   assert.equal(saved.job.language,lang);
   for(const display of ['en','zh','en']) {
    await choose(display);
    await page.waitForFunction(expected=>{const n=document.querySelector('.material-digest-text');return n?.textContent.includes(expected);},display==='en'?'City lights':'城市夜景');
    const digest=await page.locator('.material-digest-text').textContent();
    assert(digest.includes(display==='en'?'Storyline:':'故事线：'),digest);
    assert(!digest.includes(display==='en'?'城市夜景':'City lights'),digest);
    assert.equal(await page.locator('#plan-select option:checked').textContent(),lang==='en'?'Default Plan':'默认方案');
   }
  }
  // Session-title events update the dropdown without reloading the page.
  await page.locator('.agent-launcher').click();
  await page.locator('.agent-input').fill('Help plan a short video about the harbor.');
  await page.locator('.agent-send').click();
  await page.waitForFunction(()=>document.querySelector('.agent-sessions')?.selectedOptions[0]?.textContent==='Harbor video planning');
  assert.deepEqual(errors,[]);
  console.log('Localized LLM details, analysis evidence, cards, legacy fallback and live switching without requests/player reset passed');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
