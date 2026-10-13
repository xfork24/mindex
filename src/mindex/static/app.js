const $ = id => document.getElementById(id);
let mode = 'text';
let busy = false;
let previewUrl = null;
const routeNames = {video:'画面匹配', description:'动作与场景', style:'情绪与镜头', dialogue:'对白语义', lexical:'关键词'};

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
  for (const name of ['text','video']) {
    $(`${name}-tab`).classList.toggle('active',name===mode);
    $(`${name}-tab`).setAttribute('aria-selected',String(name===mode));
    $(`${name}-input`).hidden=name!==mode;
  }
  notice();
}
$('text-tab').onclick=()=>switchMode('text');
$('video-tab').onclick=()=>switchMode('video');
document.querySelectorAll('[data-query]').forEach(button=>button.onclick=()=>{
  $('query').value=button.dataset.query; $('query').focus();
});
$('video-file').onchange=()=>{
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  const file=$('video-file').files[0];
  $('query-preview').hidden=!file;
  if (file) {
    $('file-label').textContent=`${file.name} · ${(file.size/1024/1024).toFixed(1)}MB`;
    previewUrl=URL.createObjectURL(file); $('query-preview').src=previewUrl;
  } else $('file-label').textContent='最长 30 秒 · 最大 100MB';
};

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
  target.append(video,element('p',`${clock(result.start)} — ${clock(result.end)}`),element('p',result.annotation.description));
  for (const [title,items] of [['动作',result.annotation.actions],['画面依据',result.annotation.visual_evidence],['对白推断',result.annotation.dialogue_inference],['不确定信息',result.annotation.uncertainty]]) {
    if (items.length) target.append(element('p',`${title}：${items.join('；')}`));
  }
  if (result.dialogue.length) target.append(element('pre',result.dialogue.map(u=>`${clock(result.start+u.start)} ${u.text}`).join('\n')));
  target.append(element('p',`排序分数 ${result.score.toFixed(5)} · ${result.matches.map(m=>`${routeNames[m.route]||m.route}第 ${m.rank} 位`).join('；')}`));
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
    [...result.annotation.scene,...result.annotation.emotions,...result.annotation.shot_size].slice(0,5).forEach(tag=>tags.append(element('span',tag,'tag')));
    const actions=element('div',undefined,'card-actions');
    const detail=element('button','查看详情');detail.type='button';detail.onclick=()=>details(result);
    const context=element('a','播放上下文 ↗');context.href=result.context_url;context.target='_blank';context.rel='noopener';
    actions.append(detail,context,managementLink(result),element('span',`#${index+1} · ${result.score.toFixed(4)}`,'score'));
    body.append(title,element('p',result.display_name || result.annotation.description,'card-description'),tags,actions);
    card.append(video,body);$('results').append(card);
  });
}
$('search-form').onsubmit=async event=>{
  event.preventDefault();if(busy)return;
  const text=$('query').value.trim();const file=$('video-file').files[0];
  if (mode==='text'&&!text) {notice('请先描述想找的画面。','error');return;}
  if (mode==='video'&&!file) {notice('请选择一段参考视频。','error');return;}
  busy=true;$('search-button').disabled=true;$('search-button').textContent='正在查找…';
  notice('正在检索素材，请稍候。','info');
  try {
    let data;
    const common={granularity:$('granularity').value,limit:Number($('limit').value),filters:filters()};
    if(mode==='text') data=await api('/api/search/text',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,...common})});
    else {
      const form=new FormData();form.append('file',file);form.append('granularity',common.granularity);form.append('limit',common.limit);form.append('filters',JSON.stringify(common.filters));
      data=await api('/api/search/video',{method:'POST',body:form});
    }
    render(data);notice();
  } catch(error) {notice(error.message,'error');}
  finally {busy=false;$('search-button').disabled=false;$('search-button').textContent='查找片段 ↗';}
};
async function refresh() {
  $('refresh').disabled=true;
  try {
    const data=await api('/api/status?health=true');
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
refresh();
