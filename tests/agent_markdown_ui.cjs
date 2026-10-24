// 助手 Markdown 渲染：在真实页面里调用 MindexMarkdown，检查结构与 XSS 向量被清除。
const assert = require('node:assert/strict');
const {chromium} = require(process.env.MINDEX_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.MINDEX_AGENT_URL;
assert.match(base, /^http:\/\/127\.0\.0\.1:/);

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('dialog', (d) => { errors.push('dialog: ' + d.message()); d.dismiss(); });
  try {
    await page.goto(base + '/settings');
    await page.waitForFunction(() => window.MindexMarkdown && window.marked && window.DOMPurify);
    const render = (text) => page.evaluate((value) => {
      const box = document.createElement('div');
      document.body.append(box);
      window.MindexMarkdown.render(box, value);
      const out = {html: box.innerHTML, tags: [...box.querySelectorAll('*')].map((n) => n.tagName.toLowerCase()),
        links: [...box.querySelectorAll('a')].map((a) => ({text: a.textContent, href: a.getAttribute('href'), target: a.getAttribute('target'), rel: a.getAttribute('rel')})),
        text: box.textContent, markdown: box.classList.contains('agent-markdown')};
      box.remove();
      return out;
    }, text);

    const doc = await render('# 标题\n\n- 一\n- 二\n  1. 嵌套\n\n| a | b |\n|---|:-:|\n| 1 | **2** |\n\n> 引用\n\n`code` 与 ~~删除~~\n\n```js\nconst x = 1 < 2;\n```\n\n---\n第一行\n第二行');
    assert.ok(doc.markdown);
    for (const tag of ['h1', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'th', 'td', 'strong', 'blockquote', 'code', 'pre', 'del', 'hr', 'br'])
      assert.ok(doc.tags.includes(tag), `缺少 ${tag}`);
    assert.match(doc.text, /const x = 1 < 2;/);

    const attacks = [
      '<script>window.__x=1</script>文本',
      '<img src=x onerror="window.__x=1">',
      '<a href="javascript:window.__x=1">点我</a>',
      '[点我](javascript:window.__x=1)',
      '[数据](data:text/html;base64,PHNjcmlwdD4=)',
      '<iframe src="https://example.com"></iframe>',
      '<div onclick="window.__x=1" style="color:red">块</div>',
      '<svg><script>window.__x=1</script></svg>',
      '[协议相对](//evil.example/x)',
      '<details open ontoggle="window.__x=1">x</details>',
    ];
    for (const attack of attacks) {
      const out = await render(attack);
      assert.ok(!/<(script|img|iframe|svg|div|details|style)\b/i.test(out.html), `${attack} → ${out.html}`);
      assert.ok(!/\son\w+=/i.test(out.html) && !/style=/i.test(out.html), `${attack} → ${out.html}`);
      for (const link of out.links) assert.ok(!link.href || /^(https?:|\/(?!\/)|#)/i.test(link.href), `${attack} → ${link.href}`);
    }
    const links = await render('[外部](https://example.com/a) [站内](/studio?job=1) [锚点](#x)');
    assert.deepEqual(links.links, [
      {text: '外部', href: 'https://example.com/a', target: '_blank', rel: 'noopener noreferrer'},
      {text: '站内', href: '/studio?job=1', target: null, rel: null},
      {text: '锚点', href: '#x', target: null, rel: null},
    ]);
    // 流式中途：未闭合的代码块、表格不报错。
    for (const partial of ['```py\nprint(', '| a | b |\n|--', '**未闭合', '- [ ] '] ) await render(partial);
    assert.equal(await page.evaluate(() => window.__x), undefined);
    // 依赖缺失时回退为纯文本。
    const fallback = await page.evaluate(() => {
      const saved = window.marked; window.marked = undefined;
      const box = document.createElement('div');
      window.MindexMarkdown.render(box, '<b>x</b>');
      window.marked = saved;
      return [box.innerHTML, box.classList.contains('agent-markdown')];
    });
    assert.deepEqual(fallback, ['&lt;b&gt;x&lt;/b&gt;', false]);
    assert.deepEqual(errors, []);
    console.log('agent markdown ok');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
