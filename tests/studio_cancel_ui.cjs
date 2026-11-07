// 创作页“取消执行”与“删除方案”的控制逻辑（真实创作页脚本代码 + 轻量 DOM 桩）。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    Object.assign(this, {
      tagName: tag,
      textContent: "",
      hidden: false,
      disabled: false,
      title: "",
      attrs: {},
    });
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }
}
const nodes = Object.fromEntries(
  ["cancel-run", "cancel-legacy", "legacy-run", "legacy-run-text", "no-job", "job-workspace"].map(
    (id) => [id, new Node()],
  ),
);
const requests = [],
  notices = [],
  failures = [],
  accepted = [],
  confirms = [];
let confirmAnswer = true,
  refreshes = 0,
  apiImpl = async () => ({});
const job = {
  id: "job",
  title: "t",
  execution: { status: "idle", current_step: null, remaining_steps: [] },
};
const data = {
  job,
  builds: [],
  active_plan: "p1",
  plans: { p1: { id: "p1", name: "方案一" } },
};
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console,
  Map,
  Promise,
  Error,
  JSON,
  String,
  Object,
  Array,
  encodeURIComponent,
  window: {
    confirm: (message) => {
      confirms.push(message);
      return confirmAnswer;
    },
  },
  state: { id: "job", data, submitting: false, cancelling: null, timer: null },
  $: (id) => nodes[id],
  base: () => "/api/studio/jobs/job",
  plan: () => data.plans[data.active_plan],
  busy: () => sandbox.state.submitting || data.builds.some((b) => b.status === "running"),
  LABEL: { tts: "配音" },
  notice: (text) => notices.push(text),
  fail: (error) => failures.push(error.message),
  updateLocks: () => {},
  refresh: async () => {
    refreshes++;
  },
  loadArtifacts: async () => {},
  accept: async (value) => {
    accepted.push(value);
  },
  clearTimeout: () => {},
  setTimeout: () => 1,
  api: async (url, method = "GET", body) => {
    requests.push({ url, method, body });
    return apiImpl(url, method, body);
  },
};
vm.createContext(sandbox);
const source = require("./studio_source.cjs").studioSource();
const slice = (start, end) => {
  const a = source.indexOf(start),
    b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `创作页脚本中找不到代码片段：${start} … ${end}`);
  return source.slice(a, b);
};
vm.runInContext(
  slice("const STATUS = {", "const state = {") +
    slice("// 取消执行与删除方案", "function renderBuilds("),
  sandbox,
);
const run = (code) => vm.runInContext(code, sandbox);
const button = nodes["cancel-run"];

(async () => {
  // 1. 状态文案：cancelled 有译文，未知状态回退原值。
  assert.equal(run('statusText("cancelled")'), "已取消");
  assert.equal(run('statusText("complete")'), "完成");
  assert.equal(run('statusText("weird_state")'), "weird_state");

  // 2. 没有执行中的构建或步骤：按钮隐藏。
  run("renderCancel()");
  assert.equal(button.hidden, true);

  // 3. 版本工作台运行中的构建：显示“取消执行”，点击后 POST 构建取消并显示“正在取消…”。
  data.builds = [
    { id: "b-old", status: "complete" },
    { id: "b1", status: "running" },
  ];
  run("renderCancel()");
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "取消执行");
  assert.ok(button.title);
  await run("cancelRun()");
  assert.deepEqual(requests.at(-1), {
    url: "/api/studio/jobs/job/builds/b1/cancel",
    method: "POST",
    body: undefined,
  });
  assert.equal(notices.at(-1), "已请求取消，当前节点完成后停止");
  assert.equal(refreshes, 1);
  run("renderCancel()"); // 构建仍在执行：保持“正在取消…”且不可重复点击。
  assert.equal(button.textContent, "正在取消…");
  assert.equal(button.disabled, true);
  data.builds[1].status = "cancelled"; // 执行结束：按钮隐藏，取消标记清除。
  run("renderCancel()");
  assert.equal(button.hidden, true);
  assert.equal(sandbox.state.cancelling, null);

  // 4. 409：执行已结束，提示错误、恢复按钮并刷新。
  data.builds = [{ id: "b2", status: "running" }];
  apiImpl = async () => {
    throw new Error("构建不在执行中，无法取消");
  };
  await run("cancelRun()");
  assert.deepEqual(failures, ["构建不在执行中，无法取消"]);
  assert.equal(sandbox.state.cancelling, null);
  assert.equal(refreshes, 2);
  run("renderCancel()");
  assert.equal(button.disabled, false);
  apiImpl = async () => ({});
  data.builds = [];

  // 5. 旧任务的步骤执行：同一按钮改为调用任务级取消接口。
  job.execution = { status: "running", current_step: "tts", remaining_steps: ["select"] };
  run("renderCancel()");
  assert.equal(button.hidden, false);
  await run("cancelRun()");
  assert.equal(requests.at(-1).url, "/api/studio/jobs/job/cancel");
  assert.equal(notices.at(-1), "已请求取消，当前步骤完成后停止");
  job.execution = { status: "cancelled", current_step: null, remaining_steps: [] };
  run("renderCancel()");
  assert.equal(button.hidden, true);

  // 6. 工作台被旧任务执行锁住时：显示执行状态与“取消执行”；执行结束后返回 false 交回正常流程。
  let detail = { job: { id: "job", execution: { status: "running", current_step: "tts" } } };
  apiImpl = async (url) => (url === "/api/studio/jobs/job" ? detail : {});
  assert.equal(await run('legacyRunView("job")'), true);
  assert.equal(nodes["legacy-run"].hidden, false);
  assert.equal(nodes["job-workspace"].hidden, true);
  assert.match(nodes["legacy-run-text"].textContent, /配音/);
  assert.equal(nodes["cancel-legacy"].textContent, "取消执行");
  await run("cancelLegacy()");
  assert.ok(requests.some((r) => r.url === "/api/studio/jobs/job/cancel" && r.method === "POST"));
  assert.equal(nodes["cancel-legacy"].textContent, "正在取消…");
  assert.equal(nodes["cancel-legacy"].disabled, true);
  detail = { job: { id: "job", execution: { status: "cancelled", current_step: null } } };
  assert.equal(await run('legacyRunView("job")'), false);
  assert.equal(nodes["legacy-run"].hidden, true);
  assert.equal(sandbox.state.cancelling, null);

  // 7. 删除方案：只剩一个方案时不请求；取消确认时不请求；确认后 DELETE 并用返回的 detail 刷新。
  requests.length = 0;
  await run("deletePlan()");
  assert.equal(requests.length, 0);
  data.plans.p2 = { id: "p2", name: "方案二" };
  confirmAnswer = false;
  await run("deletePlan()");
  assert.equal(requests.length, 0);
  assert.match(confirms.at(-1), /方案一/);
  confirmAnswer = true;
  const remaining = { job, active_plan: "p2", plans: { p2: data.plans.p2 } };
  apiImpl = async () => remaining;
  await run("deletePlan()");
  assert.deepEqual(requests.at(-1), {
    url: "/api/studio/jobs/job/plans/p1",
    method: "DELETE",
    body: undefined,
  });
  assert.equal(accepted.at(-1), remaining);
  assert.equal(notices.at(-1), "已删除方案“方案一”");
  assert.equal(sandbox.state.submitting, false);
  // 构建执行中不允许删除。
  data.builds = [{ id: "b3", status: "running" }];
  requests.length = 0;
  await run("deletePlan()");
  assert.equal(requests.length, 0);
  // 服务端拒绝（422/409）时恢复提交状态，错误交给调用方提示。
  data.builds = [];
  apiImpl = async () => {
    throw new Error("至少保留一个方案");
  };
  await assert.rejects(run("deletePlan()"), /至少保留一个方案/);
  assert.equal(sandbox.state.submitting, false);
  console.log("Studio cancel and delete-plan controls work.");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
