'use strict';
const $ = (id) => document.getElementById(id);
const state = { entities: [], references: [], sources: [], editing: null, editReference: null, editRole: null, filters: { work: [], actor: [], role: [] }, expanded: new Set(), draft: null, entry: 0, image: null, reviewShots: [], reviewPicked: new Map(), evidenceOffset: 0, evidenceTotal: 0, reviewVersion: 0, evidenceRequest: 0, reviewSelecting: false, linkedEntity: null, linkedOffset: 0, linkedTotal: 0, linkedReviewEntity: null };
function node(tag, text, cls) {const n = document.createElement(tag);if (text !== undefined) MindexI18n.setText(n, text);if (cls) n.className = cls;return n;}
function notice(text, error = false) {MindexI18n.setText($('notice'), error ? MindexI18n.system(text) : text);$('notice').className = `notice ${error ? 'error' : 'info'}`;}
async function api(url, opts = {}) {const r = await fetch(url, opts);const d = await r.json();if (!r.ok) {const error = Error(MindexI18n.system(d));error.detail = d.detail;error.status = r.status;throw error;}return d;}
const json = (method, data) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
async function run(fn) {try {await fn();} catch (e) {notice(e.message, true);}}
function button(text, fn) {const b = node('button', text);b.type = 'button';b.onclick = () => run(fn);return b;}
// 多选框顶部的“全部选择”：选中它等同全选，手动取消任意一项时它同步取消。
const ALL_VALUE = '__all__';
function allOption(select) {return Array.from(select.options).find((o) => o.value === ALL_VALUE);}
function itemOptions(select) {return Array.from(select.options).filter((o) => o.value !== ALL_VALUE);}
function syncAll(select) {const all = allOption(select);if (!all) return;const items = itemOptions(select);all.selected = items.length > 0 && items.every((o) => o.selected);select.dataset.allSelected = all.selected ? '1' : '0';}
function applyAll(select, on) {for (const o of itemOptions(select)) o.selected = on;syncAll(select);}
// change 时无法区分是点了“全部选择”还是普通项，用上一次的全选状态判断意图。
function toggleAll(select) {const all = allOption(select);if (all && all.selected !== (select.dataset.allSelected === '1')) applyAll(select, all.selected);else syncAll(select);}
// 多选框：条目按中文字典序渲染并挂搜索框；单选框维持原样。
function options(select, items, empty = true) {const value = select.value;select.replaceChildren();const list = select.multiple ? MindexMultiSearch.sort(items) : items;if (select.multiple) {const all = node('option', MindexI18n.msg("ui.3e26ad9407ad"));all.value = ALL_VALUE;select.append(all);}if (empty) {const o = node('option', MindexI18n.msg("ui.ed9e1838cd03"));o.value = '';select.append(o);}for (const e of list) {const o = node('option', `${e.name} · ${e.id.slice(0, 8)}`);o.value = e.id;select.append(o);}select.value = value;if (select.multiple) {MindexMultiSearch.attach(select);MindexMultiSearch.refilter(select);syncAll(select);}}
function entities(kind) {return state.entities.filter((e) => e.kind === kind);}
function name(id) {return state.entities.find((e) => e.id === id)?.name || MindexI18n.msg("ui.8c577dc72c73");}
function selected(id) {return Array.from($(id).selectedOptions).map((o) => o.value).filter((v) => v && v !== ALL_VALUE);}
function entityFields() {const kind = $('entity-kind').value;$('work-field').hidden = kind !== 'role';$('actors-field').hidden = kind !== 'role';$('entity-files-field').hidden = kind !== 'actor';}
$('entity-kind').onchange = entityFields;
function fillEntity(e) {$('entity-kind').value = e?.kind || 'actor';$('entity-name').value = e?.name || '';$('entity-aliases').value = (e?.aliases || []).join('，');for (const o of $('entity-work').options) o.selected = (e?.work_ids || []).includes(o.value);for (const o of $('entity-actors').options) o.selected = (e?.actor_ids || []).includes(o.value);syncAll($('entity-work'));syncAll($('entity-actors'));entityFields();}
function editEntity(e = null, reference = null, defaults = null) {
  state.editing = e?.id || null;state.editReference = reference?.id || null;state.editRole = reference?.role_id || null;
  $('entity-form').reset();MindexI18n.setText($('entity-error'), '');fillEntity(e || defaults);
  $('entity-kind').disabled = true;$('pair-field').hidden = !reference;
  options($('reference-actor'), entities('actor'), false);if (reference) $('reference-actor').value = reference.actor_id;
  const photo = reference || state.references.find((r) => r.role_id === e?.id) || state.references.find((r) => !r.role_id && r.actor_id === e?.id);
  $('entity-photo').hidden = !photo;if (photo) $('entity-photo').src = photo.url;else $('entity-photo').removeAttribute('src');
  MindexI18n.setText($('entity-title'), e ? MindexI18n.msg("ui.c7ba8202fb72") : MindexI18n.msg(defaults?.kind === 'role' ? "ui.new_role" : "ui.4941181d36f0"));MindexI18n.setText($('entity-save'), e ? MindexI18n.msg("ui.60b4ae9082a3") : MindexI18n.msg("ui.0cda8d1c7182"));
  $('entity-dialog').showModal();
}
$('entity-new').onclick = () => editEntity();
$('entity-reset').onclick = $('entity-close').onclick = () => $('entity-dialog').close();
$('reference-actor').onchange = () => {if (state.editRole) return;const e = state.entities.find((e) => e.id === $('reference-actor').value);if (e) {state.editing = e.id;fillEntity(e);}};
async function appendReferences(actorId, files, roleId = null) {const done = [],failed = [];for (const file of files) {const form = new FormData();form.append('file', file);if (roleId) form.append('role_id', roleId);form.append('actor_id', actorId);try {await api('/api/persons/references', { method: 'POST', body: form });done.push(file.name);} catch (error) {failed.push(`${file.name}：${error.message}`);}}return { done, failed };}
$('entity-form').onsubmit = async (event) => {
  event.preventDefault();$('entity-save').disabled = true;MindexI18n.setText($('entity-error'), '');
  try {
    const kind = $('entity-kind').value,body = { kind, name: $('entity-name').value, aliases: $('entity-aliases').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean), actor_ids: kind === 'role' ? selected('entity-actors') : [], work_ids: kind === 'role' ? selected('entity-work') : [] };
    let actorId = state.editRole || state.editing;
    if (state.editReference) {const payload = state.editRole ? { role_id: state.editRole, actor_id: $('reference-actor').value, entity: body } : { actor_id: actorId, entity: body };const reference = await api('/api/persons/references/' + state.editReference, json('PUT', payload));actorId = reference.actor_id;} else
    {const entity = await api('/api/persons/entities' + (state.editing ? '/' + state.editing : ''), json(state.editing ? 'PUT' : 'POST', body));actorId = entity.id;state.editing = entity.id;}
    if (kind === 'actor') {
      const { done, failed } = await appendReferences(actorId, $('entity-files').files);
      // 部分失败：人物信息已保存、成功图已入库，弹窗留着换文件重试。
      if (failed.length) {await refresh();MindexI18n.setText($('entity-error'), MindexI18n.msg("ui.beb3ee812a54", { p0: done.length, p1: failed.length, p2: failed.join('；') }));return;}
    }
    $('entity-dialog').close();await refresh();notice(MindexI18n.msg("ui.da6356497cd6"));
  } catch (error) {MindexI18n.setText($('entity-error'), error.message);} finally {$('entity-save').disabled = false;}
};
const FILTERS = [['work', 'filter-work', 'work'], ['actor', 'filter-actor', 'actor'], ['role', 'filter-role', 'role']];
function readFilters() {for (const [key, id] of FILTERS) state.filters[key] = selected(id);}
// 多选框不能用 options()：它只恢复单个 value，重建列表时必须自己记住已选项。
function fillFilters() {
  function fill(id, items) {
    const sel = $(id), keep = new Set(selected(id));
    options(sel, items, false);
    for (const o of itemOptions(sel)) o.selected = keep.has(o.value);
    syncAll(sel);
  }
  fill('filter-work', entities('work'));
  const works = selected('filter-work');
  const roles = entities('role').filter((r) => !works.length || (r.work_ids || []).some((id) => works.includes(id)));
  const actorIds = new Set(roles.flatMap((r) => r.actor_ids));
  fill('filter-actor', entities('actor').filter((a) => !works.length || actorIds.has(a.id)));
  const actors = selected('filter-actor');
  fill('filter-role', roles.filter((r) => !actors.length || r.actor_ids.some((id) => actors.includes(id))));
}
for (const [, id] of FILTERS) $(id).onchange = () => {toggleAll($(id));fillFilters();renderCatalogue();};
for (const id of ['entity-work', 'entity-actors']) $(id).onchange = () => toggleAll($(id));
$('filter-reset').onclick = () => {for (const [, id] of FILTERS) applyAll($(id), false);state.filters = { work: [], actor: [], role: [] };fillFilters();renderCatalogue();};
function matchRole(r) {const f = state.filters;return (!f.work.length || (r.work_ids || []).some((id) => f.work.includes(id))) && (!f.actor.length || r.actor_ids.some((id) => f.actor.includes(id))) && (!f.role.length || f.role.includes(r.id));}
function rolesOf(actor) {return entities('role').filter((r) => r.actor_ids.includes(actor.id) && matchRole(r));}
function actorVisible(actor) {const f = state.filters;if (f.actor.length && !f.actor.includes(actor.id)) return false;return f.role.length || f.work.length ? rolesOf(actor).length > 0 : true;}
function fillSelectors() {options($('entity-actors'), entities('actor'));for (const id of ['entity-work', 'import-work']) options($(id), entities('work'));for (const id of ['capture-source', 'process-source', 'review-source']) options($(id), state.sources);fillFilters();fillCaptureRoles();}
// 每张参考图一张卡；extra 挂在「移除参考」右侧（实体的删除按钮放第一张参考图那行）。
// 卡片左上角显示编号前 8 位，与处理任务日志「参考图 xxxxxxxx」的指向一致；悬停可看完整编号。
function referenceCard(entity, ref, extra) {const figure = node('figure'),img = node('img'),badge = node('span', ref.id.slice(0, 8), 'reference-id');MindexI18n.setAttr(badge, "title", ref.id);img.src = ref.url;MindexI18n.setAttr(img, "alt", MindexI18n.msg("ui.5ad7ccccc4d1", { p0: entity.name }));img.loading = 'lazy';figure.append(img, badge, button(MindexI18n.msg("ui.c9c77517fe85"), async () => editEntity(entity, ref)), button(MindexI18n.msg("ui.639db0ef1050"), async () => {await api('/api/persons/references/' + ref.id, { method: 'DELETE' });await refresh();notice(MindexI18n.msg("ui.da60641f3eea"));}), ...(extra ? [extra] : []));return figure;}
function uploadForm(entity, actorId) {
  const form = node('form', undefined, 'reference-upload'),label = node('label', entity.kind === 'role' ? MindexI18n.msg("ui.ccc55cdd298a") : MindexI18n.msg("ui.a45c74768a78")),files = node('input');files.type = 'file';files.accept = 'image/*';files.multiple = true;files.required = true;label.append(files);
  let performer = null;
  if (entity.kind === 'role' && entity.actor_ids.length > 1) {const actorLabel = node('label', MindexI18n.msg("ui.d87fae16139e"));performer = node('select');performer.required = true;options(performer, entities('actor').filter((a) => entity.actor_ids.includes(a.id)));actorLabel.append(performer);form.append(actorLabel);}
  const submit = node('button', MindexI18n.msg("ui.99df67b0736d"));submit.type = 'submit';form.append(label, submit);
  form.onsubmit = (event) => {event.preventDefault();run(async () => {submit.disabled = true;try {const { done, failed } = await appendReferences(performer ? performer.value : actorId, files.files, entity.kind === 'role' ? entity.id : null);await refresh();notice(failed.length ? MindexI18n.msg("ui.1107886e0dfb", { p0: done.length, p1: failed.length, p2: failed.join('；') }) : MindexI18n.msg("ui.6457db229b2c"), failed.length > 0);} finally {submit.disabled = false;}});};
  return form;
}
function roleCard(role) {
  const card = node('article', undefined, 'person-card role-card');
  card.append(node('h4', role.name), node('p', MindexI18n.msg("ui.0dd8e74bafc3") + role.work_ids.map(name).join('、')), node('p', MindexI18n.msg("ui.1e95a9d36a9f") + role.actor_ids.map(name).join('、')));
  const refs = state.references.filter((r) => r.role_id === role.id),photos = node('div', undefined, 'person-photos');
  refs.forEach((ref, i) => photos.append(referenceCard(role, ref, i === 0 ? deleteEntityButton(role) : null)));
  card.append(refs.length ? photos : node('p', MindexI18n.msg("ui.d3cf742cfdbd"), 'muted'));
  if (role.actor_ids.length) card.append(uploadForm(role, role.actor_ids[0]));else
  card.append(node('p', MindexI18n.msg("ui.a84ae80bf114"), 'muted'));
  card.append(button(MindexI18n.msg("ui.c9c77517fe85"), async () => editEntity(role)));
  if (!refs.length) card.append(deleteEntityButton(role));
  card.append(linkedShotsButton(role));
  return card;
}
// 参考图随身份删除；有未排除的关联分镜时，直接列出具体阻碍项供核对。
function deleteEntityButton(entity) {
  const b = node('button', MindexI18n.msg("ui.3755f56f2f83"), 'danger');b.type = 'button';
  b.onclick = (event) => {event.stopPropagation();return run(async () => {
      b.disabled = true;
      try {
        await api('/api/persons/entities/' + entity.id, { method: 'DELETE' });
        await refresh();notice(MindexI18n.msg("ui.5846995a6311", { p0: entity.name }));
      } catch (error) {
        if (error.detail?.linked_shots) {await openLinkedShots(entity, error.detail.linked_shots);notice(error.message, true);} else
        throw error;
      } finally {b.disabled = false;}
    });};
  return b;
}
function linkedShotsButton(entity) {return button(MindexI18n.msg("ui.1065020f1032"), () => openLinkedShots(entity));}
async function openLinkedShots(entity, data = null) {
  if (!data) data = await api(`/api/persons/entities/${entity.id}/linked-shots?offset=${state.linkedEntity?.id === entity.id ? state.linkedOffset : 0}&limit=50`);
  state.linkedEntity = entity;state.linkedOffset = data.offset;state.linkedTotal = data.total;
  MindexI18n.setText($('linked-title'), MindexI18n.msg("ui.e46b4a42fc7b", { p0: entity.name }));
  MindexI18n.setText($('linked-summary'), data.total ? MindexI18n.msg("ui.6d3d47be022a", { p0:
    data.total }) : MindexI18n.msg("ui.557d23d04da1"));

  const list = $('linked-list');list.replaceChildren();
  for (const shot of data.items) {
    const row = node('article', undefined, 'person-card');
    row.append(node('p', MindexI18n.msg("ui.920dbadba4ad", { p0: shot.source_name, p1: shot.start.toFixed(3), p2: shot.end.toFixed(3) })),
    node('p', MindexI18n.msg("ui.c0b758429ec8", { p0: shot.shot_id, p1: shot.accepted ? MindexI18n.msg("ui.ca47a2dce932") : MindexI18n.msg("ui.27b5842c971e") })),
    button(MindexI18n.msg("ui.0c99d2c19574"), async () => {
      const reviewShot = await api(`/api/persons/review/shots/${shot.shot_id}?source_id=${encodeURIComponent(shot.source_id)}`);
      $('linked-dialog').close();openReview(reviewShot, entity);
    }));
    list.append(row);
  }
  MindexI18n.setText($('linked-page'), `${Math.floor(data.offset / 50) + 1} / ${Math.max(1, Math.ceil(data.total / 50))}`);
  $('linked-prev').disabled = !data.offset;$('linked-next').disabled = data.offset + 50 >= data.total;
  if (!$('linked-dialog').open) $('linked-dialog').showModal();
}
$('linked-close').onclick = () => $('linked-dialog').close();
$('linked-prev').onclick = () => {state.linkedOffset = Math.max(0, state.linkedOffset - 50);return run(() => openLinkedShots(state.linkedEntity));};
$('linked-next').onclick = () => {state.linkedOffset += 50;return run(() => openLinkedShots(state.linkedEntity));};
function actorCard(actor) {
  const roles = rolesOf(actor),open = state.expanded.has(actor.id);
  const card = node('article', undefined, 'person-card actor-card' + (open ? ' expanded' : ''));
  // 整卡可点击展开；按钮/表单/输入等交互子元素放行。
  card.setAttribute('role', 'button');card.tabIndex = 0;card.setAttribute('aria-expanded', String(open));
  // 手风琴：展开当前演员时收起其他演员的展开行；再点已展开的则全部收起。
  const toggle = () => {open ? state.expanded.clear() : state.expanded = new Set([actor.id]);renderCatalogue();};
  card.onclick = (e) => {if (e.target.closest('button,input,select,textarea,label,a,form')) return;toggle();};
  card.onkeydown = (e) => {if (e.target !== card && e.target.closest('button,input,select,textarea,label,a,form')) return;if (e.key === 'Enter' || e.key === ' ') {e.preventDefault();toggle();}};
  const title = node('h3', undefined, 'actor-name');title.append(node('span', actor.name));
  if (actor.aliases.length) {const foreign = node('span', actor.aliases.join(' / '), 'foreign-name');title.append(foreign);}
  const refs = state.references.filter((r) => !r.role_id && r.actor_id === actor.id),photos = node('div', undefined, 'person-photos');
  refs.forEach((ref, i) => photos.append(referenceCard(actor, ref, i === 0 ? deleteEntityButton(actor) : null)));
  card.append(title, node('p', MindexI18n.msg("ui.714823abf3d2", { p0: roles.length }), 'muted role-count'), refs.length ? photos : node('p', MindexI18n.msg("ui.d3cf742cfdbd"), 'muted'));
  if (!refs.length) card.append(button(MindexI18n.msg("ui.c9c77517fe85"), async () => editEntity(actor)), deleteEntityButton(actor));
  card.append(linkedShotsButton(actor), button(MindexI18n.msg("ui.new_role"), () => editEntity(null, null, { kind: 'role', actor_ids: [actor.id] })), uploadForm(actor, actor.id));
  return card;
}
// 展开行：占据网格整行，列出该演员的全部角色（宽卡片）。
function roleRow(actor) {
  const roles = rolesOf(actor);
  const row = node('div', undefined, 'actor-detail-row');
  const head = node('div', undefined, 'actor-detail-head');
  head.append(node('h3', MindexI18n.msg("ui.d948fb06b2c1", { p0: actor.name, p1: roles.length })), button(MindexI18n.msg("ui.a020ccf2db8d"), () => {state.expanded.delete(actor.id);renderCatalogue();}));
  const grid = node('div', undefined, 'role-grid-wide');
  for (const role of roles) grid.append(roleCard(role));
  row.append(head, grid);
  return row;
}
function renderCatalogue() {
  readFilters();
  const list = $('entity-list');list.replaceChildren();
  const actors = entities('actor').filter(actorVisible);
  const cards = actors.map((actor) => ({ actor, el: actorCard(actor) }));
  for (const c of cards) list.append(c.el);
  // 展开行插在“点击演员所在视觉行”的末尾：同排其他演员保持原位不下移。
  // 先让网格完成布局，再按 offsetTop 归组视觉行。
  const rows = [];
  for (const c of cards) {
    const current = rows[rows.length - 1];
    if (current && current[0].el.offsetTop === c.el.offsetTop) current.push(c);else
    rows.push([c]);
  }
  for (const row of rows) {
    const details = row.filter((c) => state.expanded.has(c.actor.id)).map((c) => roleRow(c.actor));
    if (!details.length) continue;
    const after = row[row.length - 1].el.nextElementSibling;
    for (const detail of details) list.insertBefore(detail, after);
  }
  const unassigned = entities('role').filter((r) => !r.actor_ids.length && matchRole(r));
  if (unassigned.length) {
    const group = node('div', undefined, 'actor-detail-row unassigned-row');
    group.append(node('h3', MindexI18n.msg("ui.33cfb54b99ef")));
    const grid = node('div', undefined, 'role-grid-wide');
    for (const role of unassigned) grid.append(roleCard(role));
    group.append(grid);list.append(group);
  }
  if (!actors.length && !unassigned.length) list.append(node('p', MindexI18n.msg("ui.5874ad9173c1"), 'empty'));
  MindexI18n.setText($('filter-summary'), MindexI18n.msg("ui.496d890b2912", { p0: actors.length, p1: entities('actor').length, p2: state.expanded.size ? MindexI18n.msg("ui.951ef9f72b23", { p0: state.expanded.size }) : '' }));
}
// 删除已发布的检索组合：与「修改」同款，只用红色区分。
function deleteReleaseButton(release) {
  const b = node('button', MindexI18n.msg("ui.3755f56f2f83"), 'danger');b.type = 'button';
  b.onclick = () => run(async () => {await api(`/api/persons/releases/${release.id}`, { method: 'DELETE' });await refresh();notice(MindexI18n.msg("ui.69d484db9977"));});
  return b;
}
function referenceErrorLabel(error) {
  const reference = state.references.find((r) => r.id === error.reference_id);
  const hasIdentity = error.actor_name !== undefined || error.actor_id !== undefined;
  const actorId = error.actor_id || reference?.actor_id,roleId = hasIdentity ? error.role_id : reference?.role_id;
  const actor = error.actor_name || (actorId ? name(actorId) : null);
  const role = error.role_name || (roleId ? name(roleId) : MindexI18n.msg("ui.51755d6cacc0"));
  const referenceLabel = MindexI18n.msg("ui.c8b458421614", { p0: error.reference_id.slice(0, 8) });
  return actor ? `${actor} · ${role}（${referenceLabel}）` : MindexI18n.msg("ui.c3e94a9d45dd", { p0: referenceLabel });
}
function renderTasks(data) {
  $('tasks').replaceChildren();
  const tasks = data.tasks || [];
  // 任务日志只展示最近 3 条；更早的记录仍保留在库里，可在需要时清理。
  for (const task of tasks.slice(0, 3)) {
    const row = node('div', undefined, 'person-card');
    const sourceName = state.sources.find((s) => s.id === task.source_id)?.name || task.source_id;
    row.append(node('p', MindexI18n.msg("ui.f491d4355af8", { p0: sourceName, p1: task.id.slice(0, 8), p2: MindexI18n.status(task.status), p3: task.progress.completed_shots || 0, p4: task.progress.total_shots || '?' })));
    if (MindexI18n.system(task.error)) row.append(node('p', MindexI18n.system(task.error)));
    if (task.result) {
      // 历史日志只读：数字一律取该次任务自己记录的结果，重跑或人工核对不会改写它。
      const result = task.result;
      const parts = [MindexI18n.msg("ui.f921908008b9", { p0: result.elapsed_seconds })];
      if (result.accepted_shots !== undefined) parts.push(MindexI18n.msg("ui.87afa683ab4f", { p0: result.accepted_shots }));
      if (result.candidate_shots !== undefined) parts.push(MindexI18n.msg("ui.cdbb3f66701a", { p0: result.candidate_shots }));
      parts.push(MindexI18n.msg("ui.458fcf810617", { p0: result.reused_shots }));
      row.append(node('p', parts.join(' · ')));
    }
    if (task.result?.reference_errors?.length) {
      row.append(node('p', task.result.reference_errors.map((e) => `${referenceErrorLabel(e)}：${MindexI18n.system(e.error)}`).join('；')));
    }
    if (['failed', 'interrupted'].includes(task.status)) {
      row.append(button(MindexI18n.msg("ui.e2d53a6d3a6a"), async () => {
        await api(`/api/persons/tasks/${task.id}/retry`, { method: 'POST' });
        await refresh();
      }), button(MindexI18n.msg("ui.0c635748b755"), async () => {
        await api(`/api/persons/tasks/${task.id}`, { method: 'DELETE' });
        await refresh();
        notice(MindexI18n.msg("ui.2369895a4183"));
      }));
    }
    $('tasks').append(row);
  }
  if (tasks.length > 3) $('tasks').append(node('p', MindexI18n.msg("ui.63c47287a832", { p0: tasks.length - 3 }), 'muted'));
}
async function refresh() {const data = await api('/api/persons');Object.assign(state, { entities: data.entities, references: data.references, sources: data.sources });fillSelectors();renderCatalogue();loadImportGallery();
  $('source-works').replaceChildren();
  for (const source of state.sources) {
    const row = node('div', undefined, 'source-work');
    const work = node('select');
    MindexI18n.setAttr(work, 'aria-label', source.name + MindexI18n.msg("ui.5a48edc9a5b1"));
    options(work, entities('work'), false);
    work.value = source.work_id;
    const count = data.coverage.filter((item) => item.source_id === source.id).length;
    const actions = node('div', undefined, 'source-work-actions');
    actions.append(button(MindexI18n.msg("ui.86e2bd39e95b"), async () => {
      await api(`/api/persons/sources/${source.id}/work`, json('PUT', { work_id: work.value }));
      await refresh();
      notice(MindexI18n.msg("ui.4418a968b969"));
    }), node('span', MindexI18n.msg("ui.306128b77e2f", { p0: count }), 'coverage'));
    row.append(node('strong', source.name), work, actions);
    $('source-works').append(row);
  }
  renderTasks(data);
  $('releases').replaceChildren();for (const r of data.releases) {const row = node('p', MindexI18n.msg("ui.edd719772ace", { p0: r.created_at, p1: r.content_id.slice(0, 8), p2: r.person_id?.slice(0, 8) || MindexI18n.msg("ui.5a65ae692a28") }));row.append(button(MindexI18n.msg("ui.9abe6a920d15"), async () => {await api(`/api/persons/releases/${r.id}/activate`, { method: 'POST' });notice(MindexI18n.msg("ui.ce106faa390a"));}), deleteReleaseButton(r));$('releases').append(row);}entityFields();}
// 从原片选取参考：演员与角色同时指定，截图始终配给角色；框选坐标是原图像素。
const capture = { box: null, ready: false };
function fillCaptureRoles() {
  const work = state.sources.find((s) => s.id === $('capture-source').value)?.work_id;
  const workRoles = entities('role').filter((r) => work && (r.work_ids || []).includes(work));
  const actorIds = new Set(workRoles.flatMap((r) => r.actor_ids));
  options($('capture-actor'), entities('actor').filter((a) => actorIds.has(a.id)));
  const actorId = $('capture-actor').value, keep = $('capture-role').value;
  const roles = workRoles.filter((r) => !actorId || r.actor_ids.includes(actorId));
  options($('capture-role'), roles.map((r) => ({ id: r.id, name: `${r.name} · ${r.work_ids.map(name).join('、')}` })), false);
  $('capture-role').value = roles.some((r) => r.id === keep) ? keep : '';
}
$('capture-actor').onchange = fillCaptureRoles;
$('capture-source').onchange = () => {fillCaptureRoles();resetCapture();const v = $('capture-video');v.pause();v.onloadedmetadata = v.onloadeddata = v.onseeked = null;v.removeAttribute('src');v.load();v.hidden = true;};
$('capture-role').onchange = () => {const role = state.entities.find((e) => e.id === $('capture-role').value);if (role?.actor_ids.length === 1) {$('capture-actor').value = role.actor_ids[0];fillCaptureRoles();}};
function drawCapture() {
  const canvas = $('capture-canvas'),ctx = canvas.getContext('2d'),v = $('capture-video').shotPlayer?.current || $('capture-video');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
  if (capture.box) {const [x1, y1, x2, y2] = capture.box;ctx.strokeStyle = '#ff5035';ctx.lineWidth = Math.max(2, canvas.width / 350);ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);}
}
function resetCapture() {capture.box = null;capture.ready = false;captureDrag = null;$('capture-canvas').hidden = true;}
function validCaptureTime(value) {
  return (Math.round((Number(value) + Number.EPSILON) * 100) / 100).toFixed(2);
}
$('capture-time').oninput = resetCapture;
$('capture-time').onchange = () => {$('capture-time').value = validCaptureTime($('capture-time').value);resetCapture();};
$('capture-preview').onclick = () => run(async () => {
  const original = $('capture-video'),canvas = $('capture-canvas'),timestamp = Number(validCaptureTime($('capture-time').value));
  $('capture-time').value = validCaptureTime(timestamp);
  if (!$('capture-source').value) throw Error(MindexI18n.msg("ui.c4b67e4559fe"));
  resetCapture();original.hidden = false;
  const player = ShotPlayer.attach(original, `/api/admin/sources/${$('capture-source').value}/playlist`);
  await player.ready;
  const grab = () => {const v = player.current;if(!v.videoWidth || v.readyState < 2 || v.seeking || !v.paused) return;
    resetCapture();$('capture-time').value = validCaptureTime(player.time);canvas.width = v.videoWidth;canvas.height = v.videoHeight;
    canvas.hidden = false;drawCapture();capture.ready = true;};
  for(const v of player.videos) {v.onseeked = grab;v.onloadeddata = grab;v.onplay = resetCapture;
    v.ontimeupdate = () => {if(v !== player.current) return;$('capture-time').value = validCaptureTime(player.time);if(!v.paused) resetCapture();};}
  await player.seek(timestamp);grab();
});
let captureDrag = null;
function capturePoint(e) {const c = $('capture-canvas'),r = c.getBoundingClientRect();return [Math.max(0, Math.min(c.width, (e.clientX - r.left) * c.width / r.width)), Math.max(0, Math.min(c.height, (e.clientY - r.top) * c.height / r.height))];}
$('capture-canvas').onpointerdown = (e) => {if (!capture.ready) return;captureDrag = capturePoint(e);$('capture-canvas').setPointerCapture(e.pointerId);};
$('capture-canvas').onpointermove = (e) => {if (!captureDrag) return;const [x, y] = capturePoint(e);capture.box = [Math.min(x, captureDrag[0]), Math.min(y, captureDrag[1]), Math.max(x, captureDrag[0]), Math.max(y, captureDrag[1])].map(Math.round);drawCapture();};
$('capture-canvas').onpointerup = (e) => {if (!captureDrag) return;$('capture-canvas').onpointermove(e);captureDrag = null;};
$('capture-canvas').onpointercancel = () => captureDrag = null;
$('capture-form').onsubmit = async (e) => {e.preventDefault();
  const error = $('capture-error');error.hidden = true;MindexI18n.setText(error, '');
  try {
    const roleId = $('capture-role').value;
    if (!roleId) throw Error(MindexI18n.msg("ui.0778e4f6efa2"));
    if (capture.box && (capture.box[2] - capture.box[0] < 8 || capture.box[3] - capture.box[1] < 8)) throw Error(MindexI18n.msg("ui.aeff122f41a5"));
    await api('/api/persons/references/from-source', json('POST', { source_id: $('capture-source').value, timestamp: Number($('capture-time').value), actor_id: $('capture-actor').value || null, role_id: roleId, bbox: capture.box }));
    resetCapture();await refresh();notice(MindexI18n.msg("ui.862a7467ad7c"));
  } catch (e) {MindexI18n.setText(error, MindexI18n.system(e.message));error.hidden = false;}
};
for (const tab of document.querySelectorAll('[data-pane]')) tab.onclick = () => {document.querySelectorAll('.pane').forEach((n) => n.hidden = n.id !== tab.dataset.pane);document.querySelectorAll('[data-pane]').forEach((n) => n.classList.toggle('active', n === tab));};
$('process-form').onsubmit = (e) => {e.preventDefault();run(async () => {if (!$('process-source').value) throw Error(MindexI18n.msg("ui.f3b909f8529a"));const task = await api(`/api/persons/sources/${$('process-source').value}/process`, json('POST', { start: Number($('process-start').value), end: Number($('process-end').value || 0), publish: false }));notice(MindexI18n.msg("ui.c2d8790c0fdb") + task.id);await refresh();});};
$('publish').onclick = () => run(async () => {const r = await api('/api/persons/publish', json('POST', {}));await refresh();notice(MindexI18n.msg("ui.719463074ede") + r.person_version);});
// 演员表导入：多图草稿，全部原图纵向排列，条目统一列在图下方（每行两个）。
function newEntry() {return { image_id: state.draft.images[0].id, actor_name: '', foreign_name: '', role_name: '', actor_id: null, role_id: null, bbox: null };}
const importCanvases = new Map();
async function displayDraft(draft) {
  state.draft = draft;$('import-id').value = draft.id;history.replaceState(null, '', '/persons?draft=' + draft.id);
  $('import-tools').hidden = false;
  if (draft.work_id) $('import-work').value = draft.work_id;
  $('import-warnings').replaceChildren(...(draft.warnings || []).map((w) => node('p', MindexI18n.system(w))));
  state.entry = 0;
  const legacy = ['stage_name', 'stage_id', 'age', 'styling', 'costume', 'single_person'];
  for (const e of draft.entries) for (const k of legacy) delete e[k];
  // 按钮始终可操作；已确认草稿的操作由服务端拒绝并给出中文提示。
  for (const id of ['import-parse', 'import-add', 'import-save', 'import-confirm']) $(id).disabled = false;
  await renderOriginals();renderEntries();
}
async function renderOriginals() {
  const host = $('import-originals');host.replaceChildren();importCanvases.clear();
  for (const [i, im] of state.draft.images.entries()) {
    const block = node('div', undefined, 'original-block');
    const head = node('div', undefined, 'original-head');
    const st = (state.draft.parse_state || {})[im.id];
    const badge = node('span', st?.status === 'parsed' ? MindexI18n.msg("ui.c21863b75e9d") : st?.status === 'failed' ? MindexI18n.msg("ui.b0bbbfbadb95") : MindexI18n.msg("ui.d6e2c97037b0"), `parse-badge ${st?.status || 'pending'}`);
    const retry = button(MindexI18n.msg("ui.fcd58099fa6b"), () => parseImages([im.id]));
    head.append(node('strong', MindexI18n.msg("ui.e3075722b930", { p0: i + 1, p1: im.width, p2: im.height })), badge, retry);
    if (st?.error) head.append(node('span', MindexI18n.system(st.error), 'muted'));
    const canvas = node('canvas');canvas.dataset.image = im.id;canvas.tabIndex = 0;MindexI18n.setAttr(canvas, 'aria-label', MindexI18n.msg("ui.9cd0e4ec3fc4", { p0: i + 1 }));
    block.append(head, canvas);host.append(block);
    const image = new Image();image.src = im.url;await image.decode();
    canvas.width = im.width;canvas.height = im.height;
    importCanvases.set(im.id, { canvas, image });
    bindCanvas(canvas, im.id);
  }
  draw();
}
function bindCanvas(canvas, imageId) {
  const point = (e) => {const r = canvas.getBoundingClientRect();return [Math.max(0, Math.min(canvas.width, (e.clientX - r.left) * canvas.width / r.width)), Math.max(0, Math.min(canvas.height, (e.clientY - r.top) * canvas.height / r.height))];};
  let drag = null;
  canvas.onpointerdown = (e) => {if (!state.draft.entries[state.entry]) return;drag = point(e);canvas.setPointerCapture(e.pointerId);};
  canvas.onpointermove = (e) => {if (!drag) return;const [x, y] = point(e),entry = state.draft.entries[state.entry];entry.image_id = imageId;entry.bbox = [Math.min(x, drag[0]), Math.min(y, drag[1]), Math.max(x, drag[0]), Math.max(y, drag[1])].map(Math.round);draw();};
  canvas.onpointerup = () => {if (!drag) return;drag = null;renderEntries();};
  canvas.onpointercancel = () => drag = null;
}
function draw() {
  if (!state.draft) return;
  for (const [imageId, { canvas, image }] of importCanvases) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);ctx.drawImage(image, 0, 0);
    for (const [index, entry] of state.draft.entries.entries()) {
      if (entry.image_id !== imageId || !entry.bbox) continue;
      const [x, y, x2, y2] = entry.bbox;
      ctx.strokeStyle = index === state.entry ? '#ff5035' : '#139f80';ctx.lineWidth = Math.max(2, canvas.width / 350);ctx.strokeRect(x, y, x2 - x, y2 - y);
      const preview = document.querySelector(`canvas[data-entry="${index}"]`);
      if (preview && x2 > x && y2 > y) {preview.width = x2 - x;preview.height = y2 - y;preview.getContext('2d').drawImage(image, x, y, x2 - x, y2 - y, 0, 0, preview.width, preview.height);}
    }
  }
}
async function parseImages(ids) {
  await displayDraft(await api('/api/persons/imports/' + state.draft.id + '/parse', json('POST', { image_ids: ids })));
}
$('import-form').onsubmit = (e) => {e.preventDefault();run(async () => {
    const form = new FormData();for (const file of $('import-files').files) form.append('files', file);
    const target = state.draft && state.draft.status === 'draft' ? state.draft.id : null;
    const draft = await api(target ? `/api/persons/imports/${target}/images` : '/api/persons/imports', { method: 'POST', body: form });
    await displayDraft(draft);
    notice(target ? MindexI18n.msg("ui.d06bc5e79207") : MindexI18n.msg("ui.79b0e904a14c"));
    loadImportGallery();
  });};
$('import-load').onclick = () => run(async () => displayDraft(await api('/api/persons/imports/' + $('import-id').value.trim())));
$('import-parse').onclick = () => run(async () => {
  const st = state.draft.parse_state || {};
  let todo = state.draft.images.filter((im) => st[im.id]?.status !== 'parsed');
  if (!todo.length) todo = state.draft.images;
  $('import-parse').disabled = true;
  try {
    for (const [i, im] of todo.entries()) {
      notice(MindexI18n.msg("ui.2d628cdfde59", { p0: i + 1, p1: todo.length }));
      await parseImages([im.id]);
    }
  } finally {$('import-parse').disabled = false;}
  const failed = Object.values(state.draft.parse_state || {}).filter((s) => s.status === 'failed').length;
  notice(failed ? MindexI18n.msg("ui.b5053ecd0151", { p0: failed }) : MindexI18n.msg("ui.b5828e6451a0"));
});
$('import-add').onclick = () => {state.draft.entries.push(newEntry());state.entry = state.draft.entries.length - 1;renderEntries();draw();};
// 演员表导入是人物的主要创建入口：填写新作品名即创建，同名身份自动复用。
async function importWorkId() {const name = $('import-new-work').value.trim();if (!name) return $('import-work').value;const created = await api('/api/persons/entities', json('POST', { kind: 'work', name }));await refresh();$('import-work').value = created.id;$('import-new-work').value = '';return created.id;}
async function draftBody() {return { work_id: await importWorkId(), entries: state.draft.entries };}
// 提交前在客户端校验，避免 422 英文校验错误；服务端仍逐条复核。
function validateDraft() {if (!$('import-work').value && !$('import-new-work').value.trim()) throw Error(MindexI18n.msg("ui.109770167dcf"));state.draft.entries.forEach((e, i) => {const n = MindexI18n.msg("ui.319903891847", { p0: i + 1 });if (!(e.actor_name || '').trim() && !e.actor_id) throw Error(MindexI18n.msg("ui.6e256657b673", { p0: n }));});}
$('import-save').onclick = () => run(async () => {validateDraft();await api('/api/persons/imports/' + state.draft.id, json('PUT', await draftBody()));notice(MindexI18n.msg("ui.ea13e3f72f59"));});
$('import-confirm').onclick = () => run(async () => {
  validateDraft();
  const result = await api('/api/persons/imports/' + state.draft.id + '/confirm', json('POST', await draftBody()));
  await displayDraft(await api('/api/persons/imports/' + state.draft.id));
  await refresh();
  const parts = [MindexI18n.msg("ui.a1ffbf75409c", { p0: result.confirmed.length })];
  if (result.skipped_duplicates?.length) {
    const names = [...new Set(result.skipped_duplicates.map((d) => `${d.actor_name}/${d.role_name || MindexI18n.msg("ui.1a691a92a4ff")}`))].join('、');
    parts.push(MindexI18n.msg("ui.c5304e2f8d14", { p0: result.skipped_duplicates.length, p1: names }));
  }
  if (result.ignored_existing) parts.push(MindexI18n.msg("ui.c6e902fd5dbf", { p0: result.ignored_existing }));
  notice(parts.join(' · '));
});
function renderEntries() {
  const target = $('import-entries');target.replaceChildren();
  if (!state.draft.entries.length) {target.append(node('p', MindexI18n.msg("ui.8eec364d9885"), 'muted'));return;}
  state.draft.entries.forEach((entry, index) => {
    const card = node('article', undefined, `entry ${index === state.entry ? 'selected' : ''}`);
    card.append(button(MindexI18n.msg("ui.319903891847", { p0: index + 1 }), () => {state.entry = index;renderEntries();draw();importCanvases.get(entry.image_id)?.canvas.closest('.original-block')?.scrollIntoView({ behavior: 'smooth', block: 'center' });}));
    const imLabel = node('label', MindexI18n.msg("ui.fc8b7efc7104")),imSelect = node('select');
    options(imSelect, state.draft.images.map((im, i) => ({ id: im.id, name: MindexI18n.msg("ui.34ffcd4bbf9a", { p0: i + 1 }) })), false);
    imSelect.value = entry.image_id;imSelect.onchange = () => {entry.image_id = imSelect.value;draw();};
    imLabel.append(imSelect);card.append(imLabel);
    for (const [key, label] of [['actor_name', MindexI18n.msg("ui.e82f8c85da64")], ['foreign_name', MindexI18n.msg("ui.1f4dae88a58e")], ['role_name', MindexI18n.msg("ui.5e04c8e09777")]]) {const l = node('label', label),input = node('input');input.value = entry[key] || '';input.oninput = () => entry[key] = input.value;l.append(input);card.append(l);}
    for (const [key, kind, label] of [['actor_id', 'actor', MindexI18n.msg("ui.f46187b79698")], ['role_id', 'role', MindexI18n.msg("ui.09b1d823c99d")]]) {const l = node('label', label),select = node('select');options(select, entities(kind));select.value = entry[key] || '';select.onchange = () => entry[key] = select.value || null;l.append(select);card.append(l);}
    const coords = node('div', undefined, 'coordinates');
    for (let n = 0; n < 4; n++) {const l = node('label', ['x1', 'y1', 'x2', 'y2'][n]),input = node('input');input.type = 'number';input.min = '0';input.value = entry.bbox?.[n] ?? '';input.onchange = () => {entry.bbox = entry.bbox || [0, 0, 0, 0];entry.bbox[n] = Number(input.value);draw();};l.append(input);coords.append(l);}
    card.append(coords);
    const preview = node('canvas');preview.className = 'crop-preview';preview.dataset.entry = index;card.append(preview);
    card.append(button(MindexI18n.msg("ui.97470a515907"), () => {state.draft.entries.splice(index, 1);state.entry = Math.min(state.entry, state.draft.entries.length - 1);renderEntries();draw();}));
    target.append(card);
  });
  draw();
}
// 历史演员表图片：默认列出全部草稿与已确认导入，点击加载对应草稿，✕ 删除单张。
async function loadImportGallery() {
  const items = await api('/api/persons/imports');
  const host = $('import-gallery');host.replaceChildren();
  if (!Array.isArray(items) || !items.length) {host.append(node('p', MindexI18n.msg("ui.d120414a3b2e"), 'muted'));return;}
  for (const item of items) for (const im of item.images) {
    const wrap = node('div', undefined, 'gallery-item' + (state.draft?.id === item.id ? ' current' : ''));
    const cell = node('button', undefined, 'gallery-cell');cell.type = 'button';
    MindexI18n.setAttr(cell, "title", MindexI18n.msg("ui.1ea3357023d9", { p0: item.id.slice(0, 8), p1: item.status === 'confirmed' ? MindexI18n.msg("ui.5c5b7cfb1208") : MindexI18n.msg("ui.0f436818c0b4"), p2: item.entry_count }));
    const img = node('img');img.src = im.url;MindexI18n.setAttr(img, "alt", cell.title);img.loading = 'lazy';
    cell.append(img, node('span', item.status === 'confirmed' ? MindexI18n.msg("ui.5c5b7cfb1208") : MindexI18n.msg("ui.0f436818c0b4"), 'gallery-tag'));
    cell.onclick = () => run(async () => displayDraft(await api('/api/persons/imports/' + item.id)));
    const del = node('button', '✕', 'gallery-delete');del.type = 'button';MindexI18n.setAttr(del, "title", MindexI18n.msg("ui.fa2e9372bc2e"));
    del.onclick = () => run(async () => {
      await api(`/api/persons/imports/${item.id}/images/${im.id}`, { method: 'DELETE' });
      notice(MindexI18n.msg("ui.1fc55dd71eb9"));
      if (state.draft?.id === item.id) {
        // 当前打开的草稿可能整行被删空：尝试重载，失败则收起工作区。
        try {await displayDraft(await api('/api/persons/imports/' + item.id));}
        catch {$('import-tools').hidden = true;state.draft = null;}
      }
      await loadImportGallery();
    });
    wrap.append(cell, del);host.append(wrap);
  }
}
const reviewPageSize = 50;
function pairKey(actor, role) {return JSON.stringify([actor, role || null]);}
// 审核语境的身份显示：角色名在前，演员名随后；未指明角色时回退到该演员在本作品的全部角色。
function personLabel(actorId, roleId, workId) {
  if (!actorId) return MindexI18n.msg("ui.d9c32a4c3dda");
  const roles = roleId ?
  state.entities.filter((e) => e.id === roleId && e.kind === 'role') :
  entities('role').filter((r) => (r.work_ids || []).includes(workId) && r.actor_ids.includes(actorId));
  return roles.length ? roles.map((r) => r.name).join('/') + ' · ' + name(actorId) : name(actorId);
}
function reviewParams(offset = state.evidenceOffset, limit = reviewPageSize) {
  const params = new URLSearchParams({ source_id: $('review-source').value, status: $('review-status').value, offset, limit });
  const pair = $('review-pair').value;
  if ($('review-status').value === 'rejected') return params;
  if (pair === '__none__') params.set('no_candidate', 'true');else
  if (pair) {const [actor, role] = JSON.parse(pair);params.set('actor_id', actor);if (role) params.set('role_id', role);}
  return params;
}
function syncReviewStatus() {
  const rejected = $('review-status').value === 'rejected';
  $('review-pair-field').hidden = rejected;$('review-pair').disabled = rejected;
  if (rejected) $('review-pair').value = '';
  MindexI18n.setText($('review-select-all'), rejected ? MindexI18n.msg("ui.f1f373533293") : MindexI18n.msg("ui.5900c9410a3e"));
  MindexI18n.setText($('review-filter-hint'), rejected ? MindexI18n.msg("ui.b588848a06d3") :

  $('review-status').value === 'accepted' ? MindexI18n.msg("ui.e7585ba2e617") : MindexI18n.msg("ui.50064f5958c4"));


  return rejected;
}
function confirmedPersonDetails(person) {
  const value = person.manual ? person.original_score : person.score;
  const score = typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : MindexI18n.msg("ui.f1b46a8c5d6c");
  return person.manual ? MindexI18n.msg("ui.dcdce6baa460", { p0: score }) : MindexI18n.msg("ui.1dc43e692df7", { p0: score });
}
function confirmedPersonLabel(person) {return `${reviewPairLabel(person.actor_id, person.role_id)} · ${confirmedPersonDetails(person)}`;}
function reviewPairLabel(actor, role) {return actor ? `${name(actor)} · ${role ? name(role) : MindexI18n.msg("ui.51755d6cacc0")}` : MindexI18n.msg("ui.74869b6ec629");}
function fillReviewFilters(filters, scopeTotal) {
  const select = $('review-pair'),keep = select.value;
  const pairs = filters.filter((f) => f.actor_id).map((f) => ({ ...f, key: pairKey(f.actor_id, f.role_id), label: reviewPairLabel(f.actor_id, f.role_id) }));
  if (keep && keep !== '__none__' && !pairs.some((p) => p.key === keep)) {
    const [actor_id, role_id] = JSON.parse(keep);pairs.push({ key: keep, label: reviewPairLabel(actor_id, role_id), count: 0 });
  }
  pairs.sort((a, b) => a.label.localeCompare(b.label, 'zh') || a.key.localeCompare(b.key));
  select.replaceChildren();
  const all = node('option', MindexI18n.msg("ui.d8747ebbaca7", { p0: scopeTotal }));all.value = '';select.append(all);
  for (const p of pairs) {const option = node('option', `${p.label}（${p.count}）`);option.value = p.key;select.append(option);}
  const none = node('option', MindexI18n.msg("ui.8a0b23409ba3", { p0: filters.find((f) => !f.actor_id)?.count || 0 }));none.value = '__none__';select.append(none);
  select.value = keep;
}
function shotKey(shot) {return shot.source_id + '|' + shot.shot_id;}
async function loadEvidence() {
  const rejected = syncReviewStatus();
  const request = ++state.evidenceRequest;
  $('review-select-all').disabled = true;
  const data = await api('/api/persons/review/shots?' + reviewParams());
  if (request !== state.evidenceRequest) return;
  if (!data.items.length && state.evidenceOffset > 0) {state.evidenceOffset = Math.max(0, Math.ceil(data.total / reviewPageSize) - 1) * reviewPageSize;return loadEvidence();}
  state.evidenceTotal = data.total;
  fillReviewFilters(data.filters, data.scope_total);
  $('evidence-list').replaceChildren();
  for (const shot of data.items) {
    const card = node('article', undefined, 'person-card');
    const pick = node('input');pick.type = 'checkbox';pick.className = 'shot-pick';MindexI18n.setAttr(pick, "title", MindexI18n.msg("ui.551c48615936"));
    MindexI18n.setAttr(pick, 'aria-label', MindexI18n.msg("ui.c9575c25aafe", { p0: shot.start.toFixed(2), p1: shot.end.toFixed(2) }));
    pick.checked = state.reviewPicked.has(shotKey(shot));pick.dataset.shotKey = shotKey(shot);
    pick.onchange = () => {pick.checked ? state.reviewPicked.set(shotKey(shot), shot) : state.reviewPicked.delete(shotKey(shot));updateBatchButton();};
    card.append(pick);
    if (shot.image_url) {const image = node('img');image.src = shot.image_url;MindexI18n.setAttr(image, "alt", MindexI18n.msg("ui.35d98eda6fad"));image.loading = 'lazy';card.append(image);}
    const accepted = (shot.confirmed_people || []).map(confirmedPersonLabel);
    const acceptedList = node('ul', undefined, 'confirmed-people');
    for (const label of accepted) acceptedList.append(node('li', label));
    const candidate = shot.candidate;
    const category = candidate ? MindexI18n.msg("ui.02f9173c8968", { p0: reviewPairLabel(candidate.actor_id, candidate.role_id), p1: candidate.score.toFixed(3) }) : MindexI18n.msg("ui.763e86d17996");
    card.append(...[node('h3', MindexI18n.msg("ui.9c272a93fabb", { p0: shot.start.toFixed(2), p1: shot.end.toFixed(2) })), node('p', state.sources.find((s) => s.id === shot.source_id)?.name || shot.source_id), rejected || shot.counts.accepted ? null : node('p', category, 'muted'), node('p', MindexI18n.msg("ui.bf08ee968d7b", { p0: shot.counts.accepted, p1: shot.counts.candidate, p2: shot.counts.rejected })), accepted.length ? acceptedList : node('p', rejected ? MindexI18n.msg("ui.a5ee4cf7a077") : MindexI18n.msg("ui.8f9733e48602")), shot.counts.candidate ? node('p', MindexI18n.msg("ui.7842fdd83262", { p0: shot.counts.candidate }), 'muted') : null, button(MindexI18n.msg("ui.193cbffaacf3"), async () => openReview(shot))].filter(Boolean));
    $('evidence-list').append(card);
  }
  if (!data.items.length) $('evidence-list').append(node('p', MindexI18n.msg("ui.fdd691c42da8"), 'empty'));
  MindexI18n.setText($('evidence-page'), MindexI18n.msg("ui.8067f5d58892", { p0: Math.floor(state.evidenceOffset / reviewPageSize) + 1, p1: Math.max(1, Math.ceil(data.total / reviewPageSize)), p2: data.total }));
  $('evidence-prev').disabled = state.evidenceOffset === 0;
  $('evidence-next').disabled = state.evidenceOffset + reviewPageSize >= data.total;
  updateBatchButton();
}
function updateBatchButton() {
  const n = state.reviewPicked.size;
  MindexI18n.setText($('review-batch'), MindexI18n.msg("ui.a07bb6eb6d73", { p0: n }));$('review-batch').disabled = !n || state.reviewSelecting;
  $('review-clear').disabled = !n && !state.reviewSelecting;
  $('review-select-all').disabled = !state.evidenceTotal || state.reviewSelecting;
  for (const pick of $('evidence-list').querySelectorAll('.shot-pick')) pick.checked = state.reviewPicked.has(pick.dataset.shotKey);
}
function resetEvidence(resetPair = false) {
  ++state.reviewVersion;++state.evidenceRequest;
  state.reviewSelecting = false;state.evidenceOffset = 0;state.evidenceTotal = 0;state.reviewPicked.clear();
  if (resetPair) $('review-pair').value = '';
  $('evidence-list').replaceChildren();updateBatchButton();
  return run(loadEvidence);
}
async function selectFilteredShots() {
  const version = state.reviewVersion,params = reviewParams(0, 200),picked = new Map();
  state.reviewSelecting = true;updateBatchButton();
  try {
    params.set('compact', 'true');
    for (let offset = 0;;) {
      params.set('offset', offset);
      const data = await api('/api/persons/review/shots?' + params);
      if (version !== state.reviewVersion) return;
      for (const shot of data.items) picked.set(shotKey(shot), shot);
      offset += data.items.length;
      if (offset >= data.total) break;
      if (!data.items.length) throw Error(MindexI18n.msg("ui.ab1dcceaf611"));
    }
    state.reviewPicked = picked;
    notice(MindexI18n.msg("ui.931509868950", { p0: picked.size }));
  } finally {if (version === state.reviewVersion) {state.reviewSelecting = false;updateBatchButton();}}
}
$('review-batch').onclick = () => {const shots = [...state.reviewPicked.values()];if (shots.length) openBatchReview(shots);};
$('review-select-all').onclick = () => run(selectFilteredShots);
$('review-clear').onclick = () => {++state.reviewVersion;state.reviewSelecting = false;state.reviewPicked.clear();updateBatchButton();};
$('review-refresh').onclick = () => resetEvidence();
$('review-source').onchange = $('review-status').onchange = () => resetEvidence(true);
$('review-pair').onchange = () => resetEvidence();
$('evidence-prev').onclick = () => {state.evidenceOffset = Math.max(0, state.evidenceOffset - reviewPageSize);return run(loadEvidence);};
$('evidence-next').onclick = () => {state.evidenceOffset += reviewPageSize;return run(loadEvidence);};
// 勾选单元是「角色 · 演员」身份：角色在前；未参加任何角色的演员单列演员名。
// 批量模式对全部所选分镜取并集构建选项；预勾选只取所有分镜都已接受的身份交集。
function fillReviewChoices(shots) {
  const works = new Set(shots.map((s) => state.sources.find((x) => x.id === s.source_id)?.work_id));
  const roleList = entities('role').filter((r) => (r.work_ids || []).some((w) => works.has(w)));
  const suggested = new Set(shots.flatMap((s) => s.evidence.filter((e) => e.actor_id).map((e) => e.actor_id)));
  const pairs = [];
  for (const role of roleList) for (const actorId of role.actor_ids) pairs.push({ actor_id: actorId, role_id: role.id, label: `${role.name} · ${name(actorId)}` });
  const cast = new Set(pairs.map((p) => p.actor_id));
  for (const actor of entities('actor')) if (!cast.has(actor.id)) pairs.push({ actor_id: actor.id, role_id: null, label: actor.name });
  pairs.sort((a, b) => Number(suggested.has(b.actor_id)) - Number(suggested.has(a.actor_id)) || a.label.localeCompare(b.label, 'zh'));
  const marks = shots.map((s) => new Set(s.evidence.filter((e) => e.status === 'accepted').map((e) => pairKey(e.actor_id, e.role_id))));
  const choices = $('review-choices');choices.replaceChildren();
  for (const pair of pairs) {
    const check = node('input');check.type = 'checkbox';check.value = pairKey(pair.actor_id, pair.role_id);
    // 已接受证据精确匹配；自动接受的行常无角色，回退到该演员在本作品的唯一身份。
    check.checked = marks.every((m) => m.has(check.value) || m.has(pairKey(pair.actor_id, null)) && pairs.filter((p) => p.actor_id === pair.actor_id).length === 1);
    const confirmed = shots.length === 1 ? shots[0].confirmed_people?.find((p) => p.actor_id === pair.actor_id && p.role_id === pair.role_id) : null;
    const label = node('label', pair.label + (confirmed ? ' · ' + confirmedPersonDetails(confirmed) : ''));label.prepend(check);choices.append(label);
  }
}
function openReview(shot, linkedEntity = null) {
  state.linkedReviewEntity = linkedEntity;state.reviewShots = [shot];
  $('review-image').hidden = !shot.image_url;
  if (shot.image_url) $('review-image').src = shot.image_url;
  MindexI18n.setText($('review-description'), MindexI18n.msg("ui.71f93b0b04cb", { p0: shot.start.toFixed(3), p1: shot.end.toFixed(3) }));
  fillReviewChoices([shot]);
  const video = $('review-video');video.pause();video.hidden = false;video.preload = 'auto';
  video.onloadedmetadata = () => video.currentTime = 0;
  video.src = `/api/admin/segments/${shot.shot_id}/media`;video.load();
  $('review-dialog').showModal();
}
function openBatchReview(shots) {
  state.linkedReviewEntity = null;state.reviewShots = shots;
  $('review-image').hidden = true;$('review-image').removeAttribute('src');
  const video = $('review-video');video.pause();video.removeAttribute('src');video.hidden = true;
  MindexI18n.setText($('review-description'), MindexI18n.msg("ui.91e497901479", { p0: shots.length }));
  fillReviewChoices(shots);
  $('review-dialog').showModal();
}
async function review(accept) {
  const selections = accept ? Array.from($('review-choices').querySelectorAll('input:checked')).map((input) => {const [actor_id, role_id] = JSON.parse(input.value);return { actor_id, role_id };}) : [];
  const shots = state.reviewShots || [];
  for (const shot of shots) {
    await api('/api/persons/review/shot', json('POST', { source_id: shot.source_id, shot_id: shot.shot_id, selections }));
    state.reviewPicked.delete(shotKey(shot));
  }
  updateBatchButton();
  $('review-dialog').close();
  if (state.linkedReviewEntity) {const entity = state.linkedReviewEntity;state.linkedReviewEntity = null;state.linkedOffset = 0;await openLinkedShots(entity);} else
  await loadEvidence();
  notice(shots.length > 1 ? MindexI18n.msg("ui.6cff5b6f493d", { p0: shots.length }) : MindexI18n.msg("ui.b870998229a0"));
}
$('review-form').onsubmit = (e) => {e.preventDefault();run(() => review(true));};
$('review-reject').onclick = () => run(() => review(false));
$('review-close').onclick = () => {state.linkedReviewEntity = null;$('review-dialog').close();};
$('review-dialog').onclose = () => $('review-video').pause();
run(async () => {
  await refresh();
  const params = new URLSearchParams(location.search),draft = params.get('draft');
  if (draft) {document.querySelector('[data-pane="cast"]').click();await displayDraft(await api('/api/persons/imports/' + draft));}
  const ev = params.get('evidence');
  if (ev) {
    document.querySelector('[data-pane="processing"]').click();
    const evidence = await api('/api/persons/evidence/' + ev);
    $('review-source').value = evidence.source_id;
    openReview(await api(`/api/persons/review/shots/${evidence.shot_id}?source_id=${encodeURIComponent(evidence.source_id)}`));
  } else if (params.get('source')) {
    document.querySelector('[data-pane="processing"]').click();
    const source = params.get('source'),shotId = params.get('shot');
    $('review-source').value = source;
    if (shotId) {
      let shot;
      try {shot = await api(`/api/persons/review/shots/${encodeURIComponent(shotId)}?source_id=${encodeURIComponent(source)}`);}
      catch (error) {
        if (error.status !== 404) throw error;
        const segment = await api(`/api/admin/segments/${encodeURIComponent(shotId)}`);
        if (segment.source_id !== source || segment.kind !== 'shot') throw new Error(MindexI18n.msg("ui.7d6acd87e0e3"));
        shot = { shot_id: segment.id, source_id: source, start: segment.start, end: segment.end, image_url: segment.thumbnail_url, evidence: [] };
      }
      openReview(shot);
    } else await resetEvidence(true);
  }
});
$('reload').onclick = () => {$('reload').disabled = true;run(async () => {await refresh();}).finally(() => {$('reload').disabled = false;});};
setInterval(() => {if (!$('processing').hidden) run(refresh);}, 5000);
