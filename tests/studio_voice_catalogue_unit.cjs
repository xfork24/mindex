const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = require("./studio_source.cjs").studioSource();

// 方案参数音色目录：往返昂贵，必须短 TTL 缓存并合并在途请求，避免每次开对话框都阻塞一次 TTS 服务调用。
let release, calls = 0;
const state = { id: 'A', matrixRows: new Map(), data: null };
const ctx = vm.createContext({
  state, Date,
  api: async (url) => {
    assert.equal(url, '/api/settings/voices/catalog');
    calls++;
    return new Promise((resolve) => { release = resolve; });
  },
  fillCreateSources() {}, renderJobMeta() {}, renderMatrix() {},
});
vm.runInContext(
  code.slice(code.indexOf('let recallCatalogue ='), code.indexOf('function fillCreateSources(')),
  ctx,
);

(async () => {
  const first = ctx.loadVoiceCatalogue();
  const second = ctx.loadVoiceCatalogue();
  assert.equal(calls, 1, 'concurrent opens share a single in-flight catalog request');
  release({ backend: 'openai', groups: [], library: [], default_voice: '' });
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.backend, 'openai');
  assert.equal(b.backend, 'openai');

  await ctx.loadVoiceCatalogue();
  assert.equal(calls, 1, 'reopening within the TTL reuses the cached catalog');

  const fresh = ctx.loadVoiceCatalogue({ fresh: true });
  assert.equal(calls, 2, 'fresh=true bypasses the cache for an explicit refresh');
  release({ backend: 'openai', groups: [], library: [], default_voice: '' });
  await fresh;

  console.log('Studio plan-settings voice catalogue caches with TTL and dedups in-flight requests');
})().catch((e) => { console.error(e); process.exitCode = 1; });
