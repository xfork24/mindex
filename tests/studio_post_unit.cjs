// 分镜后期（studio-post.js）：转场摘要、文字层清理与数据行解析、保存时提交的分镜内容。
// 真实创作页脚本 + 轻量桩，不依赖浏览器；Web 与桌面版共用这份脚本。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const dir = path.join(__dirname, "../frontend/src/studio");
const read = (name) => fs.readFileSync(path.join(dir, name), "utf8");
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
  Math,
  document: { getElementById: () => null },
};
vm.createContext(sandbox);
vm.runInContext(read("studio-state.js"), sandbox);
vm.runInContext(read("studio-post.js"), sandbox);
const run = (code) => vm.runInContext(code, sandbox);
// 沙箱里的对象原型与本进程不同：比较前经 JSON 往返。
const plain = (value) => JSON.parse(JSON.stringify(value));

const fade = { kind: "fade", duration: 0.4 };
run(
  `state.id = "job"; state.data = ${JSON.stringify({
    revision: "rev",
    active_plan: "p1",
    builds: [],
    plans: {
      p1: {
        id: "p1",
        board: "board",
        ranges: {},
        options: { burn: true },
        shots: { a: { content: "c-a" }, b: { content: "c-b" } },
      },
    },
    versions: {
      board: { data: { order: ["a", "b"] } },
      "c-a": {
        data: {
          text: "甲",
          query: "q",
          estimated_seconds: 1,
          transition: { enter: fade, next: { kind: "dissolve", duration: 0.5 } },
        },
      },
      "c-b": { data: { text: "乙", query: "q", estimated_seconds: 1 } },
    },
  })};`,
);

// 1. 摘要：硬切为空；有转场时列出入场与重叠转场。
assert.equal(run(`transitionSummary("b")`), "");
const summary = run(`transitionSummary("a")`);
assert.match(summary, /入场黑场 0\.4 秒/);
assert.match(summary, /到下一镜 dissolve 0\.5 秒/);

// 2. 文字层：custom 强制 html，去掉空字段；数据行按“标签=值”解析，值里可以有等号。
assert.deepEqual(
  plain(
    run(
      `cleanOverlay({ engine: "ass", kind: "custom", brief: " 图表 ", title: "", start: "", end: 2 })`,
    ),
  ),
  { engine: "html", kind: "custom", effect: "fade", brief: "图表", end: 2 },
);
assert.deepEqual(plain(run(`parseItems("营收=12亿\\n\\n比值=a=b\\n无效行")`)), [
  { label: "营收", value: "12亿" },
  { label: "比值", value: "a=b" },
]);
assert.equal(
  run(`itemsText([{ label: "Q1", value: "3" }, { label: "Q2", value: "4" }])`),
  "Q1=3\nQ2=4",
);

// 3. 保存：只改目标分镜的转场、高亮词与文字层，其余分镜原样提交。
let sent = null;
run(`
  draftKey = (sid) => "job:p1:" + sid;
  scopeKey = () => "job:p1";

  currentBoard = () => order().map((sid) => ({ ...ver(plan().shots[sid].content).data }));

  invalidatePanel = () => {};
  accept = async () => {};
  notice = () => {};
  state.drafts = new Map();
  state.postDrafts = new Map();
`);
sandbox.api = async (url, method, body) => {
  sent = plain({ url, method, body });
  return {};
};
run(`api = globalThis.api`);
(async () => {
  await run(`savePost("b", { data: { enter: null, leave: { kind: "zoom", duration: 0.3 }, next: null },
    highlights: ["乙"], overlays: [{ engine: "ass", kind: "title", title: " 开场 ", effect: "slide" }] })`);
  assert.equal(sent.method, "PATCH");
  assert.equal(sent.body.revision, "rev");
  assert.deepEqual(sent.body.shots[0].transition, {
    enter: fade,
    next: { kind: "dissolve", duration: 0.5 },
  });
  assert.deepEqual(sent.body.shots[1].transition, { leave: { kind: "zoom", duration: 0.3 } });
  assert.deepEqual(sent.body.shots[1].highlights, ["乙"]);
  assert.deepEqual(sent.body.shots[1].overlays, [
    { engine: "ass", kind: "title", effect: "slide", title: "开场" },
  ]);
  // 清空：三项都没有时删除对应键（硬切、无高亮词、无文字层不落键）。
  await run(
    `savePost("a", { data: { enter: null, leave: null, next: null }, highlights: [], overlays: [] })`,
  );
  for (const key of ["transition", "highlights", "overlays"])
    assert.equal(key in sent.body.shots[0], false);
  console.log("studio post unit ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
