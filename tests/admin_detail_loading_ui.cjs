const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('src/mindex/static/admin.js', 'utf8');
const pending = [], rendered = [], segments = [];
const elements = new Map();
const ctx = vm.createContext({AbortController, setTimeout, clearTimeout, console,
  $: id => {if (!elements.has(id)) elements.set(id, {value:'visible',replaceChildren(){},setAttribute(){},removeAttribute(){}});return elements.get(id);},
  node(){}, MindexI18n:{msg:x=>x}, pauseList(){}, syncUrl(){},
  preferences:()=>({workExpanded:true}),renderWork(){}, renderSource: s=>rendered.push({...s}), loadSources:async()=>{},
  loadSegments:async()=>{segments.push(vm.runInContext('state.source.id',ctx));},
  api:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}))});
vm.runInContext(`const state={source:null};let selectionRequest=0,segmentRequest=0,workRequest=0,workPoll=null,selectionController=null,segmentController=null;`+
  code.slice(code.indexOf('async function selectSource('),code.indexOf('function syncUrl()'))+
  code.slice(code.indexOf('async function refreshWork('),code.indexOf('let personContext')),ctx);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const summary=id=>({id,media:{duration:1},shot_count:1,moment_count:1});
(async()=>{
  const a=vm.runInContext("selectSource('A')",ctx);
  pending.find(p=>p.url.endsWith('A?detail=summary')).resolve(summary('A'));await tick();
  assert.equal(rendered.at(-1).id,'A');assert.equal(rendered.at(-1).work,undefined);
  assert.deepEqual(segments,['A']); // Shots have loaded while full status remains pending.
  const old=pending.find(p=>p.url.endsWith('/A?detail=work'));
  const b=vm.runInContext("selectSource('B')",ctx);
  assert.equal(old.options.signal.aborted,true);
  pending.find(p=>p.url.endsWith('B?detail=summary')).resolve(summary('B'));
  pending.find(p=>p.url.endsWith('/B?detail=work')).reject(new Error('status unavailable'));
  await b;
  old.resolve({...summary('A'),work:{shots:{}}});await a;
  assert.equal(rendered.at(-1).id,'B');assert.equal(rendered.at(-1).statusError,'status unavailable');
  const retry=vm.runInContext("refreshWork('B')",ctx);
  pending.at(-1).resolve({...summary('B'),work:{shots:{}}});await retry;
  assert.ok(rendered.at(-1).work);assert.equal(rendered.at(-1).statusError,undefined);
  ctx.preferences = () => ({workExpanded:false});
  const c=vm.runInContext("selectSource('C')",ctx);
  assert.equal(pending.some(p=>p.url.endsWith('/C?detail=work')),false,'collapsed work does not request details');
  pending.find(p=>p.url.endsWith('C?detail=summary')).resolve(summary('C'));await c;
  assert.equal(rendered.at(-1).id,'C');
  console.log('Independent loading, cancellation, stale response, retry and lazy collapsed loading checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
