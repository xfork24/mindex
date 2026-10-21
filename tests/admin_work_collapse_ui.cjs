const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('src/mindex/static/admin.js','utf8');
function node(tag,text,cls) {
  return {tag,text,cls,children:[],open:false,isConnected:true,
    append(...items){this.children.push(...items);},addEventListener(){},setAttribute(){},
    insertBefore(item){this.children.unshift(item);}};
}
const prefs = {}, source={id:'A'}, target=node('div');
let requests=0, renders=0, cleared=0;
const ctx=vm.createContext({node,preferences:()=>prefs,action:(text)=>node('button',text),
  MindexI18n:{msg:x=>x},clearTimeout(){cleared++;},renderSource(){renders++;},
  refreshWork(){requests++;},run:fn=>fn(),workNames:{}});
vm.runInContext('let workPoll=1,workRequest=0;const state={source:{id:"A"}};'+
 code.slice(code.indexOf('function renderWork('),code.indexOf('async function refreshWorkCache(')),ctx);
ctx.renderWork(source,target);
const panel=target.children[0];
assert.equal(panel.tag,'details');assert.equal(panel.open,false);
assert.equal(panel.children[0].tag,'summary');assert.equal(panel.children.length,1);
assert.equal(requests,0,'collapsed work does not load details');
panel.open=true;panel.ontoggle();
assert.equal(prefs.workExpanded,true);assert.equal(requests,1);assert.equal(renders,1);
const expanded=node('div');ctx.renderWork(source,expanded);
assert.equal(expanded.children[0].open,true,'rerender preserves expansion');
panel.open=false;panel.ontoggle();
assert.equal(prefs.workExpanded,false);assert.ok(cleared>0);
assert.equal(vm.runInContext('workPoll',ctx),null,'folding stops polling');
console.log('Default collapse, lazy expansion, stable expansion and polling cancellation passed');
