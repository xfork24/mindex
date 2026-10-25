const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const code=require("./studio_source.cjs").studioSource();
let release,calls=0,filled=0;
const state={id:'A',stagePanel:{key:'x'},data:null};
const ctx=vm.createContext({state,Date,api:async(url)=>{
 assert.equal(url,'/api/persons/filters');calls++;return new Promise(resolve=>release=resolve);
},fillCreateSources(){filled++;},renderJobMeta(){},renderMatrix(){},invalidatePanel(){state.stagePanel.key=null;}});
vm.runInContext(code.slice(code.indexOf('let recallCatalogue ='),code.indexOf('function fillCreateSources(')),ctx);
(async()=>{
 const sources=ctx.loadSources(),recall=ctx.loadRecallOptions();
 assert.equal(calls,1,'creation and job editors share one in-flight request');
 release({sources:[{id:'s',name:'Source',work_id:'w'}],entities:[{id:'actor'}]});
 await Promise.all([sources,recall]);
 assert.equal(state.sources[0].id,'s');assert.equal(state.persons[0].id,'actor');
 assert.ok(filled>0);
 state.id='B';await ctx.loadRecallOptions();
 assert.equal(calls,1,'switching jobs reuses a recent library response');
 console.log('Studio creation/recall share lightweight requests and reuse options across job switches');
})().catch(e=>{console.error(e);process.exitCode=1;});
