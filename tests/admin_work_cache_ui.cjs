const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('src/mindex/static/admin.js', 'utf8');
let resolve, reject, calls = 0, renders = 0;
const prefs = {};
const context = vm.createContext({
  preferences: () => prefs,
  post: (url) => {assert.equal(url, '/api/admin/sources/A/work-cache/refresh');calls++;return new Promise((yes, no) => {resolve = yes;reject = no;});},
  renderSource: () => renders++, loadSources: async () => {}, loadSegments: async () => {},
});
vm.runInContext('let selectionRequest=1,workRequest=0;const state={source:{id:"A",work:{coverage:1}}};' +
  code.slice(code.indexOf('async function refreshWorkCache('), code.indexOf('async function refreshWork(')), context);
(async () => {
  const pending = vm.runInContext("refreshWorkCache('A')", context);
  assert.equal(prefs.refreshingCache, true);
  assert.equal(vm.runInContext('state.source.work.coverage', context), 1);
  await vm.runInContext("refreshWorkCache('A')", context);assert.equal(calls, 1);
  resolve({id:'A',work:{coverage:2}});await pending;
  assert.equal(prefs.refreshingCache, false);
  assert.equal(vm.runInContext('state.source.work.coverage', context), 2);
  const failed = vm.runInContext("refreshWorkCache('A')", context);
  reject(new Error('refresh failed'));await failed;
  assert.equal(prefs.cacheError, 'refresh failed');
  assert.equal(vm.runInContext('state.source.work.coverage', context), 2);
  const navigated = vm.runInContext("refreshWorkCache('A')", context);
  vm.runInContext('selectionRequest++;state.source={id:"B"}', context);
  resolve({id:'A',work:{coverage:3}});await navigated;
  assert.equal(vm.runInContext('state.source.id', context), 'B');
  assert.ok(renders > 0);
  console.log('Manual cache refresh, duplicate prevention, failure retention, and navigation checks passed');
})().catch(e => {console.error(e);process.exitCode = 1;});
