// 设置页「本机服务」：状态与徽标、启动/停止、日志展开、自启与退出时停止开关、未开启时的说明、启动中轮询。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.attrs = {};
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.checked = false;
    this.type = "";
    const owner = this;
    this.classList = {
      toggle(name, on) {
        const set = new Set(owner.className.split(" ").filter(Boolean));
        (on ?? !set.has(name)) ? set.add(name) : set.delete(name);
        owner.className = [...set].join(" ");
      },
      add(name) {
        this.toggle(name, true);
      },
      remove(name) {
        this.toggle(name, false);
      },
      contains(name) {
        return owner.className.split(" ").includes(name);
      },
    };
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
  setAttribute(key, value) {
    this.attrs[key] = String(value);
  }
  removeAttribute(key) {
    delete this.attrs[key];
  }
  addEventListener() {}
  querySelectorAll() {
    return [];
  }
  get title() {
    return this.attrs.title;
  }
}
const nodes = new Map();
const get = (id) => {
  if (!nodes.has(id)) {
    nodes.set(id, new Element());
  }
  return nodes.get(id);
};
const text = (node) => [node.textContent, ...node.children.flatMap(text)].join(" ");
const find = (node, predicate) => [
  ...(predicate(node) ? [node] : []),
  ...node.children.flatMap((child) => find(child, predicate)),
];

let services = { enabled: true, items: [] };
const requests = [];
const timers = [];
let failNext = null;
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console,
  setInterval() {},
  setTimeout: (fn, ms) => {
    timers.push({ fn, ms });
    return timers.length;
  },
  clearTimeout() {},
  encodeURIComponent,
  JSON,
  localStorage: { getItem: () => null, setItem() {} },
  document: {
    getElementById: get,
    createElement: (tag) => new Element(tag),
    querySelectorAll: () => [],
  },
  fetch: async (url, options = {}) => {
    requests.push({ url, method: options.method || "GET", body: options.body });
    if (failNext && url.includes(failNext.match)) {
      const detail = failNext.detail;
      failNext = null;
      return { ok: false, status: 422, json: async () => ({ detail }) };
    }
    if (url === "/api/settings/local-services") {
      return { ok: true, json: async () => services };
    }
    if (url.endsWith("/logs?tail=200")) {
      return { ok: true, json: async () => ({ name: "wemm", text: "listening on 8888" }) };
    }
    return { ok: true, json: async () => ({}) };
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync("frontend/src/shared/api.js", "utf8"), sandbox);
vm.runInContext(fs.readFileSync("frontend/src/settings/settings.js", "utf8"), sandbox);
const flush = () => new Promise((resolve) => setImmediate(resolve));
const item = (overrides) => ({
  name: "wemm",
  label: "WeMM 嵌入",
  role: "embedding",
  port: 8888,
  pid: null,
  running: false,
  healthy: false,
  external: false,
  autostart: false,
  stop_on_exit: true,
  detail: "",
  ...overrides,
});
const rows = () => get("services").children;
const buttons = (row) => row.children.filter((n) => n.tagName === "button");

(async () => {
  await flush();

  // 1. 未开启：说明文字，没有操作按钮。
  services = { enabled: false, items: [] };
  await sandbox.loadLocalServices();
  assert.equal(get("services-summary").textContent, "未开启");
  assert.match(text(get("services")), /local_services\.enabled: true/);
  assert.equal(find(get("services"), (n) => n.tagName === "button").length, 0);

  // 2. 已开启、没有定义服务。
  services = { enabled: true, items: [] };
  await sandbox.loadLocalServices();
  assert.match(text(get("services")), /还没有定义本机服务/);

  // 3. 停止状态：显示端口/用途，提供启动与日志；开关反映当前值。
  services = {
    enabled: true,
    items: [
      item({}),
      item({
        name: "asr",
        label: "ASR",
        port: 8889,
        external: true,
        healthy: true,
        detail: "端口上已有服务在运行（不是由本应用启动的）",
      }),
    ],
  };
  timers.length = 0;
  await sandbox.loadLocalServices();
  assert.equal(get("services-summary").textContent, "2 个服务，1 个运行中");
  const [wemm, asr] = rows();
  assert.match(text(wemm), /WeMM 嵌入/);
  assert.match(text(wemm), /端口 8888 · embedding/);
  assert.match(text(wemm), /已停止/);
  assert.deepEqual(
    buttons(wemm).map((b) => b.textContent),
    ["启动", "查看日志"],
  );
  // 外部运行的服务：不能在这里启动或停止，给出原因。
  assert.match(text(asr), /外部运行/);
  assert.match(text(asr), /不是由本应用启动的/);
  assert.deepEqual(
    buttons(asr).map((b) => b.textContent),
    ["查看日志"],
  );
  const [autostart, stopOnExit] = find(wemm, (n) => n.tagName === "input");
  assert.equal(autostart.checked, false);
  assert.equal(stopOnExit.checked, true);
  assert.equal(timers.length, 0); // 没有正在启动的服务时不轮询

  // 4. 启动：POST start 后刷新；启动中（进程在、未健康）每 3 秒轮询一次。
  requests.length = 0;
  services = { enabled: true, items: [item({ running: true, pid: 4242 })] };
  await buttons(wemm)[0].onclick();
  await flush();
  assert.deepEqual(
    requests.slice(0, 2).map((r) => [r.method, r.url]),
    [
      ["POST", "/api/settings/local-services/wemm/start"],
      ["GET", "/api/settings/local-services"],
    ],
  );
  assert.match(get("services-notice").textContent, /已启动 WeMM 嵌入/);
  assert.match(text(rows()[0]), /运行中/);
  assert.match(text(rows()[0]), /未就绪/);
  assert.match(text(rows()[0]), /pid 4242/);
  assert.equal(timers.at(-1).ms, 3000);
  services = { enabled: true, items: [item({ running: true, healthy: true, pid: 4242 })] };
  timers.length = 0;
  await sandbox.loadLocalServices();
  assert.ok(rows()[0].classList.contains("is-active"));
  assert.match(text(rows()[0]), /健康/);
  assert.equal(timers.length, 0);
  assert.deepEqual(
    buttons(rows()[0]).map((b) => b.textContent),
    ["停止", "查看日志"],
  );

  // 5. 开关：PATCH 只带被修改的那个键。
  requests.length = 0;
  const [auto] = find(rows()[0], (n) => n.tagName === "input");
  auto.checked = true;
  await auto.onchange();
  assert.deepEqual(
    [requests[0].method, requests[0].url, JSON.parse(requests[0].body)],
    ["PATCH", "/api/settings/local-services/wemm", { autostart: true }],
  );

  // 6. 日志展开：显示日志尾部；再次点击收起。
  await buttons(rows()[0])[1].onclick();
  await flush();
  const log = find(rows()[0], (n) => n.tagName === "pre")[0];
  assert.equal(log.textContent, "listening on 8888");
  assert.equal(buttons(rows()[0])[1].textContent, "收起日志");
  await buttons(rows()[0])[1].onclick();
  assert.equal(find(rows()[0], (n) => n.tagName === "pre").length, 0);

  // 7. 停止失败：错误显示在面板内，按钮恢复可用。
  failNext = { match: "/wemm/stop", detail: "停止失败：权限不足" };
  const stop = buttons(rows()[0])[0];
  await stop.onclick();
  assert.match(get("services-notice").textContent, /停止失败：权限不足/);
  assert.equal(get("services-notice").className, "notice error");
  assert.equal(stop.disabled, false);
  console.log("settings local services ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
