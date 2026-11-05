// 离线验证浏览器增量摘要和续传协议，不依赖真实模型或服务。
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');
const {File} = require('node:buffer');
const responses = [];
const storage = new Map();
const context = vm.createContext({Uint32Array,Uint8Array,BigInt,Set,Map,JSON,Date,
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  fetch: async (url, options={}) => {
    const item=responses.shift(); assert.ok(item,`unexpected ${url}`); assert.equal(url,item.url);
    if(item.verify) await item.verify(options);
    return {ok:true,status:200,headers:{get:()=>null},json:async()=>item.data};
  }});
vm.runInContext(fs.readFileSync('frontend/src/shared/api.js','utf8'),context);
const api=context.MindexApi;
for(const size of [0,1,55,56,63,64,65,127,128,129,1000000]) {
  const bytes=Buffer.alloc(size,97), hash=new api.Sha256();
  for(let i=0;i<size;i+=17) hash.update(bytes.subarray(i,i+17));
  assert.equal(hash.hex(),crypto.createHash('sha256').update(bytes).digest('hex'));
}
(async()=>{
 const file=new File([Buffer.from('abcdef')],'video.mp4');
 const sha256=crypto.createHash('sha256').update('abcdef').digest('hex');
 storage.set(`mindex.upload:${sha256}:6`,JSON.stringify({id:'s',upload_token:'capability'}));
 responses.push(
  {url:'/api/uploads/s',data:{id:'s',state:'pending',expires_at:Date.now()/1000+100,part_size:3,parts:[{number:1}]}},
  {url:'/api/uploads/s/parts/2',verify:async o=>{assert.equal(o.method,'PUT');assert.equal(await o.body.text(),'def');assert.equal(o.headers['X-Upload-Token'],'capability');}},
  {url:'/api/uploads/s/complete',data:{asset_id:'s'}},
  {url:'/api/admin/sources/import-upload/s',data:{source_id:'registered'}});
 assert.equal((await api.uploadSource(file)).source_id,'registered');
 assert.equal(responses.length,0);assert.equal(storage.size,0);
 console.log('PASS: incremental SHA256 boundaries and 1 MB; resume skips registered parts; complete and register.');
})().catch(e=>{console.error(e);process.exitCode=1;});
