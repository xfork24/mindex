const $ = id => document.getElementById(id);
const state = {source:null,sourcePage:1,sourceTotal:0,segmentPage:1,segmentTotal:0,kind:'shot',parent:null,editing:null,focus:null};
let sourceRequest = 0;
let segmentRequest = 0;
let selectionRequest = 0;

function node(tag,text,className) {
  const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;
}
function notify(text='',error=false) {$('admin-notice').textContent=text;$('admin-notice').className=`notice ${error?'error':'info'}`;}
function time(value) {
  const hours=Math.floor(value/3600),minutes=Math.floor(value%3600/60),seconds=(value%60).toFixed(2).padStart(5,'0');
  return `${hours?`${hours}:`:''}${String(minutes).padStart(2,'0')}:${seconds}`;
}
function errorText(detail) {return Array.isArray(detail)?detail.map(e=>e.msg).join('；'):typeof detail==='string'?detail:JSON.stringify(detail);}
async function api(url,options={}) {const r=await fetch(url,options);const data=await r.json();if(!r.ok)throw new Error(errorText(data.detail||data));return data;}
function params(values) {return new URLSearchParams(Object.entries(values).filter(([,v])=>v!==null&&v!==undefined));}
function pager(prefix,page,total) {$(`${prefix}-page`).textContent=`${page} / ${Math.max(1,Math.ceil(total/48))}`;$(`${prefix}-prev`).disabled=page<=1;$(`${prefix}-next`).disabled=page*48>=total;}
function pauseList() {$('segment-list').querySelectorAll('video').forEach(v=>v.pause());}

async function loadSources() {
  const request=++sourceRequest;
  const data=await api(`/api/admin/sources?${params({q:$('source-query').value.trim(),visibility:$('source-visibility').value,favorite_only:$('source-favorite').checked,page:state.sourcePage})}`);
  if(request!==sourceRequest)return;
  if(state.sourcePage>1&&!data.items.length){state.sourcePage--;return loadSources();}
  state.sourceTotal=data.total;$('source-count').textContent=`${data.total} 份素材`;$('source-list').replaceChildren();
  data.items.forEach(source=>{
    const b=node('button',undefined,`source-item ${state.source?.id===source.id?'selected':''}`);b.type='button';b.setAttribute('aria-pressed',String(state.source?.id===source.id));
    b.append(node('strong',`${source.favorite?'★ ':''}${source.name}`),node('small',`${source.shot_count} 镜头 · ${source.moment_count} 精细片段 · ${time(source.media.duration)}`),node('small',`${source.hidden?'已隐藏 · ':''}${source.prepared?'切片完成':'待完成切片'} · ${source.id.slice(0,8)}`));
    b.onclick=()=>run(()=>selectSource(source.id));$('source-list').append(b);
  });
  if(!data.items.length){const empty=node('div',undefined,'empty');empty.append(node('p','没有符合条件的素材。'),node('p','可调整筛选，或通过 mindex prepare 导入视频。'));$('source-list').append(empty);}
  pager('source',state.sourcePage,data.total);
}

function action(text,onClick,pressed) {
  const b=node('button',text,'quiet');b.type='button';if(pressed!==undefined)b.setAttribute('aria-pressed',String(pressed));
  b.onclick=()=>run(async()=>{b.disabled=true;try{await onClick();}finally{b.disabled=false;}});return b;
}
function actions(kind,item) {
  const bar=node('div',undefined,'organization-actions');
  if(kind==='segment')bar.append(action('详情',()=>showAnalysis(item)));
  bar.append(action('备注',()=>edit(kind,item)),action(item.favorite?'★ 已收藏':'☆ 收藏',()=>patch(kind,item.id,{favorite:!item.favorite}),item.favorite),action(item.hidden?'恢复显示':'隐藏',()=>patch(kind,item.id,{hidden:!item.hidden}),item.hidden));
  if(kind==='source')bar.append(action('删除',()=>deleteSource(item)));
  return bar;
}
async function deleteSource(item) {
  const message=[
    `删除素材「${item.name}」？此操作不可撤销。`,``,
    `将物理删除：全部嵌入向量、${item.shot_count} 个镜头 / ${item.moment_count} 个精细片段的切片文件与缩略图、转写、分镜计划、上下文缓存、整理信息（收藏/隐藏/备注）。`,
    `保留：源视频文件本身（仍留在 data/sources/，可随时重新导入）。`,
  ].join('\n');
  if(!confirm(message))return;
  const summary=await api(`/api/admin/sources/${item.id}`,{method:'DELETE'});
  state.source=null;state.parent=null;state.focus=null;
  $('source-workspace').hidden=true;$('no-source').hidden=false;syncUrl();
  await loadSources();
  notify(`已删除 ${summary.deleted.segments} 个片段、${summary.deleted.vectors} 条向量、${summary.deleted.files} 个文件；源视频 ${summary.kept.source_file} 已保留。`);
}
function renderSource(source) {
  const target=$('source-detail');target.replaceChildren();
  const heading=node('div',undefined,'source-heading');heading.append(node('h2',source.name));
  if(source.hidden)heading.append(node('span','已隐藏','hidden-badge'));
  target.append(heading,node('p',`原文件：${source.original_name} · ${source.id.slice(0,8)}`,'source-meta'),node('p',`${time(source.media.duration)} · ${source.media.width} × ${source.media.height} · ${source.media.fps.toFixed(2)} fps · ${source.shot_count} 镜头 / ${source.moment_count} 精细片段`,'source-meta'),node('p',source.prepared?'切片完成 · 可直接浏览和整理':'切片尚未完成 · 可查看已生成片段','source-meta'));
  if(source.missing_artifacts)target.append(node('p',`缺失 ${source.missing_artifacts} 个媒体文件，请通过 split 命令修复。`,'source-warning'));
  if(source.notes)target.append(node('p',source.notes,'source-notes'));
  target.append(actions('source',source));
  renderDialogue(source, target);
  if(source.hidden)target.append(node('p','此素材及其所有片段已退出检索。恢复素材后，片段自身的隐藏设置仍会保留。','muted'));
}

async function selectSource(id) {
  const request=++selectionRequest;++segmentRequest;
  $('source-workspace').setAttribute('aria-busy','true');
  try {
    const source=await api(`/api/admin/sources/${id}`);if(request!==selectionRequest)return;
    state.source=source;state.parent=null;state.segmentPage=1;state.focus=null;
    if(source.hidden)$('segment-visibility').value='all';else $('segment-visibility').value='visible';
    $('no-source').hidden=true;$('source-workspace').hidden=false;renderSource(source);syncUrl();
    await Promise.all([loadSources(),loadSegments()]);
  } finally {if(request===selectionRequest)$('source-workspace').removeAttribute('aria-busy');}
}
function syncUrl() {history.replaceState(null,'',`/admin?${params({source:state.source?.id,kind:state.kind,parent:state.parent?.id,segment:state.focus})}`);}
function syncTabs() {
  for(const [id,kind] of [['shots-tab','shot'],['moments-tab','moment']]){$(id).classList.toggle('active',state.kind===kind);$(id).setAttribute('aria-selected',String(state.kind===kind));}
  $('parent-filter').hidden=!state.parent;if(state.parent)$('parent-label').textContent=`来自：${state.parent.name}`;
}
async function loadSegments() {
  if(!state.source)return;const request=++segmentRequest;syncTabs();syncUrl();
  const data=await api(`/api/admin/sources/${state.source.id}/segments?${params({kind:state.kind,parent_id:state.parent?.id,visibility:$('segment-visibility').value,favorite_only:$('segment-favorite').checked,page:state.segmentPage})}`);
  if(request!==segmentRequest)return;
  if(state.segmentPage>1&&!data.items.length){state.segmentPage--;return loadSegments();}
  state.segmentTotal=data.total;$('segment-count').textContent=`${data.total} 个${state.kind==='shot'?'镜头':'精细片段'}`;
  pauseList();$('segment-list').replaceChildren();
  data.items.forEach(item=>{
    const card=node('article',undefined,`segment-card ${item.effective_hidden?'is-hidden':''} ${state.focus===item.id?'is-focused':''}`);card.dataset.segmentId=item.id;
    const poster=node('button',undefined,'poster-button');poster.type='button';poster.setAttribute('aria-label',`播放 ${item.name}`);
    const image=node('img');image.src=item.thumbnail_url;image.alt=item.name;image.loading='lazy';
    poster.append(image,node('span','▶','play-symbol'));poster.onclick=()=>play(item);
    const body=node('div',undefined,'segment-body');body.append(node('h3',item.name),node('p',`${time(item.start)} — ${time(item.end)} · ${item.duration.toFixed(2)} 秒`,'segment-time'));
    if(item.effective_hidden)body.append(node('span',item.inherited_hidden?'随上级隐藏':'已隐藏','hidden-badge'));
    if(item.notes)body.append(node('p',item.notes,'segment-note'));
    body.append(actions('segment',item));
    if(item.inherited_hidden)body.append(node('p','需先恢复素材或父镜头才能重新参与检索。','muted'));
    const links=node('div',undefined,'relationship');
    if(item.kind==='shot')links.append(action(`查看 ${item.child_count} 个精细片段 →`,async()=>{state.kind='moment';state.parent=item;state.segmentPage=1;await loadSegments();}));
    else if(item.parent_id)links.append(action('返回父镜头 ↗',async()=>{const parent=await api(`/api/admin/segments/${item.parent_id}`);state.kind='shot';state.parent=null;state.focus=parent.id;state.segmentPage=Math.floor((parent.position-1)/48)+1;$('segment-visibility').value='all';$('segment-favorite').checked=false;await loadSegments();$('segment-list').querySelector('.is-focused')?.scrollIntoView({block:'center',behavior:'smooth'});}));
    const download=node('a','下载 ↧');download.href=`${item.media_url}?download=true`;links.append(download);body.append(links);card.append(poster,body);$('segment-list').append(card);
  });
  if(!data.items.length){const empty=node('div',undefined,'empty');empty.append(node('h3','没有符合条件的片段'),node('p','尝试切换显示范围、取消收藏筛选，或等待命令行完成切片。'));$('segment-list').append(empty);}
  pager('segment',state.segmentPage,data.total);
}

function edit(kind,item) {state.editing={kind,id:item.id};$('edit-title').textContent=kind==='source'?'整理素材':'整理片段';$('edit-name').value=item.custom_name||'';$('edit-notes').value=item.notes||'';$('edit-error').textContent='';$('edit-dialog').showModal();$('edit-name').focus();}
async function patch(kind,id,changes) {
  const sourceId=state.source?.id;
  await api(`/api/admin/${kind==='source'?'sources':'segments'}/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(changes)});
  if(state.source?.id!==sourceId)return;
  state.source=await api(`/api/admin/sources/${sourceId}`);renderSource(state.source);
  await Promise.all([loadSources(),loadSegments()]);notify('修改已保存。');
}
$('edit-form').onsubmit=async event=>{
  event.preventDefault();const editing=state.editing;if(!editing)return;$('edit-save').disabled=true;$('edit-error').textContent='';
  try {await patch(editing.kind,editing.id,{display_name:$('edit-name').value.trim()||null,notes:$('edit-notes').value});$('edit-dialog').close();}
  catch(e){$('edit-error').textContent=e.message;}finally{$('edit-save').disabled=false;}
};
$('edit-close').onclick=()=>$('edit-dialog').close();
function play(item) {$('play-title').textContent=item.name;$('play-video').src=item.media_url;$('play-video').poster=item.thumbnail_url;$('play-time').textContent=`原片 ${time(item.start)} — ${time(item.end)} · ${item.source_name}`;$('play-download').href=`${item.media_url}?download=true`;if(!$('play-dialog').open)$('play-dialog').showModal();}
$('play-close').onclick=()=>$('play-dialog').close();
$('play-dialog').addEventListener('close',()=>{$('play-video').pause();$('play-video').removeAttribute('src');$('play-video').load();});
async function run(task) {try{await task();}catch(e){notify(e.message,true);}}
function sourceFiltersChanged(){state.sourcePage=1;return loadSources();}
function segmentFiltersChanged(){state.segmentPage=1;return loadSegments();}
$('source-search').onsubmit=e=>{e.preventDefault();run(sourceFiltersChanged);};
for(const id of ['source-visibility','source-favorite'])$(id).onchange=()=>run(sourceFiltersChanged);
for(const id of ['segment-visibility','segment-favorite'])$(id).onchange=()=>run(segmentFiltersChanged);
$('source-prev').onclick=()=>run(async()=>{state.sourcePage--;await loadSources();});$('source-next').onclick=()=>run(async()=>{state.sourcePage++;await loadSources();});
$('segment-prev').onclick=()=>run(async()=>{state.segmentPage--;await loadSegments();});$('segment-next').onclick=()=>run(async()=>{state.segmentPage++;await loadSegments();});
for(const [id,kind] of [['shots-tab','shot'],['moments-tab','moment']])$(id).onclick=()=>run(async()=>{state.kind=kind;state.parent=null;state.segmentPage=1;await loadSegments();});
$('clear-parent').onclick=()=>run(async()=>{state.parent=null;state.segmentPage=1;await loadSegments();});
$('reload').onclick=()=>run(async()=>{$('reload').disabled=true;try{if(state.source)await selectSource(state.source.id);else await loadSources();notify('素材状态已刷新。');}finally{$('reload').disabled=false;}});
run(async()=>{
  await loadSources();const query=new URLSearchParams(location.search);
  if(query.get('kind')==='moment')state.kind='moment';const id=query.get('source');
  if(id){
    await selectSource(id);const parent=query.get('parent'),segment=query.get('segment');
    if(segment){
      const item=await api(`/api/admin/segments/${segment}`);
      if(item.source_id!==id||!['shot','moment'].includes(item.kind))throw new Error('片段不属于当前素材');
      state.kind=item.kind;state.parent=null;state.focus=item.id;state.segmentPage=Math.floor((item.position-1)/48)+1;
      $('segment-visibility').value='all';$('segment-favorite').checked=false;await loadSegments();
      $('segment-list').querySelector('.is-focused')?.scrollIntoView({block:'center',behavior:'smooth'});
    }else if(parent){
      const item=await api(`/api/admin/segments/${parent}`);if(item.source_id!==id||item.kind!=='shot')throw new Error('父镜头不属于当前素材');
      state.kind='moment';state.parent=item;await loadSegments();
    }
  }
});

let dialoguePoll = null;
const channelNames = {visual:'视频画面', dialogue:'对白文本', analysis:'LLM 分析'};
const channelStatus = {complete:'处理完成', partial:'部分完成', empty:'空内容', no_audio:'无音轨', disabled:'未启用', processing:'处理中', failed:'失败', not_processed:'未处理', pending_alignment:'待手动对齐'};
function renderDialogue(source, target) {
  clearTimeout(dialoguePoll);
  const panel = node('section', undefined, 'dialogue-panel');
  panel.append(node('h3', '处理与检索状态'));
  const statusLines = node('div', undefined, 'channel-status-lines');
  panel.append(statusLines);
  for (const [channel, label] of Object.entries(channelNames)) {
    const processed = source.channels?.[channel] || {};
    const coverage = source.published_channels?.[channel] || {};
    const total = channel === 'dialogue' ? processed.content_segments || 0 : coverage.total_segments || 0;
    const unit = channel === 'dialogue' ? '个有对白片段' : '个片段';
    statusLines.append(node('p', `${label}：${channelStatus[processed.status] || processed.status || '未处理'} · 已进入检索 ${coverage.indexed_segments || 0}/${total} ${unit}`));
    if (processed.error && channel !== 'dialogue') panel.append(node('p', processed.error, 'source-warning'));
  }
  const data = source.channels?.dialogue || {};
  if (data.source) statusLines.append(node('p', `对白来源：${{subtitle:'字幕', asr:'ASR', legacy_asr:'历史 ASR'}[data.source] || data.source}`));
  if (data.fallback_reason) panel.append(node('p', data.fallback_reason, 'muted'));
  if (data.error) panel.append(node('p', data.error, 'source-warning'));
  const label = node('label', '字幕轨道 ');
  const select = node('select'); select.setAttribute('aria-label', '对白字幕轨道');
  const auto = node('option', '自动选择完整文本字幕'); auto.value = ''; select.append(auto);
  for (const track of source.subtitle_tracks || []) {
    const option = node('option', `${track.index} · ${track.language} · ${track.codec}${track.forced?' · forced':''}${track.default?' · 默认':''}${track.title?' · '+track.title:''}`);
    option.value = track.index; select.append(option);
  }
  if (data.selected_track) select.value = String(data.selected_track.index);
  label.append(select); panel.append(label);
  const task = source.dialogue_task;
  const busy = task && ['queued', 'running'].includes(task.status);
  const publishLabel = node('label', ' 完成后自动发布 ');
  const publish = node('input'); publish.type = 'checkbox'; publish.checked = true; publishLabel.append(publish); panel.append(publishLabel);
  const start = action('补充对白', async () => {
    const options = {subtitle_stream:select.value === '' ? null : Number(select.value), publish:publish.checked};
    await api(`/api/admin/sources/${source.id}/dialogue`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(options)});
    await refreshDialogue(source.id);
  });
  start.disabled = Boolean(busy); panel.append(start);
  if (task) {
    const progress = task.progress || {};
    panel.append(node('p', `任务：${task.status} · ${task.status === 'complete' ? '处理完成' : progress.stage === 'embedding' ? '正在嵌入与发布' : `${progress.completed_chunks || 0}/${progress.total_chunks || '?'} 分块`}`));
    if (task.error) panel.append(node('p', task.error, 'source-warning'));
    if (['failed','interrupted'].includes(task.status)) panel.append(action('重试未完成部分', async () => {
      await api(`/api/admin/dialogue-tasks/${task.id}/retry`, {method:'POST'}); await refreshDialogue(source.id);
    }));
    if (task.result?.index_id) panel.append(node('p', `已发布索引 ${task.result.index_id}`));
    if (busy) dialoguePoll = setTimeout(() => run(() => refreshDialogue(source.id)), 2500);
  }
  panel.append(node('p', '本操作仅补充对白，不触发 LLM 分析；人物名字表示对白提及。', 'muted'));
  target.append(panel);
}
async function refreshDialogue(sourceId) {
  const source = await api(`/api/admin/sources/${sourceId}`);
  if (state.source?.id !== sourceId) return;
  state.source = source; renderSource(source);
}

let analysisRequest=0;
async function showAnalysis(item) {
  const request=++analysisRequest;
  $('analysis-title').textContent=`${item.name} · ${time(item.start)} — ${time(item.end)}`;
  const target=$('analysis-content');target.replaceChildren(node('p','正在读取分析结果…'));
  if(!$('analysis-dialog').open)$('analysis-dialog').showModal();
  try {
    const data=await api(`/api/admin/segments/${item.id}/details`);
    if(request!==analysisRequest)return;
    target.replaceChildren();
    function utterances(section, rows) {
      for(const u of rows||[])section.append(node('p',`${time(u.evidence_start ?? item.start+u.start)} — ${time(u.evidence_end ?? item.start+u.end)}　${u.text}`,'analysis-utterance'));
    }
    const asr=node('section',undefined,'analysis-section');asr.append(node('h3','ASR 分析结果'),node('p',data.asr.message));
    if(data.asr.error)asr.append(node('p',data.asr.error,'error'));
    utterances(asr,data.asr.utterances);target.append(asr);
    const subtitles=node('section',undefined,'analysis-section');subtitles.append(node('h3','字幕轨道结果'));
    if(!data.subtitles.tracks.length)subtitles.append(node('p',data.subtitles.message));
    if(data.subtitles.error)subtitles.append(node('p',data.subtitles.error,'error'));
    for(const track of data.subtitles.tracks){
      subtitles.append(node('h4',`轨道 ${track.index} · ${track.language} · ${track.codec}${track.title?' · '+track.title:''}`),node('p',track.message));
      if(track.error)subtitles.append(node('p',track.error,'error'));
      utterances(subtitles,track.utterances);
    }
    target.append(subtitles);
    const llm=node('section',undefined,'analysis-section');llm.append(node('h3','LLM 分析结果'));
    if(data.llm.annotation){
      const labels={description:'描述',actions:'动作',people:'人物',objects:'对象',scene:'场景',emotions:'情绪',atmosphere:'氛围',shot_size:'景别',angle:'角度',camera_motion:'镜头运动',visual_evidence:'画面依据',dialogue_inference:'对白推断',uncertainty:'不确定信息'};
      for(const [key,value] of Object.entries(data.llm.annotation)){
        const text=Array.isArray(value)?value.join('；'):String(value ?? '');
        if(text)llm.append(node('p',`${labels[key]||key}：${text}`));
      }
    }else llm.append(node('p',data.llm.message));
    target.append(llm);
    const person=node('section',undefined,'analysis-section');
    person.append(node('h3','人物处理'),node('p',data.person.message));
    for(const [key,label] of [['covered','已处理'],['uncovered','未处理']]){
      const ranges=data.person[key].map(r=>`${time(r.start)} — ${time(r.end)}`).join('、');
      if(ranges)person.append(node('p',`${label}区间：${ranges}`));
    }
    const counts=data.person.counts;
    person.append(node('p',`已接受 ${counts.accepted} · 待确认 ${counts.candidate} · 已驳回 ${counts.rejected}`));
    if(data.person.people.length)person.append(node('p',`证据关联人物：${data.person.people.map(p=>`${p.name}（${p.kind_label}）`).join('、')}`));
    target.append(person);
  }catch(error){if(request===analysisRequest)target.replaceChildren(node('p',error.message,'error'));}
}
$('analysis-close').onclick=()=>$('analysis-dialog').close();
$('analysis-dialog').addEventListener('close',()=>{analysisRequest++;});
