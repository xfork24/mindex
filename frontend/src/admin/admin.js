const $ = (id) => document.getElementById(id);
const state = { source: null, sourcePage: 1, sourceTotal: 0, segmentPage: 1, segmentTotal: 0, kind: 'shot', parent: null, editing: null, focus: null };
let sourceRequest = 0;
let segmentRequest = 0;
let selectionRequest = 0;
let selectionController = null;
let segmentController = null;
let workRequest = 0;

function node(tag, text, className) {
  const n = document.createElement(tag);if (text !== undefined) MindexI18n.setText(n, text);if (className) n.className = className;return n;
}
function notify(text = '', error = false) {
  if (globalThis.MindexFeedback) return globalThis.MindexFeedback.show('admin-notice', error ? MindexI18n.system(text) : text, error ? 'error' : 'info');
  MindexI18n.setText($('admin-notice'), error ? MindexI18n.system(text) : text);$('admin-notice').className = `notice ${error ? 'error' : 'info'}`;}
function time(value) {
  const hours = Math.floor(value / 3600),minutes = Math.floor(value % 3600 / 60),seconds = (value % 60).toFixed(2).padStart(5, '0');
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${seconds}`;
}
function errorText(detail) {return Array.isArray(detail) ? detail.map((e) => e.msg).join('；') : typeof detail === 'string' ? detail : JSON.stringify(detail);}
async function api(url, options = {}) {return MindexApi.request(url, options);}
function params(values) {return new URLSearchParams(Object.entries(values).filter(([, v]) => v !== null && v !== undefined));}
function pager(prefix, page, total) {MindexI18n.setText($(`${prefix}-page`), `${page} / ${Math.max(1, Math.ceil(total / 48))}`);$(`${prefix}-prev`).disabled = page <= 1;$(`${prefix}-next`).disabled = page * 48 >= total;}
function pauseList() {$('segment-list').querySelectorAll('video').forEach((v) => v.pause());}

const sourcePanel = document.querySelector('.source-panel');
function sizeSourcePanel() {
  if (matchMedia('(max-width:750px)').matches) {sourcePanel.style.removeProperty('--source-panel-height');return;}
  const top = Math.max(0, sourcePanel.getBoundingClientRect().top);
  sourcePanel.style.setProperty('--source-panel-height', `${Math.max(0, innerHeight - top)}px`);
}
let sourcePanelFrame = null;
function scheduleSourcePanelSize() {
  if (sourcePanelFrame !== null) return;
  sourcePanelFrame = requestAnimationFrame(() => {sourcePanelFrame = null;sizeSourcePanel();});
}
addEventListener('resize', scheduleSourcePanelSize);
addEventListener('scroll', scheduleSourcePanelSize, {passive:true});
new ResizeObserver(scheduleSourcePanelSize).observe(document.querySelector('.admin-intro'));
new ResizeObserver(scheduleSourcePanelSize).observe(document.querySelector('header'));
new ResizeObserver(scheduleSourcePanelSize).observe($('admin-notice'));
scheduleSourcePanelSize();

// 素材组筛选：与检索页同款可搜索多选列表框，默认全选（=不限），部分选择后按组过滤左侧素材列表（服务端 source_group_ids，空组结果为空）。
const sourceGroups = MindexGroups.picker("source", [], () => sourceFiltersChanged(), "ui.groups_filter", {listbox: true, defaultAll: true});
$('source-group-filter').append(sourceGroups.wrap);
window.addEventListener('mindex:groups-changed', () => { void sourceGroups.refresh(); });

async function loadSources() {
  const request = ++sourceRequest;
  const search = params({ q: $('source-query').value.trim(), visibility: $('source-visibility').value, favorite_only: $('source-favorite').checked, page: state.sourcePage });
  for (const id of sourceGroups.values()) search.append('source_group_ids', id);
  const data = await api(`/api/admin/sources?${search}`);
  if (request !== sourceRequest) return;
  if (state.sourcePage > 1 && !data.items.length) {state.sourcePage--;return loadSources();}
  state.sourceTotal = data.total;MindexI18n.setText($('source-count'), MindexI18n.msg("ui.42d218e9f146", { p0: data.total }));$('source-list').replaceChildren();
  data.items.forEach((source) => {
    const b = node('button', undefined, `source-item ${state.source?.id === source.id ? 'selected' : ''}`);b.type = 'button';b.setAttribute('aria-pressed', String(state.source?.id === source.id));
    b.append(node('strong', `${source.favorite ? '★ ' : ''}${source.name}`), node('small', MindexI18n.msg("ui.b53548f018e9", { p0: source.shot_count, p1: source.moment_count, p3: source.scene_count ?? 0 })), node('small', `${time(source.media.duration)} · ${source.hidden ? MindexI18n.msg("ui.c4a3685c02e7") : ''}${source.prepared ? MindexI18n.msg("ui.8be4f689a4da") : MindexI18n.msg("ui.db386b30e280")} · ${source.id.slice(0, 8)}`));
    b.onclick = () => run(() => selectSource(source.id));$('source-list').append(b);
  });
  if (!data.items.length) {const empty = node('div', undefined, 'empty');empty.append(node('p', MindexI18n.msg("ui.67a30427bf95")), node('p', MindexI18n.msg("ui.884adf6787b5")));$('source-list').append(empty);}
  pager('source', state.sourcePage, data.total);
}

function action(text, onClick, pressed) {
  const b = node('button', text, 'quiet');b.type = 'button';if (pressed !== undefined) b.setAttribute('aria-pressed', String(pressed));
  b.onclick = () => run(async () => {b.disabled = true;try {await onClick();} finally {b.disabled = false;}});return b;
}
function actions(kind, item) {
  const bar = node('div', undefined, 'organization-actions');
  if (kind === 'segment') bar.append(action(MindexI18n.msg("ui.4f55ee1e687f"), () => showAnalysis(item)));
  bar.append(action(MindexI18n.msg("ui.e0361480e3a5"), () => edit(kind, item)), action(item.favorite ? MindexI18n.msg("ui.f9492c56041d") : MindexI18n.msg("ui.c028b230faad"), () => patch(kind, item.id, { favorite: !item.favorite }), item.favorite), action(item.hidden ? MindexI18n.msg("ui.0a14044d1cbd") : MindexI18n.msg("ui.bb0e7e01aa8d"), () => patch(kind, item.id, { hidden: !item.hidden }), item.hidden));
  if (kind === 'source') bar.append(action(MindexI18n.msg("ui.3755f56f2f83"), () => deleteSource(item)));
  return bar;
}
async function deleteSource(item) {
  const message = [MindexI18n.msg("ui.9dff42a2b590", { p0:
    item.name }), ``, MindexI18n.msg("ui.78bbf6595f53", { p0:
    item.shot_count, p1: item.moment_count }), MindexI18n.msg("ui.8d221c85535e", {})].

  join('\n');
  if (!confirm(MindexI18n.render(message))) return;
  const summary = await api(`/api/admin/sources/${item.id}`, { method: 'DELETE' });
  clearTimeout(workPoll);++selectionRequest;++segmentRequest;++workRequest;selectionController?.abort();segmentController?.abort();state.source = null;state.parent = null;state.focus = null;
  $('source-workspace').hidden = true;$('no-source').hidden = false;syncUrl();
  await loadSources();
  notify(MindexI18n.msg("ui.e3bba6911f26", { p0: summary.deleted.segments, p1: summary.deleted.vectors, p2: summary.deleted.files, p3: summary.kept.source_file }));
}
function renderSource(source) {
  const target = $('source-detail');target.replaceChildren();
  if (!source.media) {renderWork(source, target);return;}
  const heading = node('div', undefined, 'source-heading');heading.append(node('h2', source.name));
  if (source.hidden) heading.append(node('span', MindexI18n.msg("ui.5fce4fb22d1d"), 'hidden-badge'));
  target.append(heading);
  MindexGroups.membership(target, "source", source);
  if (source.notes) target.append(node('p', source.notes, 'source-notes'));
  target.append(node('p', MindexI18n.msg("ui.b1a5c99af4bf", { p0: source.original_name, p1: source.id.slice(0, 8) }), 'source-meta'), node('p', MindexI18n.msg("ui.9df908393a6a", { p0: time(source.media.duration), p1: source.media.width, p2: source.media.height, p3: source.media.fps.toFixed(2), p4: source.shot_count, p5: source.moment_count }), 'source-meta'), node('p', source.prepared ? MindexI18n.msg("ui.db093aeb92d3") : MindexI18n.msg("ui.be2019fc93ba"), 'source-meta'));
  if (source.missing_artifacts) target.append(node('p', MindexI18n.msg("ui.95c85cb38091", { p0: source.missing_artifacts }), 'source-warning'));
  target.append(actions('source', source));
  renderWork(source, target);
  if (source.hidden) target.append(node('p', MindexI18n.msg("ui.5968052b59f1"), 'muted'));
}

async function selectSource(id) {
  const request = ++selectionRequest;++segmentRequest;++workRequest;
  clearTimeout(workPoll);workPoll = null;
  selectionController?.abort();segmentController?.abort();
  selectionController = new AbortController();
  const signal = selectionController.signal;
  state.source = {id};state.parent = null;state.segmentPage = 1;state.focus = null;
  $('segment-visibility').value = 'visible';pauseList();$('segment-list').replaceChildren();
  $('source-detail').replaceChildren();renderWork(state.source, $('source-detail'));
  $('no-source').hidden = true;$('source-workspace').hidden = false;syncUrl();
  $('source-workspace').setAttribute('aria-busy', 'true');
  if (preferences(id).workExpanded) void refreshWork(id, request);
  try {
    await Promise.all([
      api(`/api/admin/sources/${id}?detail=summary`, {signal}).then(async (source) => {
        if (request !== selectionRequest) return;
        if (!state.source.work) {state.source = {...source, statusError: state.source.statusError};renderSource(state.source);}
        if (source.hidden && $('segment-visibility').value !== 'all') {
          $('segment-visibility').value = 'all';await loadSegments();
        }
      }), loadSegments(), loadSources()
    ]);
  } catch (e) {if (e.name !== 'AbortError' && request === selectionRequest) throw e;}
  finally {if (request === selectionRequest) $('source-workspace').removeAttribute('aria-busy');}
}
function syncUrl() {history.replaceState(null, '', `/admin?${params({ source: state.source?.id, kind: state.kind, parent: state.parent?.id, segment: state.focus })}`);}
function syncTabs() {
  for (const [id, kind] of [['shots-tab', 'shot'], ['moments-tab', 'moment']]) {$(id).classList.toggle('active', state.kind === kind);$(id).setAttribute('aria-selected', String(state.kind === kind));}
  $('parent-filter').hidden = !state.parent;if (state.parent) MindexI18n.setText($('parent-label'), MindexI18n.msg("ui.6688020c0556", { p0: segmentName(state.parent) }));
}
function segmentName(item) {
  if (item.custom_name || item.custom_name === undefined) return item.name;
  return MindexI18n.msg(`ui.segment_default_${item.kind}`, {start: item.start.toFixed(3), end: item.end.toFixed(3)});
}
async function loadSegments() {
  if (!state.source) return;const request = ++segmentRequest;syncTabs();syncUrl();
  segmentController?.abort();segmentController = new AbortController();
  const data = await api(`/api/admin/sources/${state.source.id}/segments?${params({ kind: state.kind, parent_id: state.parent?.id, visibility: $('segment-visibility').value, favorite_only: $('segment-favorite').checked, page: state.segmentPage })}`, {signal: segmentController.signal}).catch((e) => {if (e.name !== 'AbortError') throw e;return null;});
  if (!data) return;
  if (request !== segmentRequest) return;
  if (state.segmentPage > 1 && !data.items.length) {state.segmentPage--;return loadSegments();}
  state.segmentTotal = data.total;MindexI18n.setText($('segment-count'), MindexI18n.msg("ui.f6141ac5b67e", { p0: data.total, p1: state.kind === 'shot' ? MindexI18n.msg("ui.c7d1925fd5af") : MindexI18n.msg("ui.5aca155225fc") }));
  pauseList();$('segment-list').replaceChildren();
  data.items.forEach((item) => {
    const card = node('article', undefined, `segment-card ${item.effective_hidden ? 'is-hidden' : ''} ${state.focus === item.id ? 'is-focused' : ''}`);card.dataset.segmentId = item.id;
    const poster = node('button', undefined, 'poster-button');poster.type = 'button';MindexI18n.setAttr(poster, 'aria-label', MindexI18n.msg("ui.c61073cbe212", { p0: segmentName(item) }));
    const image = node('img');image.src = item.thumbnail_url;MindexI18n.setAttr(image, "alt", segmentName(item));image.loading = 'lazy';
    poster.append(image, node('span', '▶', 'play-symbol'));poster.onclick = () => play(item);
    const body = node('div', undefined, 'segment-body');body.append(node('h3', segmentName(item)), node('p', MindexI18n.msg("ui.a8e728244086", { p0: time(item.start), p1: time(item.end), p2: item.duration.toFixed(2) }), 'segment-time'));
    if (item.effective_hidden) body.append(node('span', item.inherited_hidden ? MindexI18n.msg("ui.9cfcc90a06a1") : MindexI18n.msg("ui.5fce4fb22d1d"), 'hidden-badge'));
    if (item.notes) body.append(node('p', item.notes, 'segment-note'));
    body.append(actions('segment', item));
    if (item.inherited_hidden) body.append(node('p', MindexI18n.msg("ui.58926140f603"), 'muted'));
    const links = node('div', undefined, 'relationship');
    if (item.kind === 'shot') links.append(action(MindexI18n.msg("ui.8427ef7b1fc2", { p0: item.child_count }), async () => {state.kind = 'moment';state.parent = item;state.segmentPage = 1;await loadSegments();}));else
    if (item.parent_id) links.append(action(MindexI18n.msg("ui.f982f961d8a1"), async () => {const parent = await api(`/api/admin/segments/${item.parent_id}`);state.kind = 'shot';state.parent = null;state.focus = parent.id;state.segmentPage = Math.floor((parent.position - 1) / 48) + 1;$('segment-visibility').value = 'all';$('segment-favorite').checked = false;await loadSegments();$('segment-list').querySelector('.is-focused')?.scrollIntoView({ block: 'center', behavior: 'smooth' });}));
    // 出镜审核：在人物库打开该分镜的出镜人物审核；精细片段审核其所属分镜。
    const shotId = item.kind === 'shot' ? item.id : item.parent_id;
    if (shotId) {const review = node('a', MindexI18n.msg("ui.review_appearance_link"));review.href = `/persons?${params({ source: state.source.id, shot: shotId })}`;review.target = '_blank';review.rel = 'noopener';links.append(review);}
    body.append(links);card.append(poster, body);$('segment-list').append(card);
  });
  if (!data.items.length) {const empty = node('div', undefined, 'empty');empty.append(node('h3', MindexI18n.msg("ui.dd8121f6916e")), node('p', MindexI18n.msg("ui.c6c1305ed0ef")));$('segment-list').append(empty);}
  pager('segment', state.segmentPage, data.total);
}

function edit(kind, item) {state.editing = { kind, id: item.id };MindexI18n.setText($('edit-title'), kind === 'source' ? MindexI18n.msg("ui.33f127b82e7d") : MindexI18n.msg("ui.9f0fe854bb6c"));$('edit-name').value = item.custom_name || '';$('edit-notes').value = item.notes || '';MindexI18n.setText($('edit-error'), '');$('edit-dialog').showModal();$('edit-name').focus();}
async function patch(kind, id, changes) {
  const sourceId = state.source?.id, selection = selectionRequest;
  await api(`/api/admin/${kind === 'source' ? 'sources' : 'segments'}/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });
  if (state.source?.id !== sourceId || selection !== selectionRequest) return;
  await refreshWork(sourceId, selection);
  if (state.source?.id !== sourceId || selection !== selectionRequest) return;
  await Promise.all([loadSources(), loadSegments()]);notify(MindexI18n.msg("ui.963001b5d8c2"));
}
$('edit-form').onsubmit = async (event) => {
  event.preventDefault();const editing = state.editing;if (!editing) return;$('edit-save').disabled = true;MindexI18n.setText($('edit-error'), '');
  try {await patch(editing.kind, editing.id, { display_name: $('edit-name').value.trim() || null, notes: $('edit-notes').value });$('edit-dialog').close();}
  catch (e) {MindexI18n.setText($('edit-error'), e.message);} finally {$('edit-save').disabled = false;}
};
$('edit-close').onclick = () => $('edit-dialog').close();
function play(item) {MindexI18n.setText($('play-title'), segmentName(item));ShotPlayer.attach($('play-video'), `/api/admin/segments/${item.id}/playlist`);$('play-video').poster = item.thumbnail_url;MindexI18n.setText($('play-time'), MindexI18n.msg("ui.0159b6eb9834", { p0: time(item.start), p1: time(item.end), p2: item.source_name }));$('play-download').href = `${item.media_url}?download=true&v=${item.media_revision}`;if (!$('play-dialog').open) $('play-dialog').showModal();}
$('play-close').onclick = () => $('play-dialog').close();
$('play-dialog').addEventListener('close', () => {$('play-video').pause();$('play-video').removeAttribute('src');$('play-video').load();});
async function run(task) {try {await task();} catch (e) {if (e.name !== 'AbortError') notify(e.message, true);}}
function sourceFiltersChanged() {state.sourcePage = 1;return loadSources();}
function segmentFiltersChanged() {state.segmentPage = 1;return loadSegments();}
$('source-search').onsubmit = (e) => {e.preventDefault();run(sourceFiltersChanged);};
for (const id of ['source-visibility', 'source-favorite']) $(id).onchange = () => run(sourceFiltersChanged);
for (const id of ['segment-visibility', 'segment-favorite']) $(id).onchange = () => run(segmentFiltersChanged);
$('source-prev').onclick = () => run(async () => {state.sourcePage--;await loadSources();});$('source-next').onclick = () => run(async () => {state.sourcePage++;await loadSources();});
$('segment-prev').onclick = () => run(async () => {state.segmentPage--;await loadSegments();});$('segment-next').onclick = () => run(async () => {state.segmentPage++;await loadSegments();});
for (const [id, kind] of [['shots-tab', 'shot'], ['moments-tab', 'moment']]) $(id).onclick = () => run(async () => {state.kind = kind;state.parent = null;state.segmentPage = 1;await loadSegments();});
$('clear-parent').onclick = () => run(async () => {state.parent = null;state.segmentPage = 1;await loadSegments();});
$('reload').onclick = () => run(async () => {$('reload').disabled = true;workLog.snapshots = new Map();workPreferences.forEach(prefs => {prefs.errors = {};});try {if (state.source) await selectSource(state.source.id);else await loadSources();notify(MindexI18n.msg("ui.88e448fddfc8"));} finally {$('reload').disabled = false;}});
run(async () => {
  await loadSources();const query = new URLSearchParams(location.search);
  if (query.get('kind') === 'moment') state.kind = 'moment';const id = query.get('source');
  if (id) {
    await selectSource(id);const parent = query.get('parent'),segment = query.get('segment');
    if (segment) {
      await focusSegment(id, segment);
    } else if (parent) {
      const item = await api(`/api/admin/segments/${parent}`);if (item.source_id !== id || item.kind !== 'shot') throw new Error(MindexI18n.msg("ui.ea6b2bd87cda"));
      state.kind = 'moment';state.parent = item;await loadSegments();
    }
  }
});

// 打开素材下的指定镜头或片段并滚动到它；启动参数与智能助手定位共用。
async function focusSegment(sourceId, segmentId) {
  const item = await api(`/api/admin/segments/${segmentId}`);
  if (item.source_id !== sourceId || !['shot', 'moment'].includes(item.kind)) throw new Error(MindexI18n.msg("ui.1f054c110e3f"));
  state.kind = item.kind;state.parent = null;state.focus = item.id;state.segmentPage = Math.floor((item.position - 1) / 48) + 1;
  $('segment-visibility').value = 'all';$('segment-favorite').checked = false;await loadSegments();
  const focused = $('segment-list').querySelector('.is-focused');
  if (window.MindexAgent && focused) window.MindexAgent.highlight(focused);
  else focused?.scrollIntoView({ block: 'center', behavior: 'smooth' });
}
window.MindexAgentContext = () => ({ page: 'admin', source_id: state.source?.id || null, source_group_ids: sourceGroups.values() });
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  const mark = (element) => window.MindexAgent?.highlight(element);
  if (!focus.source_id) {mark(document.querySelector('.source-panel') || $('source-list'));return true;}
  if (passive && state.source?.id !== focus.source_id) return true;
  if (state.source?.id !== focus.source_id) await selectSource(focus.source_id);
  if (focus.segment_id) await focusSegment(focus.source_id, focus.segment_id);
  else mark($('source-workspace'));
  return true;
};

let workPoll = null;
const workPreferences = new Map();
const workNames = { shots: MindexI18n.msg("ui.6a4811595787"), dialogue: MindexI18n.msg("ui.d2126f3ca6e8"), analysis: MindexI18n.msg("ui.5484a583c2c9"), persons: MindexI18n.msg("ui.0c0ae35b2935") };
const workStatus = { complete: MindexI18n.msg("ui.9e593295eddf"), partial: MindexI18n.msg("ui.53951802211e"), empty: MindexI18n.msg("ui.b5a5666d7068"), no_audio: MindexI18n.msg("ui.dde19df44760"), disabled: MindexI18n.msg("ui.8bb38ef00ccc"), not_processed: MindexI18n.msg("ui.59a9eb4e6574"), failed: MindexI18n.msg("ui.3e3c8068bb0e"), processing: MindexI18n.msg("ui.fcb979ef0b91"), pending_alignment: MindexI18n.msg("ui.db56f1b7b2c7") };
const taskStatus = { queued: MindexI18n.msg("ui.4dcbbcfa6154"), running: MindexI18n.msg("ui.594249700590"), complete: MindexI18n.msg("ui.33246f6a5e5b"), failed: MindexI18n.msg("ui.3e3c8068bb0e"), interrupted: MindexI18n.msg("ui.8dd5ad764e4b"), cancelled: MindexI18n.msg("ui.a5ffdc95eeb0") };
const stages = { detect: MindexI18n.msg("ui.91642f5bbeab"), split: MindexI18n.msg("ui.717b1c7595ac"), repair: MindexI18n.msg("ui.58723ed9df0b"), subtitle: MindexI18n.msg("ui.1721b0103d0d"), asr: MindexI18n.msg("ui.65aa0b969118"), analyze: MindexI18n.msg("ui.b6582c9a3f55"), scene_grouping: MindexI18n.msg("ui.cd2b1a5f7983"), scenes: MindexI18n.msg("ui.008acfbd3a5f"), matching: MindexI18n.msg("ui.525d34888b72"), extracting: MindexI18n.msg("ui.7d76ffac1b58"), publish: MindexI18n.msg("ui.77e695de5cac"), embedding: MindexI18n.msg("ui.c56c156ecf6c"), index_check: MindexI18n.msg("ui.7e5154331f03"), published: MindexI18n.msg("ui.fb01358330b1"), indexed: MindexI18n.msg("ui.efc142bf91f3") };
function preferences(id) {if (!workPreferences.has(id)) workPreferences.set(id, { publish: { shots: true, dialogue: true, analysis: true, persons: true }, errors: {}, method: 'auto', track: '', subtitleInitialized: false, uploadingSubtitle: false });return workPreferences.get(id);}
function post(url, value) {return api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });}
const publicationStages = new Set(['index_wait', 'index_prepare', 'embedding', 'index_check', 'publish', 'published', 'indexed', 'person_index']);
function analysisCoverageText(result = {}, task) {
  let text = MindexI18n.msg('ui.work_analysis_coverage', {done: result.analyzed || 0, total: result.total || 0});
  const reasons = result.skipped_reasons || [];
  for (const item of reasons) text += '，' + MindexI18n.msg('ui.analysis_skipped_reason', {count: item.count, reason: MindexI18n.system(item.reason_message) || item.reason || MindexI18n.msg('ui.analysis_reason_unknown')});
  const unclassified = Math.max(0, (result.skipped || 0) - reasons.reduce((sum, item) => sum + item.count, 0));
  if (unclassified) text += '，' + MindexI18n.msg('ui.analysis_skipped_reason', {count: unclassified, reason: MindexI18n.msg('ui.analysis_reason_unknown')});
  const pending = result.pending ?? Math.max(0, (result.total || 0) - (result.analyzed || 0) - (result.skipped || 0));
  if (pending) text += '，' + (['failed', 'interrupted', 'cancelled'].includes(task?.status) && MindexI18n.system(task.error)
    ? MindexI18n.msg('ui.analysis_unfinished_reason', {count: pending, reason: MindexI18n.system(task.error)})
    : MindexI18n.msg('ui.analysis_pending_count', {count: pending}));
  return text;
}
// Persistent subtask coverage survives reloads and new execution records.
function workProgressRows(name, work) {
  const result = work.result || {}, search = work.search || {};
  const task = work.task, progress = task?.progress || {};
  const publishing = progress.phase === 'publish' || publicationStages.has(progress.stage);
  const processing = publishing ? progress.processing_progress || {} : progress;
  let total = name === 'shots' ? result.shots || 0 : name === 'analysis' ? result.total || 0 : name === 'persons' ? result.total_shots || 0 : result.utterances || 0;
  let done = name === 'shots' ? (result.completed_shots ?? (work.status === 'complete' ? total : 0)) : name === 'analysis' ? (result.analyzed || 0) + (result.skipped || 0) : name === 'persons' ? result.covered_shots || 0 : ['complete', 'no_audio', 'empty'].includes(work.status) ? total : 0;
  const running = task && ['queued', 'running'].includes(task.status) && !publishing;
  if (running && work.status !== 'complete' && name !== 'analysis') {
    const liveTotal = processing.total ?? processing.total_shots ?? processing.total_chunks;
    const liveDone = processing.completed ?? processing.completed_shots ?? processing.completed_chunks;
    if (Number.isFinite(liveTotal) && liveTotal > 0) {total = liveTotal;done = Math.max(done, liveDone || 0);}
  }
  const rows = [{task, progress: processing, type: 'process', key: `ui.work_${name}_coverage`, done, total, complete: ['complete', 'no_audio', 'empty'].includes(work.status), running}];
  for (const [type, coverage, independent] of [['publish', search, work.publish_task], ['scene', work.scene_search || {}, work.scene_publish_task]]) {
    if (type === 'scene' && name === 'persons') continue;
    const automatic = type === 'publish' && publishing ? task : null;
    const latest = automatic && ['queued', 'running'].includes(automatic.status) ? automatic : independent && (!automatic || independent.created_at >= automatic.created_at) ? independent : automatic;
    const active = latest && ['queued', 'running'].includes(latest.status);
    let count = coverage.embedded_segments ?? coverage.indexed_segments ?? 0, expected = coverage.expected_segments || 0;
    // Execution progress is already cumulative; reuse it within the same subtask.
    if (active && !coverage.current && latest.progress?.total === expected) count = Math.max(count, latest.progress.completed || 0);
    rows.push({coverage, task: latest, progress: latest?.progress || {}, type, key: type === 'scene' ? 'ui.work_scene_coverage' : name === 'persons' ? 'ui.work_person_index_coverage' : name === 'dialogue' ? 'ui.work_dialogue_index_coverage' : 'ui.work_shot_moment_coverage', done: count, total: expected, complete: coverage.current, running: active});
  }
  if (name === 'analysis') rows[0].text = analysisCoverageText(result, task);
  for (const row of rows) {
    const notes = [];
    if (row.type !== 'process') {
      for (const reason of row.coverage?.pending_reasons || []) notes.push(MindexI18n.msg(`ui.coverage_${reason.code}`, {count: reason.count}));
      if (row.coverage?.unpublished_segments) notes.push(MindexI18n.msg('ui.coverage_unpublished', {count: row.coverage.unpublished_segments}));
      if (name === 'persons' && row.done < row.total) notes.push(MindexI18n.msg(result.covered_shots ? 'ui.coverage_person_unpublished' : 'ui.coverage_person_unprocessed', {count: row.total - row.done}));
      if (!row.total) notes.push(MindexI18n.msg(work.status === 'no_audio' ? 'ui.coverage_no_audio' : work.status === 'disabled' ? 'ui.coverage_disabled' : ['empty', 'complete'].includes(work.status) ? 'ui.coverage_no_content' : 'ui.coverage_no_results'));
    } else if (name !== 'analysis') {
      if (name === 'shots' && result.missing_artifacts) notes.push(MindexI18n.msg('ui.coverage_missing_files', {count: result.missing_artifacts}));
      else if (name === 'persons' && row.done < row.total) notes.push(MindexI18n.msg('ui.coverage_person_unprocessed', {count: row.total - row.done}));
      else if (work.status === 'not_processed') notes.push(MindexI18n.msg('ui.coverage_not_started'));
      else if (work.status === 'no_audio') notes.push(MindexI18n.msg('ui.coverage_no_audio'));
      else if (work.status === 'disabled') notes.push(MindexI18n.msg('ui.coverage_disabled'));
      else if (work.status === 'empty') notes.push(MindexI18n.msg('ui.coverage_no_content'));
    }
    if (name === 'persons' && work.status !== 'complete') notes.push(...(work.setup?.missing || []).map(MindexI18n.system));
    if (notes.length) row.text = (row.text || MindexI18n.msg(row.key, {done: Math.min(row.done, row.total), total: row.total})) + '，' + notes.join('；');
  }
  return rows;
}
function workLog(row) {
  if (row.localError) return {message: row.localError, error: true};
  const task = row.task, progress = row.progress || {};
  if (!task) return null;
  const publishing = task.progress?.phase === 'publish' || publicationStages.has(task.progress?.stage);
  const active = !(row.type === 'process' && publishing) && ['queued', 'running'].includes(task.status);
  const publication = row.type !== 'process' ? task.publication : null;
  const lastLine = value => typeof value === 'string' ? value.split(/\r?\n/).filter(line => line.trim()).at(-1) || '' : value;
  const error = !active && ((row.type !== 'process' || !publishing) && MindexI18n.system(lastLine(task.error)) || publication?.status === 'failed' && MindexI18n.system(lastLine(publication.error)));
  const status = row.type === 'process' && publishing ? 'complete' : publication?.status === 'failed' ? 'failed' : task.status;
  const message = error || (active && task.status === 'queued' ? taskStatus.queued : (progress.latest_log ? MindexI18n.system(progress.latest_log) : '') || (progress.message || progress.message_key ? MindexI18n.system(progress) : '') || stages[progress.stage] || taskStatus[status]);
  // Persisted task output is a baseline, not a new log for this page session.
  // Only a subsequent message/status change makes this row's log visible.
  const snapshots = workLog.snapshots ||= new Map();
  const key = `${task.id}:${row.type}`;
  const signature = JSON.stringify([status, message, !!error]);
  if (!snapshots.has(key)) {snapshots.set(key, {signature, visible: false});return null;}
  const previous = snapshots.get(key);
  if (previous.signature !== signature) snapshots.set(key, {signature, visible: true});
  return snapshots.get(key).visible ? {message, error: !!error} : null;
}
function appendWorkProgress(card, label, row) {
  const done = Math.min(row.total, Math.max(0, row.done));
  const line = node('p', row.text || MindexI18n.msg(row.key, {done, total: row.total}), 'muted work-task-progress');line.dataset.phase = row.type;card.append(line);
  const bar = node('progress');bar.className = 'work-progress';bar.dataset.phase = row.type;
  bar.max = row.total || 1;bar.value = row.total ? done : row.complete ? 1 : 0;
  MindexI18n.setAttr(bar, 'aria-label', label + (row.text || MindexI18n.msg(row.key, {done, total: row.total})));card.append(bar);
  const log = workLog(row);
  const logLine = node('p', log?.message || '', `work-latest-log ${log?.error ? 'source-warning' : 'muted'}`);
  logLine.dataset.phase = row.type;logLine.setAttribute('role', 'status');card.append(logLine);
}
function analysisSummary(result, progress = {}) {
  // 旧后端仅计入已生成场景；利用已保存的场景进度补齐尚未生成的数量。
  const extraScenes = result.pending == null && progress.stage === 'scenes' ? Math.max(0, (progress.total || 0) - (result.scenes || 0)) : 0;
  const total = (result.total || 0) + extraScenes,scenes = (result.scenes || 0) + extraScenes;
  const pending = result.pending ?? Math.max(0, total - (result.analyzed || 0) - (result.skipped || 0));
  return MindexI18n.msg("ui.d57be30db383", {p0: result.analyzed || 0, p1: total, p2: scenes})
    + (result.skipped ? ' · ' + MindexI18n.msg("ui.82896d0a6ff4", {count: result.skipped}) : '')
    + (pending ? ' · ' + MindexI18n.msg("ui.44a52eb27973", {count: pending}) : '');
}
function renderWork(source, target) {
  clearTimeout(workPoll);workPoll = null;
  const panel = node('details', undefined, 'work-panel');
  const toolbar = node('summary', undefined, 'work-toolbar');toolbar.append(node('h3', MindexI18n.msg("ui.c057f7d9d46e")));
  const cachePrefs = preferences(source.id);
  const refreshCache = action(MindexI18n.msg(cachePrefs.refreshingCache ? 'ui.work_cache_refreshing' : 'ui.work_cache_refresh'), () => run(() => refreshWorkCache(source.id)));
  refreshCache.disabled = !!cachePrefs.refreshingCache;
  refreshCache.addEventListener('click', (event) => {event.preventDefault();event.stopPropagation();});
  toolbar.append(refreshCache);panel.append(toolbar);target.append(panel);
  panel.open = !!cachePrefs.workExpanded;
  panel.ontoggle = () => {
    if (!panel.isConnected || state.source?.id !== source.id || cachePrefs.workExpanded === panel.open) return;
    cachePrefs.workExpanded = panel.open;
    if (panel.open) {renderSource(state.source);void refreshWork(source.id);}
    else {clearTimeout(workPoll);workPoll = null;++workRequest;}
  };
  if (!panel.open) return;
  if (cachePrefs.cacheError) panel.append(node('p', cachePrefs.cacheError, 'source-warning'));
  const grid = node('div', undefined, 'work-grid');panel.append(grid);
  if (!source.work) {
    panel.setAttribute('aria-busy', String(!source.statusError));
    const status = node('p', source.statusError || MindexI18n.msg('ui.work_loading'), source.statusError ? 'source-warning' : 'muted');
    status.setAttribute('role', 'status');panel.insertBefore(status, grid);
    for (const [name, label] of Object.entries(workNames)) {
      const card = node('section', undefined, 'work-card');card.dataset.work = name;
      const body = node('div', undefined, 'work-card-body');card.append(body);
      const start = node('button', MindexI18n.msg("ui.ab2f669c1a01"), 'quiet');start.type = 'button';start.disabled = true;
      body.append(node('h4', label), start);grid.append(card);
    }
    if (source.statusError) panel.append(action(MindexI18n.msg("ui.e2d53a6d3a6a"), () => refreshWork(source.id)));
    return;
  }
  const prefs = preferences(source.id),busy = Object.values(source.work || {}).some((w) => [w.task, w.publish_task, w.scene_publish_task].some((t) => t && ['queued', 'running'].includes(t.status)));
  // 已请求取消、仍在运行的任务保持「正在取消…」；任务离开排队/运行后丢弃该记录。
  const activeIds = new Set(Object.values(source.work || {}).flatMap((w) => [w.task, w.publish_task, w.scene_publish_task]).filter((t) => t && ['queued', 'running'].includes(t.status)).map((t) => t.id));
  prefs.cancelling = new Set([...(prefs.cancelling || [])].filter((id) => activeIds.has(id)));
  if (!prefs.subtitleInitialized && source.subtitle_files?.length) {
    const selected = source.dialogue_task?.options.subtitle_file || source.channels?.dialogue?.subtitle_file?.id || source.subtitle_files.at(-1).id;
    prefs.track = `file:${selected}`;prefs.method = 'subtitle';prefs.subtitleInitialized = true;
  }
  for (const [name, label] of Object.entries(workNames)) {
    const work = source.work?.[name] || {},result = work.result || {},card = node('section', undefined, 'work-card');card.dataset.work = name;
    // 行布局：一行一个处理卡；左侧描述与操作按钮，右侧各任务进度。
    const body = node('div', undefined, 'work-card-body'),progressCol = node('div', undefined, 'work-card-progress');card.append(body, progressCol);
    const processBusy = work.task && ['queued', 'running'].includes(work.task.status),blocked = source.enforce_gpu_lock && busy;
    const heading = node('div', undefined, 'work-heading');heading.append(node('h4', label), node('span', workStatus[work.status] || MindexI18n.msg("ui.59a9eb4e6574"), 'work-status'));body.append(heading);
    let summary = '';
    if (name === 'shots') summary = MindexI18n.msg("ui.234493832eb5", { p0: result.shots || 0, p1: result.moments || 0, p2: result.missing_artifacts || 0 });
    if (name === 'dialogue') {
      const origin = { subtitle: MindexI18n.msg("ui.334873389d17"), asr: 'ASR', legacy_asr: MindexI18n.msg("ui.b0610c376d05") }[result.source] || MindexI18n.msg("ui.a0ca6153f62e");
      const model = result.subtitle_file?.name || (result.source === 'subtitle' && result.selected_track ? MindexI18n.msg("ui.886331068288", { p0: result.selected_track.index, p1: result.selected_track.language }) : result.model_identity?.model || '');
      summary = MindexI18n.msg("ui.bbb5bbc8c300", { p0: origin, p1: model ? ' · ' + model : '', p2: result.utterances || 0, p3: result.content_segments || 0 });
    }
    if (name === 'analysis') summary = analysisSummary(result, work.task?.progress?.processing_progress || work.task?.progress);
    if (name === 'persons') summary = MindexI18n.msg("ui.1e1649402d6a", { p0: result.covered_shots || 0, p1: result.total_shots || 0, p2: result.people || 0, p3: result.accepted || 0, p4: result.candidate || 0, p5: result.rejected || 0 });
    const rows = workProgressRows(name, work);
    for (const row of rows) row.localError = prefs.errors?.[`${name}:${row.type}`];
    body.append(node('p', rows.flatMap(row => row.coverage?.by_kind && Object.keys(row.coverage.by_kind).length > 1 ? Object.entries(row.coverage.by_kind).map(([kind, counts]) => MindexI18n.msg(`ui.work_${kind}_index_coverage`, {done: counts.embedded_segments ?? counts.indexed_segments, total: counts.expected_segments})) : [row.text || MindexI18n.msg(row.key, {done: Math.min(row.done, row.total), total: row.total})]).join('，'), 'work-summary'));
    body.append(node('p', summary, 'muted'));const search = work.search || {};
    body.append(node('p', MindexI18n.msg(name === 'persons' ? "ui.13e6dc8c4328" : "ui.shot_moment_search_coverage", { p0: search.current ? MindexI18n.msg("ui.112f98675ee2") : search.indexed_segments ? MindexI18n.msg("ui.53122735b7e9") : MindexI18n.msg("ui.30e81c8167e5"), p1: search.indexed_segments || 0, p2: search.expected_segments || 0, p3: name === 'persons' ? MindexI18n.msg("ui.313083c877dc") : MindexI18n.msg("ui.32145468cf4f") }), 'muted'));
    if (MindexI18n.system(result.fallback_reason)) body.append(node('p', MindexI18n.system(result.fallback_reason), 'muted'));
    if (name === 'dialogue') {
      // 处理方式：标签与下拉同一行左对齐（style.css 的 label:has(>select) 默认竖排，行内覆盖为横排）。
      const methodRow = node('div', undefined, 'dialogue-row');
      const methodLabel = node('label', MindexI18n.msg("ui.faaa367f81c6"), 'method-field'),method = node('select');MindexI18n.setAttr(method, 'aria-label', MindexI18n.msg("ui.0883552d2a0f"));
      for (const [v, t] of [['auto', MindexI18n.msg("ui.4d56c25d87cf")], ['subtitle', MindexI18n.msg("ui.93dc9c700cf9")], ['asr', 'ASR']]) {const o = node('option', t);o.value = v;method.append(o);}
      method.value = prefs.method;methodLabel.append(method);methodRow.append(methodLabel);body.append(methodRow);
      // 文本字幕与上传控件同一行；上传仅在未选定字幕（占位项）时出现，选定轨道或已上传文件后收起。
      const subtitleRow = node('div', undefined, 'dialogue-row');
      const trackLabel = node('label', MindexI18n.msg("ui.541d33dd3e02"), 'track-field'),track = node('select');MindexI18n.setAttr(track, 'aria-label', MindexI18n.msg("ui.6918e72a515e"));const empty = node('option', MindexI18n.msg("ui.6fddde6cab67"));empty.value = '';track.append(empty);
      for (const t of source.subtitle_tracks || []) if (t.text) {const o = node('option', `${t.index} · ${t.language} · ${t.codec}${t.title ? ' · ' + t.title : ''}`);o.value = t.index;track.append(o);}
      for (const file of source.subtitle_files || []) {const o = node('option', MindexI18n.msg("ui.383ed05f1663", { name: file.name, count: file.utterances }));o.value = `file:${file.id}`;track.append(o);}
      track.value = prefs.track;trackLabel.append(track);subtitleRow.append(trackLabel);
      const uploadLabel = node('label', MindexI18n.msg("ui.3a3ecaeab3dd"), 'upload-field'),upload = node('input');
      upload.type = 'file';upload.accept = '.srt,.ass,.ssa,.vtt,.sub,.smi,.sami';upload.disabled = blocked || prefs.uploadingSubtitle;
      MindexI18n.setAttr(upload, 'aria-label', MindexI18n.msg("ui.3a3ecaeab3dd"));uploadLabel.append(upload);subtitleRow.append(uploadLabel);
      const uploadStatus = node('p', prefs.uploadingSubtitle ? MindexI18n.msg("ui.1e7bdf1f5af6") : MindexI18n.msg("ui.48eb829eca06"), 'muted');uploadStatus.setAttribute('role', 'status');
      subtitleRow.append(uploadStatus);body.append(subtitleRow);
      const syncDialogueRows = () => {
        subtitleRow.hidden = method.value !== 'subtitle';
        uploadLabel.hidden = uploadStatus.hidden = !!track.value;
      };
      syncDialogueRows();
      upload.onchange = async () => {
        const file = upload.files[0];if (!file) return;
        prefs.uploadingSubtitle = true;upload.disabled = true;start.disabled = true;MindexI18n.setText(uploadStatus, MindexI18n.msg("ui.1e7bdf1f5af6"));
        try {
          const body = new FormData();body.append('file', file);
          const saved = await api(`/api/admin/sources/${source.id}/subtitles`, { method: 'POST', body });
          prefs.track = `file:${saved.id}`;prefs.method = 'subtitle';prefs.subtitleInitialized = true;
          prefs.uploadingSubtitle = false;await refreshWork(source.id);
        } catch (e) {MindexI18n.setText(uploadStatus, e.message);uploadStatus.className = 'source-warning';}
        finally {prefs.uploadingSubtitle = false;upload.disabled = blocked;start.disabled = blocked || processBusy || !work.can_start;upload.value = '';}
      };
      method.onchange = () => {prefs.method = method.value;syncDialogueRows();};track.onchange = () => {prefs.track = track.value;syncDialogueRows();};
    }
    const checkLabel = node('label', undefined, 'check'),check = node('input');check.type = 'checkbox';check.checked = prefs.publish[name];check.onchange = () => {prefs.publish[name] = check.checked;};checkLabel.append(check, MindexI18n.textNode(MindexI18n.msg("ui.036fd74aa103")));body.append(checkLabel);
    const perform = (type, fn) => async () => {const key = `${name}:${type}`;delete prefs.errors[key];try {await fn();} catch (e) {prefs.errors[key] = e.message;renderSource(state.source);}};
    const controls = node('div', undefined, 'work-controls');
    const start = action(name === 'persons' ? MindexI18n.msg("ui.123bbda48a5d") : name === 'dialogue' ? MindexI18n.msg("ui.941fe1a3c3c2") : work.status === 'not_processed' ? MindexI18n.msg("ui.ab2f669c1a01") : name === 'shots' ? MindexI18n.msg("ui.705c3cbac14d") : name === 'analysis' ? MindexI18n.msg("ui.b3d17c0db3dd") : MindexI18n.msg("ui.941fe1a3c3c2"), perform('process', async () => {
      if (name === 'persons') return openPerson(source.id, prefs.publish[name]);
      const options = { publish: prefs.publish[name] };
      if (name === 'dialogue') {if (prefs.uploadingSubtitle) throw Error(MindexI18n.msg("ui.49476949fc90"));options.method = prefs.method;if (prefs.method === 'subtitle') {if (!prefs.track) throw Error(MindexI18n.msg("ui.ed4caa4cf3b3"));if (prefs.track.startsWith('file:')) options.subtitle_file = prefs.track.slice(5);else options.subtitle_stream = Number(prefs.track);}}
      await post(`/api/admin/sources/${source.id}/${name}`, options);await refreshWork(source.id);
    }));start.disabled = blocked || processBusy || !work.can_start || name === 'dialogue' && prefs.uploadingSubtitle;controls.append(start);
    const update = action(MindexI18n.msg(name === 'persons' ? "ui.0cb51b994992" : "ui.update_shot_moment_embedding"), perform('publish', async () => {await post(`/api/admin/sources/${source.id}/publish`, { work: name, scope: 'shot_moment' });await refreshWork(source.id);}));update.disabled = blocked || rows[1].running || !work.can_publish;controls.append(update);
    if (['shots', 'dialogue', 'analysis'].includes(name)) {
      const scene = action(MindexI18n.msg("ui.update_scene_embedding"), perform('scene', async () => {
        await post(`/api/admin/sources/${source.id}/publish`, { work: name, scope: 'scene' });await refreshWork(source.id);
      }));scene.disabled = blocked || rows[2].running || !work.can_publish_scene;controls.append(scene);
      const coverage = work.scene_search || {};
      body.append(node('p', MindexI18n.msg("ui.scene_search_coverage", {done: coverage.indexed_segments || 0, total: coverage.expected_segments || 0}), 'muted'));
    }
    if (['shots', 'dialogue', 'analysis'].includes(name) && source.shot_count) controls.append(action(MindexI18n.msg("ui.0ef38438801b"), async () => {state.kind = 'shot';state.parent = null;state.segmentPage = 1;await loadSegments();$('segment-list').scrollIntoView({ block: 'start', behavior: 'smooth' });if (name !== 'shots') notify(MindexI18n.msg("ui.1844d03ed5e7"));}));
    if (name === 'persons') {const link = node('a', MindexI18n.msg("ui.8face9a73c34"), 'quiet nav-link');link.href = `/persons?source=${encodeURIComponent(source.id)}#review`;controls.append(link);body.append(node('p', MindexI18n.msg("ui.4e65b9bde907"), 'muted'));}
    progressCol.append(controls);
    for (const row of rows) appendWorkProgress(progressCol, label, row);
    const processEndpoint = ['shots', 'analysis'].includes(name) ? '/api/admin/source-tasks' : name === 'dialogue' ? '/api/admin/dialogue-tasks' : '/api/persons/tasks';
    // 取消：排队中或运行中的处理/发布任务各一个按钮。自动发布阶段的发布行指向处理任务本身，
    // 独立发布任务都是素材任务。运行中的任务要到下一个检查点才停，期间按钮保持「正在取消…」。
    const cancelShown = new Set();
    for (const row of rows) {
      const task = row.task;
      if (!task || !row.running || cancelShown.has(task.id)) continue;
      cancelShown.add(task.id);
      const endpoint = task === work.task ? processEndpoint : '/api/admin/source-tasks';
      const requested = prefs.cancelling.has(task.id);
      const cancel = action(MindexI18n.msg(requested ? 'ui.6bc0d687aeaa' : 'ui.4d0b4688c787'), perform(row.type, async () => {
        prefs.cancelling.add(task.id);
        try {await post(`${endpoint}/${task.id}/cancel`, {});}
        catch (e) {prefs.cancelling.delete(task.id);throw e;}
        await refreshWork(source.id);
      }));
      cancel.classList.add('work-cancel');cancel.dataset.phase = row.type;cancel.disabled = requested;
      MindexI18n.setAttr(cancel, 'aria-label', MindexI18n.msg('ui.4d0b4688c787') + ' · ' + label + ' · ' + (row.text || MindexI18n.msg(row.key, {done: Math.min(row.done, row.total), total: row.total})));
      progressCol.append(cancel);
    }
    for (const {type, task} of rows) if (type === 'process' && task) {
      if (['failed', 'interrupted', 'cancelled'].includes(task.status)) {
        const retry = action(MindexI18n.msg("ui.23e1269c0ea7"), perform('process', async () => {await post(`${processEndpoint}/${task.id}/retry`, {});await refreshWork(source.id);}));retry.disabled = blocked || processBusy;progressCol.append(retry);
      }
    }
    if (MindexI18n.system(work.start_reason)) body.append(node('p', MindexI18n.system(work.start_reason), 'muted'));
    if (name === 'persons' && work.setup?.missing?.length) {body.append(node('p', work.setup.missing.map(MindexI18n.system).join('；'), 'muted'));const link = node('a', MindexI18n.msg("ui.8af96795e1d4"));link.href = '/persons';body.append(link);}
    grid.append(card);
  }
  if (!cachePrefs.refreshingCache && (busy || source.work_refreshing)) workPoll = setTimeout(() => run(() => refreshWork(source.id)), 2000);
}
async function refreshWorkCache(sourceId) {
  const selection = selectionRequest, prefs = preferences(sourceId);
  if (prefs.refreshingCache) return;
  prefs.refreshingCache = true;delete prefs.cacheError;
  ++workRequest;renderSource(state.source);
  try {
    const source = await post(`/api/admin/sources/${sourceId}/work-cache/refresh`, {});
    if (selection !== selectionRequest || state.source?.id !== sourceId) return;
    ++workRequest;state.source = source;
    await Promise.all([loadSources(), loadSegments()]);
  } catch (e) {prefs.cacheError = e.message;}
  finally {
    prefs.refreshingCache = false;
    if (selection === selectionRequest && state.source?.id === sourceId) renderSource(state.source);
  }
}
async function refreshWork(sourceId, selection = selectionRequest) {
  const request = ++workRequest;
  try {
    const source = await api(`/api/admin/sources/${sourceId}?detail=work`, {signal: selectionController?.signal});
    if (selection !== selectionRequest || request !== workRequest || state.source?.id !== sourceId) return;
    const changed = state.source.work && (source.shot_count !== state.source.shot_count || source.moment_count !== state.source.moment_count || source.scene_count !== state.source.scene_count);
    state.source = source;renderSource(source);if (changed) await Promise.all([loadSources(), loadSegments()]);
  } catch (e) {
    if (e.name === 'AbortError' || selection !== selectionRequest || request !== workRequest || state.source?.id !== sourceId) return;
    state.source = {...state.source, work: null, statusError: e.message};
    if (state.source.media) renderSource(state.source);
    else {$('source-detail').replaceChildren();renderWork(state.source, $('source-detail'));}
  }
}
let personContext = null,personSetupRequest = 0;
async function openPerson(sourceId, publish) {
  personContext = { sourceId, publish };MindexI18n.setText($('person-error'), '');$('person-start').disabled = true;
  $('person-setup').replaceChildren(node('p', MindexI18n.msg("ui.de365dab6e9d")));$('person-work').replaceChildren();$('person-dialog').showModal();
  const data = await api(`/api/admin/sources/${sourceId}/person-setup`),empty = node('option', MindexI18n.msg("ui.c1e638f8326c"));empty.value = '';$('person-work').append(empty);
  for (const w of data.works) {const option = node('option', w.name);option.value = w.id;$('person-work').append(option);}
  $('person-work').value = data.work_id || '';renderPersonSetup(data);
}
function renderPersonSetup(data) {
  const target = $('person-setup');target.replaceChildren(node('p', MindexI18n.msg("ui.fae274c098c2", { p0: data.roles, p1: data.actors })));
  for (const text of data.missing) target.append(node('p', MindexI18n.system(text), 'source-warning'));
  const link = node('a', MindexI18n.msg("ui.30af02a77651"));link.href = '/persons';link.target = '_blank';link.rel = 'noopener';target.append(link);
  target.append(action(MindexI18n.msg("ui.f8306f242d0f"), loadPersonSetup));$('person-start').disabled = !data.ready;
}
async function loadPersonSetup() {
  const context = personContext,request = ++personSetupRequest;$('person-start').disabled = true;
  const data = await api(`/api/admin/sources/${context.sourceId}/person-setup?${params({ work_id: $('person-work').value || null })}`);
  if (request === personSetupRequest && context === personContext) renderPersonSetup(data);
}
$('person-work').onchange = () => run(loadPersonSetup);
$('person-start').onclick = async () => {const context = personContext;$('person-start').disabled = true;MindexI18n.setText($('person-error'), '');try {
    await post(`/api/persons/sources/${context.sourceId}/process`, { work_id: $('person-work').value, start: 0, end: 0, publish: context.publish });$('person-dialog').close();await refreshWork(context.sourceId);
    // A short task can finish while source_work is assembling its snapshot.
    if (!workPoll) workPoll = setTimeout(() => {workPoll = null;run(() => refreshWork(context.sourceId));}, 500);
  } catch (e) {MindexI18n.setText($('person-error'), e.message);$('person-start').disabled = false;}};
$('person-close').onclick = () => $('person-dialog').close();
const uploadQueue = [];let uploading = false;
$('add-source').onclick = () => $('upload-dialog').showModal();$('upload-close').onclick = () => $('upload-dialog').close();
function enqueueUploads(files) {
  for (const file of files) {const row = node('section', undefined, 'upload-row'),text = node('p', MindexI18n.msg("ui.89f295632114", { p0: file.name })),progress = node('progress');progress.max = 100;progress.value = 0;MindexI18n.setAttr(progress, 'aria-label', file.name + MindexI18n.msg("ui.33d774c4d765"));row.append(text, progress);$('upload-results').append(row);uploadQueue.push({ file, row, text, progress });}
  processUploads();
}
$('upload-files').onchange = (event) => {enqueueUploads(event.target.files);event.target.value = '';};
for (const event of ['dragenter', 'dragover']) $('upload-drop').addEventListener(event, (e) => {e.preventDefault();$('upload-drop').classList.add('dragging');});
$('upload-drop').addEventListener('dragleave', () => $('upload-drop').classList.remove('dragging'));
$('upload-drop').addEventListener('drop', (e) => {e.preventDefault();$('upload-drop').classList.remove('dragging');enqueueUploads(e.dataTransfer.files);});
function uploadOne(item) {return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(),form = new FormData();form.append('file', item.file);xhr.open('POST', '/api/admin/sources/upload');
    xhr.upload.onprogress = (event) => {if (event.lengthComputable) {item.progress.value = Math.round(event.loaded / event.total * 100);MindexI18n.setText(item.text, `${item.file.name} · ${item.progress.value === 100 ? MindexI18n.msg("ui.3f7ffbaff114") : item.progress.value + '%'}`);}};
    xhr.onload = () => {let data;try {data = JSON.parse(xhr.responseText);} catch {reject(Error(MindexI18n.msg("ui.8f0490c7377a")));return;}if (xhr.status >= 200 && xhr.status < 300) resolve(data);else reject(Error(MindexI18n.system(data.detail || data)));};
    xhr.onerror = () => reject(Error(MindexI18n.msg("ui.c41417a8d5c1")));xhr.onabort = () => reject(Error(MindexI18n.msg("ui.85ac88b57c72")));xhr.send(form);
  });}
async function processUploads() {
  if (uploading) return;uploading = true;
  try {while (uploadQueue.length) {const item = uploadQueue.shift();try {
        const data = await uploadOne(item);item.progress.value = 100;MindexI18n.setText(item.text, `${item.file.name} · ${data.duplicate ? MindexI18n.msg("ui.a01610e03608") : MindexI18n.msg("ui.3d5e6fbea765")}`);
        item.row.append(action(MindexI18n.msg("ui.f4efeeaf3d65"), async () => {$('upload-dialog').close();await selectSource(data.source_id);}));await loadSources();await selectSource(data.source_id);
      } catch (e) {MindexI18n.setText(item.text, MindexI18n.msg("ui.b6917a629c7d", { p0: item.file.name, p1: e.message }));item.text.className = 'source-warning';}}} finally {uploading = false;}
}

let analysisRequest = 0;
async function showAnalysis(item) {
  const request = ++analysisRequest;
  MindexI18n.setText($('analysis-title'), `${segmentName(item)} · ${time(item.start)} — ${time(item.end)}`);
  const target = $('analysis-content');target.replaceChildren(node('p', MindexI18n.msg("ui.919609d4ae0e")));
  if (!$('analysis-dialog').open) $('analysis-dialog').showModal();
  try {
    const data = await api(`/api/admin/segments/${item.id}/details`);
    if (request !== analysisRequest) return;
    target.replaceChildren();
    function utterances(section, rows) {
      for (const u of rows || []) section.append(node('p', `${time(u.evidence_start ?? item.start + u.start)} — ${time(u.evidence_end ?? item.start + u.end)}　${u.text}`, 'analysis-utterance'));
    }
    const asr = node('section', undefined, 'analysis-section');asr.append(node('h3', MindexI18n.msg("ui.4d3975e9b6bc")), node('p', MindexI18n.system(data.asr)));
    if (MindexI18n.system(data.asr.error)) asr.append(node('p', MindexI18n.system(data.asr.error), 'error'));
    utterances(asr, data.asr.utterances);target.append(asr);
    const subtitles = node('section', undefined, 'analysis-section');subtitles.append(node('h3', MindexI18n.msg("ui.0446ed2748cc")));
    if (!data.subtitles.tracks.length) subtitles.append(node('p', MindexI18n.system(data.subtitles)));
    if (MindexI18n.system(data.subtitles.error)) subtitles.append(node('p', MindexI18n.system(data.subtitles.error), 'error'));
    for (const track of data.subtitles.tracks) {
      subtitles.append(node('h4', MindexI18n.msg("ui.35dcc5bcc916", { p0: track.index, p1: track.language, p2: track.codec, p3: track.title ? ' · ' + track.title : '' })), node('p', MindexI18n.system(track)));
      if (MindexI18n.system(track.error)) subtitles.append(node('p', MindexI18n.system(track.error), 'error'));
      utterances(subtitles, track.utterances);
    }
    target.append(subtitles);
    const llm = node('section', undefined, 'analysis-section');llm.append(node('h3', MindexI18n.msg("ui.e5c008e6f34f")));
    if (data.llm.annotation) {
      const labels = { description: MindexI18n.msg("ui.412f54dc38e6"), actions: MindexI18n.msg("ui.d9d9827827e1"), people: MindexI18n.msg("ui.b755b68373c8"), objects: MindexI18n.msg("ui.539cb3b0a662"), scene: MindexI18n.msg("ui.625b392c7b8a"), emotions: MindexI18n.msg("ui.6e8e0c3eeeac"), atmosphere: MindexI18n.msg("ui.6b8f229d5eb9"), shot_size: MindexI18n.msg("ui.6867f7b4537a"), angle: MindexI18n.msg("ui.96ba4a436593"), camera_motion: MindexI18n.msg("ui.0118461d56e8"), visual_evidence: MindexI18n.msg("ui.cf5030475efb"), dialogue_inference: MindexI18n.msg("ui.cf335345e962"), uncertainty: MindexI18n.msg("ui.43f1d3835b20") };
      for (const [key, value] of Object.entries(data.llm.annotation)) {
        const text = Array.isArray(value) ? value.join('；') : String(value ?? '');
        if (text) llm.append(MindexAnalysisText.setText(node('p'), value, {label: labels[key] || key}));
      }
    } else {llm.append(node('p', MindexI18n.system(data.llm)));if (data.llm.reason) llm.append(node('p', MindexI18n.system(data.llm.reason), 'muted'));}
    target.append(llm);
    const person = node('section', undefined, 'analysis-section');
    person.append(node('h3', MindexI18n.msg("ui.0c0ae35b2935")), node('p', MindexI18n.system(data.person)));
    for (const [key, label] of [['covered', MindexI18n.msg("ui.219bde30cdf8")], ['uncovered', MindexI18n.msg("ui.453d58eaeb0c")]]) {
      const ranges = data.person[key].map((r) => `${time(r.start)} — ${time(r.end)}`).join('、');
      if (ranges) person.append(node('p', MindexI18n.msg("ui.163081b53c04", { p0: label, p1: ranges })));
    }
    const counts = data.person.counts;
    person.append(node('p', MindexI18n.msg("ui.473f201e0754", { p0: counts.accepted, p1: counts.candidate, p2: counts.rejected })));
    if (data.person.people.length) person.append(node('p', MindexI18n.msg("ui.beb1316d172c", { p0: data.person.people.map((p) => `${p.name}（${p.kind_label}）`).join('、') })));
    target.append(person);
  } catch (error) {if (request === analysisRequest) target.replaceChildren(node('p', error.message, 'error'));}
}
$('analysis-close').onclick = () => $('analysis-dialog').close();
$('analysis-dialog').addEventListener('close', () => {analysisRequest++;});

window.addEventListener('mindex:agent-mutation', (event) => {
  if (!event.detail?.groups) return;
  run(async () => { if (state.source?.id) await refreshWork(state.source.id); await loadSources(); void sourceGroups.refresh(); });
});
