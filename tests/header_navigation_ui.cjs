// Acceptance against an isolated serve_fixture.py library only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_HEADER_URL || 'http://127.0.0.1:18341';
assert.match(base, /^http:\/\/127\.0\.0\.1:/);
const out = 'artifacts/header-navigation';
const paths = ['/search', '/', '/admin', '/persons', '/settings'];
const labels = {zh:['视频检索','创作','素材管理','人物库','设置'], en:['Video search','Studio','Media library','People','Settings']};
fs.mkdirSync(out, {recursive:true});
let browser, page;
(async () => {
  browser = await chromium.launch({headless:true});
  page = await browser.newPage({viewport:{width:1440,height:1000}});
  const records = [], errors = [];
  page.on('pageerror', e => errors.push(e.message));
  // Click through every tab in both languages, including the current tab.
  for (const lang of ['zh','en']) {
    await page.goto(base);
    await page.evaluate(l => window.MindexI18n.setLanguage(l), lang);
    for (const current of paths) {
      await page.locator(`.page-nav a[href="${current}"]`).click();
      await page.waitForURL(base + current);
      await page.locator('.page-nav [aria-current=page]').waitFor();
      assert.equal(await page.locator('html').getAttribute('lang'), lang === 'zh' ? 'zh-CN' : 'en');
      for (const width of [1440,1000,800,540,390,320]) {
        await page.setViewportSize({width,height:1000});
        const r = await page.evaluate(() => {
          const rect = n => {const r=n.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,right:r.right,bottom:r.bottom};};
          const tabs = [...document.querySelectorAll('.page-nav a')].map(n => ({href:n.getAttribute('href'),text:n.textContent,selected:n.getAttribute('aria-current'),color:getComputedStyle(n).color,bg:getComputedStyle(n).backgroundColor,...rect(n)}));
          const refresh = document.querySelector('header #reload,header #refresh');
          const controls = [...document.querySelectorAll('header .brand,header .page-nav a,header button')].map(rect);
          // 右上角三枚按钮收在 .header-actions 组里：整体靠右，组内间距与左侧标签一致。
          const actions = [...document.querySelectorAll('.header-actions button')];
          const gaps = list => list.slice(1).map((b,i)=>+(rect(b).x-rect(list[i]).right).toFixed(1));
          const group = document.querySelector('.header-actions');
          return {tabs,controls,refresh:{id:refresh?.id,margin:refresh&&getComputedStyle(refresh).marginLeft,...(refresh?rect(refresh):{})},notes:document.querySelectorAll('.header-note').length,overflow:document.documentElement.scrollWidth>innerWidth+1,svg:document.querySelectorAll('.language-switch button svg').length,
            navGaps:gaps([...document.querySelectorAll('.page-nav a')]),actGaps:gaps(actions),
            actHeights:actions.map(n=>rect(n).h),actRight:group?+rect(group).right.toFixed(1):null,
            headerPadRight:parseFloat(getComputedStyle(document.querySelector('header')).paddingRight)};
        });
        assert.deepEqual(r.tabs.map(t=>t.href), paths);
        assert.deepEqual(r.tabs.map(t=>t.text), labels[lang]);
        assert.equal(r.tabs.filter(t=>t.selected).length, 1);
        assert.equal(r.tabs.find(t=>t.selected).href, current);
        assert.equal(r.tabs.find(t=>t.selected).bg, 'rgb(32, 91, 68)');
        assert.equal(r.tabs.find(t=>t.selected).color, 'rgb(255, 255, 255)');
        assert.equal(r.notes, 0);assert.equal(r.overflow, false);assert.equal(r.svg, 0);
        assert.ok(r.refresh.id);assert.equal(r.refresh.margin, '0px');
        for (const c of r.controls) assert.ok(c.w>0 && c.h>0 && c.x>=0 && c.right<=width+1);
        const rowHeight = r.tabs[0].h;
        for (const t of r.tabs) assert.equal(t.h,rowHeight);
        // 三枚按钮同高、同一行，组内间距与左侧标签一致，整组靠右贴住页边距。
        assert.equal(r.actHeights.length,3);
        for (const h of r.actHeights) assert.equal(h,rowHeight);
        // 导航换行成多行时横向间距无意义，只在单行时比对组内间距。
        const navSingleRow = r.tabs.every(t=>Math.abs(t.y-r.tabs[0].y)<2);
        assert.ok(r.actGaps.every(g=>g===r.actGaps[0]));
        if(navSingleRow) assert.ok(r.actGaps.every(g=>g===r.navGaps[0]));
        assert.equal(r.actRight,+(width-r.headerPadRight).toFixed(1));
        for(let i=0;i<r.controls.length;i++)for(let j=i+1;j<r.controls.length;j++) {
          const a=r.controls[i],b=r.controls[j];
          assert.ok(a.right<=b.x+1 || b.right<=a.x+1 || a.bottom<=b.y+1 || b.bottom<=a.y+1, 'header controls overlap');
          if(Math.abs(a.y-b.y)<16 && a.h===rowHeight && b.h===rowHeight) assert.ok(Math.abs(a.y-b.y)<=1, 'header row alignment');
        }
        records.push({current,lang,width,...r});
        if([1440,320].includes(width)) await page.screenshot({path:`${out}/${current.slice(1).replace('/','-')||'home'}-${lang}-${width}.png`});
      }
    }
  }
  // Refresh lists, refresh a selected source, delayed/error responses and retry.
  await page.setViewportSize({width:1440,height:1000});await page.goto(base+'/admin');
  await page.evaluate(() => window.MindexI18n.setLanguage('zh'));await page.locator('.source-item').first().waitFor();
  let response=page.waitForResponse(r=>r.url().includes('/api/admin/sources?') && r.request().method()==='GET');
  await page.locator('#reload').click();assert.equal((await response).status(),200);
  await page.waitForFunction(()=>!document.querySelector('#reload').disabled);
  await page.locator('.source-item').first().click();await page.locator('.work-card').last().waitFor();
  const source=await page.evaluate(()=>state.source.id);
  response=page.waitForResponse(r=>r.url().endsWith('/api/admin/sources/'+source));
  await page.locator('#reload').click();assert.equal((await response).status(),200);
  await page.waitForFunction(()=>!document.querySelector('#reload').disabled);
  assert.equal(await page.evaluate(()=>state.source.id),source);
  let release;
  await page.route('**/api/admin/sources/'+source, async route=>{await new Promise(r=>release=r);await route.fulfill({status:503,json:{detail:'工程刷新失败'}});});
  await page.locator('#reload').click();await page.waitForFunction(()=>document.querySelector('#reload').disabled);
  release();await page.locator('#admin-notice.error').waitFor();
  assert.match(await page.locator('#admin-notice').textContent(),/工程刷新失败/);
  await page.waitForFunction(()=>!document.querySelector('#reload').disabled);
  await page.unroute('**/api/admin/sources/'+source);
  response=page.waitForResponse(r=>r.url().endsWith('/api/admin/sources/'+source));
  await page.locator('#reload').click();assert.equal((await response).status(),200);
  await page.waitForFunction(()=>!document.querySelector('#reload').disabled);
  assert.equal(await page.evaluate(()=>state.source.id),source);
  assert.deepEqual(errors,[]);
  fs.writeFileSync(`${out}/coverage.json`,JSON.stringify({records,errors,refresh:['list','selected-source','disabled-during-request','error-recovery','retry']},null,2));
  console.log('PASS: 60 header layouts; all tabs in fixed order, selected state, navigation and language persistence, refresh success/error/retry.');
  await browser.close();
})().catch(async e=>{console.error(e);await page?.screenshot({path:`${out}/failure.png`});await browser?.close();process.exit(1);});
