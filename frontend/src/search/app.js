const $ = (id) => document.getElementById(id);
let mode = 'text';
let busy = false;
let previewUrl = null;
let imagePreviewUrl = null;
let registeredPeople = [];
let registeredSources = [];
// Keep the explicit material scope separate from options narrowed by person filters.
let sourceScope = null;
let groupSourceScope = null;
const routeNames = { visual: MindexI18n.msg("ui.fe84e2eb06a1"), analysis: MindexI18n.msg("ui.5484a583c2c9"), video: MindexI18n.msg("ui.83b37291f23c"), description: MindexI18n.msg("ui.7c0f30403c16"), style: MindexI18n.msg("ui.bd17acd89736"), dialogue: MindexI18n.msg("ui.7d722c18e001"), lexical: MindexI18n.msg("ui.cc1b21e80080"), person: MindexI18n.msg("ui.bc9173082f3e") };
const personKinds = { actor: MindexI18n.msg("ui.5fdb8b2c027a"), role: MindexI18n.msg("ui.6b26695e4dce") };

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) MindexI18n.setText(node, text);
  if (className) node.className = className;
  return node;
}
function notice(text = '', type = '') {
  if (globalThis.MindexFeedback) return globalThis.MindexFeedback.show('notice', type === 'error' ? MindexI18n.system(text) : text, type);
  MindexI18n.setText($('notice'), type === 'error' ? MindexI18n.system(text) : text);$('notice').className = `notice ${type}`;}
function clock(seconds) {
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, '0')}:${(seconds % 60).toFixed(1).padStart(4, '0')}`;
}
function errorText(detail) {
  if (Array.isArray(detail)) return detail.map((item) => item.msg || JSON.stringify(item)).join('；');
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
}
async function api(url, options = {}) {
  return MindexApi.request(url, options);
}
function switchMode(next) {
  if (busy) return;
  mode = next;
  for (const name of ['text', 'image', 'video']) {
    $(`${name}-tab`).classList.toggle('active', name === mode);
    $(`${name}-tab`).setAttribute('aria-selected', String(name === mode));
    $(`${name}-input`).hidden = name !== mode;
  }
  $('person-filters').hidden = mode !== 'text';
  fillSearchFilters();
  if (mode !== 'video') $('query-preview').pause();
  notice();
}
$('text-tab').onclick = () => switchMode('text');
$('image-tab').onclick = () => switchMode('image');
$('video-tab').onclick = () => switchMode('video');
document.querySelectorAll('[data-query]').forEach((button) => button.onclick = () => {
  $('query').value = button.dataset.query;$('query').focus();
});
function showVideo(file) {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  const preview = $('query-preview');
  preview.hidden = !file;
  if (file) {
    MindexI18n.setText($('file-label'), `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)}MB`);
    previewUrl = URL.createObjectURL(file);preview.src = previewUrl;
  } else {
    MindexI18n.setText($('file-label'), MindexI18n.msg("ui.67fd2fc9b41a"));preview.removeAttribute('src');
  }
}
function showImage(file) {
  if (imagePreviewUrl) URL.revokeObjectURL(imagePreviewUrl);
  imagePreviewUrl = null;
  const preview = $('image-preview');
  preview.hidden = !file;
  if (file) {
    MindexI18n.setText($('image-file-label'), `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)}MB`);
    imagePreviewUrl = URL.createObjectURL(file);preview.src = imagePreviewUrl;
  } else {
    MindexI18n.setText($('image-file-label'), MindexI18n.msg("ui.2caffec04326"));preview.removeAttribute('src');
  }
}
// 拖放的文件不属于 input.files，优先写回 input，让预览与提交共用同一条路径。
const droppedFile = { image: null, video: null };
function currentFile(mode) {return $(mode === 'image' ? 'image-file' : 'video-file').files[0] || droppedFile[mode] || null;}
function acceptsType(file, accept) {
  return accept.split(',').map((rule) => rule.trim()).filter(Boolean).some((rule) =>
  rule.endsWith('/*') ? file.type.startsWith(rule.slice(0, -1)) : file.type === rule);
}
function bindDrop(panelId, inputId, labelId, kind, show) {
  const panel = $(panelId),input = $(inputId),label = $(labelId),accept = input.getAttribute('accept') || '';
  input.onchange = () => {droppedFile[kind] = null;show(input.files[0] || null);};
  const highlight = () => label.classList.add('dragging');
  const unhighlight = () => label.classList.remove('dragging');
  panel.addEventListener('dragover', (event) => {event.preventDefault();highlight();});
  panel.addEventListener('dragenter', (event) => {event.preventDefault();highlight();});
  // 在子元素之间移动也会冒泡 dragleave，用 relatedTarget 判断是否真的离开了面板。
  panel.addEventListener('dragleave', (event) => {if (!panel.contains(event.relatedTarget)) unhighlight();});
  panel.addEventListener('drop', (event) => {
    event.preventDefault();unhighlight();
    const file = event.dataTransfer?.files?.[0];
    if (!file) {notice(MindexI18n.msg("ui.72caad14751e"), 'error');return;}
    if (!acceptsType(file, accept)) {notice(MindexI18n.msg("ui.4e5f13f13a2a", { p0: kind === 'image' ? MindexI18n.msg("ui.be8da62ea113") : MindexI18n.msg("ui.fa4e33b69853"), p1: accept }), 'error');return;}
    const transfer = new DataTransfer();transfer.items.add(file);
    try {input.files = transfer.files;} catch (e) {droppedFile[kind] = file;}
    show(file);
    if (!input.files.length && !droppedFile[kind]) notice(MindexI18n.msg("ui.e6c9a5d0b984"), 'error');
  });
}
bindDrop('image-input', 'image-file', 'image-drop', 'image', showImage);
bindDrop('video-input', 'video-file', 'video-drop', 'video', showVideo);

// 素材组筛选默认全选（全选=不限，不写入 source_group_ids）；部分选择才按组过滤。
const searchGroups = MindexGroups.picker("source", [], () => {}, "ui.groups_filter", {listbox: true, defaultAll: true});
$("source").closest(".material-filters").prepend(searchGroups.wrap);
const searchGroupLink = MindexGroups.linkSources(searchGroups, $('source'), {
  entries: () => searchFilterEntries('source'), selection: () => sourceScope,
  fill: (entries) => fillOptions('source', entries),
  onScope: (allowed) => { groupSourceScope = allowed; },
  updated: () => fillSearchFilters(), onError: (error) => notice(error.message, 'error')
});

function filters() {
  const values = { scene: $('scene').value.trim(), emotion: $('emotion').value.trim(), shot_size: $('shot-size').value.trim() };
  const sources = sourceScope === null ? selectedValues('source') : sourceScope;
  if (sources.length) values.source_ids = sources;
  const groupIds = searchGroups.values();
  if (groupIds.length) values.source_group_ids = groupIds;
  for (const [key, id] of [['min_duration', 'min-duration'], ['max_duration', 'max-duration']]) {
    if ($(id).value !== '') values[key] = Number($(id).value);
  }
  return Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ''));
}

function bestPersonEvidence(evidence) {
  const people = new Map();
  for (const item of evidence || []) {
    const key = item.actor_id ? `actor:${item.actor_id}` : item.role_id ? `role:${item.role_id}` : JSON.stringify([item.actor_name, item.role_name]);
    const current = people.get(key);
    // 同一人物只展示一张图；有截图时，在这些截图中选置信分数最高的证据。
    if (!current || item.image_url && !current.image_url ||
    Boolean(item.image_url) === Boolean(current.image_url) && (item.data?.score || 0) > (current.data?.score || 0)) people.set(key, item);
  }
  return [...people.values()];
}

function details(result) {
  const annotation = result.annotation || {};
  const target = $('detail-content');target.replaceChildren();
  target.append(element('h3', result.display_name || result.source_name));
  if (result.display_name) target.append(element('p', result.source_name));
  const video = element('video');queueMicrotask(() => ShotPlayer.attach(video, `/api/playlists/${result.id}`));video.controls = true;video.preload = 'metadata';
  const summary = element('p');
  if (annotation.description) MindexAnalysisText.setText(summary, annotation.description);
  else MindexI18n.setText(summary, MindexI18n.msg(result.annotation_status === 'skipped' ? 'ui.search_analysis_skipped' : 'ui.fec3adb80692'));
  target.append(video, element('p', `${clock(result.start)} — ${clock(result.end)}`), summary);
  for (const [title, items] of [[MindexI18n.msg("ui.d9d9827827e1"), (annotation.actions || [])], [MindexI18n.msg("ui.cf5030475efb"), (annotation.visual_evidence || [])], [MindexI18n.msg("ui.cf335345e962"), (annotation.dialogue_inference || [])], [MindexI18n.msg("ui.43f1d3835b20"), (annotation.uncertainty || [])]]) {
    if (items.length) target.append(MindexAnalysisText.setText(element('p'), items, {label: title}));
  }
  if (result.dialogue.length) target.append(element('pre', result.dialogue.map((u) => `${clock(result.start + u.start)} ${u.text}`).join('\n')));
  target.append(element('p', MindexI18n.msg("ui.f1a2a7a0cfdc", { p0: result.score.toFixed(5), p1: result.matches.map((m) => MindexI18n.msg("ui.ff429c0bec08", { p0: routeNames[m.route] || m.route, p1: m.rank })).join('；') })));
  for (const match of result.matches || []) {
    if (match.matched_text) {
      const analysis = match.channel === 'analysis' || ['description', 'style'].includes(match.route);
      target.append(analysis ? MindexAnalysisText.setText(element('p'), match.matched_text, {label: routeNames[match.channel] || routeNames[match.route] || match.channel || match.route, view: true}) :
        element('p', `${routeNames[match.channel] || match.channel}：${match.matched_text}`));
    }
    for (const evidence of match.evidence || []) {
      if (evidence.text) target.append(element('p', `${clock(evidence.start)} — ${clock(evidence.end)}：${evidence.text}`));
    }
  }
  for (const evidence of bestPersonEvidence(result.person_evidence)) {
    const section = element('section', undefined, 'person-evidence');
    const name = `${evidence.actor_name || MindexI18n.msg("ui.1b37b5539364")}${evidence.role_name ? ' · ' + evidence.role_name : ''}`;
    const evidenceType = evidence.data.evidence_type;
    const method = { face: MindexI18n.msg("ui.9a0eb165fd79"), track: MindexI18n.msg("ui.7b9b29b27092"), manual: MindexI18n.msg("ui.70036f7c4bbc") }[evidenceType] || evidenceType;
    const matchScore = evidenceType === 'face' && Number.isFinite(evidence.data.score) ? MindexI18n.msg("ui.72a44342ea9e", { p0: (evidence.data.score * 100).toFixed(2) }) : '';
    section.append(element('p', `${name} · ${clock(evidence.start)} — ${clock(evidence.end)} · ${method}${matchScore}`));
    const row = element('div', undefined, 'person-evidence-row');
    if (evidence.image_url) {const img = element('img');img.src = evidence.image_url;MindexI18n.setAttr(img, "alt", MindexI18n.msg("ui.2032116e6bcc", { p0: name }));img.loading = 'lazy';row.append(img);}
    const actions = element('div', undefined, 'person-evidence-actions');
    const locate = element('button', MindexI18n.msg("ui.51d89eeafe25"), 'quiet');locate.type = 'button';locate.onclick = () => {video.shotPlayer?.seek(Math.max(0, evidence.start - result.start));};
    const correct = correctionLink(result, evidence);
    actions.append(locate, correct);row.append(actions);section.append(row);target.append(section);
  }
  target.append(managementLink(result));
  $('detail-dialog').showModal();
}
function managementLink(result) {
  const link = element('a', MindexI18n.msg("ui.5c5434cf5afa"));
  const params = new URLSearchParams({ source: result.source_id });
  const shotId = result.kind === 'shot' ? result.id : result.kind === 'moment' ? result.parent_id : null;
  if (shotId) params.set('segment', shotId);
  link.href = `/admin?${params}`;link.target = '_blank';link.rel = 'noopener';return link;
}
function correctionLink(result, evidence = bestPersonEvidence(result.person_evidence)[0]) {
  const link = element('a', MindexI18n.msg("ui.review_appearance_link"));
  const params = new URLSearchParams();
  if (evidence) params.set('evidence', evidence.id);else
  {
    params.set('source', result.source_id);
    const shotId = result.kind === 'shot' ? result.id : result.kind === 'moment' ? result.parent_id : null;
    if (shotId) params.set('shot', shotId);
  }
  link.href = `/persons?${params}`;link.target = '_blank';link.rel = 'noopener';return link;
}
$('close-dialog').onclick = () => {$('detail-content').querySelector('video')?.pause();$('detail-dialog').close();};
$('detail-dialog').addEventListener('close', () => {$('detail-content').querySelector('video')?.pause();});

function render(data) {
  $('results').querySelectorAll('video').forEach(video => video.pause());
  $('results').replaceChildren();
  MindexI18n.setText($('result-count'), MindexI18n.msg("ui.411bdad5dc98", { p0: data.results.length }));
  MindexI18n.setText($('timing'), MindexI18n.msg("ui.7e3c096d9f68", { p0: (data.elapsed_ms / 1000).toFixed(2) }));
  if (!data.results.length) {
    const empty = element('div', undefined, 'empty');
    empty.append(element('h3', MindexI18n.msg("ui.231d915ea844")), element('p', MindexI18n.msg("ui.e51df7fcd1da")));
    $('results').append(empty);return;
  }
  data.results.forEach((result, index) => {
    const annotation = result.annotation || {};
    const card = element('article', undefined, 'card');
    const video = element('video');queueMicrotask(() => ShotPlayer.attach(video, `/api/playlists/${result.id}`));video.poster = result.thumbnail_url;video.controls = true;video.preload = 'none';
    const preview = element('div', undefined, 'card-preview');
    const score = element('span', `#${index + 1} · ${result.score.toFixed(4)}`, 'card-score');
    score.title = result.score.toFixed(5);preview.append(video, score);
    const body = element('div', undefined, 'card-body');
    const title = element('div', undefined, 'card-title');title.append(element('strong', result.source_name), element('span', `${(result.end - result.start).toFixed(1)}s`));
    const tags = element('div', undefined, 'tags');
    const channels = new Set((result.matches || []).map((match) => match.channel || (match.route === 'video' ? 'visual' : null)).filter(Boolean));
    // 同一个人物会在多个采样帧上留下出镜证据，标签按「演员 / 角色」去重后再展示。
    const personTags = [];
    for (const e of result.person_evidence || []) {
      const name = `${e.actor_name || MindexI18n.msg("ui.1b37b5539364")}${e.role_name ? ' / ' + e.role_name : ''}`;
      if (!personTags.includes(name)) personTags.push(name);
    }
    for (const name of personTags.slice(0, 3)) tags.append(element('span', name, 'tag'));
    channels.forEach((channel) => tags.append(element('span', routeNames[channel] || channel, 'tag')));
    [...(annotation.scene || []), ...(annotation.emotions || []), ...(annotation.shot_size || [])].slice(0, 5).forEach((tag) => tags.append(MindexAnalysisText.setText(element('span', undefined, 'tag'), tag)));
    const actions = element('div', undefined, 'card-actions');
    const detail = element('button', MindexI18n.msg("ui.faea8c1db9cc"));detail.type = 'button';detail.onclick = () => details(result);
    const similar = element('button', MindexI18n.msg('ui.groups_similar'));
    similar.type = 'button';
    similar.onclick = async () => {
      similar.disabled = true;
      try {
        const data = await MindexApi.json('/api/search/similar', 'POST', {
          segment_id: result.id, granularity: $('granularity').value,
          limit: Number($('limit').value), filters: filters()
        });
        render(data);
      } catch (error) { notice(error.message, 'error'); } finally { similar.disabled = false; }
    };
    actions.append(detail, similar, managementLink(result), correctionLink(result));
    const evidenceMatch = (result.matches || []).find((match) => match.matched_text);
    const evidenceText = evidenceMatch?.matched_text;
    const description = result.display_name || evidenceText || (result.annotation_status === 'not_analyzed' ? '' : annotation.description || '');
    body.append(title);
    if (description) {
      const content = element('p', undefined, 'card-description');
      const analysisEvidence = evidenceMatch?.channel === 'analysis' || ['description', 'style'].includes(evidenceMatch?.route);
      if (!result.display_name && (!evidenceText || analysisEvidence)) MindexAnalysisText.setText(content, description, {view: Boolean(evidenceText)});
      else MindexI18n.setText(content, description);
      body.append(content);
    }
    if (tags.childElementCount) body.append(tags);
    body.append(actions);
    card.append(preview, body);$('results').append(card);
  });
}
// Ctrl/⌘ + Enter 视为点击「查找片段」；按钮禁用（检索中）时 click() 不触发提交。
$('search-form').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
  event.preventDefault();$('search-button').click();
});
$('search-form').onsubmit = async (event) => {
  event.preventDefault();if (busy) return;
  if (!selectedValues('source').length) {notice(MindexI18n.msg("ui.search_source_required"), 'error');return;}
  const text = $('query').value.trim();const file = currentFile(mode);
  if (mode === 'text' && !text && !selectedValues('actor-select').length && !selectedValues('role-select').length) {notice(MindexI18n.msg("ui.c7e6fe9d1b56"), 'error');return;}
  if (mode === 'video' && !file) {notice(MindexI18n.msg("ui.41885fd88fc0"), 'error');return;}
  if (mode === 'image' && !file) {notice(MindexI18n.msg("ui.4ce8442dff31"), 'error');return;}
  busy = true;$('search-button').disabled = true;MindexI18n.setText($('search-button'), MindexI18n.msg("ui.5e04ab714544"));
  notice(MindexI18n.msg("ui.a4f7d2276e04"), 'info');
  try {
    let data;
    const common = { granularity: $('granularity').value, limit: Number($('limit').value), filters: filters() };
    if (mode === 'text') data = await api('/api/search/text', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, ...common, person_ids: [...new Set([...selectedValues('actor-select'), ...selectedValues('role-select')])], person_mode: $('person-mode').value, person_relation: $('person-relation').value }) });else
    {
      const form = new FormData();form.append('file', file);form.append('granularity', common.granularity);form.append('limit', common.limit);form.append('filters', JSON.stringify(common.filters));
      data = await api(`/api/search/${mode}`, { method: 'POST', body: form });
    }
    render(data);
    const personHints = [];
    if (data.recognized_people?.length) personHints.push(MindexI18n.msg("ui.bf052cc4d77b") + data.recognized_people.map((p) => `${personKinds[p.kind] || ''} ${p.name}`.trim()).join('、') + ' · ' + (data.person_mode === 'mentioned' ? MindexI18n.msg("ui.cbf394311140") : MindexI18n.msg("ui.c7218d155fa9")));
    if (data.referenced_people?.length) personHints.push(MindexI18n.msg("ui.b607edb41246", { p0: [...new Set(data.referenced_people.map(p => p.name))].join('、') }));
    MindexI18n.setText($('person-hint'), personHints.join('；'));
    const warnings = (data.warnings || []).map(MindexI18n.system);
    if (data.person_coverage_missing?.length) warnings.push(MindexI18n.msg("ui.575976c4be6b", { p0: data.person_coverage_missing.length }));
    notice(warnings.join('；'), warnings.length ? 'info' : '');
  } catch (error) {notice(error.message, 'error');} finally
  {busy = false;$('search-button').disabled = false;MindexI18n.setText($('search-button'), MindexI18n.msg("ui.00462d7b748d"));}
};
// 多选框顶部的“全部选择”：选中它等同全选，手动取消任意一项时它同步取消。
const ALL_VALUE = '__all__';
function allOption(select) {return select.querySelector(`option[value="${ALL_VALUE}"]`);}
function itemOptions(select) {return Array.from(select.options).filter((o) => o.value !== ALL_VALUE);}
function syncAll(select) {
  const all = allOption(select);if (!all) return;
  const items = itemOptions(select);
  all.selected = items.length > 0 && items.every((o) => o.selected);
  select.dataset.allSelected = all.selected ? '1' : '0';
}
function applyAll(select, on) {for (const o of itemOptions(select)) o.selected = on;syncAll(select);}
// change 时无法区分是点了“全部选择”还是普通项，用上一次的全选状态判断意图。
function toggleAll(select) {
  const all = allOption(select), wasAll = select.dataset.allSelected === '1';
  if (all?.selected && !wasAll) applyAll(select, true);
  else if (all && !all.selected && wasAll && itemOptions(select).every((o) => o.selected)) applyAll(select, false);
  else syncAll(select);
}
function bindAllToggle(id, after) {$(id).addEventListener('change', () => {toggleAll($(id));if (after) after();});}
function selectedValues(id) {return Array.from($(id).selectedOptions).map((o) => o.value).filter((v) => v !== ALL_VALUE);}
function fillOptions(id, entries) {
  const select = $(id);
  const selectAll = id === 'source' && sourceScope === null;
  const chosen = new Set(selectAll ? entries.map((entry) => entry.id) : id === 'source' ? sourceScope : selectedValues(id));
  select.dataset.initialized = '1';select.replaceChildren();
  const all = element('option', MindexI18n.msg("ui.3e26ad9407ad"));all.value = ALL_VALUE;select.append(all);
  for (const entry of MindexMultiSearch.sort(entries)) {const option = element('option', `${entry.name} · ${entry.id.slice(0, 8)}`);option.value = entry.id;option.selected = chosen.has(entry.id);select.append(option);}
  MindexMultiSearch.attach(select);
  MindexMultiSearch.refilter(select);
  syncAll(select);
}
function searchFilterEntries(id, useRoles = true) {
  const actors = new Set(selectedValues('actor-select'));
  const roles = new Set(useRoles ? selectedValues('role-select') : []);
  const availableSources = registeredSources.filter((item) => groupSourceScope === null || groupSourceScope.has(item.id));
  const sources = new Set(sourceScope === null ? availableSources.map((item) => item.id) : sourceScope);
  const scoped = sourceScope !== null || groupSourceScope !== null;
  const works = new Set(availableSources.filter((s) => sources.has(s.id)).map((s) => s.work_id));
  const matches = registeredPeople.filter((e) => e.kind === 'role' &&
    (id === 'source' || !scoped || (e.work_ids || []).some((w) => works.has(w))) &&
    (id === 'actor-select' || !actors.size || (e.actor_ids || []).some((a) => actors.has(a))) &&
    (id === 'role-select' || !roles.size || roles.has(e.id)));
  if (id === 'role-select') return matches;
  if (id === 'actor-select') {
    const ids = new Set(matches.flatMap((r) => r.actor_ids || []));
    return registeredPeople.filter((e) => e.kind === 'actor' && (!scoped && !roles.size || ids.has(e.id)));
  }
  if (mode !== 'text' || !actors.size && !roles.size) return availableSources;
  const ids = new Set(matches.flatMap((r) => r.work_ids || []));
  return availableSources.filter((s) => ids.has(s.work_id));
}
function fillSearchFilters(changed = 'source') {
  // First remove selections invalidated by the changed control, then update the other facets.
  if (changed === 'source') {
    fillOptions('actor-select', searchFilterEntries('actor-select', false));
    fillOptions('role-select', searchFilterEntries('role-select'));
  } else if (changed === 'actor-select') {
    fillOptions('role-select', searchFilterEntries('role-select'));
  } else {
    fillOptions('actor-select', searchFilterEntries('actor-select'));
  }
  fillOptions('source', searchFilterEntries('source'));
  fillOptions('actor-select', searchFilterEntries('actor-select'));
  fillOptions('role-select', searchFilterEntries('role-select'));
}
$('filter-reset').onclick = () => {
  searchGroups.setValues([]);
  sourceScope = null; groupSourceScope = null;
  void searchGroupLink.refresh();
  for (const [id, value] of Object.entries({ 'person-mode': 'auto', 'person-relation': 'all', granularity: 'moment', limit: '20', scene: '', emotion: '', 'shot-size': '', 'min-duration': '', 'max-duration': '' })) $(id).value = value;
  for (const id of ['actor-select', 'role-select', 'source']) {
    applyAll($(id), id === 'source');
    const search = $(id).closest('.multi-select').querySelector('.multi-search');
    if (search) search.value = '';
    $(id).dataset.query = '';
    MindexMultiSearch.refilter($(id));
  }
  fillOptions('source', registeredSources);
  applyAll($('source'), true);
  fillSearchFilters();
  MindexI18n.setText($('person-hint'), MindexI18n.msg("ui.adb050b9cc83"));
};
async function refresh() {
  $('refresh').disabled = true;
  try {
    const filtersReady = api('/api/persons/filters').then(async (catalogue) => {
      registeredPeople = catalogue.entities;
      registeredSources = catalogue.sources;
      if (sourceScope !== null) {
        sourceScope = sourceScope.filter((id) => registeredSources.some((s) => s.id === id));
      }
      await searchGroupLink.refresh();
    });
    const [data] = await Promise.all([api('/api/status?health=true&summary=true'), filtersReady]);
    const count = data.segment_counts.find((item) => item.kind === 'moment')?.count || 0;
    MindexI18n.setText($('library-summary'), MindexI18n.msg("ui.7859fa5eb7a3", { p0: data.sources.length, p1: count }));
    const target = $('library-detail');target.replaceChildren();
    target.append(element('div', data.active_index ? MindexI18n.msg("ui.2f21466172b8", { p0: data.active_index.id }) : MindexI18n.msg("ui.f8ebfd03d1ee")));
    if (data.active_index?.spec.mode === 'visual') target.append(element('div', MindexI18n.msg("ui.53ece886f7d6")));
    target.append(element('div', data.model_health.ready ? data.model_health.matches_index ? MindexI18n.msg("ui.c3bd1bed562d") : MindexI18n.msg("ui.30f5ee45740b") : MindexI18n.msg("ui.5b380045c7b4")));
    if (MindexI18n.system(data.model_health.error)) target.append(element('div', MindexI18n.system(data.model_health.error)));
    if (data.runs.length) target.append(element('div', MindexI18n.msg("ui.7faf1b9a0a32", { p0: data.runs[0].stage, p1: data.runs[0].status })));
    if (!data.active_index) notice(MindexI18n.msg("ui.07cd274a859d"), 'info');
  } catch (error) {MindexI18n.setText($('library-summary'), MindexI18n.msg("ui.d9f607a20068"));notice(error.message, 'error');} finally
  {$('refresh').disabled = false;}
}
$('refresh').onclick = refresh;
for (const id of ['actor-select', 'role-select', 'source']) bindAllToggle(id, () => {
  if (id === 'source') {const ids = selectedValues(id);sourceScope = ids;}
  fillSearchFilters(id);
});
refresh();

$('query').addEventListener('input', () => {
  const text = $('query').value;
  const names = registeredPeople.filter((p) => [p.name, ...p.aliases].some((name) => text.toLocaleLowerCase().includes(name.toLocaleLowerCase())));
  MindexI18n.setText($('person-hint'), names.length ? MindexI18n.msg("ui.4759bc4a8e9a") + names.map((p) => `${personKinds[p.kind] || p.kind} ${p.name} · ${p.id.slice(0, 8)}`).join('、') + MindexI18n.msg("ui.58b4558e0c39") : MindexI18n.msg("ui.e18397481723"));
});
// 智能助手：上下文提供当前检索词；定位时填入检索词并执行（被动定位只高亮，不替用户检索）。
window.MindexAgentContext = () => ({ page: 'search', mode, query: $('query').value.trim() || null, source_group_ids: searchGroups.values() });
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  const mark = (element) => window.MindexAgent?.highlight(element);
  if (!focus.query?.text || passive) {
    mark(document.querySelector('.search-panel') || $('search-form'));
    return true;
  }
  if (!$('source').options.length || busy) return false; // 素材选项尚未加载或正在检索
  if (mode !== 'text') switchMode('text');
  if (focus.query.filters) {
    const values = focus.query.filters;
    searchGroups.setValues(values.source_group_ids || []);
    sourceScope = values.source_ids?.length ? values.source_ids : null;
    await searchGroupLink.refresh();
    for (const [key, id] of [['scene', 'scene'], ['emotion', 'emotion'], ['shot_size', 'shot-size'], ['min_duration', 'min-duration'], ['max_duration', 'max-duration']]) {
      $(id).value = values[key] ?? '';
    }
  }
  $('query').value = focus.query.text;
  if (focus.query.granularity && [...$('granularity').options].some((o) => o.value === focus.query.granularity))
    $('granularity').value = focus.query.granularity;
  $('search-form').requestSubmit();
  mark(document.querySelector('.results-section'));
  return true;
};
