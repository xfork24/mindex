const { between } = require("./source_slices.cjs");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("frontend/src/admin/admin.js", "utf8");
const context = vm.createContext({
  MindexI18n: {
    msg: (key, args = {}) => JSON.stringify({ key, ...args }),
    system: (value) => value?.message || value || "",
  },
  publicationStages: new Set(["embedding", "publish"]),
});
vm.runInContext(
  between(source, "function workProgressRows(", "function appendWorkProgress("),
  context,
);
const rows = (work) => context.workProgressRows("shots", work);
const saved = {
  status: "complete",
  result: { shots: 35 },
  search: { current: true, indexed_segments: 36, expected_segments: 36 },
  scene_search: { current: false, indexed_segments: 2, expected_segments: 10 },
};
let result = rows(saved);
assert.equal(result.length, 3);
assert.deepEqual(
  Array.from(result, (r) => [r.done, r.total]),
  [
    [35, 35],
    [36, 36],
    [2, 10],
  ],
);
// A newly queued or running execution must not reset durable completed coverage.
for (const status of ["queued", "running", "complete"]) {
  result = rows({
    ...saved,
    task: { status, progress: { stage: "split", completed: 0, total: 35 } },
    publish_task: { status, progress: { completed: 0, total: 36 } },
  });
  assert.equal(result[0].done, 35);
  assert.equal(result[1].done, 36);
}
// Automatic publication stays within the same persistent row.
result = rows({
  ...saved,
  search: { indexed_segments: 4, expected_segments: 36 },
  task: {
    status: "running",
    progress: { phase: "publish", stage: "embedding", completed: 12, total: 36 },
  },
});
assert.equal(result.length, 3);
assert.equal(result[1].done, 12);
result = rows({ status: "not_processed" });
assert.equal(result.length, 3);
assert.ok(result.every((r) => r.done === 0 && r.total === 0));
console.log("Persistent subtask coverage checks passed");
// Completed embedding checkpoints can exist before publication.
result = rows({
  ...saved,
  scene_search: {
    indexed_segments: 0,
    embedded_segments: 28,
    expected_segments: 28,
    current: false,
  },
});
assert.equal(result[2].done, 28);
// The bar and its caption must use the same numeric coverage, even with stale flags.
const rendered = [];
context.node = (tag, text) => ({ tag, text, dataset: {}, setAttribute() {} });
context.MindexI18n = { msg: (_key, args) => args, setAttr: () => {} };
vm.runInContext(
  between(source, "function appendWorkProgress(", "function analysisSummary("),
  context,
);
context.appendWorkProgress({ append: (el) => rendered.push(el) }, "人物", {
  type: "publish",
  key: "test",
  done: 0,
  total: 22,
  complete: true,
});
assert.equal(rendered[0].text.done, 0);
assert.equal(rendered[1].value, 0);
assert.equal(rendered[2].tag, "p");
assert.equal(rendered[2].dataset.phase, "publish");
assert.equal(rendered[2].text, "");
vm.runInContext(
  between(source, "function analysisCoverageText(", "// Persistent subtask coverage"),
  context,
);
context.MindexI18n = {
  msg: (key, args = {}) => JSON.stringify({ key, ...args }),
  system: (value) => value?.message || value || "",
};
const analysis = context.workProgressRows("analysis", {
  status: "complete",
  result: {
    analyzed: 50,
    total: 56,
    skipped: 6,
    pending: 0,
    skipped_reasons: [{ reason: "模型内容安全策略拒绝", count: 6 }],
  },
})[0];
assert.ok(analysis.text.includes('"done":50'));
assert.ok(analysis.text.includes('"count":6'));
assert.ok(analysis.text.includes("模型内容安全策略拒绝"));
assert.equal(analysis.done, 56); // Resolved skips finish processing, but aren't successful analyses.
const unknown = context.analysisCoverageText({ analyzed: 50, total: 56, skipped: 2, pending: 4 });
assert.ok(unknown.includes("analysis_reason_unknown"));
assert.ok(unknown.includes("analysis_pending_count"));
assert.ok(!unknown.includes("敏感"));
const failed = context.analysisCoverageText(
  { analyzed: 50, total: 56, pending: 6 },
  { status: "failed", error: "模型服务连接超时" },
);
assert.ok(failed.includes("模型服务连接超时"));
context.taskStatus = { queued: "排队中", running: "运行中", complete: "已完成", failed: "失败" };
context.stages = { embedding: "嵌入中" };
const row = {
  type: "scene",
  task: { id: "existing-task", status: "running" },
  progress: { latest_log: "old running log" },
};
assert.equal(context.workLog(row), null);
assert.equal(context.workLog(row), null);
row.progress.latest_log = "new embedding log";
let log = context.workLog(row);
assert.equal(log.message, "new embedding log");
assert.equal(log.error, false);
assert.equal(context.workLog(row).message, "new embedding log");
row.task.status = "complete";
row.task.publication = { status: "failed", error: "trace line\nlatest error" };
log = context.workLog(row);
assert.equal(log.message, "latest error");
assert.equal(log.error, true);
// Both a full reload and the page's refresh button start a fresh baseline.
context.workLog.snapshots = new Map();
assert.equal(context.workLog(row), null);
const process = {
  type: "process",
  task: { id: "process-task", status: "running" },
  progress: { latest_log: "running" },
};
assert.equal(context.workLog(process), null);
process.task.status = "complete";
process.progress.latest_log = "processing finished";
assert.equal(context.workLog(process).message, "processing finished");
assert.equal(context.workLog(row), null);
console.log("Page-session log reset checks passed");

// Every subtask has its own log stream, including automatic publication.
context.workLog.snapshots = new Map();
const separate = {
  ...saved,
  task: {
    id: "processing",
    status: "running",
    progress: {
      phase: "publish",
      stage: "embedding",
      latest_log: "old moment log",
      processing_progress: { latest_log: "processing done" },
    },
  },
  scene_publish_task: {
    id: "scene",
    status: "running",
    options: { work: "shots", scope: "scene" },
    progress: { stage: "embedding", latest_log: "old scene log" },
  },
};
let scoped = rows(separate);
scoped.forEach((r) => assert.equal(context.workLog(r), null));
separate.task.progress.latest_log = "moment-only index diagnostic";
scoped = rows(separate);
assert.equal(context.workLog(scoped[0]), null);
assert.equal(context.workLog(scoped[1]).message, "moment-only index diagnostic");
assert.equal(context.workLog(scoped[2]), null);
separate.scene_publish_task.progress.latest_log = "scene-only index diagnostic";
scoped = rows(separate);
assert.equal(context.workLog(scoped[0]), null);
assert.equal(context.workLog(scoped[1]).message, "moment-only index diagnostic");
assert.equal(context.workLog(scoped[2]).message, "scene-only index diagnostic");
console.log("Independent subtask log checks passed");
// Four work cards expose exactly eleven independent rows and log areas.
let subtaskCount = 0;
for (const name of ["shots", "dialogue", "analysis", "persons"]) {
  const taskFor = (phase) => ({
    id: `${name}-${phase}`,
    status: "running",
    created_at: "2026-10-20",
    options: { work: name },
    progress: { latest_log: `old-${name}-${phase}` },
  });
  const work = {
    status: "complete",
    result: { shots: 1, total: 1, total_shots: 1, covered_shots: 1, analyzed: 1, utterances: 1 },
    search: { expected_segments: 1, indexed_segments: 1, current: true },
    scene_search: { expected_segments: 1, indexed_segments: 1, current: true },
    task: taskFor("process"),
    publish_task: taskFor("publish"),
    scene_publish_task: taskFor("scene"),
  };
  const workRows = context.workProgressRows(name, work);
  subtaskCount += workRows.length;
  workRows.forEach((r) => assert.equal(context.workLog(r), null));
  for (const current of workRows) {
    current.task.progress.latest_log = `new-${name}-${current.type}`;
    assert.equal(context.workLog(current).message, `new-${name}-${current.type}`);
  }
}
assert.equal(subtaskCount, 11);
console.log("All eleven subtask log streams passed");
