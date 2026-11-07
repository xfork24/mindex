// 设置页「索引版本」：当前生效的内容索引 / 检索发布版 / 人物快照 ID，恢复、删除与发布反馈。
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

const CONTENT = "cfbdd3fcccdae82970f426c7";
const OLD_CONTENT = "948ffaef006da3df9d827c41";
const PERSON = "d045d64f39c2f065026a4538";
const NEW_RELEASE = "e60e29dc0d45a1ce93ed34b7";
const OLD_RELEASE = "594791409ff1b0fac35c2ff6";
let payload;
const requests = [];
let failNext = null;
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console,
  setInterval() {},
  localStorage: { getItem: () => null, setItem() {} },
  document: {
    getElementById: get,
    createElement: (tag) => new Element(tag),
    querySelectorAll: () => [],
  },
  fetch: async (url, options = {}) => {
    requests.push({ url, method: options.method || "GET" });
    if (failNext) {
      const detail = failNext;
      failNext = null;
      return { ok: false, json: async () => ({ detail }) };
    }
    if (url === "/api/persons/releases") {
      return { ok: true, json: async () => payload };
    }
    return { ok: true, json: async () => ({}) };
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync("frontend/src/shared/api.js", "utf8"), sandbox);
vm.runInContext(fs.readFileSync("frontend/src/settings/settings.js", "utf8"), sandbox);
const flush = () => new Promise((resolve) => setImmediate(resolve));
const buttons = (row) => row.children.filter((n) => n.tagName === "button");

(async () => {
  await flush();
  requests.length = 0;
  payload = {
    active: { content_id: CONTENT, release_id: NEW_RELEASE, person_id: PERSON },
    releases: [
      {
        id: NEW_RELEASE,
        content_id: CONTENT,
        person_id: PERSON,
        created_at: "2026-10-23 14:02:05",
      },
      {
        id: OLD_RELEASE,
        content_id: OLD_CONTENT,
        person_id: PERSON,
        created_at: "2026-10-22 00:41:31",
      },
    ],
  };
  await sandbox.loadReleases();

  // 当前生效行同时显示检索发布版、内容索引与人物快照；悬停显示完整 ID。
  const current = get("release-current");
  for (const id of ["e60e29dc", "cfbdd3fc", "d045d64f"]) {
    assert.ok(current.textContent.includes(id), id);
  }
  assert.ok(!current.textContent.includes(NEW_RELEASE));
  for (const id of [NEW_RELEASE, CONTENT, PERSON]) {
    assert.ok(current.title.includes(id), id);
  }
  assert.equal(get("releases-summary").textContent, "检索发布版 e60e29dc");

  // 列表每行包含三个 ID；生效组合标记且不提供恢复，其他组合可恢复。
  const [activeRow, oldRow] = get("releases").children;
  assert.ok(activeRow.classList.contains("is-active"));
  assert.ok(activeRow.children[0].textContent.includes("检索发布版 e60e29dc"));
  assert.ok(activeRow.children[0].title.includes(NEW_RELEASE));
  assert.ok(
    oldRow.children[0].textContent.includes("内容索引 948ffaef") &&
      oldRow.children[0].textContent.includes("人物快照 d045d64f"),
  );
  assert.deepEqual(
    buttons(activeRow).map((b) => b.textContent),
    ["删除"],
  );
  assert.deepEqual(
    buttons(oldRow).map((b) => b.textContent),
    ["恢复此组合", "删除"],
  );

  // 恢复：调用激活接口并刷新列表与嵌入横幅，反馈在面板内显示。
  const topNotice = get("notice").textContent;
  requests.length = 0;
  await buttons(oldRow)[0].onclick();
  assert.deepEqual(requests.map((r) => [r.method, r.url]).slice(0, 2), [
    ["POST", `/api/persons/releases/${OLD_RELEASE}/activate`],
    ["GET", "/api/persons/releases"],
  ]);
  assert.ok(requests.some((r) => r.url === "/api/settings/embedding-status"));
  assert.equal(get("release-notice").textContent, "已恢复检索组合。");
  assert.equal(get("release-notice").className, "notice info");
  assert.equal(get("notice").textContent, topNotice, "索引版本反馈不写入页面顶部提示");

  // 后端拒绝（例如删除生效组合）时显示错误并恢复按钮。
  failNext = "这是当前生效的检索组合；请先恢复到其他组合再删除";
  const deleteButton = buttons(get("releases").children[0])[0];
  await deleteButton.onclick();
  assert.equal(
    get("release-notice").textContent,
    "这是当前生效的检索组合；请先恢复到其他组合再删除",
  );
  assert.equal(get("release-notice").className, "notice error");
  assert.equal(deleteButton.disabled, false);

  // 发布当前检索组合。
  requests.length = 0;
  await get("publish").onclick();
  assert.deepEqual(requests[0], { url: "/api/persons/publish", method: "POST" });
  assert.equal(get("release-notice").textContent, "人物证据已发布；内容未变化时复用已有发布记录。");

  // 生效内容索引没有匹配的检索发布版：明确显示未绑定，而不是空白。
  payload = { active: { content_id: CONTENT, release_id: null, person_id: null }, releases: [] };
  await sandbox.loadReleases();
  assert.ok(get("release-current").textContent.includes("检索发布版 未绑定"));
  assert.equal(get("releases").children[0].textContent, "还没有任何检索组合");

  // 没有任何生效索引。
  payload = { active: null, releases: [] };
  await sandbox.loadReleases();
  assert.equal(get("release-current").textContent, "当前没有生效的检索组合");
  assert.equal(get("release-current").title, undefined);

  // 页面结构：入口位于设置页最底部的「索引版本」，人物库页不再保留。
  const settings = fs.readFileSync("frontend/src/settings/settings.html", "utf8");
  const persons = fs.readFileSync("frontend/src/persons/persons.html", "utf8");
  for (const id of [
    "releases-row",
    "releases-body",
    "release-current",
    "releases",
    "publish",
    "release-notice",
  ]) {
    assert.ok(settings.includes(`id="${id}"`), id);
    assert.ok(!persons.includes(`id="${id}"`), id);
  }
  assert.ok(
    settings.lastIndexOf('aria-label="索引版本"') >
      settings.lastIndexOf('aria-label="语音合成设置"'),
  );
  assert.ok(settings.indexOf('aria-label="索引版本"') < settings.indexOf("</main>"));
  assert.ok(!persons.includes("发布与回退") && !persons.includes("处理与发布"));
  console.log("settings release UI checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
