// 文案模板下拉与管理页入口：真实 studio-template-text.js + studio-templates.js 代码 + 轻量 DOM 桩，不依赖浏览器。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    Object.assign(this, { tagName: tag, textContent: "", value: "", hidden: false, disabled: false, dataset: {}, attrs: {}, children: [] });
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  focus() {}
}
const nodes = { "script-template": new Node("select"), "script-template-changed": new Node("p"),
  "script-template-manage": new Node("a"), "studio-templates-tab": new Node("a") };
const BUILTINS = [
  { id: "base", name: "通用解说", category: "general", kind: "outline", builtin: true, description: "", updated_at: null },
  { id: "interview", name: "访谈改写", category: "interview", kind: "rewrite", builtin: true, description: "", updated_at: null },
];
const USER = { id: "user-abc", name: "我的模板", category: "自定义分类", kind: "outline", builtin: false,
  description: "说明", instructions: "提示词", constraints: { max_chars: null }, updated_at: "t1" };
const requests = [], notices = [];
let catalogue = [...BUILTINS];
const plans = { p1: { id: "p1", options: { recall_limit: 20 } } };
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console, Promise, JSON, String, Object, Number, Array, encodeURIComponent, URLSearchParams,
  window: { confirm: () => true },
  state: { id: "job1", data: { revision: "r1", plans, active_plan: "p1", script_template_changed: true, builds: [] } },
  $: id => nodes[id],
  el: (tag, text = "", cls = "") => { const n = new Node(tag); n.textContent = text ?? ""; n.className = cls; return n; },
  button: (text, action) => { const n = new Node("button"); n.textContent = text; n.onclick = action; return n; },
  fillSelect: (select, choices, value) => {
    select.replaceChildren(...choices.map(([v, text]) => { const o = new Node("option"); o.value = v; o.textContent = text; return o; }));
    select.value = value;
  },
  plan: () => sandbox.state.data.plans[sandbox.state.data.active_plan],
  planBase: () => "/plan",
  busy: () => false,
  fail: error => { throw error; },
  notice: text => notices.push(text),
  accept: async data => { sandbox.state.data = data; },
  api: async (url, method = "GET", body) => {
    requests.push({ url, method, body });
    if (url === "/api/studio/templates") return { items: catalogue, default: "base" };
    if (url === "/plan") return { ...sandbox.state.data, plans: { p1: { id: "p1", options: body.options } },
      script_template_changed: false };
    return {};
  },
};
vm.createContext(sandbox);
for (const name of ["studio-template-text.js", "studio-templates.js"])
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../frontend/src/studio", name), "utf8"), sandbox);
const run = code => vm.runInContext(code, sandbox);
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const select = nodes["script-template"];
  // 1. 目录未就绪：下拉禁用并触发一次读取；提示随 script_template_changed 显示。
  run("renderTemplateSelect()");
  assert.equal(select.disabled, true);
  assert.equal(nodes["script-template-changed"].hidden, false);
  await flush();
  assert.equal(requests.filter(r => r.url === "/api/studio/templates").length, 1);
  // 2. 内置模板名称与分类走文案目录；旧方案没有 template_id 时选中 base。
  assert.equal(select.disabled, false);
  assert.equal(select.value, "base");
  assert.deepEqual(select.children.map(o => o.textContent), ["通用 · 通用解说", "访谈 · 访谈改写"]);
  // 3. 切换模板：提交完整方案参数并带 revision。
  await run("chooseTemplate('interview')");
  const patch = requests.find(r => r.url === "/plan");
  assert.deepEqual(JSON.parse(JSON.stringify(patch.body)), { revision: "r1", options: { recall_limit: 20, template_id: "interview" } });
  assert.equal(nodes["script-template-changed"].hidden, true);
  assert.equal(select.value, "interview");
  // 4. 「管理模板」链接指向独立的模板管理页，带上当前模板与来源任务；分区标签带上来源任务。
  assert.equal(nodes["script-template-manage"].href, "/studio/templates?id=interview&job=job1");
  assert.equal(nodes["studio-templates-tab"].href, "/studio/templates?job=job1");
  assert.equal(run("templatesPageUrl(null, null)"), "/studio/templates");
  // 5. 目录变化（如在管理页新增了自定义模板）后刷新：用户模板原样显示用户填写的名称与分类。
  catalogue = [...catalogue, USER];
  await run("refreshTemplates()");
  assert.ok(select.children.some(o => o.value === "user-abc" && o.textContent === "自定义分类 · 我的模板"));
  console.log("studio templates ok");
})().catch(error => { console.error(error); process.exit(1); });
