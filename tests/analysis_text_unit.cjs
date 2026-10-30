const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = [], events = {};
const catalog = {'ui.d9d9827827e1':{zh:'动作',en:'Actions'},'ui.6867f7b4537a':{zh:'景别',en:'Shot size'}};
const i18n = {language:'zh',catalog,msg:key=>'@'+key,render:value=>catalog[value.slice(1)]?.[i18n.language] || value,setText:(node,value)=>{node.textContent=value;}};
const root = {MindexI18n:i18n,document:{querySelectorAll:()=>nodes},addEventListener:(name,callback)=>{events[name]=callback;}};
root.window=root;
vm.runInNewContext(fs.readFileSync('frontend/src/shared/analysis-text.js','utf8'),root);
const {select,view,setText}=root.MindexAnalysisText;
function node(){const value={setAttribute(){},textContent:''};nodes.push(value);return value;}
function language(value){i18n.language=value;events['mindex:languagechange']();}
for(const [raw,zh,en] of [
 ['城市夜景。 / City lights at night.','城市夜景。','City lights at night.'],
 ['远景 / wide shot','远景','wide shot'],
 ['室内 / 室外 / Interior or exterior','室内 / 室外','Interior or exterior'],
 ['人物在路边。 / A person walks near a cafe / restaurant.','人物在路边。','A person walks near a cafe / restaurant.'],
 ['镜头中有三个人。 / 3 people stand together.','镜头中有三个人。','3 people stand together.'],
 ['中文\n描述。\n / English\ndescription.','中文\n描述。','English\ndescription.'],
 ['只有中文','只有中文','只有中文'],['English only','English only','English only'],
 ['演员 / 角色','演员 / 角色','演员 / 角色'],
 ['网址 / https://example.com/a/b','网址 / https://example.com/a/b','网址 / https://example.com/a/b'],
 ['AC/DC','AC/DC','AC/DC'],['https://example.com/a/b','https://example.com/a/b','https://example.com/a/b'],
 ['', '', ''],[null,'','']
]){assert.equal(select(raw,'zh'),zh);assert.equal(select(raw,'en'),en);}
const annotation = {description:'城市夜景。 / City lights at night.',actions:['行走 / walking','交谈 / talking']};
const original = JSON.stringify(annotation);
const description=node(), actions=node(), match=node();
setText(description,annotation.description);
setText(actions,annotation.actions,{label:i18n.msg('ui.d9d9827827e1')});
setText(match,'城市\n夜景。 / City lights\nat night.\n动作：行走 / walking；交谈 / talking\n景别：远景 / wide shot',{view:true});
assert.equal(description.textContent,'城市夜景。');
assert.equal(actions.textContent,'动作：行走；交谈');
assert.equal(match.textContent,'城市\n夜景。\n动作：行走；交谈\n景别：远景');
language('en');
assert.equal(description.textContent,'City lights at night.');
assert.equal(actions.textContent,'Actions: walking; talking');
assert.equal(match.textContent,'City lights\nat night.\nActions: walking; talking\nShot size: wide shot');
assert.equal(view('动作：行走 / walking','en'),'Actions: walking');
language('zh');
assert.equal(actions.textContent,'动作：行走；交谈');
assert.equal(JSON.stringify(annotation),original);
assert.equal(select('<img src=x onerror=alert(1)>'),' <img src=x onerror=alert(1)>'.trim());
console.log('Analysis text selection, legacy fallback, slashes, multiline views and live language bindings passed');
