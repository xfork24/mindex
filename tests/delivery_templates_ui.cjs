const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    // 四路并行回归时页面与模型服务争用 CPU：默认等待按满载情况设置，显式 timeout 的断言不受影响。
    page.setDefaultTimeout(90000);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const base = process.env.MINDEX_DELIVERY_URL;
    await page.goto(base + '/studio/delivery-templates');
    await page.locator('#delivery-template-name').waitFor();
    await page.waitForFunction(() => document.querySelector('#delivery-template-copy')?.textContent);
    // 保存按钮的可用性由首批模板加载后置位：等状态稳定再断言（满载时脚本晚到）。
    await page.waitForFunction(() => document.querySelector('#delivery-template-save')?.disabled === true,
                               undefined, {timeout: 15000});
    assert(await page.locator('#delivery-template-save').isDisabled());
    page.once('dialog', dialog => dialog.accept('Browser delivery'));
    await page.locator('#delivery-template-copy').click();
    await page.waitForFunction(() => document.querySelector('#delivery-template-name').value === 'Browser delivery');
    assert.equal(await page.locator('.delivery-config-fields select').first().locator('option').count(), 4);
    await page.locator('.delivery-config-fields select').first().selectOption('pad');
    await page.locator('#delivery-template-save').click();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('保存'));
    await page.reload();
    await page.getByRole('button', {name: 'Browser delivery', exact:true}).click();
    assert.equal(await page.locator('.delivery-config-fields select').nth(1).inputValue(), 'pad');
    const wrapping = await page.locator('.delivery-export-card').evaluateAll(nodes => nodes.map(n=>n.getBoundingClientRect().top));
    assert(wrapping.some((top,i)=>i>0 && Math.abs(top-wrapping[i-1])<2), 'Export cards should share a row on desktop');
    const management = page.locator('#delivery-platform-management');
    await management.getByRole('button',{name:'新增平台',exact:true}).click();
    await management.locator('[data-platform-field="id"]').fill('browser-platform');
    await management.locator('[data-platform-field="name"]').fill('浏览器平台');
    await management.locator('form').getByRole('button',{name:'保存',exact:true}).click();
    await management.locator('.delivery-platform-list').getByRole('button',{name:'浏览器平台',exact:true}).waitFor();
    const choice = page.locator('#delivery-template-editor .delivery-platform-choice').filter({hasText:'浏览器平台'});
    await choice.locator('input[type=checkbox]').check();
    await page.locator('#delivery-template-save').click();
    await page.waitForFunction(async()=>{
      const items=(await (await fetch('/api/studio/delivery-templates')).json()).items;
      return items.find(x=>x.name==='Browser delivery')?.config.publish.platforms.includes('browser-platform');
    });
    await management.locator('.delivery-platform-list').getByRole('button',{name:'浏览器平台',exact:true}).click();
    await management.locator('[data-platform-field="title.max_chars"]').fill('25');
    await management.locator('form').getByRole('button',{name:'保存',exact:true}).click();
    await page.waitForFunction(async()=> (await (await fetch('/api/studio/platforms/browser-platform')).json()).title.max_chars===25);
    const update = choice.getByRole('button',{name:'更新平台规则',exact:true});
    await update.click();
    await page.locator('#delivery-template-save').click();
    await page.waitForFunction(async()=>{
      const items=(await (await fetch('/api/studio/delivery-templates')).json()).items;
      return items.find(x=>x.name==='Browser delivery')?.config.publish.platform_specs['browser-platform']?.title.max_chars===25;
    });
    page.once('dialog',d=>d.accept());
    await management.getByRole('button',{name:'删除平台',exact:true}).click();
    await choice.getByText('已从平台库删除',{exact:true}).waitFor();
    await page.goto(base + '/studio?job=' + process.env.MINDEX_DELIVERY_JOB);
    await page.locator('#delivery-section').evaluate(node=>node.open=true);
    await page.waitForFunction(() => [...document.querySelectorAll('#delivery-body select option')].some(x=>x.textContent==='Browser delivery'));
    const template = page.locator('#delivery-body > .detail-actions select');
    await template.selectOption({label:'Browser delivery'});
    page.once('dialog', d=>d.accept());
    await page.getByRole('button', {name:'复制到当前方案', exact:true}).click();
    await page.waitForFunction(() => plan().options.delivery.template_name === 'Browser delivery');
    const snapshot = page.locator('#delivery-body > details'); await snapshot.evaluate(n=>n.open=true);
    const taskWrapping=await snapshot.locator('.delivery-export-card').evaluateAll(nodes=>nodes.map(n=>n.getBoundingClientRect().top));
    assert(taskWrapping.some((top,i)=>i>0 && Math.abs(top-taskWrapping[i-1])<2));
    await snapshot.getByText('已从平台库删除',{exact:true}).waitFor();
    await snapshot.locator('.delivery-config-fields select').first().selectOption('crop');
    await snapshot.getByRole('button', {name:'保存', exact:true}).click();
    await page.waitForFunction(() => plan().options.delivery.config.exports.every(x=>x.fit==='crop'));
    const items = await page.evaluate(async()=> (await (await fetch('/api/studio/delivery-templates')).json()).items);
    assert(items.find(x=>x.name==='Browser delivery').config.exports.every(x=>x.fit==='pad'));
    await page.setViewportSize({width:390,height:844});
    await page.goto(base+'/studio/delivery-templates');
    await page.getByRole('button', {name:'Browser delivery', exact:true}).click();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.locator('[data-language-toggle]').click();
    assert.equal(await page.locator('#delivery-title').textContent(), 'Delivery templates');
    assert.equal(errors.length,0,errors.join('\n'));
    console.log('Template copy, persistence, three strategies, independent plan edits, mobile and English: passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
