const { between } = require("./source_slices.cjs");
// 分镜工作台固定五列：召回列统一显示各分镜的画面阶段（召回 / 生成图片 / 生成画面 / 生成动画），
// 上传素材的分镜显示素材预览；列操作用虚拟列 picture 一次覆盖全部画面阶段。真实创作页脚本 + 轻量桩，不依赖浏览器。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const dir = path.join(__dirname, "../frontend/src/studio");
const read = (name) => fs.readFileSync(path.join(dir, name), "utf8");
const slice = (text, start, end) => between(text, start, end);
const requests = [];
const notices = [];
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console,
  Map,
  Set,
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
  document: { getElementById: () => null },
  render: () => {},
  refresh: async () => {},
  hasDirtyDraft: () => false,
};
vm.createContext(sandbox);
vm.runInContext(read("studio-state.js"), sandbox);
const stages = read("studio-stages.js");
vm.runInContext(
  slice(stages, "async function regenerateColumn(", "function renderLogShell("),
  sandbox,
);
vm.runInContext(slice(stages, "function nodeInputs(", "async function runNode("), sandbox);
vm.runInContext(
  `notice = (text) => __notices.push(text); api = async (url, method, body) => { __requests.push({ url, method, body }); return {}; };`,
  Object.assign(sandbox, { __requests: requests, __notices: notices }),
);
const run = (code) => vm.runInContext(code, sandbox);

const shots = {
  r: { content: "c-r", recall: "v-r", tts: "t-r" },
  d: { content: "c-d", tts: "t-d" },
  g: { content: "c-g", tts: "t-g" },
  a: { content: "c-a", tts: null },
  i: { content: "c-i", tts: "t-i" },
};
run(
  `state.id = "job"; state.data = ${JSON.stringify({
    revision: "rev",
    active_plan: "p1",
    builds: [],
    plans: { p1: { id: "p1", board: "b", shots, ranges: {}, options: {} } },
    versions: { b: { data: { order: ["r", "d", "g", "a", "i"] } } },
    shot_stages: {
      r: ["recall", "tts", "select", "render"],
      d: ["tts", "draw", "render"],
      g: ["tts", "generate", "select", "render"],
      a: ["tts", "animate", "render"],
      i: ["tts", "render"],
    },
  })};`,
);

(async () => {
  // 1. 无论方案里有哪些画面来源，分镜表都只有五列（文案与查询 + 召回、配音、选片与对齐、分镜渲染）。
  assert.deepEqual([...run("matrixStages()")], ["recall", "tts", "select", "render"]);
  // 2. 召回列显示各分镜实际的画面阶段；上传图片分镜显示素材预览（source）；其他列不变。
  assert.deepEqual(
    ["r", "d", "g", "a", "i"].map((sid) => run(`cellStage(${JSON.stringify(sid)}, "recall")`)),
    ["recall", "draw", "generate", "animate", "source"],
  );
  assert.equal(run(`cellStage("g", "select")`), "select");
  assert.equal(run(`pictureStage("i")`), null);
  // 3. 召回列的列操作：虚拟列 picture，绑定每个有画面阶段的分镜在该阶段的输入；上传素材的分镜不参与。
  await run(`regenerateColumn(PICTURE_COLUMN, false)`);
  const [request] = requests;
  assert.equal(request.url, "/api/studio/jobs/job/plans/p1/execute-column");
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), {
    revision: "rev",
    stage: "picture",
    force: false,
    inputs_by_shot: {
      r: { content: "c-r" },
      d: { content: "c-d" },
      g: { content: "c-g", tts: "t-g" },
      a: { content: "c-a", tts: null },
    },
  });
  assert.equal(notices.length, 1);
  console.log("studio picture column ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
