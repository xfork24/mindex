// 分镜表的画面意象（visual）：编辑器预填与双语标签、未保存标记、格内摘要、整份 PATCH、
// 保存被拒后保留草稿、合并时拼接截断（真实创作页脚本代码 + 轻量 DOM 桩）。
// 用例以 cases 数组登记，后续功能（如按画面意象重写召回词）可直接追加。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const catalog = require("../frontend/src/shared/messages.json");

class Node {
  constructor(tag = "div") {
    Object.assign(this, {
      tagName: tag, textContent: "", value: "", className: "", hidden: false, disabled: false,
      dataset: {}, attrs: {}, children: [], listeners: {}, parent: null,
    });
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...children) {
    for (const c of children) { if (c && typeof c === "object") c.parent = this; this.children.push(c); }
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  replaceWith(other) {
    const list = this.parent.children;
    list[list.indexOf(this)] = other;
    other.parent = this.parent;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type) { for (const fn of this.listeners[type] || []) fn({ detail: 1, key: "", preventDefault() {} }); }
}
const walk = (node, out = []) => {
  if (!node || typeof node !== "object") return out;
  out.push(node);
  for (const c of node.children) walk(c, out);
  return out;
};
const find = (root, pred) => walk(root).find(pred);
const findAll = (root, pred) => walk(root).filter(pred);
const plain = value => JSON.parse(JSON.stringify(value));

const requests = [];
let apiImpl = async () => ({ ok: true });
const versions = {};
const plans = {};
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console, Map, Set, Promise, Error, JSON, String, Boolean, Object, Date, Array, Number, RegExp,
  window: { getSelection: () => null },
  document: { createElement: tag => new Node(tag) },
  state: {
    id: "job",
    data: { revision: "r1", conflicts: {}, recall_granularity: "shot" },
    drafts: new Map(), matrixRows: new Map(), expanded: new Map(),
  },
  LABEL: { content: "文案与查询", recall: "召回", tts: "配音", select: "选片", render: "渲染" },
  GRANULARITY_LABEL: { shot: "镜头", moment: "片段", scene: "场景" },
  STATUS: { running: "执行中", failed: "失败", complete: "完成" },
  el: (tag, text = "", cls = "") => { const n = new Node(tag); n.textContent = text; n.className = cls; return n; },
  button: (text, action) => { const n = new Node("button"); n.textContent = text; n.onclick = action; return n; },
  mutationButton: (text, action, disabled = false) => {
    const n = new Node("button"); n.textContent = text; n.onclick = action; n.disabled = !!disabled; return n;
  },
  plan: () => plans.p1,
  ver: id => versions[id],
  order: () => [...plans.p1.order],
  scopeKey: () => "job/p1",
  planBase: () => "/plan",
  shortId: id => String(id).slice(0, 8),
  shortIds: value => String(value),
  latestNode: () => undefined,
  executionError: () => "",
  selectionWithin: () => false,
  toggleCell: () => {},
  updateLocks: () => {},
  renderMatrix: () => {},
  renderRecallEditor: () => {},
  notice: () => {},
  accept: async () => {},
  api: async (url, method, body) => { requests.push(plain({ url, method, body })); return apiImpl(url, method, body); },
};
sandbox.statusText = value => sandbox.STATUS[value] || String(value ?? "");
vm.createContext(sandbox);
const source = require("./studio_source.cjs").studioSource();
const slice = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `创作页脚本中找不到代码片段：${start} … ${end}`);
  return source.slice(a, b);
};
vm.runInContext([
  slice("function field(", "function jsonView("),
  slice("function draftKey(", "function expandedRows("),
  "function expandedRows() { return state.expanded.get(scopeKey()) || state.expanded.set(scopeKey(), new Map()).get(scopeKey()); }",
  slice("function invalidatePanel(", "\n}\n") + "\n}\n",
  slice("function recallEditable(", "function queryPreviewNode("),
  slice("function cellSummary(", "function retrievalCombo("),
  slice("function renderShotEditor(", "function renderStageDetails("),
].join("\n"), sandbox);

const run = code => vm.runInContext(code, sandbox);
const shot = (id, text, query, extra = {}) => ({
  id, text, query, estimated_seconds: 2, recall: null, granularity: null, recall_limit: null, ...extra,
});
// 每个用例前重建：s1 有 visual，s2 是老分镜（无 visual 键），s3 有 visual。
function reset() {
  for (const k of Object.keys(versions)) delete versions[k];
  const data = [
    shot("s1", "From misty mountain trails", "山间 步道 晨雾", { visual: "晨雾笼罩的山间步道" }),
    shot("s2", " and quiet lakeside villages", "湖畔 村庄"),
    shot("s3", " to bustling night markets", "夜市 摊位 人群", { visual: "人头攒动的夜市摊位" }),
  ];
  plans.p1 = { id: "p1", order: [], shots: {}, options: { recall_limit: 20 } };
  for (const d of data) {
    versions[`c-${d.id}`] = { id: `c-${d.id}`, stage: "content", shot_id: d.id, data: d };
    plans.p1.order.push(d.id);
    plans.p1.shots[d.id] = { content: `c-${d.id}` };
  }
  sandbox.state.drafts.clear();
  sandbox.state.matrixRows.clear();
  requests.length = 0;
  apiImpl = async () => ({ ok: true });
}
// 打开某镜编辑器：为 input 监听准备分镜行（summary 第 2 格是 content 摘要）。
function openEditor(sid) {
  const summary = new Node("tr");
  summary.append(new Node("td"), run(`cellSummary(${JSON.stringify(sid)}, "content")`));
  sandbox.state.matrixRows.set(sid, { summary, panelKey: "x" });
  const body = new Node("div");
  run(`renderShotEditor`)(body, sid);
  const input = name => find(body, n => n.dataset.shotField === name);
  const draft = () => sandbox.state.drafts.get(`job/p1/${sid}`);
  const type = (name, value) => { input(name).value = value; input(name).fire("input"); };
  return { body, summary, input, draft, type };
}
const visualSpans = cell => findAll(cell, n => String(n.className).includes("visual-summary"));

const cases = [
  ["编辑器字段顺序为旁白与字幕、画面意象、召回关键字；预填已存 visual，标签双语", () => {
    assert.equal(catalog["ui.visual_label"].zh, "画面意象");
    assert.equal(catalog["ui.visual_label"].en, "Visual imagery");
    assert.equal(catalog["ui.recall_keywords_label"].zh, "召回关键字");
    const { body, input } = openEditor("s1");
    const fields = findAll(body, n => n.dataset.shotField);
    assert.deepEqual(fields.slice(0, 3).map(n => n.dataset.shotField), ["text", "visual", "query"]);
    assert.equal(input("visual").tagName, "textarea");
    assert.equal(input("query").attrs["aria-label"], "召回关键字");
    assert.equal(input("visual").value, "晨雾笼罩的山间步道");
    assert.equal(input("visual").attrs["aria-label"], "画面意象");
    const label = find(body, n => n.tagName === "label" && n.children.includes(input("visual")));
    assert.equal(label.textContent, "画面意象");
    // 老分镜没有 visual：输入框为空。
    assert.equal(openEditor("s2").input("visual").value, "");
  }],
  ["输入后标记未保存，改回原值后取消标记", () => {
    const e = openEditor("s1");
    e.type("visual", "晨雾笼罩的山间步道，远处有人");
    assert.equal(e.draft().dirty, true);
    // 格内摘要随之刷新：显示未保存标记，摘要仍是已存版本的 visual。
    const cell = e.summary.children[1];
    assert.ok(find(cell, n => n.className === "badge warn"));
    assert.equal(visualSpans(cell)[0].textContent, "画面意象：晨雾笼罩的山间步道");
    e.type("visual", "晨雾笼罩的山间步道");
    assert.equal(e.draft().dirty, false);
    // 老分镜：填入再清空（含纯空白）后 visual 键被删除，不算未保存。
    const o = openEditor("s2");
    o.type("visual", "湖边安静的村落");
    assert.equal(o.draft().dirty, true);
    o.type("visual", "   ");
    assert.equal(o.draft().dirty, false);
    assert.equal("visual" in o.draft().data, false);
  }],
  ["cellSummary 有 visual 时完整显示，为空时不显示", () => {
    const long = "字".repeat(120);
    versions["c-s1"].data.visual = long;
    const spans = visualSpans(run(`cellSummary("s1", "content")`));
    assert.equal(spans.length, 1);
    assert.equal(spans[0].textContent, `画面意象：${long}`);
    assert.equal(visualSpans(run(`cellSummary("s2", "content")`)).length, 0);
    versions["c-s3"].data.visual = "  ";
    assert.equal(visualSpans(run(`cellSummary("s3", "content")`)).length, 0);
  }],
  ["保存发出整份 PATCH，每镜都带当前 visual", async () => {
    const e = openEditor("s2");
    e.type("visual", "  湖边安静的村落  ");
    await run(`saveShot("s2")`);
    const req = requests.pop();
    assert.equal(req.method, "PATCH");
    assert.equal(req.url, "/plan");
    assert.equal(req.body.revision, "r1");
    assert.deepEqual(req.body.shots.map(s => s.visual), ["晨雾笼罩的山间步道", "湖边安静的村落", "人头攒动的夜市摊位"]);
    assert.equal(sandbox.state.drafts.has("job/p1/s2"), false);
    // 清空当前镜的 visual：PATCH 中该镜没有 visual 键，其他镜不受影响。
    const c = openEditor("s1");
    c.type("visual", "");
    await run(`saveShot("s1")`);
    const shots = requests.pop().body.shots;
    assert.equal("visual" in shots[0], false);
    assert.equal("visual" in shots[1], false);
    assert.equal(shots[2].visual, "人头攒动的夜市摊位");
  }],
  ["保存被拒时保留草稿与未保存标记，并抛出服务端中文 detail", async () => {
    const detail = "第 1 个分镜的画面意象超过 120 字上限（当前 121 字）";
    apiImpl = async () => { throw new Error(detail); };
    const e = openEditor("s1");
    const tooLong = "字".repeat(121);
    e.type("visual", tooLong);
    await assert.rejects(() => run(`saveShot("s1")`), err => err.message === detail);
    assert.equal(requests.length, 1);
    assert.equal(e.draft().dirty, true);
    assert.equal(e.draft().data.visual, tooLong);
  }],
  ["joinVisual：跳过空值、用「；」拼接、按码点截断到 120 字", () => {
    const join = run("joinVisual");
    assert.equal(join("甲", null, "  ", undefined, " 乙 "), "甲；乙");
    assert.equal(join(null, ""), "");
    // 恰好 120 字：完整保留。
    const exact = join("字".repeat(60), "画".repeat(59));
    assert.equal(Array.from(exact).length, 120);
    assert.equal(exact, `${"字".repeat(60)}；${"画".repeat(59)}`);
    // 121 字：去掉最后一个字。
    assert.equal(join("字".repeat(60), "画".repeat(60)), `${"字".repeat(60)}；${"画".repeat(59)}`);
    // emoji 按一个码点计，不会被截成半个代理对。
    const emoji = join("字".repeat(118), "😀😀");
    assert.equal(emoji, `${"字".repeat(118)}；😀`);
    assert.equal(Array.from(emoji).length, 120);
    assert.equal(emoji.length, 121); // UTF-16 长度更大，说明按码点截断
  }],
  ["合并分镜：PATCH 中上一镜 visual 为拼接截断结果，全空时没有 visual", async () => {
    await run(`changeStructure("s2", "merge")`);
    let shots = requests.pop().body.shots;
    assert.equal(shots.length, 2);
    assert.equal(shots[0].visual, "晨雾笼罩的山间步道"); // s2 无 visual，被跳过
    assert.equal(shots[0].text, "From misty mountain trails and quiet lakeside villages");
    reset();
    versions["c-s2"].data.visual = "画".repeat(60);
    versions["c-s1"].data.visual = "字".repeat(60);
    await run(`changeStructure("s2", "merge")`);
    assert.equal(requests.pop().body.shots[0].visual, `${"字".repeat(60)}；${"画".repeat(59)}`);
    reset();
    versions["c-s3"].data.visual = "😀".repeat(130);
    versions["c-s2"].data.visual = "  ";
    await run(`changeStructure("s3", "merge")`);
    shots = requests.pop().body.shots;
    // s2 为空白被跳过，s3 的 130 个 emoji 截到 120 个码点。
    assert.equal(shots[1].visual, "😀".repeat(120));
    reset();
    delete versions["c-s1"].data.visual;
    versions["c-s2"].data.visual = "   ";
    await run(`changeStructure("s2", "merge")`);
    assert.equal("visual" in requests.pop().body.shots[0], false);
  }],
  // ---------------------------------------------------------------- 按画面意象重写召回词（需求 10.5、10.6、10.8）
  ["重写召回词：请求期间禁用按钮，成功后替换草稿 query 并标记未保存", async () => {
    let release;
    apiImpl = () => new Promise(resolve => { release = () => resolve({ query: "晨雾 山道 远景" }); });
    const e = openEditor("s1");
    e.type("visual", "晨雾中的山道远景");
    e.type("visual", "晨雾笼罩的山间步道"); // 改回原值：此时没有未保存改动
    assert.equal(e.draft().dirty, false);
    e.type("visual", "晨雾中的山道远景");
    const btn = find(e.body, n => n.dataset.rewriteQuery === "true");
    assert.equal(btn.textContent, "按画面意象重写召回词");
    const pending = btn.onclick();
    assert.equal(btn.disabled, true);
    assert.equal(btn.textContent, "正在重写召回词…");
    // 请求期间再次点击不会发出第二个请求。
    await btn.onclick();
    assert.equal(requests.length, 1);
    const req = requests[0];
    assert.equal(req.url, "/plan/shots/s1/query-suggestion");
    assert.equal(req.method, "POST");
    assert.deepEqual(req.body, { visual: "晨雾中的山道远景" });
    release();
    await pending;
    assert.equal(btn.disabled, false);
    assert.equal(btn.textContent, "按画面意象重写召回词");
    assert.equal(e.draft().data.query, "晨雾 山道 远景");
    assert.equal(e.input("query").value, "晨雾 山道 远景");
    // 其他草稿字段保留，已存版本不变。
    assert.equal(e.draft().data.visual, "晨雾中的山道远景");
    assert.equal(e.draft().data.text, "From misty mountain trails");
    assert.equal(e.draft().dirty, true);
    assert.equal(versions["c-s1"].data.query, "山间 步道 晨雾");
    const status = find(e.body, n => n.attrs.role === "status");
    assert.equal(status.hidden, true);
    // 格内摘要随之显示未保存标记。
    assert.ok(find(e.summary.children[1], n => n.className === "badge warn"));
    // 保存后整份 PATCH 带上建议 query。
    apiImpl = async () => ({ ok: true });
    await run(`saveShot("s1")`);
    const shots = requests.pop().body.shots;
    assert.equal(shots[0].query, "晨雾 山道 远景");
    assert.equal(shots[1].query, "湖畔 村庄");
  }],
  ["重写召回词：未编辑时以已存 visual 请求，无 visual 的老分镜发送空串", async () => {
    apiImpl = async () => ({ query: "建议 画面" });
    const e = openEditor("s1");
    await find(e.body, n => n.dataset.rewriteQuery === "true").onclick();
    assert.deepEqual(requests.pop().body, { visual: "晨雾笼罩的山间步道" });
    const o = openEditor("s2");
    apiImpl = async () => { throw new Error("请先填写画面意象，再重写召回词"); };
    await find(o.body, n => n.dataset.rewriteQuery === "true").onclick();
    assert.deepEqual(requests.pop().body, { visual: "" });
  }],
  ["重写召回词失败：显示中文错误，query 与未保存标记不变，按钮恢复", async () => {
    for (const [error, expected] of [
      [new Error("召回词重写失败：服务不可用"), "召回词重写失败：服务不可用"],
      [new Error("找不到分镜"), "找不到分镜"],
      [Object.assign(new TypeError("Failed to fetch"), { name: "TypeError" }), "召回词重写失败：Failed to fetch"],
    ]) {
      reset();
      apiImpl = async () => { throw error; };
      const e = openEditor("s1");
      e.type("query", "手改 查询");
      const before = JSON.stringify(e.draft());
      const btn = find(e.body, n => n.dataset.rewriteQuery === "true");
      await btn.onclick();
      assert.equal(btn.disabled, false);
      assert.equal(btn.textContent, "按画面意象重写召回词");
      const status = find(e.body, n => n.attrs.role === "status");
      assert.equal(status.hidden, false);
      assert.equal(status.textContent, expected);
      assert.equal(JSON.stringify(e.draft()), before);
      assert.equal(e.draft().data.query, "手改 查询");
      assert.equal(e.input("query").value, "手改 查询");
      assert.equal(e.draft().dirty, true);
      // 未编辑过的分镜失败后也不会被标记为未保存。
      reset();
      apiImpl = async () => { throw error; };
      const clean = openEditor("s3");
      await find(clean.body, n => n.dataset.rewriteQuery === "true").onclick();
      assert.equal(clean.draft().dirty, false);
      assert.equal(clean.draft().data.query, "夜市 摊位 人群");
    }
  }],
];

module.exports = { cases, sandbox, run, reset, openEditor, find, findAll, requests, versions, plans,
  setApi: fn => { apiImpl = fn; } };

if (require.main === module) {
  (async () => {
    for (const [name, fn] of cases) {
      reset();
      try { await fn(); } catch (error) { error.message = `${name}：${error.message}`; throw error; }
    }
    console.log(`Shot visual: ${cases.length} cases passed.`);
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
