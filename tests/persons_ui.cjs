const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    Object.assign(this, {tagName: tag, children: [], textContent: '', _value: '', hidden: false,
      selected: false, files: [], dataset: {}, paused: true, currentTime: 0, readyState: 4,
      videoWidth: 320, videoHeight: 240, classList: {toggle() {}}});
  }
  get options() { return this.children; }
  get selectedOptions() { return this.children.filter(o => o.selected); }
  get value() {
    return this.tagName === 'select' ? this.selectedOptions[0]?.value || '' : this._value;
  }
  set value(value) {
    this._value = value;
    if (this.tagName === 'select') this.children.forEach(o => o.selected = o.value === value);
  }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  querySelectorAll(selector) {
    const children = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
    if (selector === '*') return children;
    if (selector === '.shot-pick') return children.filter(n => n.className === 'shot-pick');
    if (selector === 'input:checked') return children.filter(n => n.tagName === 'input' && n.checked);
    return children.filter(n => n.tagName === selector);
  }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute(key) { delete this[key]; }
  pause() { this.paused = true; }
  load() {}
  reset() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  setPointerCapture() {}
  getBoundingClientRect() { return {left: 0, top: 0, width: 320, height: 240}; }
  getContext() { return {clearRect() {}, drawImage() {}, strokeRect() {}}; }
}
const nodes = new Map();
const selectIds = new Set(['capture-source', 'capture-role', 'capture-actor', 'entity-actors',
  'entity-kind', 'entity-work', 'import-work', 'process-source', 'review-source',
  'filter-work', 'filter-actor', 'filter-role', 'reference-actor', 'review-pair', 'review-status']);
const multiSelectIds = new Set(['entity-actors', 'entity-work', 'filter-work', 'filter-actor', 'filter-role']);
function get(id) {
  if (!nodes.has(id)) nodes.set(id, new Element(selectIds.has(id) ? 'select' : 'div'));
  if (multiSelectIds.has(id)) nodes.get(id).multiple = true;
  return nodes.get(id);
}
const actor = {id: 'a', kind: 'actor', name: '甲', aliases: []};
const other = {id: 'b', kind: 'actor', name: '乙', aliases: []};
const role = {id: 'role', kind: 'role', name: '角色', aliases: [], actor_ids: ['a', 'b'], work_ids: ['work']};
const orphan = {...role, id: 'orphan', name: '未配对角色', actor_ids: []};
const data = {
  entities: [actor, other, role, orphan, {id: 'work', kind: 'work', name: '作品', aliases: []}],
  references: [], sources: [{id: 'source', name: '原片', work_id: 'work'}], coverage: [], releases: [],
  review_shots: {}, tasks: [{id: 'failed', source_id: 'source', status: 'failed', progress: {}}],
};
const requests = [];
let reviewItems = [];
let deferSelection = null;
let linkedResult = {entity: actor, items: [], total: 0, limit: 50, offset: 0};
let blockDeletion = false;
const reviewResponse = url => {
  const params = new URLSearchParams(url.split('?')[1]);
  const scoped = reviewItems.filter(s => !params.get('status') || s.bucket === params.get('status'));
  if (params.get('status') === 'rejected') {
    const offset = Number(params.get('offset')), limit = Number(params.get('limit'));
    return {items: scoped.slice(offset, offset + limit).map(s => ({...s, candidate: null})),
      total: scoped.length, scope_total: scoped.length, filters: []};
  }
  const memberships = s => s.bucket === 'accepted'
    ? s.confirmed_people || s.evidence.filter(e => e.status === 'accepted').map(e => ({actor_id: e.actor_id, role_id: e.role_id}))
    : [s.candidate || {actor_id: null, role_id: null}];
  const groups = new Map();
  for (const s of scoped) {
    for (const key of new Set(memberships(s).map(p => JSON.stringify([p.actor_id, p.role_id])))) {
      groups.set(key, (groups.get(key) || 0) + 1);
    }
  }
  const items = scoped.filter(s => params.get('no_candidate') ? memberships(s).some(p => !p.actor_id) :
    !params.get('actor_id') || memberships(s).some(p => p.actor_id === params.get('actor_id') &&
      (p.role_id || null) === params.get('role_id')));
  const offset = Number(params.get('offset')), limit = Number(params.get('limit'));
  return {items: items.slice(offset, offset + limit), total: items.length, scope_total: scoped.length,
    filters: [...groups].map(([key, count]) => {
      const [actor_id, role_id] = JSON.parse(key); return {actor_id, role_id, count};
    })};
};
const sandbox = {
  MindexI18n: require('./i18n_adapter.cjs'),
  ShotPlayer: {attach(video) {const player = {current: video, videos: [video], ready: Promise.resolve(),
    get time() {return video.currentTime;}, seek(time) {video.currentTime = time;}};
    video.shotPlayer = player;return player;}},
  // multiselect.js 未在 vm 中加载；给出与真实组件同接口的桩。
  MindexMultiSearch: {sort: entries => entries.slice().sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh')), attach() {}, refilter() {}},
  console, URLSearchParams, FormData, location: {search: ''}, setInterval() {},
  document: {getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => []},
  fetch: async (url, options = {}) => {
    requests.push({url, ...options});
    if (url === '/api/persons/references' && options.method === 'POST' && options.body.get('file')?.name === 'bad.png') {
      return {ok: false, json: async() => ({detail: '参考照片需要恰好一张清晰人脸，且属于画面主体'})};
    }
    if (url.startsWith('/api/persons/entities/') && options.method === 'DELETE' && blockDeletion) {
      return {ok: false, json: async () => ({detail: {message: '该身份仍有关联出镜证据：1 个分镜', linked_shots: linkedResult}})};
    }
    if (url.includes('/linked-shots')) return {ok: true, json: async () => linkedResult};
    if (url.startsWith('/api/persons/review/shots/')) return {ok: true, json: async () => ({
      source_id: 'source', shot_id: 'linked-shot', start: 10, end: 12, counts: {accepted: 2, candidate: 0, rejected: 0},
      candidate: {actor_id: 'b', role_id: role.id, score: 0.9}, evidence: [
        {actor_id: 'a', role_id: role.id, status: 'accepted'},
        {actor_id: 'b', role_id: role.id, status: 'accepted'},
      ],
    })};
    if (url === '/api/persons/review/shot' && blockDeletion) {
      blockDeletion = false; linkedResult = {...linkedResult, items: [], total: 0};
    }
    if (url.startsWith('/api/persons/review/shots?')) {
      const result = reviewResponse(url);
      if (deferSelection && url.includes('compact=true')) {
        const defer = deferSelection; deferSelection = null;
        await new Promise(resolve => defer(resolve));
      }
      return {ok: true, json: async () => result};
    }
    return {ok: true, json: async () => url === '/api/persons' ? data : {}};
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('src/mindex/static/persons.js', 'utf8'), sandbox);
const flush = () => new Promise(resolve => setImmediate(resolve));
const event = {preventDefault() {}, stopPropagation() {}};
function find(root, predicate) {
  if (predicate(root)) return root;
  return root.children.map(n => find(n, predicate)).find(Boolean);
}

(async () => {
  await flush();
  assert.ok(find(get('entity-list'), n => n.textContent === orphan.name), 'unassigned roles remain editable');
  const retry = find(get('tasks'), n => n.textContent === '重试');
  await retry.onclick();
  assert.ok(requests.some(r => r.url === '/api/persons/tasks/failed/retry' && r.method === 'POST'));

  const card = sandbox.roleCard(role);
  const form = find(card, n => n.tagName === 'form');
  find(form, n => n.tagName === 'select').value = other.id;
  find(form, n => n.tagName === 'input').files = ['face'];
  form.onsubmit(event);
  await flush();
  const upload = requests.find(r => r.url === '/api/persons/references');
  assert.equal(upload.body.get('actor_id'), other.id, 'role upload preserves the selected face actor');
  assert.equal(upload.body.get('role_id'), role.id);

  // 添加时质检拒绝按文件聚合：坏文件报原因，不再中断同批其余文件。
  const postsBefore = requests.filter(r => r.url === '/api/persons/references').length;
  find(form, n => n.tagName === 'input').files = [new File([''], 'good.png'), new File([''], 'bad.png'), new File([''], 'fine.png')];
  form.onsubmit(event);
  await flush();
  assert.equal(
    requests.filter(r => r.url === '/api/persons/references').length - postsBefore,
    3, 'a rejected file must not abort the remaining uploads');
  assert.match(get('notice').textContent, /成功 2 张，失败 1 张/);
  assert.ok(get('notice').textContent.includes('bad.png：参考照片需要恰好一张清晰人脸，且属于画面主体'));

  get('capture-source').value = 'source';
  get('capture-actor').value = other.id;
  sandbox.fillCaptureRoles();
  get('capture-role').value = role.id;
  get('capture-role').onchange();
  assert.equal(get('capture-actor').value, other.id, 'selecting a role must not replace the chosen performer');

  get('capture-time').value = '1.235';
  await get('capture-preview').onclick();
  const video = get('capture-video');
  assert.equal(video.currentTime, 1.24, 'preview seeks to the nearest valid hundredth');
  video.currentTime = 1.756;
  video.onseeked();
  assert.equal(Number(get('capture-time').value), 1.76, 'paused seeking rounds to the valid input step');
  assert.equal(get('capture-canvas').hidden, false);
  get('capture-canvas').onpointerdown({clientX: 10, clientY: 20, pointerId: 1});
  get('capture-canvas').onpointerup({clientX: 100, clientY: 140});
  get('capture-form').onsubmit(event);
  await flush();
  const captured = JSON.parse(requests.find(r => r.url.endsWith('/references/from-source')).body);
  assert.equal(captured.timestamp, 1.76);
  assert.deepEqual(captured.bbox, [10, 20, 100, 140]);
  assert.equal(captured.actor_id, other.id);

  vm.runInContext('capture.box=[1,2,100,140];capture.ready=true', sandbox);
  get('capture-source').onchange();
  assert.equal(vm.runInContext('capture.box', sandbox), null, 'changing sources discards the old crop');
  assert.equal(get('capture-canvas').hidden, true);
  assert.equal(video.hidden, true);

  // 多选框顶部的“全部选择”
  const actors = get('filter-actor');
  const allOf = select => select.options.find(o => o.value === '__all__');
  const picked = select => select.options.filter(o => o.value && o.value !== '__all__' && o.selected);
  assert.equal(allOf(actors).textContent, '全部选择', '多选筛选框顶部提供“全部选择”');
  allOf(actors).selected = true;
  actors.onchange();
  assert.deepEqual(picked(actors).map(o => o.value), [actor.id, other.id], '点击“全部选择”等于全选');
  // 比较序列化结果：vm 沙箱里的对象原型与本模块不同，deepEqual 会误判。
  assert.equal(JSON.stringify(vm.runInContext('state.filters', sandbox)),
    JSON.stringify({work: [], actor: [actor.id, other.id], role: []}), '全选结果写入筛选状态');
  allOf(actors).selected = false;
  actors.onchange();
  assert.equal(picked(actors).length, 0, '再次点击“全部选择”清空全选');
  get('filter-reset').onclick();
  assert.equal(allOf(actors).selected, false, '清除筛选后“全部选择”同步取消');
  const roleActors = get('entity-actors');
  assert.equal(allOf(roleActors).textContent, '全部选择', '编辑对话框的多选框也有“全部选择”');
  allOf(roleActors).selected = true;
  roleActors.onchange();
  assert.deepEqual(picked(roleActors).map(o => o.value), [actor.id, other.id], '编辑对话框的全选同样生效');

  // 审核按演员-角色筛选，并跨 50 条显示页、200 条读取页全选。
  for (const value of ['candidate', 'accepted', 'rejected']) {
    const option = new Element('option'); option.value = value; get('review-status').append(option);
  }
  get('review-status').value = 'candidate';
  const makeShot = (id, who, bucket = 'candidate') => ({source_id: 'source', shot_id: id,
    start: 0, end: 1, bucket, counts: {accepted: bucket === 'accepted' ? 1 : 0,
      candidate: bucket === 'candidate' ? 1 : 0, rejected: bucket === 'rejected' ? 1 : 0},
    candidate: who ? {actor_id: who, role_id: role.id, score: 0.9} : null,
    confirmed_people: bucket === 'accepted' && who
      ? [{actor_id: who, role_id: role.id, score: 0.9, original_score: null, manual: false}] : [],
    evidence: [{actor_id: who, role_id: who ? role.id : null, status: bucket}]});
  reviewItems = [...Array.from({length: 205}, (_, n) => makeShot('b' + n, 'b')),
    makeShot('a0', 'a'), makeShot('unknown', null), makeShot('accepted-a', 'a', 'accepted'),
    makeShot('rejected-a', 'a', 'rejected'), makeShot('rejected-b', 'b', 'rejected'),
    makeShot('rejected-unknown', null, 'rejected')];
  const sharedShot = makeShot('shared-confirmed', 'b', 'accepted');
  sharedShot.confirmed_people = [
    {actor_id: 'a', role_id: role.id, score: 0.41, original_score: 0.41, manual: true},
    {actor_id: 'b', role_id: role.id, score: 0.9, original_score: null, manual: false},
  ];
  sharedShot.evidence.push({actor_id: 'a', role_id: role.id, status: 'accepted'});
  reviewItems.push(sharedShot);
  await get('review-refresh').onclick();
  assert.equal(get('evidence-list').children.length, 50);
  assert.ok(get('review-pair').options.some(o => o.textContent === '乙 · 角色（205）'), 'filter counts cover all pages');
  get('review-pair').value = JSON.stringify(['b', role.id]);
  await get('review-pair').onchange();
  await get('review-select-all').onclick();
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 205, 'select all spans every page');
  assert.ok(vm.runInContext("state.reviewPicked.has('source|b204')", sandbox));
  assert.ok(!vm.runInContext("state.reviewPicked.has('source|a0')", sandbox), 'other filters are excluded');
  assert.ok(get('evidence-list').querySelectorAll('.shot-pick').every(p => p.checked));
  await get('evidence-next').onclick();
  assert.ok(get('evidence-list').querySelectorAll('.shot-pick').every(p => p.checked), 'page changes retain picks');
  get('review-batch').onclick();
  assert.equal(get('review-image').hidden, true);
  const choice = find(get('review-choices'), n => n.tagName === 'input' && n.value === JSON.stringify(['b', role.id]));
  choice.checked = true;
  const before = requests.filter(r => r.url === '/api/persons/review/shot').length;
  await sandbox.review(true);
  const saved = requests.filter(r => r.url === '/api/persons/review/shot').slice(before);
  assert.equal(saved.length, 205, 'batch review applies to every selected shot');
  assert.ok(saved.every(r => JSON.parse(r.body).selections[0].actor_id === 'b'));
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 0);

  get('review-pair').value = '__none__';
  await get('review-pair').onchange();
  await get('review-select-all').onclick();
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 1);
  assert.ok(vm.runInContext("state.reviewPicked.has('source|unknown')", sandbox));
  get('review-status').value = 'accepted';
  await get('review-status').onchange();
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 0, 'status changes discard previous picks');
  assert.equal(get('review-pair').value, '', 'status changes reset the pair');
  assert.ok(get('review-pair').options.some(o => o.textContent === '甲 · 角色（2）'));
  assert.ok(get('review-pair').options.some(o => o.textContent === '全部（2）'), 'shared confirmed shots are not double-counted');
  get('review-pair').value = JSON.stringify(['a', role.id]);
  await get('review-pair').onchange();
  assert.equal(get('evidence-list').children.length, 2, 'low-scoring confirmed identity can retrieve a shared shot');
  const confirmedText = get('evidence-list').querySelectorAll('li').map(n => n.textContent).join('\n');
  assert.ok(confirmedText.includes('甲 · 角色 · 原始分数 0.410（人工确认）'));
  assert.ok(confirmedText.includes('乙 · 角色 · 分数 0.900（自动确认）'));
  assert.ok(!confirmedText.includes('原始分数 1.000'));
  assert.ok(sandbox.confirmedPersonLabel({actor_id:'a',role_id:role.id,manual:true,original_score:null})
    .includes('原始分数 未记录（人工确认）'));
  await get('review-select-all').onclick();
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 2);
  assert.ok(vm.runInContext("state.reviewPicked.has('source|shared-confirmed')", sandbox));

  get('review-status').value = 'rejected';
  await get('review-status').onchange();
  assert.equal(get('review-pair-field').hidden, true, 'rejected shots have no identity filter');
  assert.equal(get('review-pair').disabled, true);
  assert.ok(!find(get('evidence-list'), n => n.textContent.startsWith('分类：')), 'rejected cards have no category');
  assert.equal(get('review-select-all').textContent, '全选已排除（含所有页）');
  // 即使保留了旧筛选值，请求也不携带它。
  get('review-pair').value = '__none__';
  assert.equal(sandbox.reviewParams().has('no_candidate'), false);
  await get('review-select-all').onclick();
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 3, 'all rejected identities share one list');
  get('review-status').value = 'accepted';
  await get('review-status').onchange();
  assert.equal(get('review-pair-field').hidden, false, 'accepted status restores identity filtering');
  assert.equal(get('review-pair').disabled, false);
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 0);

  // 切换筛选后，迟到的旧全选请求不能恢复旧选择。
  get('review-status').value = 'candidate';
  await get('review-status').onchange();
  let releaseSelection;
  deferSelection = resolve => { releaseSelection = resolve; };
  const selecting = get('review-select-all').onclick();
  await flush();
  get('review-pair').value = '__none__';
  await get('review-pair').onchange();
  releaseSelection();
  await selecting;
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 0, 'stale selection is ignored');
  await get('review-select-all').onclick();
  get('review-source').onchange();
  await flush();
  assert.equal(vm.runInContext('state.reviewPicked.size', sandbox), 0, 'source changes discard previous picks');

  const warning = '参考照片需要恰好一张清晰人脸，且属于画面主体';
  data.references = [{id: 'legacy-role-photo', actor_id: 'a', role_id: role.id}];
  data.review_shots = {source: {accepted: 999, candidate: 999}};
  data.tasks = [{id: 'historical', source_id: 'source', status: 'complete',
    progress: {completed_shots: 3, total_shots: 3}, result: {elapsed_seconds: 2, reused_shots: 2,
      accepted_shots: 1, candidate_shots: 2, accepted: 500, candidates: 600, reference_errors: [
        {reference_id: 'removed-photo', actor_id: 'a', role_id: role.id,
          actor_name: '任务当时演员', role_name: '任务当时角色', error: warning},
        {reference_id: 'legacy-role-photo', error: warning},
        {reference_id: 'legacy-role-photo', actor_id: 'a', role_id: null,
          actor_name: '任务当时演员', role_name: null, error: warning},
      ]}}];
  await sandbox.refresh();
  const taskText = get('tasks').querySelectorAll('p').map(n => n.textContent).join('\n');
  assert.match(taskText, /3\/3 个分镜/);
  assert.match(taskText, /本次已确认 1 个分镜.*待核对 2 个分镜.*复用 2 分镜/);
  assert.ok(!taskText.includes('帧') && !taskText.includes('镜头') && !taskText.includes('999'),
    'historical shot counts remain task-specific and frame counts are omitted');
  assert.ok(taskText.includes('任务当时演员 · 任务当时角色'), 'saved names survive removed references');
  assert.ok(taskText.includes('甲 · 角色'), 'legacy logs resolve identity from existing reference');
  assert.ok(taskText.includes('任务当时演员 · 未指定角色'), 'actor-level snapshots do not inherit a later role pairing');

  // 参考图卡片带编号徽章：前 8 位与任务日志「参考图 xxxxxxxx」对应，悬停显示完整编号。
  const badge = find(sandbox.referenceCard(role, data.references[0]), n => n.className === 'reference-id');
  assert.ok(badge, 'reference card carries a uuid badge');
  assert.equal(badge.textContent, data.references[0].id.slice(0, 8));
  assert.equal(badge.title, data.references[0].id);

  get('process-source').value = 'source';
  get('process-start').value = '1.2';
  get('process-end').value = '0';
  get('process-form').onsubmit(event);
  await flush();
  const processRequest = requests.find(r => r.url === '/api/persons/sources/source/process');
  assert.deepEqual(JSON.parse(processRequest.body), {start: 1.2, end: 0, publish: false});

  linkedResult = {entity: actor, total: 1, offset: 0, limit: 50,
    items: [{shot_id: 'linked-shot', source_id: 'source', source_name: '原片', start: 10, end: 12, accepted: 1, candidate: 0}]};
  blockDeletion = true;
  await sandbox.deleteEntityButton(actor).onclick(event);
  assert.equal(get('linked-dialog').open, true, 'blocked deletion opens concrete shot dependencies');
  assert.ok(get('linked-summary').textContent.includes('不受最高分演员-角色分类限制'));
  assert.ok(find(get('linked-list'), n => n.textContent.includes('10.000–12.000 秒')));
  assert.ok(find(get('linked-list'), n => n.textContent.includes('linked-shot')));
  await find(get('linked-list'), n => n.textContent === '核对这个分镜').onclick();
  assert.equal(get('linked-dialog').open, false);
  assert.equal(get('review-dialog').open, true, 'dependent shot opens directly without classification filtering');
  const linkedChoices = get('review-choices').querySelectorAll('input');
  for (const check of linkedChoices) check.checked = JSON.parse(check.value)[0] === 'b';
  await sandbox.review(true);
  assert.equal(get('linked-dialog').open, true, 'saving returns to an updated dependency list');
  assert.ok(get('linked-summary').textContent.includes('可以删除该身份'));
  assert.equal(get('linked-list').children.length, 0);

  console.log('persons UI regression checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
