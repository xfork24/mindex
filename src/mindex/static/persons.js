'use strict';
const $=id=>document.getElementById(id);
const state={entities:[],references:[],sources:[],editing:null,editReference:null,editRole:null,filters:{work:[],actor:[],role:[]},expanded:new Set(),draft:null,entry:0,image:null,review:null,evidenceOffset:0};
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=`notice ${error?'error':'info'}`;}
async function api(url,opts={}){const r=await fetch(url,opts);const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail||d));return d;}
const json=(method,data)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
async function run(fn){try{await fn();}catch(e){notice(e.message,true);}}
function button(text,fn){const b=node('button',text);b.type='button';b.onclick=()=>run(fn);return b;}
function options(select,items,empty=true){const value=select.value;select.replaceChildren();if(empty){const o=node('option','请选择 / 不指定');o.value='';select.append(o);}for(const e of items){const o=node('option',`${e.name} · ${e.id.slice(0,8)}`);o.value=e.id;select.append(o);}select.value=value;}
function entities(kind){return state.entities.filter(e=>e.kind===kind);}
function name(id){return state.entities.find(e=>e.id===id)?.name||'未指定';}
function selected(id){return Array.from($(id).selectedOptions).map(o=>o.value).filter(Boolean);}
function entityFields(){const kind=$('entity-kind').value;$('work-field').hidden=kind!=='role';$('actors-field').hidden=kind!=='role';$('entity-files-field').hidden=kind!=='actor';}
$('entity-kind').onchange=entityFields;
function fillEntity(e){$('entity-kind').value=e?.kind||'actor';$('entity-name').value=e?.name||'';$('entity-aliases').value=(e?.aliases||[]).join('，');for(const o of $('entity-work').options)o.selected=(e?.work_ids||[]).includes(o.value);for(const o of $('entity-actors').options)o.selected=(e?.actor_ids||[]).includes(o.value);entityFields();}
function editEntity(e=null,reference=null){
  state.editing=e?.id||null;state.editReference=reference?.id||null;state.editRole=reference?.role_id||null;
  $('entity-form').reset();$('entity-error').textContent='';fillEntity(e);
  $('entity-kind').disabled=!!e||!!state.editRole;$('pair-field').hidden=!reference;
  options($('reference-actor'),entities('actor'),false);if(reference)$('reference-actor').value=reference.actor_id;
  const photo=reference||state.references.find(r=>r.role_id===e?.id)||state.references.find(r=>!r.role_id&&r.actor_id===e?.id);
  $('entity-photo').hidden=!photo;if(photo)$('entity-photo').src=photo.url;else $('entity-photo').removeAttribute('src');
  $('entity-title').textContent=e?'修改配对与人物信息':'新建演员或角色';$('entity-save').textContent=e?'保存修改':'新建';
  $('entity-dialog').showModal();
}
$('entity-new').onclick=()=>editEntity();
$('entity-reset').onclick=$('entity-close').onclick=()=>$('entity-dialog').close();
$('reference-actor').onchange=()=>{if(state.editRole)return;const e=state.entities.find(e=>e.id===$('reference-actor').value);if(e){state.editing=e.id;fillEntity(e);}};
async function appendReferences(actorId,files,roleId=null){for(const file of files){const form=new FormData();form.append('file',file);if(roleId)form.append('role_id',roleId);form.append('actor_id',actorId);await api('/api/persons/references',{method:'POST',body:form});}}
$('entity-form').onsubmit=async event=>{
  event.preventDefault();$('entity-save').disabled=true;$('entity-error').textContent='';
  try{
    const kind=$('entity-kind').value,body={kind,name:$('entity-name').value,aliases:$('entity-aliases').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),actor_ids:kind==='role'?selected('entity-actors'):[],work_ids:kind==='role'?selected('entity-work'):[]};
    let actorId=state.editRole||state.editing;
    if(state.editReference){const payload=state.editRole?{role_id:state.editRole,actor_id:$('reference-actor').value,entity:body}:{actor_id:actorId,entity:body};const reference=await api('/api/persons/references/'+state.editReference,json('PUT',payload));actorId=reference.actor_id;}
    else{const entity=await api('/api/persons/entities'+(state.editing?'/'+state.editing:''),json(state.editing?'PUT':'POST',body));actorId=entity.id;state.editing=entity.id;}
    if(kind==='actor')await appendReferences(actorId,$('entity-files').files);
    $('entity-dialog').close();await refresh();notice('人物信息与参考图已保存；重新发布后用于检索。');
  }catch(error){$('entity-error').textContent=error.message;}finally{$('entity-save').disabled=false;}
};
const FILTERS=[['work','filter-work','work'],['actor','filter-actor','actor'],['role','filter-role','role']];
function readFilters(){for(const [key,id] of FILTERS)state.filters[key]=selected(id);}
// 多选框不能用 options()：它只恢复单个 value，重建列表时必须自己记住已选项。
function fillFilters(){for(const [key,id,kind] of FILTERS){const sel=$(id),keep=new Set(selected(id));options(sel,entities(kind),false);for(const o of sel.options)o.selected=keep.has(o.value);}}
for(const [,id] of FILTERS)$(id).onchange=()=>renderCatalogue();
$('filter-reset').onclick=()=>{for(const [,id] of FILTERS)for(const o of $(id).options)o.selected=false;state.filters={work:[],actor:[],role:[]};renderCatalogue();};
function matchRole(r){const f=state.filters;return(!f.work.length||(r.work_ids||[]).some(id=>f.work.includes(id)))&&(!f.actor.length||r.actor_ids.some(id=>f.actor.includes(id)))&&(!f.role.length||f.role.includes(r.id));}
function rolesOf(actor){return entities('role').filter(r=>r.actor_ids.includes(actor.id)&&matchRole(r));}
function actorVisible(actor){const f=state.filters;if(f.actor.length&&!f.actor.includes(actor.id))return false;return(f.role.length||f.work.length)?rolesOf(actor).length>0:true;}
function fillSelectors(){for(const id of ['capture-actor','entity-actors'])options($(id),entities('actor'));for(const id of ['entity-work','import-work'])options($(id),entities('work'));for(const id of ['capture-source','process-source','review-source'])options($(id),state.sources);fillFilters();fillCaptureRoles();}
function referenceCard(entity,ref){const figure=node('figure'),img=node('img');img.src=ref.url;img.alt=`${entity.name} 参考图`;img.loading='lazy';figure.append(img,button('修改配对',async()=>editEntity(entity,ref)),button('移除参考',async()=>{await api('/api/persons/references/'+ref.id,{method:'DELETE'});await refresh();notice('参考图已移除。');}));return figure;}
function uploadForm(entity,actorId){
  const form=node('form',undefined,'reference-upload'),label=node('label',entity.kind==='role'?'角色参考图':'追加参考图（可多选）'),files=node('input');files.type='file';files.accept='image/*';files.multiple=true;files.required=true;label.append(files);
  let performer=null;
  if(entity.kind==='role'&&entity.actor_ids.length>1){const actorLabel=node('label','照片中的演员');performer=node('select');performer.required=true;options(performer,entities('actor').filter(a=>entity.actor_ids.includes(a.id)));actorLabel.append(performer);form.append(actorLabel);}
  const submit=node('button','追加参考图');submit.type='submit';form.append(label,submit);
  form.onsubmit=event=>{event.preventDefault();run(async()=>{submit.disabled=true;try{await appendReferences(performer?performer.value:actorId,files.files,entity.kind==='role'?entity.id:null);await refresh();notice('照片已追加；再次处理可复用已有视频特征。');}finally{submit.disabled=false;}});};
  return form;
}
function roleCard(role){
  const card=node('article',undefined,'person-card role-card');
  card.append(node('h4',role.name),node('p','作品：'+role.work_ids.map(name).join('、')),node('p','演员：'+role.actor_ids.map(name).join('、')));
  const refs=state.references.filter(r=>r.role_id===role.id),photos=node('div',undefined,'person-photos');
  for(const ref of refs)photos.append(referenceCard(role,ref));
  card.append(refs.length?photos:node('p','暂无参考图','muted'));
  if(role.actor_ids.length)card.append(uploadForm(role,role.actor_ids[0]));
  else card.append(node('p','请先在「修改配对」中指定出演演员','muted'));
  card.append(button('修改配对',async()=>editEntity(role)));
  return card;
}
function actorCard(actor){
  const roles=rolesOf(actor),open=state.expanded.has(actor.id);
  const card=node('article',undefined,'person-card');
  const head=button(`${open?'▾':'▸'} ${actor.name}${roles.length?` · ${roles.length} 个角色`:''}`,()=>{open?state.expanded.delete(actor.id):state.expanded.add(actor.id);renderCatalogue();});
  head.className='person-toggle';head.setAttribute('aria-expanded',String(open));
  const refs=state.references.filter(r=>!r.role_id&&r.actor_id===actor.id),photos=node('div',undefined,'person-photos');
  for(const ref of refs)photos.append(referenceCard(actor,ref));
  card.append(head,node('p',actor.aliases.join(' / ')),refs.length?photos:node('p','暂无参考图','muted'));
  if(!refs.length)card.append(button('修改配对',async()=>editEntity(actor)));
  card.append(uploadForm(actor,actor.id));
  if(open){const sub=node('div',undefined,'role-grid');for(const role of roles)sub.append(roleCard(role));card.append(sub);}
  return card;
}
function renderCatalogue(){
  readFilters();
  const list=$('entity-list');list.replaceChildren();
  const actors=entities('actor').filter(actorVisible);
  for(const actor of actors)list.append(actorCard(actor));
  const unassigned=entities('role').filter(r=>!r.actor_ids.length&&matchRole(r));
  if(unassigned.length){const group=node('div');group.append(node('h3','未关联演员的角色'));for(const role of unassigned)group.append(roleCard(role));list.append(group);}
  if(!actors.length&&!unassigned.length)list.append(node('p','没有符合筛选条件的人物','empty'));
  $('filter-summary').textContent=`显示 ${actors.length} / ${entities('actor').length} 名演员${state.expanded.size?` · ${state.expanded.size} 名已展开`:''}`;
}
function renderTasks(data) {
  $('tasks').replaceChildren();
  for (const task of data.tasks) {
    const row = node('div', undefined, 'person-card');
    const review = data.review_shots?.[task.source_id] || {};
    const sourceName = state.sources.find(s => s.id === task.source_id)?.name || task.source_id;
    row.append(node('p', `${sourceName} ${task.id.slice(0, 8)} · ${task.status} · ${task.progress.completed_shots || 0}/${task.progress.total_shots || '?'}`));
    if (task.error) row.append(node('p', task.error));
    if (task.result) {
      const result = task.result;
      const parts = [`耗时 ${result.elapsed_seconds}s`];
      if (result.accepted_shots !== undefined) parts.push(`本次处理已接受 ${result.accepted_shots} 个分镜`);
      parts.push(`复用 ${result.reused_shots} 镜头`, `当前已确认 ${review.accepted || 0} / 待核对 ${review.candidate || 0} 个分镜`);
      row.append(node('p', parts.join(' · ')));
    }
    if (task.result?.reference_errors?.length) {
      row.append(node('p', task.result.reference_errors.map(e => `${e.reference_id.slice(0, 8)}：${e.error}`).join('；')));
    }
    if (['failed', 'interrupted'].includes(task.status)) {
      row.append(button('重试', async () => {
        await api(`/api/persons/tasks/${task.id}/retry`, {method: 'POST'});
        await refresh();
      }), button('删除失败任务', async () => {
        await api(`/api/persons/tasks/${task.id}`, {method: 'DELETE'});
        await refresh();
        notice('任务记录已删除；镜头检查点和人物证据保留。');
      }));
    }
    $('tasks').append(row);
  }
}
async function refresh(){const data=await api('/api/persons');Object.assign(state,{entities:data.entities,references:data.references,sources:data.sources});fillSelectors();renderCatalogue();
$('source-works').replaceChildren();for(const s of state.sources){const row=node('div',undefined,'source-work');const work=node('select');work.setAttribute('aria-label',s.name+' 所属作品');options(work,entities('work'),false);work.value=s.work_id;const coverage=data.coverage.filter(c=>c.source_id===s.id);const intervals=coverage.map(c=>`${c.data.start.toFixed(1)}–${c.data.end.toFixed(1)}`).slice(0,6);row.append(node('strong',s.name),work,button('关联作品',async()=>{await api(`/api/persons/sources/${s.id}/work`,json('PUT',{work_id:work.value}));await refresh();notice('作品关系已变更，请重新配对并发布人物证据。');}),node('span',coverage.length?`已处理 ${coverage.length} 个镜头区间：${intervals.join('、')}${coverage.length>6?'…':''}`:'人物覆盖缺失','coverage'));$('source-works').append(row);}
renderTasks(data);
$('releases').replaceChildren();for(const r of data.releases){const row=node('p',`${r.created_at} · 内容 ${r.content_id.slice(0,8)} · 人物 ${r.person_id?.slice(0,8)||'未发布'} `);row.append(button('恢复此组合',async()=>{await api(`/api/persons/releases/${r.id}/activate`,{method:'POST'});notice('已恢复检索组合。');}));$('releases').append(row);}entityFields();}
// 从原片选取参考：演员与角色同时指定，截图始终配给角色；框选坐标是原图像素。
const capture={box:null,ready:false};
function fillCaptureRoles(){
  const actorId=$('capture-actor').value,work=state.sources.find(s=>s.id===$('capture-source').value)?.work_id;
  const keep=$('capture-role').value;
  const roles=entities('role').filter(r=>(!actorId||r.actor_ids.includes(actorId))&&(!work||(r.work_ids||[]).includes(work)));
  options($('capture-role'),roles.map(r=>({id:r.id,name:`${r.name} · ${r.work_ids.map(name).join('、')}`})),false);
  $('capture-role').value=roles.some(r=>r.id===keep)?keep:'';
}
$('capture-actor').onchange=fillCaptureRoles;
$('capture-source').onchange=()=>{fillCaptureRoles();resetCapture();const v=$('capture-video');v.pause();v.onloadedmetadata=v.onloadeddata=v.onseeked=null;v.removeAttribute('src');v.load();v.hidden=true;};
$('capture-role').onchange=()=>{const role=state.entities.find(e=>e.id===$('capture-role').value);if(role?.actor_ids.length===1)$('capture-actor').value=role.actor_ids[0];};
function drawCapture(){
  const canvas=$('capture-canvas'),ctx=canvas.getContext('2d'),v=$('capture-video');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.drawImage(v,0,0,canvas.width,canvas.height);
  if(capture.box){const [x1,y1,x2,y2]=capture.box;ctx.strokeStyle='#ff5035';ctx.lineWidth=Math.max(2,canvas.width/350);ctx.strokeRect(x1,y1,x2-x1,y2-y1);}
}
function resetCapture(){capture.box=null;capture.ready=false;captureDrag=null;$('capture-canvas').hidden=true;}
$('capture-time').oninput=resetCapture;
$('capture-preview').onclick=()=>run(async()=>{
  const v=$('capture-video'),canvas=$('capture-canvas'),timestamp=Number($('capture-time').value);
  if(!$('capture-source').value)throw Error('请先选择素材');
  const grab=()=>{if(!v.videoWidth||v.readyState<2)return;resetCapture();$('capture-time').value=v.currentTime.toFixed(3);if(!v.paused)return;canvas.width=v.videoWidth;canvas.height=v.videoHeight;canvas.hidden=false;drawCapture();capture.ready=true;};
  resetCapture();v.pause();v.hidden=false;v.onseeked=grab;v.onloadeddata=grab;
  v.onloadedmetadata=()=>{if(Math.abs(v.currentTime-timestamp)>0.001)v.currentTime=timestamp;else grab();};
  v.src=`/api/admin/sources/${$('capture-source').value}/media`;
  v.load();  // preload=none 时只赋 src 不会真的加载，必须显式 load
});
$('capture-video').onplay=resetCapture;
$('capture-video').ontimeupdate=()=>{const v=$('capture-video');$('capture-time').value=v.currentTime.toFixed(3);if(!v.paused)resetCapture();};
let captureDrag=null;
function capturePoint(e){const c=$('capture-canvas'),r=c.getBoundingClientRect();return[Math.max(0,Math.min(c.width,(e.clientX-r.left)*c.width/r.width)),Math.max(0,Math.min(c.height,(e.clientY-r.top)*c.height/r.height))];}
$('capture-canvas').onpointerdown=e=>{if(!capture.ready)return;captureDrag=capturePoint(e);$('capture-canvas').setPointerCapture(e.pointerId);};
$('capture-canvas').onpointermove=e=>{if(!captureDrag)return;const[x,y]=capturePoint(e);capture.box=[Math.min(x,captureDrag[0]),Math.min(y,captureDrag[1]),Math.max(x,captureDrag[0]),Math.max(y,captureDrag[1])].map(Math.round);drawCapture();};
$('capture-canvas').onpointerup=e=>{if(!captureDrag)return;$('capture-canvas').onpointermove(e);captureDrag=null;};
$('capture-canvas').onpointercancel=()=>captureDrag=null;
$('capture-form').onsubmit=e=>{e.preventDefault();run(async()=>{
  const roleId=$('capture-role').value;
  if(!roleId)throw Error('请选择角色；这里的截图始终作为该角色的参考图。');
  if(capture.box&&(capture.box[2]-capture.box[0]<8||capture.box[3]-capture.box[1]<8))throw Error('框选区域太小，请重新拖拽。');
  await api('/api/persons/references/from-source',json('POST',{source_id:$('capture-source').value,timestamp:Number($('capture-time').value),actor_id:$('capture-actor').value||null,role_id:roleId,bbox:capture.box}));
  resetCapture();await refresh();notice('参考截图已保存到该角色。');
});};
for(const tab of document.querySelectorAll('[data-pane]'))tab.onclick=()=>{document.querySelectorAll('.pane').forEach(n=>n.hidden=n.id!==tab.dataset.pane);document.querySelectorAll('[data-pane]').forEach(n=>n.classList.toggle('active',n===tab));};
$('process-form').onsubmit=e=>{e.preventDefault();run(async()=>{if(!$('process-source').value)throw Error('请先选择素材。');const task=await api(`/api/persons/sources/${$('process-source').value}/process`,json('POST',{start:Number($('process-start').value),end:$('process-end').value?Number($('process-end').value):null,publish:false}));notice('已启动人物任务：'+task.id);await refresh();});};
$('publish').onclick=()=>run(async()=>{const r=await api('/api/persons/publish',json('POST',{}));await refresh();notice('已发布人物版本：'+r.person_version);});
// Draft editing preserves original-pixel boxes for every uploaded layout.
function newEntry(){return{image_id:$('import-image').value||state.draft.images[0].id,actor_name:'',foreign_name:'',role_name:'',actor_id:null,role_id:null,bbox:null,single_person:false};}
async function displayDraft(draft){state.draft=draft;$('import-id').value=draft.id;history.replaceState(null,'','/persons?draft='+draft.id);$('import-tools').hidden=false;options($('import-image'),draft.images.map((im,i)=>({id:im.id,name:`原图 ${i+1} · ${im.width}×${im.height}`})),false);$('import-image').value=draft.images[0].id;if(draft.work_id)$('import-work').value=draft.work_id;$('import-warnings').replaceChildren(...draft.warnings.map(w=>node('p',w)));state.entry=0;const legacy=['stage_name','stage_id','age','styling','costume'];for(const e of draft.entries)for(const k of legacy)delete e[k];renderEntries();await loadImage();}
$('import-form').onsubmit=e=>{e.preventDefault();run(async()=>{const form=new FormData();for(const file of $('import-files').files)form.append('files',file);await displayDraft(await api('/api/persons/imports',{method:'POST',body:form}));});};
$('import-load').onclick=()=>run(async()=>displayDraft(await api('/api/persons/imports/'+$('import-id').value.trim())));
$('import-parse').onclick=()=>run(async()=>{notice('正在拆分演员表…');$('import-parse').disabled=true;try{await displayDraft(await api('/api/persons/imports/'+state.draft.id+'/parse',{method:'POST'}));notice('请逐条核对后确认。');}finally{$('import-parse').disabled=false;}});
$('import-add').onclick=()=>{state.draft.entries.push(newEntry());state.entry=state.draft.entries.length-1;renderEntries();draw();};
// 演员表导入是人物的主要创建入口：填写新作品名即创建，同名身份自动复用。
async function importWorkId(){const name=$('import-new-work').value.trim();if(!name)return $('import-work').value;const created=await api('/api/persons/entities',json('POST',{kind:'work',name}));await refresh();$('import-work').value=created.id;$('import-new-work').value='';return created.id;}
async function draftBody(){return{work_id:await importWorkId(),entries:state.draft.entries};}
// 提交前在客户端校验，避免 422 英文校验错误；服务端仍逐条复核。
function validateDraft(){if(!$('import-work').value&&!$('import-new-work').value.trim())throw Error('请选择导入作品，或填写新作品名。');state.draft.entries.forEach((e,i)=>{const n=`条目 ${i+1}`;if(!(e.actor_name||'').trim()&&!e.actor_id)throw Error(`${n}：请填写演员名，或关联已有演员。`);if(e.bbox&&!e.single_person)throw Error(`${n}：照片框需要勾选“已确认照片框仅一个人物”。`);});}
$('import-save').onclick=()=>run(async()=>{validateDraft();await api('/api/persons/imports/'+state.draft.id,json('PUT',await draftBody()));notice('草稿已保存。');});
$('import-confirm').onclick=()=>run(async()=>{validateDraft();await api('/api/persons/imports/'+state.draft.id+'/confirm',json('POST',await draftBody()));state.draft.status='confirmed';await refresh();notice('已导入人物库。');});
function renderEntries(){const target=$('import-entries');target.replaceChildren();state.draft.entries.forEach((entry,index)=>{const card=node('article',undefined,`entry ${index===state.entry?'selected':''}`);card.append(button(`条目 ${index+1} · 选择照片框`,async()=>{state.entry=index;$('import-image').value=entry.image_id;renderEntries();await loadImage();}));for(const [key,label] of [['actor_name','演员名'],['foreign_name','外文名'],['role_name','角色名（不同时期直接写全名，如老年叶淑柔）']]){const l=node('label',label),input=node('input');input.value=entry[key]||'';input.oninput=()=>entry[key]=input.value;l.append(input);card.append(l);}for(const [key,kind,label] of [['actor_id','actor','关联已有演员（默认按姓名复用）'],['role_id','role','合并已有角色（跨电影同一角色）']]){const l=node('label',label),select=node('select');options(select,entities(kind));select.value=entry[key]||'';select.onchange=()=>entry[key]=select.value||null;l.append(select);card.append(l);}const coords=node('div',undefined,'coordinates');for(let n=0;n<4;n++){const l=node('label',['x1','y1','x2','y2'][n]),input=node('input');input.type='number';input.min='0';input.value=entry.bbox?.[n]??'';input.onchange=()=>{entry.bbox=entry.bbox||[0,0,0,0];entry.bbox[n]=Number(input.value);draw();};l.append(input);coords.append(l);}card.append(coords);const check=node('input');check.type='checkbox';check.checked=entry.single_person;check.onchange=()=>entry.single_person=check.checked;const l=node('label','已确认照片框仅一个人物');l.prepend(check);card.append(l);const preview=node('canvas');preview.className='crop-preview';preview.dataset.entry=index;card.append(preview);card.append(button('删除重复 / 错误条目',async()=>{state.draft.entries.splice(index,1);state.entry=Math.min(state.entry,state.draft.entries.length-1);renderEntries();draw();}));target.append(card);});draw();}
async function loadImage(){const im=state.draft.images.find(im=>im.id===$('import-image').value);const image=new Image();image.src=im.url;await image.decode();state.image=image;const canvas=$('import-canvas');canvas.width=im.width;canvas.height=im.height;draw();}
$('import-image').onchange=()=>run(loadImage);
function draw(){if(!state.image||!state.draft)return;const canvas=$('import-canvas'),ctx=canvas.getContext('2d');if(state.image.naturalWidth!==canvas.width)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(state.image,0,0);for(const [index,entry] of state.draft.entries.entries()){if(entry.image_id!==$('import-image').value||!entry.bbox)continue;const [x,y,x2,y2]=entry.bbox;ctx.strokeStyle=index===state.entry?'#ff5035':'#139f80';ctx.lineWidth=Math.max(2,canvas.width/350);ctx.strokeRect(x,y,x2-x,y2-y);const preview=document.querySelector(`canvas[data-entry="${index}"]`);if(preview&&x2>x&&y2>y){preview.width=x2-x;preview.height=y2-y;preview.getContext('2d').drawImage(state.image,x,y,x2-x,y2-y,0,0,preview.width,preview.height);}}}
let drag=null;function point(e){const c=$('import-canvas'),r=c.getBoundingClientRect();return[Math.max(0,Math.min(c.width,(e.clientX-r.left)*c.width/r.width)),Math.max(0,Math.min(c.height,(e.clientY-r.top)*c.height/r.height))];}
$('import-canvas').onpointerdown=e=>{if(!state.draft.entries[state.entry])return;drag=point(e);$('import-canvas').setPointerCapture(e.pointerId);};
$('import-canvas').onpointermove=e=>{if(!drag)return;const [x,y]=point(e),entry=state.draft.entries[state.entry];entry.image_id=$('import-image').value;entry.bbox=[Math.min(x,drag[0]),Math.min(y,drag[1]),Math.max(x,drag[0]),Math.max(y,drag[1])].map(Math.round);entry.single_person=false;draw();};
$('import-canvas').onpointerup=e=>{if(!drag)return;$('import-canvas').onpointermove(e);drag=null;renderEntries();};
$('import-canvas').onpointercancel=()=>drag=null;
const reviewPageSize=50;
function pairKey(actor,role){return JSON.stringify([actor,role||null]);}
// 审核语境的身份显示：角色名在前，演员名随后；未指明角色时回退到该演员在本作品的全部角色。
function personLabel(actorId,roleId,workId){
  if(!actorId)return '未知';
  const roles=roleId
    ?state.entities.filter(e=>e.id===roleId&&e.kind==='role')
    :entities('role').filter(r=>(r.work_ids||[]).includes(workId)&&r.actor_ids.includes(actorId));
  return roles.length?roles.map(r=>r.name).join('/')+' · '+name(actorId):name(actorId);
}
async function loadEvidence(){
  const params=new URLSearchParams({source_id:$('review-source').value,status:$('review-status').value,offset:state.evidenceOffset,limit:reviewPageSize});
  const data=await api('/api/persons/review/shots?'+params);
  if(!data.items.length&&state.evidenceOffset>0){state.evidenceOffset=Math.max(0,state.evidenceOffset-reviewPageSize);return loadEvidence();}
  $('evidence-list').replaceChildren();
  for(const shot of data.items){
    const card=node('article',undefined,'person-card');
    if(shot.image_url){const image=node('img');image.src=shot.image_url;image.alt='分镜证据截图';image.loading='lazy';card.append(image);}
    const work=state.sources.find(s=>s.id===shot.source_id)?.work_id;
    const accepted=[...new Set(shot.evidence.filter(e=>e.status==='accepted').map(e=>personLabel(e.actor_id,e.role_id,work)))];
    card.append(...[node('h3',`${shot.start.toFixed(2)}–${shot.end.toFixed(2)} 秒`),node('p',state.sources.find(s=>s.id===shot.source_id)?.name||shot.source_id),node('p',`已接受 ${shot.counts.accepted} · 待确认 ${shot.counts.candidate} · 已排除 ${shot.counts.rejected}`),node('p',accepted.join('、')||'尚未确认出镜人物'),shot.counts.candidate?node('p',`本镜头仍有 ${shot.counts.candidate} 帧未判定；核对整个镜头会一次性结清整段。`,'muted'):null,button('核对整个镜头',async()=>openReview(shot))].filter(Boolean));
    $('evidence-list').append(card);
  }
  $('evidence-page').textContent=`${Math.floor(state.evidenceOffset/reviewPageSize)+1} / ${Math.max(1,Math.ceil(data.total/reviewPageSize))} · 共 ${data.total} 个镜头`;
  $('evidence-prev').disabled=state.evidenceOffset===0;
  $('evidence-next').disabled=state.evidenceOffset+reviewPageSize>=data.total;
}
$('review-refresh').onclick=()=>{state.evidenceOffset=0;run(loadEvidence);};
$('evidence-prev').onclick=()=>{state.evidenceOffset=Math.max(0,state.evidenceOffset-reviewPageSize);run(loadEvidence);};
$('evidence-next').onclick=()=>{state.evidenceOffset+=reviewPageSize;run(loadEvidence);};
function openReview(shot){
  state.review=shot;
  $('review-image').hidden=!shot.image_url;
  if(shot.image_url)$('review-image').src=shot.image_url;
  $('review-description').textContent=`原片 ${shot.start.toFixed(3)}–${shot.end.toFixed(3)} 秒。勾选的人物将按整个镜头确认出镜；不勾选任何人物即排除整个分镜。`;
  const work=state.sources.find(s=>s.id===shot.source_id)?.work_id;
  const roleList=entities('role').filter(r=>(r.work_ids||[]).includes(work));
  const marks=new Set(shot.evidence.filter(e=>e.status==='accepted').map(e=>pairKey(e.actor_id,e.role_id)));
  const suggested=new Set(shot.evidence.filter(e=>e.actor_id).map(e=>e.actor_id));
  // 勾选单元是「角色 · 演员」身份：角色在前；未参加任何角色的演员单列演员名。
  const pairs=[];
  for(const role of roleList)for(const actorId of role.actor_ids)pairs.push({actor_id:actorId,role_id:role.id,label:`${role.name} · ${name(actorId)}`});
  const cast=new Set(pairs.map(p=>p.actor_id));
  for(const actor of entities('actor'))if(!cast.has(actor.id))pairs.push({actor_id:actor.id,role_id:null,label:actor.name});
  pairs.sort((a,b)=>Number(suggested.has(b.actor_id))-Number(suggested.has(a.actor_id))||a.label.localeCompare(b.label,'zh'));
  const choices=$('review-choices');choices.replaceChildren();
  for(const pair of pairs){
    const check=node('input');check.type='checkbox';check.value=pairKey(pair.actor_id,pair.role_id);
    // 已接受证据精确匹配；自动接受的行常无角色，回退到该演员在本作品的唯一身份。
    check.checked=marks.has(check.value)||(marks.has(pairKey(pair.actor_id,null))&&pairs.filter(p=>p.actor_id===pair.actor_id).length===1);
    const label=node('label',pair.label);label.prepend(check);choices.append(label);
  }
  const video=$('review-video');video.src=`/api/admin/segments/${shot.shot_id}/media`;video.onloadedmetadata=()=>video.currentTime=0;
  $('review-dialog').showModal();
}
async function review(accept){
  const selections=accept?Array.from($('review-choices').querySelectorAll('input:checked')).map(input=>{const [actor_id,role_id]=JSON.parse(input.value);return{actor_id,role_id};}):[];
  await api('/api/persons/review/shot',json('POST',{source_id:state.review.source_id,shot_id:state.review.shot_id,selections}));
  $('review-dialog').close();await loadEvidence();notice('分镜审核已保存；发布新人物版本后检索生效。');
}
$('review-form').onsubmit=e=>{e.preventDefault();run(()=>review(true));};
$('review-reject').onclick=()=>run(()=>review(false));
$('review-close').onclick=()=>$('review-dialog').close();
$('review-dialog').onclose=()=>$('review-video').pause();
run(async()=>{
  await refresh();
  const params=new URLSearchParams(location.search),draft=params.get('draft');
  if(draft){document.querySelector('[data-pane="cast"]').click();await displayDraft(await api('/api/persons/imports/'+draft));}
  const ev=params.get('evidence');
  if(ev){
    document.querySelector('[data-pane="review"]').click();
    const evidence=await api('/api/persons/evidence/'+ev);
    $('review-source').value=evidence.source_id;
    openReview(await api(`/api/persons/review/shots/${evidence.shot_id}?source_id=${encodeURIComponent(evidence.source_id)}`));
  }
});
setInterval(()=>{if(!$('processing').hidden)run(refresh);},5000);
