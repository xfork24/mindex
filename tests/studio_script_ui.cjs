// 文案创作区域的控制逻辑：状态文字、未保存修改保护、自动保存、重新生成与历史版本切换
// （真实创作页脚本代码 + 轻量 DOM 桩）。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    Object.assign(this, {
      tagName: tag,
      textContent: "",
      value: "",
      hidden: false,
      disabled: false,
      dataset: {},
      attrs: {},
      children: [],
    });
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  querySelector(sel) {
    return sel === "select" ? this.children.find((c) => c.tagName === "select") || null : null;
  }
  addEventListener() {}
}
const nodes = Object.fromEntries(
  [
    "script-text",
    "script-generate",
    "script-counter",
    "script-outline",
    "script-stale",
    "script-error",
    "script-history",
  ].map((id) => [id, new Node(id === "script-text" ? "textarea" : "div")]),
);
const requests = [],
  runs = [],
  confirms = [],
  accepted = [];
let confirmAnswer = true,
  scope = "job/p1",
  busyNow = false,
  lastScriptNode = null;
const job = { outline: "写一个新加坡旅游视频", title: "t" };
const plans = {
  p1: { id: "p1", script: null, board: null, shots: {}, ranges: {} },
};
const versions = {};
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console,
  Map,
  Promise,
  Error,
  JSON,
  String,
  Boolean,
  Object,
  Date,
  Array,
  Number,
  RegExp,
  window: {
    confirm: (message) => {
      confirms.push(message);
      return confirmAnswer;
    },
  },
  document: { createElement: (tag) => new Node(tag) },
  state: {
    data: {
      job,
      builds: [],
      script_stale: false,
      revision: "r1",
      plans,
      active_plan: "p1",
      versions,
    },
  },
  $: (id) => nodes[id],
  el: (tag, text = "", cls = "") => {
    const n = new Node(tag);
    n.textContent = text;
    n.className = cls;
    return n;
  },
  button: (text, action) => {
    const n = new Node("button");
    n.textContent = text;
    n.onclick = action;
    return n;
  },
  mutationButton: (text, action) => {
    const n = new Node("button");
    n.textContent = text;
    n.onclick = action;
    return n;
  },
  selectionWithin: () => false,
  showVersion: () => {},
  hasDirtyDraft: () => false,
  updateLocks: () => {},
  plan: () => plans.p1,
  ver: (id) => versions[id],
  scopeKey: () => scope,
  planBase: () => "/plan",
  busy: () => busyNow,
  shortId: (id) => String(id).slice(0, 8),
  shortIds: (value) => String(value),
  latestNode: (stage, sid) => (stage === "script" && sid === null ? lastScriptNode : undefined),
  notice: () => {},
  runNode: async (...args) => {
    runs.push(args);
  },
  api: async (url, method, body) => {
    requests.push({ url, method, body });
    return {};
  },
  accept: async (data) => {
    accepted.push(data);
  },
  renderTemplateSelect: () => {}, // 模板下拉在 studio-templates.js，另有用例覆盖
  renderReferences: () => {}, // 参考文稿面板同在 studio-templates.js
  planReferences: () => plans.p1.options?.references || [],
};
vm.createContext(sandbox);
const source = require("./studio_source.cjs").studioSource();
vm.runInContext(
  source.slice(source.indexOf("const scriptDrafts"), source.indexOf("async function inputUrl(")),
  sandbox,
);
const area = nodes["script-text"];
const view = () => vm.runInContext("renderScript()", sandbox);
const sync = () => vm.runInContext("syncScriptControls()", sandbox);
const type = (value) => {
  area.value = value;
  vm.runInContext(
    `scriptDrafts.set(scopeKey(), { base: plan().script || "", value: ${JSON.stringify(value)} })`,
    sandbox,
  );
  sync();
};
function setScript(id, text, origin = "generated") {
  versions[id] = {
    id,
    stage: "script",
    shot_id: null,
    created_at: "2026-10-23T00:00:00",
    data: { text, origin },
  };
  plans.p1.script = id;
}

(async () => {
  // 1. 默认：标题栏固定为「文案创作」（无状态后缀），只能生成。
  view();
  assert.equal(nodes["script-generate"].textContent, "生成文案");
  assert.equal(nodes["script-generate"].disabled, false);
  assert.equal(nodes["script-outline"].hidden, false);
  assert.match(nodes["script-outline"].textContent, /写一个新加坡旅游视频/);
  assert.equal(nodes["script-stale"].hidden, true);
  // 没有任何文案版本时历史区显示占位。
  assert.equal(nodes["script-history"].children.length, 1);

  // 首次生成不需要确认。
  await vm.runInContext("generateScript()", sandbox);
  assert.deepEqual(runs.pop(), ["script", null, true]);
  assert.equal(confirms.length, 0);

  // 2. 生成后：标题栏只保留「文案创作」（状态栏为空），版本来源移到历史版本区；按钮变为“重新生成”。
  setScript("aaaaaaaa11111111", "第一版文案。");
  view();
  assert.equal(area.value, "第一版文案。");
  assert.equal(nodes["script-generate"].textContent, "重新生成文案");
  assert.equal(vm.runInContext("scriptDirty()", sandbox), false);
  // 词数统计（按空白切分）：整段中文无空格计为 1 词。
  assert.match(nodes["script-counter"].textContent, /^1 词$/);

  // 3. 编辑后：标记未保存（scriptDirty），阻止构建类操作（hasDirtyDraft 依赖 scriptDirty）。
  type("第一版文案。改了一句。");
  assert.equal(vm.runInContext("scriptDirty()", sandbox), true);
  // 同一版本内再次渲染（例如轮询刷新）不能覆盖输入。
  view();
  assert.equal(area.value, "第一版文案。改了一句。");

  // 4. 自动保存（失焦触发）：只提交去掉首尾空白后的文本，带当前 revision。
  type("  第一版文案。改了一句。  ");
  await vm.runInContext("saveScript()", sandbox);
  // vm 内创建的对象原型不同，按 JSON 比较。
  assert.deepEqual(JSON.parse(JSON.stringify(requests.pop())), {
    url: "/plan",
    method: "PATCH",
    body: { revision: "r1", script: "第一版文案。改了一句。" },
  });
  assert.equal(accepted.length, 1);
  // 服务端返回新版本后文本框重置为新版本内容，草稿被清掉。
  setScript("bbbbbbbb22222222", "第一版文案。改了一句。", "manual");
  view();
  assert.equal(area.value, "第一版文案。改了一句。");
  assert.equal(vm.runInContext("scriptDrafts.has(scopeKey())", sandbox), false);

  // 内容没有变化时不发请求。
  await vm.runInContext("saveScript()", sandbox);
  assert.equal(requests.length, 0);

  // 5. 重新生成：有未保存修改时要确认，拒绝则什么都不做。
  type("还没保存的内容");
  confirmAnswer = false;
  await vm.runInContext("generateScript()", sandbox);
  assert.equal(runs.length, 0);
  assert.match(confirms.pop(), /未保存的修改/);
  confirmAnswer = true;
  await vm.runInContext("generateScript()", sandbox);
  assert.deepEqual(runs.pop(), ["script", null, true]);
  assert.match(confirms.pop(), /未保存的修改/);
  assert.equal(vm.runInContext("scriptDrafts.has(scopeKey())", sandbox), false);

  // 已有分镜时重新生成要提示会清空分镜工作台；没有分镜则直接执行。
  // （真实流程里新版本落定后文本框会重置；这里直接还原为已保存内容。）
  area.value = versions[plans.p1.script].data.text;
  view();
  plans.p1.board = "board1";
  await vm.runInContext("generateScript()", sandbox);
  assert.match(confirms.pop(), /清空当前分镜工作台/);
  runs.length = 0;
  plans.p1.board = null;
  await vm.runInContext("generateScript()", sandbox);
  assert.equal(confirms.length, 0);
  assert.equal(runs.length, 1);

  // 6. 历史版本：列出全部文案版本，可切换（选为构建输入）。
  view();
  const history = nodes["script-history"];
  const select = history.children.find((c) => c.tagName === "select");
  assert.ok(select, "历史区应渲染一个下拉选择");
  assert.equal(select.children.length, 2); // 两个文案版本
  const useButton = history.children.find(
    (c) => c.tagName === "button" && c.textContent === "选为构建输入",
  );
  assert.ok(useButton, "历史区应有“选为构建输入”按钮");
  // 切到第一版并选用：POST select-version，stage=script。
  select.value = "aaaaaaaa11111111";
  await useButton.onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(requests.pop())), {
    url: "/plan/select-version",
    method: "POST",
    body: { revision: "r1", version_id: "aaaaaaaa11111111", stage: "script", shot_id: null },
  });

  // 6b. 重新生成产生新版本并自动切为当前版本：历史下拉随之跳到新版本，即便之前手动选过旧版本。
  setScript("cccccccc33333333", "第三版文案。");
  view();
  const afterRegen = nodes["script-history"].children.find((c) => c.tagName === "select");
  assert.equal(afterRegen.children.length, 3);
  assert.equal(afterRegen.value, "cccccccc33333333");

  // 7. 分镜过期提示。
  sandbox.state.data.script_stale = true;
  view();
  assert.equal(nodes["script-stale"].hidden, false);

  // 8. 执行中：整体锁定（文本框与生成按钮禁用）。
  busyNow = true;
  sandbox.state.data.builds = [
    { status: "running", plan_id: "p1", nodes: [{ stage: "script", status: "running" }] },
  ];
  view();
  assert.equal(area.disabled, true);
  assert.equal(nodes["script-generate"].disabled, true);
  busyNow = false;
  sandbox.state.data.builds = [];
  sandbox.state.data.script_stale = false;

  // 8b. 最近一次生成失败：直接显示原因；有了更新的版本后不再显示。
  lastScriptNode = {
    status: "failed",
    error: "文案服务不可用",
    finished_at: "2026-10-23T10:00:00",
  };
  versions[plans.p1.script].created_at = "2026-10-23T09:00:00";
  view();
  assert.equal(nodes["script-error"].hidden, false);
  assert.match(nodes["script-error"].textContent, /文案生成失败：文案服务不可用/);
  versions[plans.p1.script].created_at = "2026-10-23T11:00:00"; // 之后手动保存了新版本
  view();
  assert.equal(nodes["script-error"].hidden, true);
  lastScriptNode = { status: "complete" };
  view();
  assert.equal(nodes["script-error"].hidden, true);
  lastScriptNode = null;

  // 9. 老任务没有大纲：不能生成，但已有文案可以编辑保存。
  job.outline = "";
  view();
  assert.equal(nodes["script-generate"].disabled, true);
  assert.equal(nodes["script-outline"].hidden, true);

  // 10. 切换方案：文本框内容按方案区分，未保存内容按方案暂存。
  job.outline = "写一个新加坡旅游视频";
  type("方案一的草稿");
  scope = "job/p2";
  const savedPlan = plans.p1;
  plans.p1 = { id: "p2", script: null, board: null, shots: {}, ranges: {} };
  view();
  assert.equal(area.value, "");
  scope = "job/p1";
  plans.p1 = savedPlan;
  view();
  assert.equal(area.value, "方案一的草稿");
  console.log(
    "Script section: keeps unsaved edits, auto-saves trimmed text, counts words, and switches history versions.",
  );
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
