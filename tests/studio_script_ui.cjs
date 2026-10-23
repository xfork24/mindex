// 文案创作区域的控制逻辑：状态文字、未保存修改保护、保存与重新生成（真实 studio.js 代码 + 轻量 DOM 桩）。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    Object.assign(this, { tagName: tag, textContent: "", value: "", hidden: false, disabled: false, dataset: {}, attrs: {} });
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
}
const nodes = Object.fromEntries(
  ["script-text", "script-state", "script-generate", "script-save", "script-counter", "script-outline", "script-stale", "script-error"]
    .map(id => [id, new Node(id === "script-text" ? "textarea" : "div")]),
);
const requests = [], runs = [], confirms = [], accepted = [];
let confirmAnswer = true, scope = "job/p1", busyNow = false, lastScriptNode = null;
const job = { outline: "写一个新加坡旅游视频", title: "t" };
const plans = {
  p1: { id: "p1", script: null, board: null, shots: {}, ranges: {} },
};
const versions = {};
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console, Map, Promise, Error, JSON, String, Boolean,
  window: { confirm: message => { confirms.push(message); return confirmAnswer; } },
  state: { data: { job, builds: [], script_stale: false, revision: "r1", plans, active_plan: "p1", versions } },
  $: id => nodes[id],
  plan: () => plans.p1,
  ver: id => versions[id],
  scopeKey: () => scope,
  planBase: () => "/plan",
  busy: () => busyNow,
  shortId: id => String(id).slice(0, 8),
  shortIds: value => String(value),
  latestNode: (stage, sid) => (stage === "script" && sid === null ? lastScriptNode : undefined),
  notice: () => {},
  runNode: async (...args) => { runs.push(args); },
  api: async (url, method, body) => { requests.push({ url, method, body }); return {}; },
  accept: async data => { accepted.push(data); },
};
vm.createContext(sandbox);
const source = fs.readFileSync("frontend/src/studio/studio.js", "utf8");
vm.runInContext(source.slice(source.indexOf("const scriptDrafts"), source.indexOf("async function inputUrl(")), sandbox);
const area = nodes["script-text"];
const view = () => vm.runInContext("renderScript()", sandbox);
const sync = () => vm.runInContext("syncScriptControls()", sandbox);
const type = value => {
  area.value = value;
  vm.runInContext(`scriptDrafts.set(scopeKey(), { base: plan().script || "", value: ${JSON.stringify(value)} })`, sandbox);
  sync();
};
function setScript(id, text, origin = "generated") {
  versions[id] = { id, stage: "script", data: { text, origin } };
  plans.p1.script = id;
}

(async () => {
  // 1. 默认：文案待创作，只能生成。
  view();
  assert.equal(nodes["script-state"].textContent, "文案待创作");
  assert.equal(nodes["script-generate"].textContent, "生成文案");
  assert.equal(nodes["script-save"].disabled, true);
  assert.equal(nodes["script-generate"].disabled, false);
  assert.equal(nodes["script-outline"].hidden, false);
  assert.match(nodes["script-outline"].textContent, /写一个新加坡旅游视频/);
  assert.equal(nodes["script-stale"].hidden, true);

  // 首次生成不需要确认。
  await vm.runInContext("generateScript()", sandbox);
  assert.deepEqual(runs.pop(), ["script", null, true]);
  assert.equal(confirms.length, 0);

  // 2. 生成后：展示文案与来源，按钮变为"重新生成"。
  setScript("aaaaaaaa11111111", "第一版文案。");
  view();
  assert.equal(area.value, "第一版文案。");
  assert.equal(nodes["script-state"].textContent, "模型生成 · 版本 aaaaaaaa");
  assert.equal(nodes["script-generate"].textContent, "重新生成文案");
  assert.equal(nodes["script-save"].disabled, true);
  assert.equal(vm.runInContext("scriptDirty()", sandbox), false);

  // 3. 编辑后：未保存提示、可保存，并阻止构建类操作（hasDirtyDraft 依赖 scriptDirty）。
  type("第一版文案。改了一句。");
  assert.equal(vm.runInContext("scriptDirty()", sandbox), true);
  assert.match(nodes["script-state"].textContent, /有未保存的修改/);
  assert.equal(nodes["script-save"].disabled, false);
  // 同一版本内再次渲染（例如轮询刷新）不能覆盖输入。
  view();
  assert.equal(area.value, "第一版文案。改了一句。");

  // 4. 保存：只提交去掉首尾空白后的文本，带当前 revision。
  type("  第一版文案。改了一句。  ");
  await vm.runInContext("saveScript()", sandbox);
  // vm 内创建的对象原型不同，按 JSON 比较。
  assert.deepEqual(JSON.parse(JSON.stringify(requests.pop())),
    { url: "/plan", method: "PATCH", body: { revision: "r1", script: "第一版文案。改了一句。" } });
  assert.equal(accepted.length, 1);
  // 服务端返回新版本后文本框重置为新版本内容，草稿被清掉。
  setScript("bbbbbbbb22222222", "第一版文案。改了一句。", "manual");
  view();
  assert.equal(area.value, "第一版文案。改了一句。");
  assert.equal(nodes["script-state"].textContent, "手动编辑 · 版本 bbbbbbbb");
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

  // 已有分镜时重新生成要提示分镜会过期；没有分镜则直接执行。
  // （真实流程里新版本落定后文本框会重置；这里直接还原为已保存内容。）
  area.value = versions[plans.p1.script].data.text;
  view();
  plans.p1.board = "board1";
  await vm.runInContext("generateScript()", sandbox);
  assert.match(confirms.pop(), /分镜将过期/);
  runs.length = 0;
  plans.p1.board = null;
  await vm.runInContext("generateScript()", sandbox);
  assert.equal(confirms.length, 0);
  assert.equal(runs.length, 1);

  // 6. 分镜过期提示。
  sandbox.state.data.script_stale = true;
  view();
  assert.equal(nodes["script-stale"].hidden, false);

  // 7. 执行中：整体锁定；运行中的文案节点显示"生成中"。
  busyNow = true;
  sandbox.state.data.builds = [{ status: "running", plan_id: "p1", nodes: [{ stage: "script", status: "running" }] }];
  view();
  assert.equal(area.disabled, true);
  assert.equal(nodes["script-generate"].disabled, true);
  assert.equal(nodes["script-save"].disabled, true);
  assert.equal(nodes["script-state"].textContent, "生成中…");
  busyNow = false;
  sandbox.state.data.builds = [];

  // 7b. 最近一次生成失败：直接显示原因；有了更新的版本后不再显示。
  lastScriptNode = { status: "failed", error: "文案服务不可用", finished_at: "2026-10-23T10:00:00" };
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

  // 8. 老任务没有大纲：不能生成，但已有文案可以编辑保存。
  job.outline = "";
  view();
  assert.equal(nodes["script-generate"].disabled, true);
  assert.equal(nodes["script-outline"].hidden, true);

  // 9. 切换方案：文本框内容按方案区分，未保存内容按方案暂存。
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
  console.log("Script section keeps unsaved edits, confirms destructive regeneration and saves trimmed text.");
})().catch(error => { console.error(error); process.exitCode = 1; });
