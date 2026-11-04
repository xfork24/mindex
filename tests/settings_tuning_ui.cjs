const assert=require('node:assert/strict');
const {chromium}=require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.MINDEX_TUNING_URL+'/settings');
  await page.locator('[data-section=tuning]').click();
  await page.locator('#duration-weight').waitFor();
  await page.waitForFunction(()=>document.querySelector('#duration-weight').value !== '');
  for(const lang of ['en','zh']) {
   await page.evaluate(value=>MindexI18n.setLanguage(value),lang);
   for(const width of [1280,768,390]) {
    await page.setViewportSize({width,height:1000});
    const layout=await page.evaluate(()=>({
     right:document.querySelector('.tuning-controls').getBoundingClientRect().right,
     labels:[...document.querySelectorAll('.tuning-controls label')].map(n=>{
      const r=n.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,width:r.width};
     }),overflow:document.documentElement.scrollWidth>innerWidth+1,
    }));
    assert.equal(layout.labels.length,12);
    assert.equal(layout.overflow,false);
    for(let i=1;i<layout.labels.length;i++) {
     const a=layout.labels[i-1],b=layout.labels[i];
     if(b.top>a.top+2) assert(a.right+14+b.width>layout.right-2,JSON.stringify({width,lang,a,b,right:layout.right}));
    }
   }
  }
  await page.locator('#duration-weight').fill('0.2');
  await page.locator('#duration-exponent').fill('3');
  const response=page.waitForResponse(r=>r.url().endsWith('/api/settings/business')&&r.request().method()==='PUT');
  await page.locator('#tuning-save').click();assert.equal((await response).status(),200);
  await page.reload();await page.locator('[data-section=tuning]').click();
  await page.waitForFunction(()=>document.querySelector('#duration-weight').value === '0.2');
  assert.equal(await page.locator('#duration-weight').inputValue(),'0.2');
  assert.equal(await page.locator('#duration-exponent').inputValue(),'3');
  const settings=await (await page.request.get(process.env.MINDEX_TUNING_URL+'/api/settings')).json();
  assert.equal(settings.business.search.duration_weight,.2);
  assert.deepEqual(errors,[]);
  console.log('Continuous tuning layout in both languages at 3 widths and hot settings persistence passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
