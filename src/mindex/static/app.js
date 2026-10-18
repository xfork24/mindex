const $ = id => document.getElementById(id);
let mode = 'text';
let busy = false;
let previewUrl = null;
let imagePreviewUrl = null;
let registeredPeople = [];
const routeNames = {visual:'视频画面', analysis:'LLM 分析', video:'画面匹配', description:'动作与场景', style:'情绪与镜头', dialogue:'对白语义', lexical:'关键词',person:'人物出镜'};
const personKinds = {actor:'演员', role:'角色'};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(text='', type='') { $('notice').textContent=text; $('notice').className=`notice ${type}`; }
function clock(seconds) {
  const minutes = Math.floor(seconds/60);
  return `${String(minutes).padStart(2,'0')}:${(seconds%60).toFixed(1).padStart(4,'0')}`;
}
function errorText(detail) {
  if (Array.isArray(detail)) return detail.map(item => item.msg || JSON.stringify(item)).join('；');
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
}
async function api(url, options={}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(errorText(data.detail || data));
  return data;
}
function switchMode(next) {
  if (busy) return;
  mode = next;
  for (const name of ['text','image','video']) {
    $(`${name}-tab`).classList.toggle('active',name===mode);
    $(`${name}-tab`).setAttribute('aria-selected',String(name===mode));
    $(`${name}-input`).hidden=name!==mode;
  }
  $('person-controls').hidden=mode!=='text';
  $('person-hint').hidden=mode!=='text';
  if(mode!=='video') $('query-preview').pause();
  notice();
}
$('text-tab').onclick=()=>switchMode('text');
$('image-tab').onclick=()=>switchMode('image');
$('video-tab').onclick=()=>switchMode('video');
document.querySelectorAll('[data-query]').forEach(button=>button.onclick=()=>{
  $('query').value=button.dataset.query; $('query').focus();
});
function showVideo(file){
  if(previewUrl)URL.revokeObjectURL(previewUrl);
  const preview=$('query-preview');
  preview.hidden=!file;
  if(file){
    $('file-label').textContent=`${file.name} · ${(file.size/1024/1024).toFixed(1)}MB`;
    previewUrl=URL.createObjectURL(file);preview.src=previewUrl;
  }else{
    $('file-label').textContent='最长 30 秒 · 最大 100MB';preview.removeAttribute('src');
  }
}
function showImage(file){
  if(imagePreviewUrl)URL.revokeObjectURL(imagePreviewUrl);
  imagePreviewUrl=null;
  const preview=$('image-preview');
  preview.hidden=!file;
  if(file){
    $('image-file-label').textContent=`${file.name} · ${(file.size/1024/1024).toFixed(1)}MB`;
    imagePreviewUrl=URL.createObjectURL(file);preview.src=imagePreviewUrl;
  }else{
    $('image-file-label').textContent='JPEG、PNG、WebP 静态图片 · 最大 20MB';preview.removeAttribute('src');
  }
}
// 拖放的文件不属于 input.files，优先写回 input，让预览与提交共用同一条路径。
const droppedFile={image:null,video:null};
function currentFile(mode){return $(mode==='image'?'image-file':'video-file').files[0]||droppedFile[mode]||null;}
function acceptsType(file,accept){
  return accept.split(',').map(rule=>rule.trim()).filter(Boolean).some(rule=>
    rule.endsWith('/*')?file.type.startsWith(rule.slice(0,-1)):file.type===rule);
}
function bindDrop(panelId,inputId,labelId,kind,show){
  const panel=$(panelId),input=$(inputId),label=$(labelId),accept=input.getAttribute('accept')||'';
  input.onchange=()=>{droppedFile[kind]=null;show(input.files[0]||null);};
  const highlight=()=>label.classList.add('dragging');
  const unhighlight=()=>label.classList.remove('dragging');
  panel.addEventListener('dragover',event=>{event.preventDefault();highlight();});
  panel.addEventListener('dragenter',event=>{event.preventDefault();highlight();});
  // 在子元素之间移动也会冒泡 dragleave，用 relatedTarget 判断是否真的离开了面板。
  panel.addEventListener('dragleave',event=>{if(!panel.contains(event.relatedTarget))unhighlight();});
  panel.addEventListener('drop',event=>{
    event.preventDefault();unhighlight();
    const file=event.dataTransfer?.files?.[0];
    if(!file){notice('没有读到拖入的文件，请重试或点击选择。','error');return;}
    if(!acceptsType(file,accept)){notice(`请拖入${kind==='image'?'图片':'视频'}文件，当前支持：${accept}。`,'error');return;}
    const transfer=new DataTransfer();transfer.items.add(file);
    try{input.files=transfer.files;}catch(e){droppedFile[kind]=file;}
    show(file);
    if(!input.files.length&&!droppedFile[kind])notice('当前浏览器不支持拖放上传，请点击选择文件。','error');
  });
}
bindDrop('image-input','image-file','image-drop','image',showImage);
bindDrop('video-input','video-file','video-drop','video',showVideo);

function filters() {
  const values={source_id:$('source').value,scene:$('scene').value.trim(),emotion:$('emotion').value.trim(),shot_size:$('shot-size').value.trim()};
  for (const [key,id] of [['min_duration','min-duration'],['max_duration','max-duration']]) {
    if ($(id).value !== '') values[key]=Number($(id).value);
  }
  return Object.fromEntries(Object.entries(values).filter(([,v])=>v!==''));
}

function details(result) {
  const target=$('detail-content'); target.replaceChildren();
  target.append(element('h3',result.display_name || result.source_name));
  if(result.display_name) target.append(element('p',result.source_name));
  const video=element('video');video.src=result.media_url;video.controls=true;video.preload='metadata';
  target.append(video,element('p',`${clock(result.start)} — ${clock(result.end)}`),element('p',result.annotation_status==='not_analyzed'?'尚未进行 LLM 分析':result.annotation.description));
  for (const [title,items] of [['动作',result.annotation.actions],['画面依据',result.annotation.visual_evidence],['对白推断',result.annotation.dialogue_inference],['不确定信息',result.annotation.uncertainty]]) {
    if (items.length) target.append(element('p',`${title}：${items.join('；')}`));
  }
  if (result.dialogue.length) target.append(element('pre',result.dialogue.map(u=>`${clock(result.start+u.start)} ${u.text}`).join('\n')));
  target.append(element('p',`排序分数 ${result.score.toFixed(5)} · ${result.matches.map(m=>`${routeNames[m.route]||m.route}第 ${m.rank} 位`).join('；')}`));
  for (const match of result.matches || []) {
    if (match.matched_text) target.append(element('p', `${routeNames[match.channel] || match.channel}：${match.matched_text}`));
    for (const evidence of match.evidence || []) {
      if (evidence.text) target.append(element('p', `${clock(evidence.start)} — ${clock(evidence.end)}：${evidence.text}`));
    }
  }
  for(const evidence of result.person_evidence || []){
    const section=element('section');
    section.append(element('p',`${evidence.actor_name || '未知演员'}${evidence.role_name?' · '+evidence.role_name:''} · ${clock(evidence.start)} — ${clock(evidence.end)} · ${{face:'人脸匹配',track:'连续跟踪',manual:'人工确认'}[evidence.data.evidence_type] || evidence.data.evidence_type}`));
    if(evidence.image_url){const img=element('img');img.src=evidence.image_url;img.alt='人物出镜证据';img.style.maxWidth='300px';img.loading='lazy';section.append(img);}
    const locate=element('button','定位出镜时间');locate.onclick=()=>{video.currentTime=Math.max(0,evidence.start-result.start);};
    const correct=element('a','核对 / 纠错 ↗');correct.href='/persons?evidence='+evidence.id;correct.target='_blank';correct.rel='noopener';
    section.append(locate,correct);target.append(section);
  }
  target.append(managementLink(result));
  $('detail-dialog').showModal();
}
function managementLink(result) {
  const link=element('a','管理此片段 ↗');
  const params=new URLSearchParams({source:result.source_id});
  if(result.kind==='shot'||result.kind==='moment')params.set('segment',result.id);
  link.href=`/admin?${params}`;link.target='_blank';link.rel='noopener';return link;
}
$('close-dialog').onclick=()=>{$('detail-content').querySelector('video')?.pause();$('detail-dialog').close();};
$('detail-dialog').addEventListener('close',()=>{$('detail-content').querySelector('video')?.pause();});

function render(data) {
  $('results').replaceChildren();
  $('result-count').textContent=`${data.results.length} 个片段`;
  $('timing').textContent=`${(data.elapsed_ms/1000).toFixed(2)} 秒 · 按相关性排序`;
  if (!data.results.length) {
    const empty=element('div',undefined,'empty');
    empty.append(element('h3','没有符合筛选条件的片段'),element('p','尝试放宽筛选，或换一种方式描述画面。'));
    $('results').append(empty);return;
  }
  data.results.forEach((result,index)=>{
    const card=element('article',undefined,'card');
    const video=element('video');video.src=result.media_url;video.poster=result.thumbnail_url;video.controls=true;video.preload='none';
    const body=element('div',undefined,'card-body');
    const title=element('div',undefined,'card-title');title.append(element('strong',result.source_name),element('span',`${(result.end-result.start).toFixed(1)}s`));
    const tags=element('div',undefined,'tags');
    const channels = new Set((result.matches || []).map(match => match.channel || (match.route==='video'?'visual':null)).filter(Boolean));
    // 同一个人物会在多个采样帧上留下出镜证据，标签按「演员 / 角色」去重后再展示。
    const personTags=[];
    for(const e of result.person_evidence||[]){
      const name=`${e.actor_name||'未知演员'}${e.role_name?' / '+e.role_name:''}`;
      if(!personTags.includes(name))personTags.push(name);
    }
    for(const name of personTags.slice(0,3))tags.append(element('span',name,'tag'));
    channels.forEach(channel => tags.append(element('span',routeNames[channel] || channel,'tag')));
    [...result.annotation.scene,...result.annotation.emotions,...result.annotation.shot_size].slice(0,5).forEach(tag=>tags.append(element('span',tag,'tag')));
    const actions=element('div',undefined,'card-actions');
    const detail=element('button','查看详情');detail.type='button';detail.onclick=()=>details(result);
    const context=element('a','播放上下文 ↗');context.href=result.context_url;context.target='_blank';context.rel='noopener';
    actions.append(detail,context,managementLink(result),element('span',`#${index+1} · ${result.score.toFixed(4)}`,'score'));
    const evidenceText = (result.matches || []).find(match => match.matched_text)?.matched_text;
    const description = result.display_name || evidenceText || (result.annotation_status==='not_analyzed'?'尚未进行 LLM 分析':result.annotation.description);
    body.append(title,element('p',description,'card-description'),tags,actions);
    card.append(video,body);$('results').append(card);
  });
}
$('search-form').onsubmit=async event=>{
  event.preventDefault();if(busy)return;
  const text=$('query').value.trim();const file=currentFile(mode);
  if (mode==='text'&&!text&&!selectedValues('actor-select').length&&!selectedValues('role-select').length) {notice('请先描述想找的画面。','error');return;}
  if (mode==='video'&&!file) {notice('请选择一段参考视频。','error');return;}
  if (mode==='image'&&!file) {notice('请选择一张参考图片。','error');return;}
  busy=true;$('search-button').disabled=true;$('search-button').textContent='正在查找…';
  notice('正在检索素材，请稍候。','info');
  try {
    let data;
    const common={granularity:$('granularity').value,limit:Number($('limit').value),filters:filters()};
    if(mode==='text') data=await api('/api/search/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,...common,person_ids:[...new Set([...selectedValues('actor-select'),...selectedValues('role-select')])],person_mode:$('person-mode').value,person_relation:$('person-relation').value})});
    else {
      const form=new FormData();form.append('file',file);form.append('granularity',common.granularity);form.append('limit',common.limit);form.append('filters',JSON.stringify(common.filters));
      data=await api(`/api/search/${mode}`,{method:'POST',body:form});
    }
    render(data);
    if(data.recognized_people?.length)$('person-hint').textContent='已识别：'+data.recognized_people.map(p=>`${personKinds[p.kind]||''} ${p.name}`.trim()).join('、')+' · '+(data.person_mode==='mentioned'?'对白提及':'要求出镜证据');
    const warnings=[...(data.warnings||[])];
    if(data.person_coverage_missing?.length)warnings.push(`${data.person_coverage_missing.length} 份素材的人物覆盖缺失；仅返回已发布的出镜证据。`);
    notice(warnings.join('；'), warnings.length?'info':'');
  } catch(error) {notice(error.message,'error');}
  finally {busy=false;$('search-button').disabled=false;$('search-button').textContent='查找片段 ↗';}
};
// 多选框顶部的“全部选择”：选中它等同全选，手动取消任意一项时它同步取消。
const ALL_VALUE='__all__';
function allOption(select){return select.querySelector(`option[value="${ALL_VALUE}"]`);}
function itemOptions(select){return Array.from(select.options).filter(o=>o.value!==ALL_VALUE);}
function syncAll(select){
  const all=allOption(select);if(!all)return;
  const items=itemOptions(select);
  all.selected=items.length>0&&items.every(o=>o.selected);
  select.dataset.allSelected=all.selected?'1':'0';
}
function applyAll(select,on){for(const o of itemOptions(select))o.selected=on;syncAll(select);}
// change 时无法区分是点了“全部选择”还是普通项，用上一次的全选状态判断意图。
function toggleAll(select){const all=allOption(select);if(all&&all.selected!==(select.dataset.allSelected==='1'))applyAll(select,all.selected);else syncAll(select);}
function bindAllToggle(id,after){$(id).addEventListener('change',()=>{toggleAll($(id));if(after)after();});}
function selectedValues(id) { return Array.from($(id).selectedOptions).map(o=>o.value).filter(v=>v!==ALL_VALUE); }
function fillOptions(id, entries) {
  const chosen=new Set(selectedValues(id));
  const select=$(id);select.replaceChildren();
  const all=element('option','全部选择');all.value=ALL_VALUE;select.append(all);
  for(const entry of MindexMultiSearch.sort(entries)){const option=element('option',`${entry.name} · ${entry.id.slice(0,8)}`);option.value=entry.id;option.selected=chosen.has(entry.id);select.append(option);}
  MindexMultiSearch.attach(select);
  MindexMultiSearch.refilter(select);
  syncAll(select);
}
function fillRoleOptions() {
  const actors=new Set(selectedValues('actor-select'));
  const roles=registeredPeople.filter(e=>e.kind==='role'&&(actors.size===0||(e.actor_ids||[]).some(id=>actors.has(id))));
  fillOptions('role-select',roles);
}
async function refresh() {
  $('refresh').disabled=true;
  try {
    const data=await api('/api/status?health=true');
    const catalogue=await api('/api/persons');
    registeredPeople=catalogue.entities.filter(e=>['actor','role'].includes(e.kind));
    fillOptions('actor-select',registeredPeople.filter(e=>e.kind==='actor'));
    fillRoleOptions();
    const current=$('source').value;$('source').replaceChildren(element('option','全部素材'));
    $('source').firstChild.value='';
    data.sources.forEach(source=>{const option=element('option',source.name);option.value=source.id;$('source').append(option);});$('source').value=current;
    const count=data.segment_counts.find(item=>item.kind==='moment')?.count||0;
    $('library-summary').textContent=`${data.sources.length} 个视频 · ${count} 个精细片段`;
    const target=$('library-detail');target.replaceChildren();
    target.append(element('div',data.active_index?`当前索引：${data.active_index.id}`:'尚未发布素材索引。先切片，再建立画面索引；也可转写、分析后建立完整索引。'));
    if(data.active_index?.spec.mode==='visual') target.append(element('div','当前仅检索视频画面，尚未加入对白与语义描述。'));
    target.append(element('div',data.model_health.ready?(data.model_health.matches_index?'检索模型已就绪。':'模型配置与当前索引不匹配。'):'检索模型尚未就绪。请启动配置对应的嵌入服务。'));
    if(data.model_health.error) target.append(element('div',data.model_health.error));
    if(data.runs.length) target.append(element('div',`最近处理：${data.runs[0].stage} / ${data.runs[0].status}`));
    if(!data.active_index) notice('素材库尚未建立。完成离线导入后，即可开始检索。','info');
  } catch(error) {$('library-summary').textContent='读取失败';notice(error.message,'error');}
  finally {$('refresh').disabled=false;}
}
$('refresh').onclick=refresh;
bindAllToggle('actor-select',fillRoleOptions);
bindAllToggle('role-select');
refresh();

$('query').addEventListener('input',()=>{
  const text=$('query').value;
  const names=registeredPeople.filter(p=>[p.name,...p.aliases].some(name=>text.toLocaleLowerCase().includes(name.toLocaleLowerCase())));
  $('person-hint').textContent=names.length?'姓名提示：'+names.map(p=>`${personKinds[p.kind]||p.kind} ${p.name} · ${p.id.slice(0,8)}`).join('、')+'；演员与角色是不同身份，同名请在列表中选定。':'登记姓名默认只检索有出镜证据的片段。';
});
