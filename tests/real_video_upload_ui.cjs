// 仅对 serve_media_validation.py 的独立素材库运行；不评估模型推理质量。
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.MINDEX_PLAYWRIGHT_MODULE||'playwright');
const base=process.env.MINDEX_MEDIA_TEST_URL||'http://127.0.0.1:18765';
const video=process.env.MINDEX_MEDIA_TEST_VIDEO;
assert.ok(video,'请设置 MINDEX_MEDIA_TEST_VIDEO');
const output=process.env.MINDEX_MEDIA_TEST_OUTPUT||'/tmp/mindex-real-media-results';
fs.mkdirSync(output,{recursive:true});
const report={video,base,events:[]};
function save(){fs.writeFileSync(path.join(output,'browser-report.json'),JSON.stringify(report,null,2));}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});report.errors=[];report.failedAssets=[];
  page.on('pageerror',e=>{report.errors.push(e.message);save()});
  page.on('response',r=>{if(r.url().includes('/assets/')&&r.status()!==200)report.failedAssets.push([r.url(),r.status()]);});
  await page.goto(base+'/admin');await page.locator('#add-source').click();
  const upload=()=>page.waitForResponse(r=>r.url()===base+'/api/admin/sources/upload',{timeout:180000});
  let pending=upload();await page.locator('#upload-files').setInputFiles({name:'invalid.mp4',mimeType:'video/mp4',buffer:Buffer.from('invalid video')});
  let response=await pending;assert.equal(response.status(),422);report.invalidUpload=await response.json();save();
  pending=upload();await page.locator('#upload-files').setInputFiles(report.video);
  response=await pending;assert.equal(response.status(),201,await response.text());let source=await response.json();
  assert.equal(source.duplicate,false);report.sourceId=source.source_id;report.events.push('real video uploaded');save();console.log('uploaded',report.sourceId);
  pending=upload();await page.locator('#upload-files').setInputFiles(report.video);
  response=await pending;assert.equal(response.status(),201);source=await response.json();assert.equal(source.duplicate,true);assert.equal(source.source_id,report.sourceId);
  report.events.push('duplicate upload deduplicated');save();await page.locator('#upload-close').click();
  await page.locator('details.work-panel > summary').click();
  const shots=page.locator('[data-work=shots]');await shots.locator('input[type=checkbox]').uncheck();
  const started=page.waitForResponse(r=>r.url().endsWith('/'+report.sourceId+'/shots')&&r.request().method()==='POST');
  await shots.locator('.work-controls button').first().click();response=await started;assert.equal(response.status(),202,await response.text());
  const task=await response.json();report.taskId=task.id;report.events.push('processing started from browser');save();
  let last='',done=false;
  for(let n=0;n<600;n++){
   const r=await page.request.get(base+'/api/admin/source-tasks/'+report.taskId,{timeout:15000});assert.equal(r.status(),200);
   const state=await r.json();report.task=state;
   const key=JSON.stringify([state.status,state.progress?.stage,state.progress?.completed,state.progress?.total]);
   if(key!==last){console.log(key);last=key;save();}
   if(['complete','failed','interrupted'].includes(state.status)){assert.equal(state.status,'complete',JSON.stringify(state));done=true;break;}
   await new Promise(resolve=>setTimeout(resolve,2000));
  }
  assert.ok(done,'processing timed out');
  const refreshed=await page.request.post(base+'/api/admin/sources/'+report.sourceId+'/work-cache/refresh',{data:{},timeout:30000});assert.equal(refreshed.status(),200);report.source=await refreshed.json();
  assert.ok(report.source.shot_count>0);assert.ok(report.source.moment_count>0);assert.equal(report.source.work.shots.status,'complete');
  await page.reload();await page.locator('details.work-panel > summary').click();await page.locator('[data-work=shots] .work-status').filter({hasText:'处理完成'}).waitFor({timeout:30000});
  const segments=await (await page.request.get(base+'/api/admin/sources/'+report.sourceId+'/segments?kind=shot&page_size=100')).json();
  report.segments=segments;assert.ok(segments.items.length);
  const first=segments.items[0];const range=await page.request.get(base+'/api/admin/segments/'+first.id+'/media',{headers:{Range:'bytes=0-1023'}});
  assert.equal(range.status(),206);assert.equal((await range.body()).length,1024);report.mediaRangeStatus=206;
  const thumb=await page.request.get(base+'/api/admin/segments/'+first.id+'/thumbnail');assert.equal(thumb.status(),200);assert.match(thumb.headers()['content-type'],/image\/jpeg/);
  await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.failedAssets,[]);report.events.push('processing, reload, media range, thumbnail and mobile checks passed');report.passed=true;save();
  console.log('PASS',JSON.stringify({shots:report.source.shot_count,moments:report.source.moment_count,source:report.sourceId}));
 }catch(e){report.failure=String(e.stack||e);save();throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
