#!/usr/bin/env node
/* 重拍 docs/assets/ 的 README 截图：以暗色主题 + 英文界面对线上服务逐张复现原图的页面与功能状态。

   用法：
     MINDEX_WEB_TOKEN=<令牌> node scripts/capture-readme-screenshots.cjs [--only 名字1,名字2]
   环境变量：
     MINDEX_BASE_URL   服务地址，默认 http://192.168.1.86:8000
     MINDEX_WEB_TOKEN  访问令牌（POST /login 换会话 Cookie，只进进程内存）
   说明：
     - 视口 1890×915 @2x，与原图（3780×1830 附近）一致；四张对话框截图按原图 CSS 尺寸选择元素。
     - 直接覆写 docs/assets/<名字>.png；浅色原图由 git 历史保留。
     - 数据取服务当前状态（用户要求），页面/功能位置与原图一一对应。 */

'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', 'node_modules', 'playwright'));

const BASE = process.env.MINDEX_BASE_URL || 'http://192.168.1.86:8000';
const TOKEN = process.env.MINDEX_WEB_TOKEN;
if (!TOKEN) {
  console.error('缺少 MINDEX_WEB_TOKEN（线上服务开启了令牌鉴权）');
  process.exit(1);
}
const ASSETS = path.join(__dirname, '..', 'docs', 'assets');

// 原图 PNG 尺寸（用于元素截图挑选与结果核对）：full 视口图 ~3780×1830。
const ORIGINAL = {
  'admin-shot-analysis': [1426, 1654],
  'settings-model-config': [1518, 1170],
  'topics-settings': [1386, 1636],
  'studio-build-dependencies': [1802, 1576],
};

const onlyArg = process.argv.indexOf('--only');
const ONLY = onlyArg > 0 ? process.argv[onlyArg + 1].split(',').map((s) => s.trim()).filter(Boolean) : null;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- 基础设施
let browser, ctx, data = {};

async function apiJson(p) {
  const res = await ctx.request.get(BASE + p);
  if (!res.ok()) throw new Error(`GET ${p} -> ${res.status()}`);
  return res.json();
}

async function newPage(extraInit) {
  const page = await ctx.newPage();
  page.setDefaultTimeout(45000);
  if (extraInit) await page.addInitScript(extraInit);
  return page;
}

async function open(page, url, readySel) {
  await page.goto(BASE + url, { waitUntil: 'domcontentloaded' });
  if (page.url().includes('/login')) throw new Error('未登录（令牌无效？）');
  if (readySel) await page.waitForSelector(readySel, { state: 'visible' });
  await sleep(500); // i18n/主题同步脚本与首轮数据请求落地
}

async function snap(page, name) {
  await sleep(600); // 等过渡动画与懒加载缩略图
  const file = path.join(ASSETS, name + '.png');
  await page.screenshot({ path: file });
  logShot(name, file);
}

// 70% 缩放截图（用户要求：一屏装下更多内容）。
async function zoom70(page) {
  await page.addStyleTag({ content: 'body{zoom:.7}' });
  await sleep(200);
}

// 顶部露出查询表单（保住搜索关键字），再补全底部整行（底部完整性优先）。
async function topFormBottomRows(page, formSel, rowSel) {
  await page.evaluate((sel) => {
    const form = document.querySelector(sel);
    if (form) window.scrollTo({ top: Math.max(0, form.getBoundingClientRect().top + window.scrollY - 8) });
  }, formSel);
  await sleep(200);
  await revealBottomRow(page, rowSel);
}

// 等一组惰性图片真正加载完成（进入视口后才开始拉取，需显式等待）。
async function waitImagesLoaded(page, selector, timeout = 20000) {
  await page.waitForFunction((sel) => {
    const imgs = [...document.querySelectorAll(sel)].filter((i) => {
      const r = i.getBoundingClientRect();
      return r.top < window.innerHeight && r.width > 4;
    });
    return imgs.length > 0 && imgs.every((i) => i.complete && i.naturalWidth > 0);
  }, selector, { timeout }).catch(() => {});
  await sleep(500);
}

// “往下截一点”：把视口底部被切成半行的卡片滚成完整一行（找不到被切元素时保守下滚一截）。
async function revealBottomRow(page, selector) {
  await page.evaluate((sel) => {
    const vh = window.innerHeight;
    const els = [...document.querySelectorAll(sel)];
    const cut = els.find((e) => {
      const r = e.getBoundingClientRect();
      return r.bottom > vh - 4 && r.top < vh && r.height > 20;
    });
    if (cut) window.scrollBy({ top: cut.getBoundingClientRect().bottom - vh + 16 });
    else window.scrollBy({ top: 240 });
  }, selector);
  await sleep(350);
}

// 元素截图但四周留出空白边距（page.screenshot 的 clip 以视口坐标为准）。
async function snapPadded(page, name, sel, pad = 28) {
  const box = await page.locator(sel).first().boundingBox();
  const vw = 1890, vh = 915;
  const x = Math.max(0, box.x - pad), y = Math.max(0, box.y - pad);
  const clip = { x, y, width: Math.min(box.width + pad * 2, 1890 - x), height: Math.min(box.height + pad * 2, 915 - y) };
  await sleep(400);
  const file = path.join(ASSETS, name + '.png');
  await page.screenshot({ path: file, clip });
  logShot(name, file);
}

// 对话框类截图：候选元素里挑外框尺寸最接近原图的那个（原图即这么截的）。
async function snapClosest(page, name, selectors) {
  const [tw, th] = ORIGINAL[name];
  const scored = [];
  for (const sel of selectors) {
    try {
      const box = await page.locator(sel).first().boundingBox();
      if (box && box.width > 40 && box.height > 40) {
        const err = Math.abs(box.width * 2 - tw) / tw + Math.abs(box.height * 2 - th) / th;
        scored.push({ sel, err, box });
      }
    } catch { /* 元素不存在则跳过 */ }
  }
  if (!scored.length) throw new Error(`候选元素都不可见: ${selectors.join(', ')}`);
  scored.sort((a, b) => a.err - b.err);
  await sleep(400);
  const file = path.join(ASSETS, name + '.png');
  await page.locator(scored[0].sel).first().screenshot({ path: file });
  console.log(`  [${name}] 选用 ${scored[0].sel} (${Math.round(scored[0].box.width)}×${Math.round(scored[0].box.height)} CSS, 原图 ${tw / 2}×${th / 2})`);
  logShot(name, file);
}

function logShot(name, file) {
  const buf = fs.readFileSync(file);
  // PNG 宽高在 IHDR：偏移 16/20 大端。
  const w = buf.readUInt32BE(16), h = buf.readUInt32BE(20);
  console.log(`  ✓ ${name}.png ${w}×${h} (${(buf.length / 1024).toFixed(0)} KB)`);
}

// agent 抽屉：等历史事件回放完（状态行 idle 且卡片数稳定）。
async function waitDrawer(page) {
  await page.waitForSelector('#agent-drawer:not([hidden])', { state: 'attached' });
  await page.waitForSelector('#agent-drawer .agent-log details.agent-tool', { state: 'attached' });
  await page.waitForFunction(() => {
    const status = document.querySelector('#agent-drawer .agent-status');
    return status && (status.dataset.status === 'idle' || status.dataset.status === 'running');
  });
  // 回放是流式的：等卡片数量 1.5s 不再增长。
  await page.waitForFunction(() => {
    const n = document.querySelectorAll('#agent-drawer details.agent-tool').length;
    if (window.__lastToolCount === n) return true;
    window.__lastToolCount = n;
    return false;
  }, undefined, { polling: 400 }).catch(() => {});
  await sleep(800);
}

// 把抽屉日志滚到指定工具卡（按 summary 文本过滤，取第 nth 个，从 0 计），可展开它。
async function drawerScrollToTool(page, toolName, nth, expand) {
  await page.evaluate(([name, idx, open]) => {
    const cards = [...document.querySelectorAll('#agent-drawer details.agent-tool')]
      .filter((d) => d.querySelector('summary')?.textContent.includes(name));
    const target = cards[Math.min(idx, cards.length - 1)];
    if (!target) return;
    if (open) target.open = true;
    target.scrollIntoView({ block: 'center' });
  }, [toolName, nth, expand]);
  await sleep(400);
}

async function drawerScrollToText(page, text) {
  await page.evaluate((needle) => {
    const drawer = document.querySelector('#agent-drawer');
    const walker = [...drawer.querySelectorAll('#agent-drawer .agent-log *')];
    const target = walker.reverse().find((n) => n.children.length === 0 && n.textContent.includes(needle));
    (target || drawer.querySelector('.agent-log').lastElementChild)?.scrollIntoView({ block: 'center' });
  }, text);
  await sleep(400);
}

async function drawerScrollBottom(page) {
  await page.evaluate(() => {
    const log = document.querySelector('#agent-drawer .agent-log');
    log.scrollTop = log.scrollHeight;
  });
  await sleep(400);
}

// 展开分镜卡指定阶段（召回 picture/source / 选片 select / 渲染 render …）的详情面板。
// 召回列的阶段键因分镜而异（pictureStage(sid) || "source"），指定键不存在时回退到第 2 项（召回列）。
async function openShotStage(page, shotIndex, stage) {
  const card = page.locator(`#shot-strip .shot-card`).nth(shotIndex);
  await card.scrollIntoViewIfNeeded();
  await card.locator('.stage-menu-button').click();
  await page.waitForSelector('#stage-popover:not([hidden])', { state: 'attached' });
  const option = stage
    ? page.locator(`#stage-popover .stage-option[data-stage-option="${stage}"]`)
    : page.locator('#stage-popover .stage-option').nth(1);
  const specific = stage && (await option.count()) > 0 ? option : page.locator('#stage-popover .stage-option').nth(1);
  await specific.click();
  await page.waitForSelector('#stage-panel:not([hidden])', { state: 'attached' });
  await sleep(700); // 候选缩略图加载
}

// ---------------------------------------------------------------- 截图配方
const RECIPES = [
  // ---------- 检索（/search）
  {
    name: 'search-text-and-person',
    run: async (page) => {
      await open(page, '/search', '#source[data-initialized="1"]');
      // 用户要求：70% 缩放，让填入的查询文字与结果同屏。
      await page.addStyleTag({ content: 'body{zoom:.7}' });
      await page.fill('#query', 'boy and girl');
      await page.selectOption('#source', [data.dearYou.id]);
      // 用户指定人物过滤：郑木生 + 叶淑柔（青年）——两个都是角色，按全名精确匹配。
      const picks = await page.evaluate(() => {
        const want = ['郑木生', '叶淑柔（青年）'];
        const out = { actor: [], role: [], missed: [] };
        for (const [sel, key] of [['#actor-select', 'actor'], ['#role-select', 'role']]) {
          for (const o of document.querySelectorAll(sel + ' option')) {
            const t = (o.textContent || '').split(' ·')[0].trim();
            if (want.includes(t)) out[key].push(o.value);
          }
        }
        const selected = new Set([...out.actor, ...out.role]);
        out.missed = want.filter((w) => {
          const opts = [...document.querySelectorAll('#actor-select option, #role-select option')];
          const match = opts.find((o) => (o.textContent || '').split(' ·')[0].trim() === w);
          return !match || !selected.has(match.value);
        });
        return out;
      });
      if (picks.missed.length) console.log(`  ⚠ 未命中: ${picks.missed.join(', ')}`);
      console.log(`  人物过滤: 演员 ${picks.actor.length} 项, 角色 ${picks.role.length} 项`);
      if (picks.actor.length) await page.selectOption('#actor-select', picks.actor);
      if (picks.role.length) await page.selectOption('#role-select', picks.role);
      await page.click('#search-button');
      await page.waitForSelector('#results article.card');
      await page.waitForFunction(() => !document.querySelector('#search-button[disabled]'));
      await revealBottomRow(page, '#results article.card');
      await snap(page, 'search-text-and-person');
    },
  },
  {
    name: 'search-text-and-object',
    run: async (page) => {
      await open(page, '/search', '#source[data-initialized="1"]');
      await zoom70(page);
      await page.fill('#query', 'singapore map');
      await page.click('#search-button');
      await page.waitForSelector('#results article.card');
      await page.waitForFunction(() => !document.querySelector('#search-button[disabled]'));
      await waitImagesLoaded(page, '#results article.card img');
      // 用户要求：搜索关键字必须可见；底部最后一行要么完整、要么整行移出。
      // 结果图是懒加载，滚动会触发新图加载并撑高卡片 → 先强制全部立即加载，再循环修正到稳定。
      await page.evaluate(() => {
        for (const img of document.querySelectorAll('#results article.card img')) {
          img.loading = 'eager';
          if (!img.complete) img.src = img.currentSrc || img.src;
        }
      });
      await page.waitForFunction(() => {
        const imgs = [...document.querySelectorAll('#results article.card img')];
        return imgs.length > 0 && imgs.every((i) => i.complete && (i.naturalWidth > 0 || i.getBoundingClientRect().height === 0));
      }, undefined, { timeout: 15000 }).catch(() => {});
      // 先把查询框锚定到视口顶
      await page.evaluate(() => {
        const q = document.querySelector('#query');
        window.scrollTo(0, Math.max(0, q.getBoundingClientRect().top + window.scrollY - 8));
      });
      // 再循环修正底部行：补全（查询文字行仍可见时）或整行移出
      for (let round = 0; round < 4; round++) {
        const acted = await page.evaluate(() => {
          const vh = window.innerHeight;
          const q = document.querySelector('#query');
          const rows = [];
          for (const c of document.querySelectorAll('#results article.card')) {
            const top = c.getBoundingClientRect().top;
            const last = rows[rows.length - 1];
            if (last && Math.abs(last.top - top) < 24) last.bottom = Math.max(last.bottom, c.getBoundingClientRect().bottom);
            else rows.push({ top, bottom: c.getBoundingClientRect().bottom });
          }
          const cut = rows.find((r) => r.bottom > vh - 4 && r.top < vh - 4);
          if (!cut) return false;
          const need = cut.bottom - (vh - 8);
          if (need <= q.getBoundingClientRect().top + 40) window.scrollBy(need);
          else window.scrollBy(cut.top - (vh - 8)); // 整行移出视口（向上滚），末行完整收尾
          return true;
        }).catch(() => false);
        if (!acted) break;
        await sleep(250);
      }
      await snap(page, 'search-text-and-object');
    },
  },
  {
    name: 'search-emotion-closeup',
    run: async (page) => {
      await open(page, '/search', '#source[data-initialized="1"]');
      await page.addStyleTag({ content: 'body{zoom:.7}' });
      await page.fill('#query', 'person');
      await page.fill('#emotion', 'sad');
      await page.fill('#shot-size', 'close-up');
      await page.selectOption('#source', [data.dearYou.id]);
      await page.click('#search-button');
      await page.waitForSelector('#results article.card');
      await page.waitForFunction(() => !document.querySelector('#search-button[disabled]'));
      await revealBottomRow(page, '#results article.card');
      await snap(page, 'search-emotion-closeup');
    },
  },
  {
    name: 'search-similar-scenes',
    run: async (page) => {
      await open(page, '/search', '#source[data-initialized="1"]');
      await page.fill('#query', 'two people eating at a table in a dim room');
      await page.selectOption('#source', [data.dearYou.id]);
      await page.click('#search-button');
      await page.waitForSelector('#results article.card');
      await page.waitForFunction(() => !document.querySelector('#search-button[disabled]'));
      await page.locator('#results article.card').first().locator('.card-actions button').nth(1).click();
      await page.waitForFunction(() => document.querySelector('#result-count')?.textContent.trim() !== '');
      await sleep(2500); // 相似结果重渲染
      await snap(page, 'search-similar-scenes');
    },
  },
  {
    name: 'search-by-image-or-video',
    run: async (page) => {
      await open(page, '/search', '#source[data-initialized="1"]');
      await page.click('#image-tab');
      await page.setInputFiles('#image-file', data.wineImage);
      await page.waitForSelector('#image-preview:not([hidden])', { state: 'attached' });
      await page.click('#search-button');
      await page.waitForSelector('#results article.card');
      await page.waitForFunction(() => !document.querySelector('#search-button[disabled]'));
      // 用户要求：完整看到搜索结果的第一行。
      await page.locator('#results article.card').first().scrollIntoViewIfNeeded();
      await sleep(400);
      await snap(page, 'search-by-image-or-video');
    },
  },

  // ---------- 素材管理（/admin）
  {
    name: 'admin-preprocess',
    run: async (page) => {
      await open(page, `/admin?source=${data.dearYou.id}`, '#segment-list .segment-card');
      await page.click('details.work-panel > summary.work-toolbar');
      await page.waitForSelector('.work-card[data-work]');
      await sleep(800);
      await snap(page, 'admin-preprocess');
    },
  },
  {
    name: 'admin-shots',
    run: async (page) => {
      await open(page, `/admin?source=${data.botanic.id}`, '#segment-list .segment-card');
      // 用户要求：右侧显示 Media processing workspace 之下的分镜缩略图网格。
      await page.evaluate(() => document.querySelector('#segment-list').scrollIntoView({ block: 'start' }));
      await sleep(600);
      await snap(page, 'admin-shots');
    },
  },
  {
    name: 'admin-shot-analysis',
    run: async (page) => {
      await open(page, `/admin?source=${data.botanic.id}`, '#segment-list .segment-card');
      await page.locator('#segment-list .segment-card .organization-actions button').first().click();
      await page.waitForSelector('#analysis-dialog[open]');
      await page.waitForSelector('#analysis-content .analysis-section');
      await snapClosest(page, 'admin-shot-analysis', ['#analysis-dialog', '#analysis-content']);
    },
  },
  {
    name: 'admin-music',
    run: async (page) => {
      await open(page, '/admin/music', '#music-list > *');
      await page.locator('#music-list > *').first().click();
      await page.waitForSelector('#music-view:not([hidden])', { state: 'attached' });
      await sleep(500);
      await snap(page, 'admin-music');
    },
  },
  {
    name: 'admin-groups',
    run: async (page) => {
      await open(page, '/admin/groups', '#groups-list .group-item');
      await page.locator('#groups-list .group-item').first().click();
      await sleep(500);
      await snap(page, 'admin-groups');
    },
  },

  // ---------- 人物库（/persons）
  {
    name: 'persons-actors-roles',
    run: async (page) => {
      await open(page, '/persons', '#entity-list > *');
      await snap(page, 'persons-actors-roles');
    },
  },
  {
    name: 'persons-cast-import',
    run: async (page) => {
      await open(page, `/persons?draft=${data.draft.id}`, '#import-entries > *');
      await snap(page, 'persons-cast-import');
    },
  },
  {
    name: 'persons-appearance-detection',
    run: async (page) => {
      await open(page, `/persons?source=${data.dearYou.id}`, '#evidence-list > *');
      await page.selectOption('#review-status', 'accepted');
      await page.click('#review-refresh');
      await page.waitForFunction(() => document.querySelector('#evidence-list').children.length > 0);
      await sleep(600);
      // 用户要求：缩略图必须显示出来（证据网格在页面下方，惰性图片要先滚进视口再等加载完成）。
      await page.evaluate(() => {
        const el = document.querySelector('#evidence-list');
        window.scrollTo({ top: Math.max(0, el.getBoundingClientRect().top + window.scrollY - 12) });
      });
      await waitImagesLoaded(page, '#evidence-list img');
      await snap(page, 'persons-appearance-detection');
    },
  },

  // ---------- 创作（/studio）
  {
    name: 'studio-jobs',
    run: async (page) => {
      await open(page, `/?job=${data.job.id}`, '#job-workspace:not([hidden])');
      await page.waitForSelector('#plan-select option', { state: 'attached' });
      await sleep(800);
      await snap(page, 'studio-jobs');
    },
  },
  {
    name: 'studio-storyboard',
    run: async (page) => {
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await openShotStage(page, 8 % data.shotCount, 'picture'); // 原图：中段分镜打开“召回”详情 + 候选网格
      // 用户要求：分镜工作台滚到顶部，其上方的内容不需要出现。
      await page.evaluate(() => document.querySelector('#storyboard-section').scrollIntoView({ block: 'start' }));
      await sleep(400);
      await snap(page, 'studio-storyboard');
    },
  },
  {
    name: 'studio-timeline-alignment',
    run: async (page) => {
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await zoom70(page);
      await openShotStage(page, 8 % data.shotCount, 'select'); // 原图：选片与对齐（时间窗 + 缩放滑杆）
      await page.waitForSelector('.window-viewport .fixed-window', { state: 'attached' }).catch(() => {});
      // 用户要求：选片面板内容显示全（滚到 #stage-panel 底部即候选区，不越过下方的成片区）。
      await page.evaluate(() => {
        const el = document.querySelector('#stage-panel');
        const need = el.getBoundingClientRect().bottom - window.innerHeight;
        if (need > 0) window.scrollBy({ top: need + 16 });
      });
      await sleep(500);
      await snap(page, 'studio-timeline-alignment');
    },
  },
  {
    name: 'studio-build-dependencies',
    run: async (page) => {
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await page.locator('#result-section').scrollIntoViewIfNeeded();
      await page.evaluate(() => {
        document.querySelector('#result-section').open = true;
        const inner = document.querySelector('#final-history > details');
        if (inner) inner.open = true; // 成片历史默认折叠，先展开才有行与依赖树
      });
      await page.waitForSelector('#final-history details.dependency', { state: 'attached' });
      await sleep(600);
      // 行内“查看并播放”打开版本对话框（原图为 900px 宽的 work-dialog）。
      await page.locator('#final-history .final-row button').first().click();
      await page.waitForSelector('#work-dialog[open]', { timeout: 10000 });
      await page.evaluate(() => {
        for (const d of document.querySelectorAll('#work-dialog details.dependency')) d.open = true;
      });
      await sleep(400);
      await snapClosest(page, 'studio-build-dependencies', ['#work-dialog', '#dialog-content']);
    },
  },
  {
    name: 'studio-script-templates',
    run: async (page) => {
      await open(page, '/studio/templates', '#template-list > *');
      await page.locator('#template-list > *').first().click();
      await page.waitForSelector('#template-view:not([hidden])', { state: 'attached' }).catch(() => {});
      await snap(page, 'studio-script-templates');
    },
  },
  {
    name: 'studio-delivery-templates',
    run: async (page) => {
      await open(page, '/studio/delivery-templates', '#delivery-template-list > *');
      await page.locator('#delivery-template-list > *').first().click();
      await sleep(600);
      await snap(page, 'studio-delivery-templates');
    },
  },
  {
    name: 'studio-style-templates',
    run: async (page) => {
      await open(page, '/studio/brands', '#brand-list > *');
      await page.locator('#brand-list > *').first().click();
      await page.waitForSelector('#brand-view:not([hidden])', { state: 'attached' }).catch(() => {});
      await snap(page, 'studio-style-templates');
    },
  },

  // ---------- 选题（/studio/topics）
  {
    name: 'topics-mining',
    run: async (page) => {
      await open(page, `/studio/topics?profile=${data.topicProfile.id}`, '#card-list .topic-item');
      await zoom70(page);
      await page.locator('#card-list .topic-item').first().click();
      await page.waitForSelector('#card-view:not([hidden])', { state: 'attached' });
      await sleep(700);
      // 用户要求：底部内容显示全（滚到页底），顶部可裁。
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await sleep(400);
      await snap(page, 'topics-mining');
    },
  },
  {
    name: 'topics-settings',
    run: async (page) => {
      await open(page, `/studio/topics?profile=${data.topicProfile.id}`, '#topics-bar');
      await page.click('#profile-edit');
      await page.waitForSelector('#profile-dialog[open]');
      await sleep(400);
      await snapClosest(page, 'topics-settings', ['#profile-dialog', '#profile-form']);
    },
  },

  // ---------- 设置（/settings）
  {
    name: 'settings-models-harness',
    run: async (page) => {
      await open(page, '/settings', '#roles .role-row');
      await snap(page, 'settings-models-harness');
    },
  },
  {
    name: 'settings-model-card',
    run: async (page) => {
      await open(page, '/settings', '#roles .role-row');
      // 展开第一个带档案卡的角色行（原图：模型档案卡列表 + 激活徽标）。
      await page.evaluate(() => {
        const head = document.querySelector('#roles .role-row .role-row-head');
        if (head && head.getAttribute('aria-expanded') !== 'true') head.click();
      });
      await page.waitForSelector('#roles .role-row.is-open .profile-item', { state: 'attached' }).catch(() => {});
      await sleep(500);
      await snap(page, 'settings-model-card');
    },
  },
  {
    name: 'settings-model-config',
    run: async (page) => {
      await open(page, '/settings', '#roles .role-row');
      await page.evaluate(() => {
        const head = document.querySelector('#roles .role-row .role-row-head');
        if (head && head.getAttribute('aria-expanded') !== 'true') head.click();
      });
      await page.waitForSelector('#roles .role-row.is-open .profile-item', { state: 'attached' });
      await page.locator('#roles .role-row.is-open .profile-item button', { hasText: '' }).nth(2).click()
        .catch(() => page.locator('#roles .role-row.is-open .profile-item .actions button').last().click());
      await page.waitForSelector('#profile-dialog[open]');
      await sleep(400);
      // 用户要求：对话框四周留一点外部空间。
      await snapPadded(page, 'settings-model-config', '#profile-dialog', 28);
    },
  },
  {
    name: 'settings-weights-voices-services',
    run: async (page) => {
      await open(page, '/settings', '#roles .role-row');
      // 原图：顶部“检索调参”与“检索组合”展开，其余折叠。
      await page.evaluate(() => {
        const want = new Set(['tuning', 'releases']);
        for (const head of document.querySelectorAll('.role-row-head[data-section]')) {
          const isOpen = head.closest('.role-row').classList.contains('is-open');
          if (want.has(head.dataset.section) !== isOpen) head.click();
        }
      });
      await sleep(600);
      // 用户要求：截取下方的“检索调参/音色库/本地服务/工具”分区，而非顶部模型角色区。
      await page.evaluate(() => {
        const head = document.querySelector('.role-row-head[data-section="tuning"]');
        if (head) {
          head.scrollIntoView({ block: 'start' });
          window.scrollBy(0, -70);
        }
      });
      await sleep(400);
      await snap(page, 'settings-weights-voices-services');
    },
  },
  {
    name: 'settings-dark-theme-i18n',
    run: async (page) => {
      // 用户要求：这张保持浅色主题 + 中文界面（演示“暗色风格和中英文切换”的能力本身）。
      // 页面级 init 在上下文级之后执行，覆盖默认的 dark/en。
      await page.addInitScript(() => {
        try {
          localStorage.setItem('mindex.theme', 'light');
          localStorage.setItem('mindex.language', 'zh');
        } catch {}
      });
      await open(page, '/settings', '#roles .role-row');
      await snap(page, 'settings-dark-theme-i18n');
    },
  },

  // ---------- 智能助手会话（同一现存会话的不同历史时刻）
  ...[
    ['agent-create-video-1', async (page) => {
      await open(page, `/?job=${data.job.id}`, '#job-workspace:not([hidden])');
      await waitDrawer(page);
      // 用户要求：右侧展示最初的提示词（生成视频的那条请求）。
      await drawerScrollToText(page, 'travel introduction video');
      await snap(page, 'agent-create-video-1');
    }],
    ['agent-create-video-2', async (page) => {
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await openShotStage(page, 0, 'picture'); // 原图：分镜 1 打开召回详情
      await waitDrawer(page);
      await drawerScrollToTool(page, 'run_node', 1, true); // 第二个 run_node = 拆分镜
      await snap(page, 'agent-create-video-2');
    }],
    ['agent-create-video-3', async (page) => {
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await openShotStage(page, data.shotCount - 1, null); // 原图：末段分镜渲染卡
      await waitDrawer(page);
      await drawerScrollToText(page, data.bigBuildId); // 全量构建（原图：逐镜生产与配音）
      await snap(page, 'agent-create-video-3');
    }],
    ['agent-create-video-4', async (page) => {
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await page.locator('#result-section').scrollIntoViewIfNeeded();
      await page.evaluate(() => {
        const sec = document.querySelector('#result-section');
        sec.open = true;
        for (const d of sec.querySelectorAll('details.dependency')) d.open = true;
      });
      await page.waitForSelector('#final-video', { state: 'attached' }).catch(() => {});
      await waitDrawer(page);
      await drawerScrollBottom(page);
      await snap(page, 'agent-create-video-4');
    }],
    ['agent-merge-shots', async (page) => {
      // 用户指定：替代原“Agent 检索片段”——右侧展示“将分镜1和2合并”命令，
      // 左侧展示合并后第一个分镜的召回详情。
      await open(page, `/?job=${data.job.id}`, '#shot-strip .shot-card');
      await openShotStage(page, 0, 'picture');
      await waitDrawer(page);
      await drawerScrollToText(page, '将分镜1和2合并');
      await snap(page, 'agent-merge-shots');
    }],
  ].map(([name, run]) => ({ name, run })),
];

// ---------------------------------------------------------------- 主流程
(async () => {
  browser = await chromium.launch({ headless: true });
  ctx = await browser.newContext({
    viewport: { width: 1890, height: 915 },
    deviceScaleFactor: 2,
  });
  ctx.setDefaultTimeout(45000);

  const login = await ctx.request.post(BASE + '/login', { form: { token: TOKEN, next: '/' } });
  // 303 会被 playwright 自动跟随成 200（落到首页），两者都算登录成功。
  if (![200, 303].includes(login.status())) throw new Error(`登录失败: HTTP ${login.status()}`);

  // 数据解析：素材、任务、会话、草稿、选题档案、查询图。
  console.log('解析线上数据 …');
  const sources = (await apiJson('/api/admin/sources?visibility=all&page_size=100&page=1')).items;
  data.dearYou = sources.find((s) => /Dear You/i.test(s.name)) || sources[0];
  data.botanic = sources.find((s) => /Botanic/i.test(s.name)) || sources.find((s) => s.id !== data.dearYou.id);
  data.singaporeSourceIds = sources.filter((s) => s.id !== data.dearYou.id).map((s) => s.id);
  data.job = (await apiJson('/api/studio/jobs')).items[0];
  if (!data.job) throw new Error('服务上没有创作任务');
  const ws = await apiJson(`/api/studio/jobs/${data.job.id}/workspace?compact=true`);
  const plan = ws.plans[ws.active_plan] || Object.values(ws.plans)[0];
  data.shotCount = Object.keys(plan.shots).length;
  // 会话里第 3 次构建是全量逐镜生产（104 节点），作为 agent-3 的滚动锚点。
  data.bigBuildId = (ws.builds || [])[2]?.id || (ws.builds || [])[0]?.id || '';
  const sessions = (await apiJson('/api/agent/sessions')).items;
  data.session = sessions.find((s) => /新加坡|Singapore/i.test(s.title)) || sessions[0];
  data.draft = (await apiJson('/api/persons/imports')).find((d) => d.entry_count >= 8)
    || (await apiJson('/api/persons/imports'))[0];
  data.topicProfile = (await apiJson('/api/topics/profiles')).items[0];
  // 用户提供的以图搜图查询图（与原始浅色截图里的手持红酒照片同源）。
  data.wineImage = '/Users/hengliu/Downloads/Hackathon/wine.webp';
  if (!fs.existsSync(data.wineImage)) throw new Error(`查询图不存在: ${data.wineImage}`);
  // 查询图：取一帧 Dear You 分镜图。
  const review = await apiJson('/api/persons/review/shots?offset=0&limit=1');
  const imgUrl = review.items?.[0]?.image_url;
  if (imgUrl) {
    const res = await ctx.request.get(BASE + imgUrl);
    fs.mkdirSync('/tmp/mindex-capture', { recursive: true });
    data.queryImage = '/tmp/mindex-capture/query.jpg';
    fs.writeFileSync(data.queryImage, await res.body());
  } else data.queryImage = null;
  console.log(`  素材 ${sources.length} 个 | 任务 ${data.job.title} | 分镜 ${data.shotCount} | 会话 ${data.session?.title}`);

  // 暗色主题 + 英文界面 + 抽屉偏好，写进每个页面的 localStorage/sessionStorage（首帧前生效）。
  await ctx.addInitScript(({ sid, width }) => {
    try {
      localStorage.setItem('mindex.theme', 'dark');
      localStorage.setItem('mindex.language', 'en');
      localStorage.setItem('mindex.agent.session', sid);
      localStorage.setItem('mindex.agent.width', width);
      localStorage.setItem('mindex.agent.follow', '1');
      localStorage.setItem('mindex.agent.alwaysAllow', '1');
      localStorage.setItem('mindex.agent.followScroll', '0');
    } catch {}
  }, { sid: data.session?.id || '', width: '760' });
  // agent 系列截图需要自动展开抽屉。
  for (const r of RECIPES) {
    if (r.name.startsWith('agent-') && !r.run.toString().includes('mindex.agent.open')) {
      const inner = r.run;
      r.run = async (page) => {
        await page.addInitScript(() => { try { sessionStorage.setItem('mindex.agent.open', '1'); } catch {} });
        await inner(page);
      };
    }
  }

  const names = ONLY ? RECIPES.filter((r) => ONLY.includes(r.name)) : RECIPES;
  if (ONLY && names.length !== ONLY.length) {
    console.warn(`警告：--only 中有未知名字: ${ONLY.filter((n) => !RECIPES.some((r) => r.name === n)).join(', ')}`);
  }
  console.log(`开始截图 ${names.length} 张 …`);
  const failed = [];
  for (const recipe of names) {
    process.stdout.write(`· ${recipe.name} …\n`);
    const page = await newPage();
    try {
      await recipe.run(page);
    } catch (err) {
      failed.push(recipe.name);
      console.error(`  ✗ ${recipe.name}: ${err.message}`);
    } finally {
      await page.close().catch(() => {});
    }
  }
  await browser.close();
  if (failed.length) {
    console.error(`\n失败 ${failed.length} 张: ${failed.join(', ')}（可 --only 重试）`);
    process.exitCode = 1;
  } else console.log('\n全部完成');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
