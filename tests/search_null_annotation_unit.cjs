const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/src/search/app.js','utf8');
class Node {
  constructor(tag,text) {this.tag=tag;this.text=text;this.children=[];}
  setAttribute() {}
  addEventListener() {}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.children=nodes;}
  querySelectorAll() {return [];}
  get childElementCount() {return this.children.length;}
  showModal() {this.open=true;}
}
const dom = Object.fromEntries(['results','result-count','timing','detail-content','detail-dialog','search-form'].map(id=>[id,new Node('div')]));
const context = vm.createContext({
  $:id=>dom[id], element:(tag,text)=>new Node(tag,text),queueMicrotask:()=>{},clock:String,
  MindexI18n:{msg:key=>key,setText:(node,text)=>{node.text=text;}},
  routeNames:{},managementLink:()=>new Node('a'),correctionLink:()=>new Node('a'),bestPersonEvidence:()=>[],
});
context.window = context;
context.addEventListener = () => {};
context.document = {querySelectorAll: () => []};
context.MindexI18n.language = 'zh';
context.MindexI18n.render = text => text;
vm.runInContext(fs.readFileSync('frontend/src/shared/analysis-text.js', 'utf8'), context);
vm.runInContext(source.slice(source.indexOf('function details('),source.indexOf('function managementLink(')),context);
vm.runInContext(source.slice(source.indexOf('function render('),source.indexOf("$('search-form').onsubmit")),context);
const base = {id:'skipped',source_id:'s',source_name:'给阿嬷的情书',kind:'moment',start:0,end:1,score:.8,dialogue:[],matches:[],person_evidence:[]};
for (const annotation of [null,undefined,{}, {scene:null,emotions:null,shot_size:null,actions:null}]) {
  const result = {...base,annotation,annotation_status:'skipped'};
  context.render({results:[result],elapsed_ms:5});
  assert.equal(dom.results.children.length,1);
  context.details(result);
  assert.equal(dom['detail-dialog'].open,true);
  assert.ok(dom['detail-content'].children.some(node=>node.text==='ui.search_analysis_skipped'));
}
const complete = {...base,id:'complete',annotation_status:'complete',annotation:{scene:['室内'],emotions:['温馨'],shot_size:['近景'],description:'完整分析'}};
context.render({results:[{...base,annotation:null},complete],elapsed_ms:5});
assert.equal(dom.results.children.length,2);
context.details(complete);
assert.ok(dom['detail-content'].children.some(node=>node.text==='完整分析'));
console.log('Null and skipped annotation result rendering passed');
