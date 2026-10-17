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
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute(key) { delete this[key]; }
  pause() { this.paused = true; }
  load() {}
  reset() {}
  showModal() {}
  close() {}
  setPointerCapture() {}
  getBoundingClientRect() { return {left: 0, top: 0, width: 320, height: 240}; }
  getContext() { return {clearRect() {}, drawImage() {}, strokeRect() {}}; }
}
const nodes = new Map();
const selectIds = new Set(['capture-source', 'capture-role', 'capture-actor', 'entity-actors',
  'entity-kind', 'entity-work', 'import-work', 'process-source', 'review-source',
  'filter-work', 'filter-actor', 'filter-role', 'reference-actor']);
function get(id) {
  if (!nodes.has(id)) nodes.set(id, new Element(selectIds.has(id) ? 'select' : 'div'));
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
const sandbox = {
  console, URLSearchParams, FormData, location: {search: ''}, setInterval() {},
  document: {getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => []},
  fetch: async (url, options = {}) => {
    requests.push({url, ...options});
    return {ok: true, json: async () => url === '/api/persons' ? data : {}};
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('src/mindex/static/persons.js', 'utf8'), sandbox);
const flush = () => new Promise(resolve => setImmediate(resolve));
const event = {preventDefault() {}};
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

  get('capture-source').value = 'source';
  get('capture-actor').value = other.id;
  sandbox.fillCaptureRoles();
  get('capture-role').value = role.id;
  get('capture-role').onchange();
  assert.equal(get('capture-actor').value, other.id, 'selecting a role must not replace the chosen performer');

  get('capture-time').value = '1';
  await get('capture-preview').onclick();
  const video = get('capture-video');
  video.currentTime = 1.75;
  video.onseeked();
  assert.equal(Number(get('capture-time').value), 1.75, 'paused seeking updates the saved timestamp');
  assert.equal(get('capture-canvas').hidden, false);
  get('capture-canvas').onpointerdown({clientX: 10, clientY: 20, pointerId: 1});
  get('capture-canvas').onpointerup({clientX: 100, clientY: 140});
  get('capture-form').onsubmit(event);
  await flush();
  const captured = JSON.parse(requests.find(r => r.url.endsWith('/references/from-source')).body);
  assert.equal(captured.timestamp, 1.75);
  assert.deepEqual(captured.bbox, [10, 20, 100, 140]);
  assert.equal(captured.actor_id, other.id);

  vm.runInContext('capture.box=[1,2,100,140];capture.ready=true', sandbox);
  get('capture-source').onchange();
  assert.equal(vm.runInContext('capture.box', sandbox), null, 'changing sources discards the old crop');
  assert.equal(get('capture-canvas').hidden, true);
  assert.equal(video.hidden, true);
  console.log('persons UI regression checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
