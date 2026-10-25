// 分镜卡片的阶段模型：barStages（可打开的阶段）、stageState（五种状态）、cardStage（卡片显示的阶段）。
// 运行真实的 studio-state.js，不依赖浏览器。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console, Map, Set, Promise, Error, JSON, String, Boolean, Object, Date, Array, Number, RegExp,
  document: { getElementById: () => null },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend/src/studio/studio-state.js"), "utf8"), sandbox);
const run = (code) => vm.runInContext(code, sandbox);

const RECALL = ["recall", "tts", "select", "render"];
function setup({ shots, stages = {}, conflicts = {}, builds = [], versions = {} }) {
  const ids = Object.keys(shots);
  const data = {
    active_plan: "p1", builds, conflicts,
    plans: { p1: { id: "p1", board: "b", shots, ranges: {}, options: {} } },
    versions: { b: { data: { order: ids } }, ...versions },
    shot_stages: Object.fromEntries(ids.map((sid) => [sid, stages[sid] || RECALL])),
  };
  run(`state.data = ${JSON.stringify(data)};`);
}
const call = (fn, ...args) => run(`${fn}(${args.map((a) => JSON.stringify(a)).join(", ")})`);
const states = (sid) => [...call("barStages", sid)].map((stage) => [stage, call("stageState", sid, stage)]);

const cases = [
  ["barStages：各画面来源的阶段顺序", () => {
    setup({
      shots: { r: {}, c: {}, i: {}, g: {}, a: {}, d: {} },
      stages: {
        c: ["tts", "select", "render"], i: ["tts", "render"], g: ["tts", "generate", "select", "render"],
        a: ["tts", "animate", "render"], d: ["tts", "draw", "render"],
      },
    });
    assert.deepEqual([...call("barStages", "r")], ["content", "recall", "tts", "select", "render"]);
    assert.deepEqual([...call("barStages", "c")], ["content", "source", "tts", "select", "render"]);
    assert.deepEqual([...call("barStages", "i")], ["content", "source", "tts", "render"]);
    assert.deepEqual([...call("barStages", "g")], ["content", "generate", "tts", "select", "render"]);
    assert.deepEqual([...call("barStages", "a")], ["content", "animate", "tts", "render"]);
    assert.deepEqual([...call("barStages", "d")], ["content", "draw", "tts", "render"]);
  }],
  ["没有任何版本：全部 none，卡片显示文案与查询", () => {
    setup({ shots: { s: {} } });
    assert.ok(states("s").every(([, value]) => value === "none"));
    assert.equal(call("cardStage", "s"), "content");
  }],
  ["只有文案：卡片显示文案与查询", () => {
    setup({ shots: { s: { content: "c" } } });
    assert.deepEqual(states("s")[0], ["content", "complete"]);
    assert.equal(call("cardStage", "s"), "content");
  }],
  ["多阶段完成：卡片显示最后一个已有产物的阶段", () => {
    setup({ shots: { s: { content: "c", recall: "r", tts: "t" } } });
    assert.equal(call("cardStage", "s"), "tts");
    setup({ shots: { s: { content: "c", recall: "r", tts: "t", select: "x", render: "y" } } });
    assert.equal(call("cardStage", "s"), "render");
    // 中间有缺口时仍取最后一个有产物的阶段。
    setup({ shots: { s: { content: "c", tts: "t", render: "y" } } });
    assert.equal(call("cardStage", "s"), "render");
  }],
  ["中间阶段过期：卡片显示第一个过期的阶段", () => {
    setup({
      shots: { s: { content: "c", recall: "r", tts: "t", select: "x", render: "y" } },
      conflicts: { "tts:s": ["配音输入已变化"], "render:s": ["渲染输入已变化"], "select:s": [] },
    });
    assert.equal(call("stageState", "s", "tts"), "stale");
    assert.equal(call("stageState", "s", "select"), "complete");
    assert.equal(call("stageState", "s", "render"), "stale");
    assert.equal(call("cardStage", "s"), "tts");
    // 没有版本的阶段即使有冲突记录也不算过期。
    setup({ shots: { s: { content: "c" } }, conflicts: { "tts:s": ["x"] } });
    assert.equal(call("stageState", "s", "tts"), "none");
    assert.equal(call("cardStage", "s"), "content");
  }],
  ["失败与执行中：取最近一次包含该节点的构建", () => {
    const node = (stage, status) => ({ stage, shot_id: "s", status });
    setup({
      shots: { s: { content: "c", tts: "t" } },
      builds: [
        { plan_id: "p1", status: "complete", nodes: [node("tts", "failed")] },
        { plan_id: "p1", status: "partial", nodes: [node("render", "failed")] },
        { plan_id: "other", status: "running", nodes: [node("tts", "running")] },
      ],
    });
    assert.equal(call("stageState", "s", "tts"), "failed");
    assert.equal(call("stageState", "s", "render"), "failed");
    // 失败但保留旧版本：卡片仍按已有产物选择。
    assert.equal(call("cardStage", "s"), "tts");
    setup({
      shots: { s: { content: "c", tts: "t" } },
      conflicts: { "tts:s": ["x"] },
      builds: [{ plan_id: "p1", status: "running", nodes: [node("tts", "pending"), node("select", "running")] }],
    });
    assert.equal(call("stageState", "s", "tts"), "running");
    assert.equal(call("stageState", "s", "select"), "running");
    assert.equal(call("cardStage", "s"), "tts"); // 过期优先于执行中的展示
    // 构建已结束：残留的 pending 不算执行中。
    setup({ shots: { s: {} }, builds: [{ plan_id: "p1", status: "cancelled", nodes: [node("tts", "pending")] }] });
    assert.equal(call("stageState", "s", "tts"), "none");
  }],
  ["上传素材分镜：source 按是否有上传文件判定", () => {
    const versions = (source) => ({ c: { data: { text: "t", source } } });
    setup({ shots: { s: { content: "c" } }, stages: { s: ["tts", "render"] }, versions: versions({ kind: "image", media_id: "m" }) });
    assert.equal(call("stageState", "s", "source"), "complete");
    assert.equal(call("cardStage", "s"), "source");
    setup({ shots: { s: { content: "c" } }, stages: { s: ["tts", "render"] }, versions: versions({ kind: "image" }) });
    assert.equal(call("stageState", "s", "source"), "none");
    assert.equal(call("cardStage", "s"), "content");
  }],
  ["AIGC 与动画分镜：画面阶段参与判定", () => {
    setup({ shots: { g: { content: "c", generate: "v" }, a: { content: "c", animate: "v", tts: "t" } },
      stages: { g: ["tts", "generate", "select", "render"], a: ["tts", "animate", "render"] },
      conflicts: { "animate:a": ["x"] } });
    assert.equal(call("cardStage", "g"), "generate");
    assert.equal(call("cardStage", "a"), "animate");
  }],
];

for (const [name, fn] of cases) {
  try { fn(); } catch (error) { error.message = `${name}：${error.message}`; console.error(error); process.exit(1); }
}
console.log(`Card stage: ${cases.length} cases passed.`);
