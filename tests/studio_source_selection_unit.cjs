const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag) {Object.assign(this, {tag, children: [], dataset: {}, events: {}, selected: false});}
  get options() {return this.children;}
  get selectedOptions() {return this.children.filter(o => o.selected);}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.children = nodes;}
  addEventListener(type, fn) {this.events[type] = fn;}
}
const state = {
  sources: ['s1', 's2', 's3'].map(id => ({id, name: id})),
  persons: [], personSources: [], shotCards: new Map(), data: {job: {source_ids: null}},
};
const context = vm.createContext({
  state, document: {createElement: tag => new Node(tag)},
  el: tag => new Node(tag), field: () => ({wrap: new Node('label'), input: new Node('input')}),
  updateLocks() {}, refreshShotCard() {},
  MindexI18n: {msg: id => id, setAttr() {}},
  MindexMultiSearch: {sort: x => x, attach() {}, refilter() {}},
  RECALL_MODES: [],
});
const code = require("./studio_source.cjs").studioSource();
vm.runInContext(code.slice(code.indexOf('const ALL_VALUE'), code.indexOf('const LABEL')), context);
vm.runInContext(code.slice(code.indexOf('function jobScope('), code.indexOf('function personName(')), context);
vm.runInContext(code.slice(code.indexOf('function recallEditable('), code.indexOf('function renderShotEditor(')), context);
const body = new Node('div');
const draft = {data: {recall: null}, original: {recall: null}, dirty: false};
context.renderRecallEditor(body, 'shot', draft, new Node('div'));
function find(root, predicate) {
  if (predicate(root)) return root;
  return root.children.map(n => find(n, predicate)).find(Boolean);
}
function select() {return find(body, n => n.dataset.recallFilter === 'source_ids');}
function choose(ids) {
  const control = select();
  for (const o of control.options) o.selected = ids.includes(o.value);
  control.events.change();
}
const picked = () => Array.from(context.pickedIds(select()));
assert.deepEqual(picked(), ['s1', 's2', 's3']);
const originalControl = select();
const originalOptions = originalControl.options.slice();
const originalScene = find(body, n => n.dataset.recallFilter === 'scene');
originalControl.dataset.query = '素材';
originalControl.scrollTop = 37;
choose(['s2']);
assert.equal(select(), originalControl, 'choosing materials must not recreate the active control');
assert.equal(select().options[2], originalOptions[2], 'keep native options and selection interaction intact');
assert.equal(find(body, n => n.dataset.recallFilter === 'scene'), originalScene, 'unrelated fields must not be rebuilt');
assert.equal(select().dataset.query, '素材', 'retain the search filter');
assert.equal(select().scrollTop, 37, 'retain scroll position');
assert.deepEqual(picked(), ['s2'], 'a regular option replaces the initial all selection');
assert.deepEqual(Array.from(draft.data.recall.filters.source_ids), ['s2'], 'subset is committed to the shot draft');
assert.equal(draft.dirty, true);
choose(['s1', 's2']);
assert.deepEqual(picked(), ['s1', 's2'], 'multiple source choices update the same editor');
choose(['__all__']);
assert.deepEqual(picked(), ['s1', 's2', 's3']);
assert.equal(draft.data.recall, null, 'all restores default recall and existing version signatures');
assert.equal(draft.dirty, false);
// Ctrl/Cmd-click removes one real option while the all row was previously selected.
choose(['__all__', 's1', 's3']);
assert.deepEqual(picked(), ['s1', 's3']);
choose(['__all__']);
choose(['s3']);
assert.deepEqual(picked(), ['s3'], 'selection remains editable after returning to all');
// Job-level scope is still the hard limit on the per-shot options.
state.data.job.source_ids = ['s1', 's2'];
const scopedBody = new Node('div');
context.renderRecallEditor(scopedBody, 'scoped', {data: {recall: null}, original: {recall: null}}, new Node('div'));
const scoped = find(scopedBody, n => n.dataset.recallFilter === 'source_ids');
assert.deepEqual(Array.from(context.pickedIds(scoped)), ['s1', 's2']);
console.log('Studio source selection checks passed');

// Changing materials still refreshes the dependent person options and removes stale selections.
state.data.job.source_ids = null;
state.personSources = [{id: 's1', work_id: 'w1'}, {id: 's2', work_id: 'w2'}];
state.persons = [
  {id: 'a1', kind: 'actor', name: '演员一'}, {id: 'a2', kind: 'actor', name: '演员二'},
  {id: 'r1', kind: 'role', name: '角色一', actor_ids: ['a1'], work_ids: ['w1']},
  {id: 'r2', kind: 'role', name: '角色二', actor_ids: ['a2'], work_ids: ['w2']},
];
const personBody = new Node('div');
const personDraft = {data: {recall: {person_ids: ['a1', 'r1']}}, original: {recall: null}};
context.renderRecallEditor(personBody, 'people', personDraft, new Node('div'));
const sourceControl = find(personBody, n => n.dataset.recallFilter === 'source_ids');
function selects(root) {return [root, ...root.children.flatMap(selects)].filter(n => n.tag === 'select');}
const [actorControl, roleControl] = selects(personBody).filter(n => n !== sourceControl);
for (const option of sourceControl.options) option.selected = option.value === 's2';
sourceControl.events.change();
assert.deepEqual(Array.from(context.itemOptionsOf(actorControl), o => o.value), ['a2']);
assert.deepEqual(Array.from(context.itemOptionsOf(roleControl), o => o.value), ['r2']);
assert.deepEqual(Array.from(personDraft.data.recall.person_ids), [], 'remove identities outside the new material scope');
assert.equal(find(personBody, n => n.dataset.recallFilter === 'source_ids'), sourceControl);
console.log('Stable recall controls and dependent person filters passed');
