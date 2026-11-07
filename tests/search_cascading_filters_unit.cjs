const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { between } = require("./source_slices.cjs");
class Select {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.search = { value: "" };
  }
  get options() {
    return this.children;
  }
  get selectedOptions() {
    return this.children.filter((o) => o.selected);
  }
  replaceChildren() {
    this.children = [];
  }
  append(o) {
    this.children.push(o);
  }
  querySelector() {
    return this.children.find((o) => o.value === "__all__");
  }
  closest() {
    return { querySelector: () => this.search };
  }
  addEventListener(type, fn) {
    this.listeners[type] = fn;
  }
}
const nodes = new Map();
const get = (id) => {
  if (!nodes.has(id)) {
    nodes.set(id, new Select());
  }
  return nodes.get(id);
};
const people = [
  { id: "a", name: "甲", kind: "actor" },
  { id: "b", name: "乙", kind: "actor" },
  { id: "c", name: "丙", kind: "actor" },
  { id: "unused", name: "未关联演员", kind: "actor" },
  { id: "r1", name: "角色一", kind: "role", actor_ids: ["a", "b"], work_ids: ["w1"] },
  { id: "r2", name: "角色二", kind: "role", actor_ids: ["b", "c"], work_ids: ["w2"] },
  { id: "r3", name: "角色三", kind: "role", actor_ids: ["a"], work_ids: ["w1"] },
  { id: "orphan", name: "未配对角色", kind: "role", actor_ids: [], work_ids: ["w1"] },
];
const sources = [
  { id: "s1", name: "素材一", work_id: "w1" },
  { id: "s1b", name: "素材一第二集", work_id: "w1" },
  { id: "s2", name: "素材二", work_id: "w2" },
  { id: "empty", name: "空素材", work_id: "empty" },
];
let groupSelection = [];
let groupRefreshes = 0;
const context = vm.createContext({
  $: get,
  registeredPeople: people,
  registeredSources: sources,
  sourceScope: null,
  groupSourceScope: null,
  mode: "text",
  searchGroups: {
    values: () => groupSelection,
    setValues: (ids) => {
      groupSelection = [...ids];
    },
  },
  searchGroupLink: {
    refresh: async () => {
      groupRefreshes++;
    },
  },
  element: () => ({ selected: false }),
  MindexI18n: { msg: (k) => k, setText() {} },
  MindexMultiSearch: { sort: (e) => e, attach() {}, refilter() {} },
});
const code = fs.readFileSync("frontend/src/search/app.js", "utf8");
vm.runInContext(between(code, "const ALL_VALUE =", "async function refresh()"), context);
vm.runInContext(
  between(
    code,
    'for (const id of ["actor-select", "role-select", "source"])',
    "\nrefresh();",
    code.indexOf("async function refresh()"),
  ),
  context,
);
const values = (id) =>
  get(id)
    .options.filter((o) => o.value !== "__all__")
    .map((o) => o.value)
    .sort();
const picked = (id) =>
  get(id)
    .selectedOptions.filter((o) => o.value !== "__all__")
    .map((o) => o.value)
    .sort();
function choose(id, ids) {
  for (const o of get(id).options) {
    o.selected = ids.includes(o.value);
  }
  get(id).listeners.change();
}
function reset() {
  const previousRefreshes = groupRefreshes;
  get("filter-reset").onclick();
  assert.deepEqual(groupSelection, [], "reset clears the group filter");
  assert.equal(context.groupSourceScope, null, "reset clears the resolved group scope");
  assert.equal(groupRefreshes, previousRefreshes + 1, "reset refreshes the linked source options");
}
reset();
assert.deepEqual(picked("source"), ["empty", "s1", "s1b", "s2"]);
assert.deepEqual(values("actor-select"), ["a", "b", "c", "unused"]);
choose("source", ["s1"]);
assert.deepEqual(values("actor-select"), ["a", "b"]);
assert.deepEqual(values("role-select"), ["orphan", "r1", "r3"]);
choose("actor-select", ["b"]);
assert.deepEqual(values("role-select"), ["r1"]);
assert.deepEqual(values("source"), ["s1", "s1b", "s2"]);
assert.deepEqual(picked("source"), ["s1"], "explicit material selection is retained");
choose("role-select", ["r1"]);
choose("source", ["s1b"]);
assert.deepEqual(picked("role-select"), ["r1"], "valid role survives material change");
reset();
choose("actor-select", ["c"]);
assert.deepEqual(values("role-select"), ["r2"]);
assert.deepEqual(values("source"), ["s2"]);
assert.deepEqual(picked("source"), ["s2"], "automatic all-source scope follows the actor");
choose("actor-select", []);
assert.deepEqual(values("source"), ["empty", "s1", "s1b", "s2"]);
assert.deepEqual(picked("source"), ["empty", "s1", "s1b", "s2"]);
choose("role-select", ["r2"]);
assert.deepEqual(values("actor-select"), ["b", "c"]);
assert.deepEqual(values("source"), ["s2"]);
choose("role-select", []);
assert.deepEqual(values("actor-select"), ["a", "b", "c", "unused"]);
reset();
choose("actor-select", ["a", "c"]);
assert.deepEqual(values("role-select"), ["r1", "r2", "r3"]);
assert.deepEqual(values("source"), ["s1", "s1b", "s2"]);
reset();
choose("source", ["s1", "s2"]);
assert.deepEqual(values("actor-select"), ["a", "b", "c"]);
choose("actor-select", ["a"]);
assert.deepEqual(picked("source"), ["s1"]);
choose("actor-select", []);
assert.deepEqual(
  picked("source"),
  ["s1", "s2"],
  "clearing people restores the explicit material scope",
);
choose("source", ["empty"]);
assert.deepEqual(values("actor-select"), []);
assert.deepEqual(values("role-select"), []);
reset();
choose("source", ["s1"]);
choose("actor-select", ["__all__"]);
assert.deepEqual(picked("actor-select"), ["a", "b"]);
assert.deepEqual(values("role-select"), ["r1", "r3"]);
choose("role-select", ["__all__"]);
assert.deepEqual(picked("role-select"), ["r1", "r3"]);
const all = get("role-select").querySelector();
all.selected = false;
get("role-select").listeners.change();
assert.deepEqual(picked("role-select"), [], "toggling all off clears the filtered role selection");
vm.runInContext("fillOptions('source', registeredSources);fillSearchFilters()", context);
assert.deepEqual(picked("source"), ["s1"], "refresh preserves the explicit source scope");
reset();
assert.deepEqual(picked("actor-select"), []);
assert.deepEqual(picked("role-select"), []);
assert.deepEqual(picked("source"), ["empty", "s1", "s1b", "s2"]);
choose("actor-select", ["c"]);
context.mode = "image";
context.fillSearchFilters();
assert.deepEqual(
  values("source"),
  ["empty", "s1", "s1b", "s2"],
  "hidden person controls do not narrow image/video materials",
);
groupSelection = ["group-two"];
context.mode = "text";
context.groupSourceScope = new Set(["s2"]);
context.fillSearchFilters();
assert.deepEqual(values("source"), ["s2"], "group membership narrows available materials");
assert.deepEqual(values("actor-select"), ["b", "c"], "people follow the selected group scope");
assert.deepEqual(values("role-select"), ["r2"]);
reset();
assert.deepEqual(
  values("source"),
  ["empty", "s1", "s1b", "s2"],
  "reset restores materials outside the previous group",
);

reset();
choose("source", []);
assert.deepEqual(
  picked("source"),
  [],
  "deselecting all sources must retain an explicit empty scope",
);
vm.runInContext("fillSearchFilters()", context);
assert.deepEqual(picked("source"), [], "rerender must not silently select all sources");
reset();
choose("source", ["empty"]);
choose("source", []);
assert.deepEqual(picked("source"), [], "the final selected source can be deselected");
reset();
assert.equal(picked("source").length, 4);

// Explicitly empty UI selection must never issue an unrestricted API search.
const notices = [];
context.busy = false;
context.notice = (message, kind) => notices.push({ message, kind });
vm.runInContext(between(code, '$("search-form").onsubmit =', "// 多选框顶部"), context);
choose("source", []);
(async () => {
  let prevented = false;
  await get("search-form").onsubmit({
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(prevented, true);
  assert.equal(notices.at(-1).message, "ui.search_source_required");
  assert.equal(notices.at(-1).kind, "error");
  console.log("Search cascading filter checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
