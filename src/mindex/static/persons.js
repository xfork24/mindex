'use strict';
const $=id=>document.getElementById(id);
const state={entities:[],references:[],sources:[],editing:null,editReference:null,editRole:null,filters:{work:[],actor:[],role:[]},expanded:new Set(),draft:null,entry:0,image:null,reviewShots:[],reviewPicked:new Map(),evidenceOffset:0,evidenceTotal:0,reviewVersion:0,evidenceRequest:0,reviewSelecting:false,linkedEntity:null,linkedOffset:0,linkedTotal:0,linkedReviewEntity:null};
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=`notice ${error?'error':'info'}`;}
async function api(url,opts={}){const r=await fetch(url,opts);const d=await r.json();if(!r.ok){const error=Error(typeof d.detail==='string'?d.detail:d.detail?.message||JSON.stringify(d.detail||d));error.detail=d.detail;throw error;}return d;}
const json=(method,data)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
async function run(fn){try{await fn();}catch(e){notice(e.message,true);}}
function button(text,fn){const b=node('button',text);b.type='button';b.onclick=()=>run(fn);return b;}
// 多选框顶部的“全部选择”：选中它等同全选，手动取消任意一项时它同步取消。
const ALL_VALUE='__all__';
function allOption(select){return Array.from(select.options).find(o=>o.value===ALL_VALUE);}
function itemOptions(select){return Array.from(select.options).filter(o=>o.value!==ALL_VALUE);}
function syncAll(select){const all=allOption(select);if(!all)return;const items=itemOptions(select);all.selected=items.length>0&&items.every(o=>o.selected);select.dataset.allSelected=all.selected?'1':'0';}
function applyAll(select,on){for(const o of itemOptions(select))o.selected=on;syncAll(select);}
// change 时无法区分是点了“全部选择”还是普通项，用上一次的全选状态判断意图。
function toggleAll(select){const all=allOption(select);if(all&&all.selected!==(select.dataset.allSelected==='1'))applyAll(select,all.selected);else syncAll(select);}
// 多选框：条目按中文字典序渲染并挂搜索框；单选框维持原样。
function options(select,items,empty=true){const value=select.value;select.replaceChildren();const list=select.multiple?MindexMultiSearch.sort(items):items;if(select.multiple){const all=node('option','全部选择');all.value=ALL_VALUE;select.append(all);}if(empty){const o=node('option','请选择 / 不指定');o.value='';select.append(o);}for(const e of list){const o=node('option',`${e.name} · ${e.id.slice(0,8)}`);o.value=e.id;select.append(o);}select.value=value;if(select.multiple){MindexMultiSearch.attach(select);MindexMultiSearch.refilter(select);syncAll(select);}}
function entities(kind){return state.entities.filter(e=>e.kind===kind);}
function name(id){return state.entities.find(e=>e.id===id)?.name||'未指定';}
function selected(id){return Array.from($(id).selectedOptions).map(o=>o.value).filter(v=>v&&v!==ALL_VALUE);}
function entityFields(){const kind=$('entity-kind').value;$('work-field').hidden=kind!=='role';$('actors-field').hidden=kind!=='role';$('entity-files-field').hidden=kind!=='actor';}
$('entity-kind').onchange=entityFields;
function fillEntity(e){$('entity-kind').value=e?.kind||'actor';$('entity-name').value=e?.name||'';$('entity-aliases').value=(e?.aliases||[]).join('，');for(const o of $('entity-work').options)o.selected=(e?.work_ids||[]).includes(o.value);for(const o of $('entity-actors').options)o.selected=(e?.actor_ids||[]).includes(o.value);syncAll($('entity-work'));syncAll($('entity-actors'));entityFields();}
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
async function appendReferences(actorId,files,roleId=null){const done=[],failed=[];for(const file of files){const form=new FormData();form.append('file',file);if(roleId)form.append('role_id',roleId);form.append('actor_id',actorId);try{await api('/api/persons/references',{method:'POST',body:form});done.push(file.name);}catch(error){failed.push(`${file.name}：${error.message}`);}}return {done,failed};}
$('entity-form').onsubmit=async event=>{
  event.preventDefault();$('entity-save').disabled=true;$('entity-error').textContent='';
  try{
    const kind=$('entity-kind').value,body={kind,name:$('entity-name').value,aliases:$('entity-aliases').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),actor_ids:kind==='role'?selected('entity-actors'):[],work_ids:kind==='role'?selected('entity-work'):[]};
    let actorId=state.editRole||state.editing;
    if(state.editReference){const payload=state.editRole?{role_id:state.editRole,actor_id:$('reference-actor').value,entity:body}:{actor_id:actorId,entity:body};const reference=await api('/api/persons/references/'+state.editReference,json('PUT',payload));actorId=reference.actor_id;}
    else{const entity=await api('/api/persons/entities'+(state.editing?'/'+state.editing:''),json(state.editing?'PUT':'POST',body));actorId=entity.id;state.editing=entity.id;}
    if(kind==='actor'){
      const {done,failed}=await appendReferences(actorId,$('entity-files').files);
      // 部分失败：人物信息已保存、成功图已入库，弹窗留着换文件重试。
      if(failed.length){await refresh();$('entity-error').textContent=`人物信息已保存；成功上传 ${done.length} 张，失败 ${failed.length} 张：${failed.join('；')}`;return;}
    }
    $('entity-dialog').close();await refresh();notice('人物信息与参考图已保存；重新发布后用于检索。');
  }catch(error){$('entity-error').textContent=error.message;}finally{$('entity-save').disabled=false;}
};
const FILTERS=[['work','filter-work','work'],['actor','filter-actor','actor'],['role','filter-role','role']];
function readFilters(){for(const [key,id] of FILTERS)state.filters[key]=selected(id);}
// 多选框不能用 options()：它只恢复单个 value，重建列表时必须自己记住已选项。
function fillFilters(){for(const [key,id,kind] of FILTERS){const sel=$(id),keep=new Set(selected(id));options(sel,entities(kind),false);for(const o of sel.options)if(o.value!==ALL_VALUE)o.selected=keep.has(o.value);syncAll(sel);}}
for(const [,id] of FILTERS)$(id).onchange=()=>{toggleAll($(id));renderCatalogue();};
for(const id of ['entity-work','entity-actors'])$(id).onchange=()=>toggleAll($(id));
$('filter-reset').onclick=()=>{for(const [,id] of FILTERS)applyAll($(id),false);state.filters={work:[],actor:[],role:[]};renderCatalogue();};
function matchRole(r){const f=state.filters;return(!f.work.length||(r.work_ids||[]).some(id=>f.work.includes(id)))&&(!f.actor.length||r.actor_ids.some(id=>f.actor.includes(id)))&&(!f.role.length||f.role.includes(r.id));}
function rolesOf(actor){return entities('role').filter(r=>r.actor_ids.includes(actor.id)&&matchRole(r));}
function actorVisible(actor){const f=state.filters;if(f.actor.length&&!f.actor.includes(actor.id))return false;return(f.role.length||f.work.length)?rolesOf(actor).length>0:true;}
function fillSelectors(){for(const id of ['capture-actor','entity-actors'])options($(id),entities('actor'));for(const id of ['entity-work','import-work'])options($(id),entities('work'));for(const id of ['capture-source','process-source','review-source'])options($(id),state.sources);fillFilters();fillCaptureRoles();}
// 每张参考图一张卡；extra 挂在「移除参考」右侧（实体的删除按钮放第一张参考图那行）。
// 卡片左上角显示编号前 8 位，与处理任务日志「参考图 xxxxxxxx」的指向一致；悬停可看完整编号。
function referenceCard(entity,ref,extra){const figure=node('figure'),img=node('img'),badge=node('span',ref.id.slice(0,8),'reference-id');badge.title=ref.id;img.src=ref.url;img.alt=`${entity.name} 参考图`;img.loading='lazy';figure.append(img,badge,button('修改',async()=>editEntity(entity,ref)),button('移除参考',async()=>{await api('/api/persons/references/'+ref.id,{method:'DELETE'});await refresh();notice('参考图已移除。');}),...(extra?[extra]:[]));return figure;}
function uploadForm(entity,actorId){
  const form=node('form',undefined,'reference-upload'),label=node('label',entity.kind==='role'?'角色参考图':'追加参考图（可多选）'),files=node('input');files.type='file';files.accept='image/*';files.multiple=true;files.required=true;label.append(files);
  let performer=null;
  if(entity.kind==='role'&&entity.actor_ids.length>1){const actorLabel=node('label','照片中的演员');performer=node('select');performer.required=true;options(performer,entities('actor').filter(a=>entity.actor_ids.includes(a.id)));actorLabel.append(performer);form.append(actorLabel);}
  const submit=node('button','追加参考图');submit.type='submit';form.append(label,submit);
  form.onsubmit=event=>{event.preventDefault();run(async()=>{submit.disabled=true;try{const {done,failed}=await appendReferences(performer?performer.value:actorId,files.files,entity.kind==='role'?entity.id:null);await refresh();notice(failed.length?`成功 ${done.length} 张，失败 ${failed.length} 张：${failed.join('；')}`:'照片已追加；再次处理可复用已有视频特征。',failed.length>0);}finally{submit.disabled=false;}});};
  return form;
}
function roleCard(role){
  const card=node('article',undefined,'person-card role-card');
  card.append(node('h4',role.name),node('p','作品：'+role.work_ids.map(name).join('、')),node('p','演员：'+role.actor_ids.map(name).join('、')));
  const refs=state.references.filter(r=>r.role_id===role.id),photos=node('div',undefined,'person-photos');
  refs.forEach((ref,i)=>photos.append(referenceCard(role,ref,i===0?deleteEntityButton(role):null)));
  card.append(refs.length?photos:node('p','暂无参考图','muted'));
  if(role.actor_ids.length)card.append(uploadForm(role,role.actor_ids[0]));
  else card.append(node('p','请先在「修改」中指定出演演员','muted'));
  card.append(button('修改',async()=>editEntity(role)));
  if(!refs.length)card.append(deleteEntityButton(role));
  card.append(linkedShotsButton(role));
  return card;
}
// 参考图随身份删除；有未排除的关联分镜时，直接列出具体阻碍项供核对。
function deleteEntityButton(entity){
  const b=node('button','删除','danger');b.type='button';
  b.onclick=event=>{event.stopPropagation();return run(async()=>{
    b.disabled=true;
    try{
      await api('/api/persons/entities/'+entity.id,{method:'DELETE'});
      await refresh();notice(`${entity.name} 及其参考图已删除。`);
    }catch(error){
      if(error.detail?.linked_shots){await openLinkedShots(entity,error.detail.linked_shots);notice(error.message,true);}
      else throw error;
    }finally{b.disabled=false;}
  });};
  return b;
}
function linkedShotsButton(entity){return button('关联分镜',()=>openLinkedShots(entity));}
async function openLinkedShots(entity,data=null){
  if(!data)data=await api(`/api/persons/entities/${entity.id}/linked-shots?offset=${state.linkedEntity?.id===entity.id?state.linkedOffset:0}&limit=50`);
  state.linkedEntity=entity;state.linkedOffset=data.offset;state.linkedTotal=data.total;
  $('linked-title').textContent=`${entity.name} 的关联分镜`;
  $('linked-summary').textContent=data.total
    ?`共 ${data.total} 个分镜仍有该身份的已接受或待确认证据，需先核对才能删除。已接受分镜仅检查确认身份；待确认分镜检查候选身份，不受最高分演员-角色分类限制。`
    :'没有未排除的关联分镜，可以删除该身份；参考图会随之移除。';
  const list=$('linked-list');list.replaceChildren();
  for(const shot of data.items){
    const row=node('article',undefined,'person-card');
    row.append(node('p',`${shot.source_name} · ${shot.start.toFixed(3)}–${shot.end.toFixed(3)} 秒`),
      node('p',`分镜 ${shot.shot_id} · 该身份${shot.accepted?'已接受':'待确认'}`),
      button('核对这个分镜',async()=>{
        const reviewShot=await api(`/api/persons/review/shots/${shot.shot_id}?source_id=${encodeURIComponent(shot.source_id)}`);
        $('linked-dialog').close();openReview(reviewShot,entity);
      }));
    list.append(row);
  }
  $('linked-page').textContent=`${Math.floor(data.offset/50)+1} / ${Math.max(1,Math.ceil(data.total/50))}`;
  $('linked-prev').disabled=!data.offset;$('linked-next').disabled=data.offset+50>=data.total;
  if(!$('linked-dialog').open)$('linked-dialog').showModal();
}
$('linked-close').onclick=()=>$('linked-dialog').close();
$('linked-prev').onclick=()=>{state.linkedOffset=Math.max(0,state.linkedOffset-50);return run(()=>openLinkedShots(state.linkedEntity));};
$('linked-next').onclick=()=>{state.linkedOffset+=50;return run(()=>openLinkedShots(state.linkedEntity));};
function actorCard(actor){
  const roles=rolesOf(actor),open=state.expanded.has(actor.id);
  const card=node('article',undefined,'person-card actor-card'+(open?' expanded':''));
  // 整卡可点击展开；按钮/表单/输入等交互子元素放行。
  card.setAttribute('role','button');card.tabIndex=0;card.setAttribute('aria-expanded',String(open));
  // 手风琴：展开当前演员时收起其他演员的展开行；再点已展开的则全部收起。
  const toggle=()=>{open?state.expanded.clear():state.expanded=new Set([actor.id]);renderCatalogue();};
  card.onclick=e=>{if(e.target.closest('button,input,select,textarea,label,a,form'))return;toggle();};
  card.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}};
  const title=node('h3',undefined,'actor-name');title.append(node('span',actor.name));
  if(actor.aliases.length){const foreign=node('span',actor.aliases.join(' / '),'foreign-name');title.append(foreign);}
  const refs=state.references.filter(r=>!r.role_id&&r.actor_id===actor.id),photos=node('div',undefined,'person-photos');
  refs.forEach((ref,i)=>photos.append(referenceCard(actor,ref,i===0?deleteEntityButton(actor):null)));
  card.append(title,node('p',`${roles.length} 个角色`,'muted role-count'),refs.length?photos:node('p','暂无参考图','muted'));
  if(!refs.length)card.append(button('修改',async()=>editEntity(actor)),deleteEntityButton(actor));
  card.append(linkedShotsButton(actor),uploadForm(actor,actor.id));
  return card;
}
// 展开行：占据网格整行，列出该演员的全部角色（宽卡片）。
function roleRow(actor){
  const roles=rolesOf(actor);
  const row=node('div',undefined,'actor-detail-row');
  const head=node('div',undefined,'actor-detail-head');
  head.append(node('h3',`${actor.name} 的 ${roles.length} 个角色`),button('收起 ▴',()=>{state.expanded.delete(actor.id);renderCatalogue();}));
  const grid=node('div',undefined,'role-grid-wide');
  for(const role of roles)grid.append(roleCard(role));
  row.append(head,grid);
  return row;
}
function renderCatalogue(){
  readFilters();
  const list=$('entity-list');list.replaceChildren();
  const actors=entities('actor').filter(actorVisible);
  const cards=actors.map(actor=>({actor,el:actorCard(actor)}));
  for(const c of cards)list.append(c.el);
  // 展开行插在“点击演员所在视觉行”的末尾：同排其他演员保持原位不下移。
  // 先让网格完成布局，再按 offsetTop 归组视觉行。
  const rows=[];
  for(const c of cards){
    const current=rows[rows.length-1];
    if(current&&current[0].el.offsetTop===c.el.offsetTop)current.push(c);
    else rows.push([c]);
  }
  for(const row of rows){
    const details=row.filter(c=>state.expanded.has(c.actor.id)).map(c=>roleRow(c.actor));
    if(!details.length)continue;
    const after=row[row.length-1].el.nextElementSibling;
    for(const detail of details)list.insertBefore(detail,after);
  }
  const unassigned=entities('role').filter(r=>!r.actor_ids.length&&matchRole(r));
  if(unassigned.length){
    const group=node('div',undefined,'actor-detail-row unassigned-row');
    group.append(node('h3','未关联演员的角色'));
    const grid=node('div',undefined,'role-grid-wide');
    for(const role of unassigned)grid.append(roleCard(role));
    group.append(grid);list.append(group);
  }
  if(!actors.length&&!unassigned.length)list.append(node('p','没有符合筛选条件的人物','empty'));
  $('filter-summary').textContent=`显示 ${actors.length} / ${entities('actor').length} 名演员${state.expanded.size?` · ${state.expanded.size} 名已展开`:''}`;
}
// 删除已发布的检索组合：与「修改」同款，只用红色区分。
function deleteReleaseButton(release){
  const b=node('button','删除','danger');b.type='button';
  b.onclick=()=>run(async()=>{await api(`/api/persons/releases/${release.id}`,{method:'DELETE'});await refresh();notice('发布组合已删除；人物身份与证据保留。');});
  return b;
}
function referenceErrorLabel(error){
  const reference=state.references.find(r=>r.id===error.reference_id);
  const hasIdentity=error.actor_name!==undefined||error.actor_id!==undefined;
  const actorId=error.actor_id||reference?.actor_id,roleId=hasIdentity?error.role_id:reference?.role_id;
  const actor=error.actor_name||(actorId?name(actorId):null);
  const role=error.role_name||(roleId?name(roleId):'未指定角色');
  const referenceLabel=`参考图 ${error.reference_id.slice(0,8)}`;
  return actor?`${actor} · ${role}（${referenceLabel}）`:`${referenceLabel}（原身份信息未记录）`;
}
function renderTasks(data) {
  $('tasks').replaceChildren();
  const tasks = data.tasks || [];
  // 任务日志只展示最近 3 条；更早的记录仍保留在库里，可在需要时清理。
  for (const task of tasks.slice(0, 3)) {
    const row = node('div', undefined, 'person-card');
    const sourceName = state.sources.find(s => s.id === task.source_id)?.name || task.source_id;
    row.append(node('p', `${sourceName} ${task.id.slice(0, 8)} · ${task.status} · ${task.progress.completed_shots || 0}/${task.progress.total_shots || '?'} 个分镜`));
    if (task.error) row.append(node('p', task.error));
    if (task.result) {
      // 历史日志只读：数字一律取该次任务自己记录的结果，重跑或人工核对不会改写它。
      const result = task.result;
      const parts = [`耗时 ${result.elapsed_seconds}s`];
      if (result.accepted_shots !== undefined) parts.push(`本次已确认 ${result.accepted_shots} 个分镜`);
      if (result.candidate_shots !== undefined) parts.push(`待核对 ${result.candidate_shots} 个分镜`);
      parts.push(`复用 ${result.reused_shots} 分镜`);
      row.append(node('p', parts.join(' · ')));
    }
    if (task.result?.reference_errors?.length) {
      row.append(node('p', task.result.reference_errors.map(e => `${referenceErrorLabel(e)}：${e.error}`).join('；')));
    }
    if (['failed', 'interrupted'].includes(task.status)) {
      row.append(button('重试', async () => {
        await api(`/api/persons/tasks/${task.id}/retry`, {method: 'POST'});
        await refresh();
      }), button('删除失败任务', async () => {
        await api(`/api/persons/tasks/${task.id}`, {method: 'DELETE'});
        await refresh();
        notice('任务记录已删除；分镜检查点和人物证据保留。');
      }));
    }
    $('tasks').append(row);
  }
  if (tasks.length > 3) $('tasks').append(node('p', `另有 ${tasks.length - 3} 条较早的处理记录未显示。`, 'muted'));
}
async function refresh(){const data=await api('/api/persons');Object.assign(state,{entities:data.entities,references:data.references,sources:data.sources});fillSelectors();renderCatalogue();loadImportGallery();
$('source-works').replaceChildren();for(const s of state.sources){const row=node('div',undefined,'source-work');const work=node('select');work.setAttribute('aria-label',s.name+' 所属作品');options(work,entities('work'),false);work.value=s.work_id;const coverage=data.coverage.filter(c=>c.source_id===s.id);const intervals=coverage.map(c=>`${c.data.start.toFixed(1)}–${c.data.end.toFixed(1)}`).slice(0,6);row.append(node('strong',s.name),work,button('关联作品',async()=>{await api(`/api/persons/sources/${s.id}/work`,json('PUT',{work_id:work.value}));await refresh();notice('作品关系已变更，请重新配对并发布人物证据。');}),node('span',coverage.length?`已处理 ${coverage.length} 个分镜区间：${intervals.join('、')}${coverage.length>6?'…':''}`:'人物覆盖缺失','coverage'));$('source-works').append(row);}
renderTasks(data);
$('releases').replaceChildren();for(const r of data.releases){const row=node('p',`${r.created_at} · 内容 ${r.content_id.slice(0,8)} · 人物 ${r.person_id?.slice(0,8)||'未发布'} `);row.append(button('恢复此组合',async()=>{await api(`/api/persons/releases/${r.id}/activate`,{method:'POST'});notice('已恢复检索组合。');}),deleteReleaseButton(r));$('releases').append(row);}entityFields();}
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
$('process-form').onsubmit=e=>{e.preventDefault();run(async()=>{if(!$('process-source').value)throw Error('请先选择素材。');const task=await api(`/api/persons/sources/${$('process-source').value}/process`,json('POST',{start:Number($('process-start').value),end:Number($('process-end').value||0),publish:false}));notice('已启动人物任务：'+task.id);await refresh();});};
$('publish').onclick=()=>run(async()=>{const r=await api('/api/persons/publish',json('POST',{}));await refresh();notice('已发布人物版本：'+r.person_version);});
// 演员表导入：多图草稿，全部原图纵向排列，条目统一列在图下方（每行两个）。
function newEntry(){return{image_id:state.draft.images[0].id,actor_name:'',foreign_name:'',role_name:'',actor_id:null,role_id:null,bbox:null};}
const importCanvases=new Map();
async function displayDraft(draft){
  state.draft=draft;$('import-id').value=draft.id;history.replaceState(null,'','/persons?draft='+draft.id);
  $('import-tools').hidden=false;
  if(draft.work_id)$('import-work').value=draft.work_id;
  $('import-warnings').replaceChildren(...(draft.warnings||[]).map(w=>node('p',w)));
  state.entry=0;
  const legacy=['stage_name','stage_id','age','styling','costume','single_person'];
  for(const e of draft.entries)for(const k of legacy)delete e[k];
  // 按钮始终可操作；已确认草稿的操作由服务端拒绝并给出中文提示。
  for(const id of ['import-parse','import-add','import-save','import-confirm'])$(id).disabled=false;
  await renderOriginals();renderEntries();
}
async function renderOriginals(){
  const host=$('import-originals');host.replaceChildren();importCanvases.clear();
  for(const [i,im] of state.draft.images.entries()){
    const block=node('div',undefined,'original-block');
    const head=node('div',undefined,'original-head');
    const st=(state.draft.parse_state||{})[im.id];
    const badge=node('span',st?.status==='parsed'?'已拆分':st?.status==='failed'?'拆分失败':'未拆分',`parse-badge ${st?.status||'pending'}`);
    const retry=button('重新拆分',()=>parseImages([im.id]));
    head.append(node('strong',`原图 ${i+1} · ${im.width}×${im.height}`),badge,retry);
    if(st?.error)head.append(node('span',st.error,'muted'));
    const canvas=node('canvas');canvas.dataset.image=im.id;canvas.tabIndex=0;canvas.setAttribute('aria-label',`原图 ${i+1} 照片框选区域`);
    block.append(head,canvas);host.append(block);
    const image=new Image();image.src=im.url;await image.decode();
    canvas.width=im.width;canvas.height=im.height;
    importCanvases.set(im.id,{canvas,image});
    bindCanvas(canvas,im.id);
  }
  draw();
}
function bindCanvas(canvas,imageId){
  const point=e=>{const r=canvas.getBoundingClientRect();return[Math.max(0,Math.min(canvas.width,(e.clientX-r.left)*canvas.width/r.width)),Math.max(0,Math.min(canvas.height,(e.clientY-r.top)*canvas.height/r.height))];};
  let drag=null;
  canvas.onpointerdown=e=>{if(!state.draft.entries[state.entry])return;drag=point(e);canvas.setPointerCapture(e.pointerId);};
  canvas.onpointermove=e=>{if(!drag)return;const [x,y]=point(e),entry=state.draft.entries[state.entry];entry.image_id=imageId;entry.bbox=[Math.min(x,drag[0]),Math.min(y,drag[1]),Math.max(x,drag[0]),Math.max(y,drag[1])].map(Math.round);draw();};
  canvas.onpointerup=()=>{if(!drag)return;drag=null;renderEntries();};
  canvas.onpointercancel=()=>drag=null;
}
function draw(){
  if(!state.draft)return;
  for(const [imageId,{canvas,image}] of importCanvases){
    const ctx=canvas.getContext('2d');
    ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0);
    for(const [index,entry] of state.draft.entries.entries()){
      if(entry.image_id!==imageId||!entry.bbox)continue;
      const [x,y,x2,y2]=entry.bbox;
      ctx.strokeStyle=index===state.entry?'#ff5035':'#139f80';ctx.lineWidth=Math.max(2,canvas.width/350);ctx.strokeRect(x,y,x2-x,y2-y);
      const preview=document.querySelector(`canvas[data-entry="${index}"]`);
      if(preview&&x2>x&&y2>y){preview.width=x2-x;preview.height=y2-y;preview.getContext('2d').drawImage(image,x,y,x2-x,y2-y,0,0,preview.width,preview.height);}
    }
  }
}
async function parseImages(ids){
  await displayDraft(await api('/api/persons/imports/'+state.draft.id+'/parse',json('POST',{image_ids:ids})));
}
$('import-form').onsubmit=e=>{e.preventDefault();run(async()=>{
  const form=new FormData();for(const file of $('import-files').files)form.append('files',file);
  const target=state.draft&&state.draft.status==='draft'?state.draft.id:null;
  const draft=await api(target?`/api/persons/imports/${target}/images`:'/api/persons/imports',{method:'POST',body:form});
  await displayDraft(draft);
  notice(target?'截图已追加到当前草稿。':'新草稿已创建，可继续上传追加截图。');
  loadImportGallery();
});};
$('import-load').onclick=()=>run(async()=>displayDraft(await api('/api/persons/imports/'+$('import-id').value.trim())));
$('import-parse').onclick=()=>run(async()=>{
  const st=state.draft.parse_state||{};
  let todo=state.draft.images.filter(im=>st[im.id]?.status!=='parsed');
  if(!todo.length)todo=state.draft.images;
  $('import-parse').disabled=true;
  try{
    for(const [i,im] of todo.entries()){
      notice(`正在拆分 ${i+1}/${todo.length} 张…`);
      await parseImages([im.id]);
    }
  }finally{$('import-parse').disabled=false;}
  const failed=Object.values(state.draft.parse_state||{}).filter(s=>s.status==='failed').length;
  notice(failed?`${failed} 张拆分失败，可单张重试或手工录入。`:'拆分完成，请逐条核对后确认。');
});
$('import-add').onclick=()=>{state.draft.entries.push(newEntry());state.entry=state.draft.entries.length-1;renderEntries();draw();};
// 演员表导入是人物的主要创建入口：填写新作品名即创建，同名身份自动复用。
async function importWorkId(){const name=$('import-new-work').value.trim();if(!name)return $('import-work').value;const created=await api('/api/persons/entities',json('POST',{kind:'work',name}));await refresh();$('import-work').value=created.id;$('import-new-work').value='';return created.id;}
async function draftBody(){return{work_id:await importWorkId(),entries:state.draft.entries};}
// 提交前在客户端校验，避免 422 英文校验错误；服务端仍逐条复核。
function validateDraft(){if(!$('import-work').value&&!$('import-new-work').value.trim())throw Error('请选择导入作品，或填写新作品名。');state.draft.entries.forEach((e,i)=>{const n=`条目 ${i+1}`;if(!(e.actor_name||'').trim()&&!e.actor_id)throw Error(`${n}：请填写演员名，或关联已有演员。`);});}
$('import-save').onclick=()=>run(async()=>{validateDraft();await api('/api/persons/imports/'+state.draft.id,json('PUT',await draftBody()));notice('草稿已保存。');});
$('import-confirm').onclick=()=>run(async()=>{
  validateDraft();
  const result=await api('/api/persons/imports/'+state.draft.id+'/confirm',json('POST',await draftBody()));
  await displayDraft(await api('/api/persons/imports/'+state.draft.id));
  await refresh();
  const parts=[`已导入 ${result.confirmed.length} 条`];
  if(result.skipped_duplicates?.length){
    const names=[...new Set(result.skipped_duplicates.map(d=>`${d.actor_name}/${d.role_name||'无角色'}`))].join('、');
    parts.push(`跳过 ${result.skipped_duplicates.length} 条重复（${names}）`);
  }
  if(result.ignored_existing)parts.push(`${result.ignored_existing} 条参考图已存在，未重复导入`);
  notice(parts.join(' · '));
});
function renderEntries(){
  const target=$('import-entries');target.replaceChildren();
  if(!state.draft.entries.length){target.append(node('p','尚无条目：上传截图后点「自动拆分演员表」，或手动增加条目。','muted'));return;}
  state.draft.entries.forEach((entry,index)=>{
    const card=node('article',undefined,`entry ${index===state.entry?'selected':''}`);
    card.append(button(`条目 ${index+1}`,()=>{state.entry=index;renderEntries();draw();importCanvases.get(entry.image_id)?.canvas.closest('.original-block')?.scrollIntoView({behavior:'smooth',block:'center'});}));
    const imLabel=node('label','所属原图'),imSelect=node('select');
    options(imSelect,state.draft.images.map((im,i)=>({id:im.id,name:`原图 ${i+1}`})),false);
    imSelect.value=entry.image_id;imSelect.onchange=()=>{entry.image_id=imSelect.value;draw();};
    imLabel.append(imSelect);card.append(imLabel);
    for(const [key,label] of [['actor_name','演员名'],['foreign_name','外文名'],['role_name','角色名（不同时期直接写全名，如老年叶淑柔）']]){const l=node('label',label),input=node('input');input.value=entry[key]||'';input.oninput=()=>entry[key]=input.value;l.append(input);card.append(l);}
    for(const [key,kind,label] of [['actor_id','actor','关联已有演员（默认按姓名复用）'],['role_id','role','合并已有角色（跨电影同一角色）']]){const l=node('label',label),select=node('select');options(select,entities(kind));select.value=entry[key]||'';select.onchange=()=>entry[key]=select.value||null;l.append(select);card.append(l);}
    const coords=node('div',undefined,'coordinates');
    for(let n=0;n<4;n++){const l=node('label',['x1','y1','x2','y2'][n]),input=node('input');input.type='number';input.min='0';input.value=entry.bbox?.[n]??'';input.onchange=()=>{entry.bbox=entry.bbox||[0,0,0,0];entry.bbox[n]=Number(input.value);draw();};l.append(input);coords.append(l);}
    card.append(coords);
    const preview=node('canvas');preview.className='crop-preview';preview.dataset.entry=index;card.append(preview);
    card.append(button('删除条目',()=>{state.draft.entries.splice(index,1);state.entry=Math.min(state.entry,state.draft.entries.length-1);renderEntries();draw();}));
    target.append(card);
  });
  draw();
}
// 历史演员表图片：默认列出全部草稿与已确认导入，点击加载对应草稿，✕ 删除单张。
async function loadImportGallery(){
  const items=await api('/api/persons/imports');
  const host=$('import-gallery');host.replaceChildren();
  if(!Array.isArray(items)||!items.length){host.append(node('p','还没有演员表图片。','muted'));return;}
  for(const item of items)for(const im of item.images){
    const wrap=node('div',undefined,'gallery-item'+(state.draft?.id===item.id?' current':''));
    const cell=node('button',undefined,'gallery-cell');cell.type='button';
    cell.title=`草稿 ${item.id.slice(0,8)} · ${item.status==='confirmed'?'已导入':'草稿'} · ${item.entry_count} 条`;
    const img=node('img');img.src=im.url;img.alt=cell.title;img.loading='lazy';
    cell.append(img,node('span',item.status==='confirmed'?'已导入':'草稿','gallery-tag'));
    cell.onclick=()=>run(async()=>displayDraft(await api('/api/persons/imports/'+item.id)));
    const del=node('button','✕','gallery-delete');del.type='button';del.title='删除此历史图片';
    del.onclick=()=>run(async()=>{
      await api(`/api/persons/imports/${item.id}/images/${im.id}`,{method:'DELETE'});
      notice('历史图片已删除；已导入的人物与参考图不受影响。');
      if(state.draft?.id===item.id){
        // 当前打开的草稿可能整行被删空：尝试重载，失败则收起工作区。
        try{await displayDraft(await api('/api/persons/imports/'+item.id));}
        catch{$('import-tools').hidden=true;state.draft=null;}
      }
      await loadImportGallery();
    });
    wrap.append(cell,del);host.append(wrap);
  }
}
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
function reviewParams(offset=state.evidenceOffset,limit=reviewPageSize){
  const params=new URLSearchParams({source_id:$('review-source').value,status:$('review-status').value,offset,limit});
  const pair=$('review-pair').value;
  if($('review-status').value==='rejected')return params;
  if(pair==='__none__')params.set('no_candidate','true');
  else if(pair){const [actor,role]=JSON.parse(pair);params.set('actor_id',actor);if(role)params.set('role_id',role);}
  return params;
}
function syncReviewStatus(){
  const rejected=$('review-status').value==='rejected';
  $('review-pair-field').hidden=rejected;$('review-pair').disabled=rejected;
  if(rejected)$('review-pair').value='';
  $('review-select-all').textContent=rejected?'全选已排除（含所有页）':'全选当前筛选（含所有页）';
  $('review-filter-hint').textContent=rejected
    ?'已排除的分镜不按演员-角色分类，可重新核对。全选包含当前素材下已排除列表的所有页。'
    :$('review-status').value==='accepted'
      ?'按已确认的演员-角色筛选；多人出镜的分镜会分别出现在各确认人物的筛选结果中。全选包含当前筛选的所有页。'
      :'待确认分镜按所有证据中分数最高的演员-角色候选归类；没有身份候选的分镜归入“无候选”。全选包含当前筛选的所有页。';
  return rejected;
}
function confirmedPersonDetails(person){
  const value=person.manual?person.original_score:person.score;
  const score=typeof value==='number'&&Number.isFinite(value)?value.toFixed(3):'未记录';
  return person.manual?`原始分数 ${score}（人工确认）`:`分数 ${score}（自动确认）`;
}
function confirmedPersonLabel(person){return `${reviewPairLabel(person.actor_id,person.role_id)} · ${confirmedPersonDetails(person)}`;}
function reviewPairLabel(actor,role){return actor?`${name(actor)} · ${role?name(role):'未指定角色'}`:'无候选';}
function fillReviewFilters(filters,scopeTotal){
  const select=$('review-pair'),keep=select.value;
  const pairs=filters.filter(f=>f.actor_id).map(f=>({...f,key:pairKey(f.actor_id,f.role_id),label:reviewPairLabel(f.actor_id,f.role_id)}));
  if(keep&&keep!=='__none__'&&!pairs.some(p=>p.key===keep)){
    const [actor_id,role_id]=JSON.parse(keep);pairs.push({key:keep,label:reviewPairLabel(actor_id,role_id),count:0});
  }
  pairs.sort((a,b)=>a.label.localeCompare(b.label,'zh')||a.key.localeCompare(b.key));
  select.replaceChildren();
  const all=node('option',`全部（${scopeTotal}）`);all.value='';select.append(all);
  for(const p of pairs){const option=node('option',`${p.label}（${p.count}）`);option.value=p.key;select.append(option);}
  const none=node('option',`无候选（${filters.find(f=>!f.actor_id)?.count||0}）`);none.value='__none__';select.append(none);
  select.value=keep;
}
function shotKey(shot){return shot.source_id+'|'+shot.shot_id;}
async function loadEvidence(){
  const rejected=syncReviewStatus();
  const request=++state.evidenceRequest;
  $('review-select-all').disabled=true;
  const data=await api('/api/persons/review/shots?'+reviewParams());
  if(request!==state.evidenceRequest)return;
  if(!data.items.length&&state.evidenceOffset>0){state.evidenceOffset=Math.max(0,Math.ceil(data.total/reviewPageSize)-1)*reviewPageSize;return loadEvidence();}
  state.evidenceTotal=data.total;
  fillReviewFilters(data.filters,data.scope_total);
  $('evidence-list').replaceChildren();
  for(const shot of data.items){
    const card=node('article',undefined,'person-card');
    const pick=node('input');pick.type='checkbox';pick.className='shot-pick';pick.title='加入批量核对';
    pick.setAttribute('aria-label',`选择 ${shot.start.toFixed(2)}–${shot.end.toFixed(2)} 秒分镜`);
    pick.checked=state.reviewPicked.has(shotKey(shot));pick.dataset.shotKey=shotKey(shot);
    pick.onchange=()=>{pick.checked?state.reviewPicked.set(shotKey(shot),shot):state.reviewPicked.delete(shotKey(shot));updateBatchButton();};
    card.append(pick);
    if(shot.image_url){const image=node('img');image.src=shot.image_url;image.alt='分镜证据截图';image.loading='lazy';card.append(image);}
    const accepted=(shot.confirmed_people||[]).map(confirmedPersonLabel);
    const acceptedList=node('ul',undefined,'confirmed-people');
    for(const label of accepted)acceptedList.append(node('li',label));
    const candidate=shot.candidate;
    const category=candidate?`分类：${reviewPairLabel(candidate.actor_id,candidate.role_id)} · 分数 ${candidate.score.toFixed(3)}`:'分类：无候选';
    card.append(...[node('h3',`${shot.start.toFixed(2)}–${shot.end.toFixed(2)} 秒`),node('p',state.sources.find(s=>s.id===shot.source_id)?.name||shot.source_id),rejected||shot.counts.accepted?null:node('p',category,'muted'),node('p',`已接受 ${shot.counts.accepted} 帧 · 待确认 ${shot.counts.candidate} 帧 · 已排除 ${shot.counts.rejected} 帧`),accepted.length?acceptedList:node('p',rejected?'已确认无出镜人物':'尚未确认出镜人物'),shot.counts.candidate?node('p',`本分镜仍有 ${shot.counts.candidate} 帧未判定；核对整个分镜会一次性结清整段。`,'muted'):null,button('核对整个分镜',async()=>openReview(shot))].filter(Boolean));
    $('evidence-list').append(card);
  }
  if(!data.items.length)$('evidence-list').append(node('p','当前筛选下没有分镜。','empty'));
  $('evidence-page').textContent=`${Math.floor(state.evidenceOffset/reviewPageSize)+1} / ${Math.max(1,Math.ceil(data.total/reviewPageSize))} · 共 ${data.total} 个分镜`;
  $('evidence-prev').disabled=state.evidenceOffset===0;
  $('evidence-next').disabled=state.evidenceOffset+reviewPageSize>=data.total;
  updateBatchButton();
}
function updateBatchButton(){
  const n=state.reviewPicked.size;
  $('review-batch').textContent=`批量核对（${n}）`;$('review-batch').disabled=!n||state.reviewSelecting;
  $('review-clear').disabled=!n&&!state.reviewSelecting;
  $('review-select-all').disabled=!state.evidenceTotal||state.reviewSelecting;
  for(const pick of $('evidence-list').querySelectorAll('.shot-pick'))pick.checked=state.reviewPicked.has(pick.dataset.shotKey);
}
function resetEvidence(resetPair=false){
  ++state.reviewVersion;++state.evidenceRequest;
  state.reviewSelecting=false;state.evidenceOffset=0;state.evidenceTotal=0;state.reviewPicked.clear();
  if(resetPair)$('review-pair').value='';
  $('evidence-list').replaceChildren();updateBatchButton();
  return run(loadEvidence);
}
async function selectFilteredShots(){
  const version=state.reviewVersion,params=reviewParams(0,200),picked=new Map();
  state.reviewSelecting=true;updateBatchButton();
  try{
    params.set('compact','true');
    for(let offset=0;;){
      params.set('offset',offset);
      const data=await api('/api/persons/review/shots?'+params);
      if(version!==state.reviewVersion)return;
      for(const shot of data.items)picked.set(shotKey(shot),shot);
      offset+=data.items.length;
      if(offset>=data.total)break;
      if(!data.items.length)throw Error('分镜列表已变化，请重新读取证据后全选。');
    }
    state.reviewPicked=picked;
    notice(`已选择当前筛选下的全部 ${picked.size} 个分镜（含所有页）。`);
  }finally{if(version===state.reviewVersion){state.reviewSelecting=false;updateBatchButton();}}
}
$('review-batch').onclick=()=>{const shots=[...state.reviewPicked.values()];if(shots.length)openBatchReview(shots);};
$('review-select-all').onclick=()=>run(selectFilteredShots);
$('review-clear').onclick=()=>{++state.reviewVersion;state.reviewSelecting=false;state.reviewPicked.clear();updateBatchButton();};
$('review-refresh').onclick=()=>resetEvidence();
$('review-source').onchange=$('review-status').onchange=()=>resetEvidence(true);
$('review-pair').onchange=()=>resetEvidence();
$('evidence-prev').onclick=()=>{state.evidenceOffset=Math.max(0,state.evidenceOffset-reviewPageSize);return run(loadEvidence);};
$('evidence-next').onclick=()=>{state.evidenceOffset+=reviewPageSize;return run(loadEvidence);};
// 勾选单元是「角色 · 演员」身份：角色在前；未参加任何角色的演员单列演员名。
// 批量模式对全部所选分镜取并集构建选项；预勾选只取所有分镜都已接受的身份交集。
function fillReviewChoices(shots){
  const works=new Set(shots.map(s=>state.sources.find(x=>x.id===s.source_id)?.work_id));
  const roleList=entities('role').filter(r=>(r.work_ids||[]).some(w=>works.has(w)));
  const suggested=new Set(shots.flatMap(s=>s.evidence.filter(e=>e.actor_id).map(e=>e.actor_id)));
  const pairs=[];
  for(const role of roleList)for(const actorId of role.actor_ids)pairs.push({actor_id:actorId,role_id:role.id,label:`${role.name} · ${name(actorId)}`});
  const cast=new Set(pairs.map(p=>p.actor_id));
  for(const actor of entities('actor'))if(!cast.has(actor.id))pairs.push({actor_id:actor.id,role_id:null,label:actor.name});
  pairs.sort((a,b)=>Number(suggested.has(b.actor_id))-Number(suggested.has(a.actor_id))||a.label.localeCompare(b.label,'zh'));
  const marks=shots.map(s=>new Set(s.evidence.filter(e=>e.status==='accepted').map(e=>pairKey(e.actor_id,e.role_id))));
  const choices=$('review-choices');choices.replaceChildren();
  for(const pair of pairs){
    const check=node('input');check.type='checkbox';check.value=pairKey(pair.actor_id,pair.role_id);
    // 已接受证据精确匹配；自动接受的行常无角色，回退到该演员在本作品的唯一身份。
    check.checked=marks.every(m=>m.has(check.value)||(m.has(pairKey(pair.actor_id,null))&&pairs.filter(p=>p.actor_id===pair.actor_id).length===1));
    const confirmed=shots.length===1?shots[0].confirmed_people?.find(p=>p.actor_id===pair.actor_id&&p.role_id===pair.role_id):null;
    const label=node('label',pair.label+(confirmed?' · '+confirmedPersonDetails(confirmed):''));label.prepend(check);choices.append(label);
  }
}
function openReview(shot,linkedEntity=null){
  state.linkedReviewEntity=linkedEntity;state.reviewShots=[shot];
  $('review-image').hidden=!shot.image_url;
  if(shot.image_url)$('review-image').src=shot.image_url;
  $('review-description').textContent=`原片 ${shot.start.toFixed(3)}–${shot.end.toFixed(3)} 秒。勾选的人物将按整个分镜确认出镜；不勾选任何人物即排除整个分镜。`;
  fillReviewChoices([shot]);
  const video=$('review-video');video.hidden=false;video.src=`/api/admin/segments/${shot.shot_id}/media`;video.onloadedmetadata=()=>video.currentTime=0;
  $('review-dialog').showModal();
}
function openBatchReview(shots){
  state.linkedReviewEntity=null;state.reviewShots=shots;
  $('review-image').hidden=true;$('review-image').removeAttribute('src');
  const video=$('review-video');video.pause();video.removeAttribute('src');video.hidden=true;
  $('review-description').textContent=`已选择 ${shots.length} 个分镜。勾选的人物将按整个分镜统一确认出镜；不勾选任何人物即排除这些分镜的全部人物证据。`;
  fillReviewChoices(shots);
  $('review-dialog').showModal();
}
async function review(accept){
  const selections=accept?Array.from($('review-choices').querySelectorAll('input:checked')).map(input=>{const [actor_id,role_id]=JSON.parse(input.value);return{actor_id,role_id};}):[];
  const shots=state.reviewShots||[];
  for(const shot of shots){
    await api('/api/persons/review/shot',json('POST',{source_id:shot.source_id,shot_id:shot.shot_id,selections}));
    state.reviewPicked.delete(shotKey(shot));
  }
  updateBatchButton();
  $('review-dialog').close();
  if(state.linkedReviewEntity){const entity=state.linkedReviewEntity;state.linkedReviewEntity=null;state.linkedOffset=0;await openLinkedShots(entity);}
  else await loadEvidence();
  notice(shots.length>1?`已批量核对 ${shots.length} 个分镜；发布新人物版本后检索生效。`:'分镜审核已保存；发布新人物版本后检索生效。');
}
$('review-form').onsubmit=e=>{e.preventDefault();run(()=>review(true));};
$('review-reject').onclick=()=>run(()=>review(false));
$('review-close').onclick=()=>{state.linkedReviewEntity=null;$('review-dialog').close();};
$('review-dialog').onclose=()=>$('review-video').pause();
run(async()=>{
  await refresh();
  const params=new URLSearchParams(location.search),draft=params.get('draft');
  if(draft){document.querySelector('[data-pane="cast"]').click();await displayDraft(await api('/api/persons/imports/'+draft));}
  const ev=params.get('evidence');
  if(ev){
    document.querySelector('[data-pane="processing"]').click();
    const evidence=await api('/api/persons/evidence/'+ev);
    $('review-source').value=evidence.source_id;
    openReview(await api(`/api/persons/review/shots/${evidence.shot_id}?source_id=${encodeURIComponent(evidence.source_id)}`));
  }
});
$('reload').onclick=()=>{ $('reload').disabled=true; run(async()=>{await refresh();}).finally(()=>{$('reload').disabled=false;}); };
setInterval(()=>{if(!$('processing').hidden)run(refresh);},5000);
