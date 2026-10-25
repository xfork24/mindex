// 任务元信息中的可用素材摘要折叠块：默认折叠并显示计数、为空时显示原因、加载失败时显示提示
// 且其余元信息正常（真实创作页脚本代码 + 轻量 DOM 桩，不依赖浏览器）。
// 用例以 cases 数组登记，后续功能（如文案区"参考了 N 个素材"）可直接追加。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    Object.assign(this, {
      tagName: tag, textContent: "", className: "", hidden: false, open: false,
      dataset: {}, attrs: {}, children: [], listeners: {}, parent: null,
    });
    const node = this;
    this.classList = {
      add: (...names) => { node.className = [...new Set([...node.className.split(" ").filter(Boolean), ...names])].join(" "); },
      contains: name => node.className.split(" ").includes(name),
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...children) {
    for (const c of children) { if (c && typeof c === "object") c.parent = this; this.children.push(c); }
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type) { for (const fn of this.listeners[type] || []) fn({ preventDefault() {} }); }
}
const walk = (node, out = []) => {
  if (!node || typeof node !== "object") return out;
  out.push(node);
  for (const c of node.children) walk(c, out);
  return out;
};
const find = (root, pred) => walk(root).find(pred);
const findAll = (root, pred) => walk(root).filter(pred);
// summary 的可见文字：自身文字 + 字符串子节点 + 子元素文字。
const textOf = node => [node.textContent, ...node.children.map(c => (typeof c === "string" ? c : textOf(c)))].join("");

const requests = [];
let apiImpl = async () => ({});
const metaBody = new Node("dl");
const scriptNote = new Node("p");
const nodes = { "job-meta-body": metaBody, "script-materials": scriptNote };
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console, Map, Set, Promise, Error, JSON, String, Boolean, Object, Date, Array, Number,
  document: { createElement: tag => new Node(tag) },
  state: { id: "job-1", data: null, sources: null, materialDigest: null },
  $: id => { assert.ok(id in nodes, `未预期的元素 id：${id}`); return nodes[id]; },
  el: (tag, text = "", cls = "") => { const n = new Node(tag); n.textContent = text ?? ""; n.className = cls; return n; },
  shortId: id => String(id).slice(0, 8),
  sourceName: id => `素材-${id}`,
  api: async (url, method = "GET", body) => { requests.push({ url, method, body }); return apiImpl(url, method, body); },
};
vm.createContext(sandbox);
const source = require("./studio_source.cjs").studioSource();
const slice = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `创作页脚本中找不到代码片段：${start} … ${end}`);
  return source.slice(a, b);
};
// renderJobMeta、DIGEST_EMPTY_REASON、loadMaterialDigest、materialDigestNode 是连续的顶层代码。
vm.runInContext(slice("function renderJobMeta(", "const scriptDrafts = new Map()"), sandbox);
// 文案区"参考了 N 个素材、M 个镜头描述"提示（renderScript 中调用的顶层函数）。
vm.runInContext(slice("function renderScriptMaterials(", "function renderScript()"), sandbox);
const run = code => vm.runInContext(code, sandbox);

const job = (id = "job-1") => ({
  id, outline: "港口的一天\n\n渔船靠岸", created_at: "2026-10-26T08:00:00Z", source_ids: ["src-a", "src-b"],
});
function reset() {
  sandbox.state.id = "job-1";
  sandbox.state.data = { job: job() };
  sandbox.state.materialDigest = null;
  metaBody.replaceChildren();
  Object.assign(scriptNote, { hidden: false, textContent: "" });
  requests.length = 0;
  apiImpl = async () => ({});
}
// 渲染一次并等待摘要请求结束（结束后 loadMaterialDigest 会自动重渲染）。
async function renderAndLoad() {
  run("renderJobMeta()");
  await sandbox.state.materialDigest?.request;
}
const digestBox = () => find(metaBody, n => n.tagName === "details" && n.className === "material-digest");
const metaRows = () => findAll(metaBody, n => String(n.className).startsWith("job-meta-row"));
const statusOf = box => find(box, n => String(n.className).includes("material-digest-status"));
const digestData = (over = {}) => ({
  text: "【港口素材】时长 60 秒\n画面词：渔船、码头\n故事线：港口清晨的渔船\n",
  sources: [{ source_id: "src-a", name: "港口素材" }],
  stats: { sources: 2, segments: 17, fingerprint: "f".repeat(24), content_index_id: "idx-1" },
  empty_reason: null,
  ...over,
});
function assertMetaRowsIntact() {
  const rows = metaRows();
  assert.equal(rows.length, 3);
  assert.equal(rows[0].children[1].textContent, "港口的一天\n渔船靠岸");
  assert.equal(rows[2].children[1].textContent, "素材-src-a\n素材-src-b");
}

const cases = [
  ["加载中先显示占位，加载后默认折叠，summary 显示标题与计数，正文在折叠块内", async () => {
    apiImpl = async () => digestData();
    run("renderJobMeta()");
    assert.equal(textOf(statusOf(digestBox())), "加载中…");
    await sandbox.state.materialDigest.request;
    assert.deepEqual(requests.map(r => [r.url, r.method]), [["/api/studio/jobs/job-1/material-digest", "GET"]]);
    const box = digestBox();
    assert.equal(box.open, false);
    const summary = find(box, n => n.tagName === "summary");
    assert.equal(textOf(summary), "可用素材摘要 · 2 个素材 · 17 个镜头");
    const pre = find(box, n => n.tagName === "pre");
    assert.equal(pre.textContent, digestData().text);
    assert.equal(box.children.length, 2); // summary + 正文
    assertMetaRowsIntact();
  }],
  ["展开状态在重渲染后保留，同一任务不重复请求", async () => {
    apiImpl = async () => digestData();
    await renderAndLoad();
    const box = digestBox();
    box.open = true;
    box.fire("toggle");
    run("renderJobMeta()");
    assert.equal(digestBox().open, true);
    assert.equal(requests.length, 1);
    // 切换到另一任务时重新获取。
    sandbox.state.id = "job-2";
    sandbox.state.data = { job: job("job-2") };
    await renderAndLoad();
    assert.equal(requests.at(-1).url, "/api/studio/jobs/job-2/material-digest");
    assert.equal(digestBox().open, false);
  }],
  ["摘要为空时显示接口给出的原因，不显示正文", async () => {
    for (const reason of ["预算为 0", "无生效索引", "无已分析镜头"]) {
      reset();
      apiImpl = async () => digestData({
        text: "", sources: [], empty_reason: reason,
        stats: { sources: 0, segments: 0, fingerprint: null, content_index_id: null },
      });
      await renderAndLoad();
      const box = digestBox();
      assert.equal(box.open, false);
      assert.equal(textOf(statusOf(box)), reason);
      assert.equal(find(box, n => n.tagName === "pre"), undefined);
      assertMetaRowsIntact();
    }
  }],
  ["请求失败时显示加载失败提示，其余元信息正常显示", async () => {
    apiImpl = async () => { throw new Error("网络错误"); };
    await renderAndLoad();
    const box = digestBox();
    const status = statusOf(box);
    assert.equal(textOf(status), "素材摘要加载失败");
    assert.ok(status.classList.contains("warn-text"));
    assert.equal(find(box, n => n.tagName === "pre"), undefined);
    assertMetaRowsIntact();
    assert.equal(textOf(metaRows()[1].children[0]).length > 0, true);
  }],
  ["文案版本带 data.materials 时显示\u201c参考了 N 个素材、M 个镜头描述\u201d", async () => {
    sandbox.renderScriptMaterials({ data: { text: "文案", materials: { sources: 3, segments: 512 } } });
    assert.equal(scriptNote.hidden, false);
    assert.equal(scriptNote.textContent, "参考了 3 个素材、512 个镜头描述");
  }],
  ["文案版本没有 data.materials 时隐藏提示（含切换到无摘要版本与无版本）", async () => {
    sandbox.renderScriptMaterials({ data: { text: "文案", materials: { sources: 1, segments: 2 } } });
    assert.equal(scriptNote.hidden, false);
    for (const item of [{ data: { text: "手动文案", origin: "manual" } }, { data: {} }, null, undefined]) {
      sandbox.renderScriptMaterials(item);
      assert.equal(scriptNote.hidden, true, JSON.stringify(item));
    }
  }],
];

module.exports = { cases, sandbox, run, reset, renderAndLoad, find, findAll, textOf, requests, metaBody,
  setApi: fn => { apiImpl = fn; } };

if (require.main === module) {
  (async () => {
    for (const [name, fn] of cases) {
      reset();
      try { await fn(); } catch (error) { error.message = `${name}：${error.message}`; throw error; }
    }
    console.log(`Material digest: ${cases.length} cases passed.`);
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
