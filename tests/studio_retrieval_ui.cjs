// 检索组合依赖：分镜表上方一行（当前生效 / 工作台依赖 / 是否一致），以及成片合成依赖树里的检索组合
// （真实创作页脚本代码 + DOM 桩）。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    Object.assign(this, {
      tagName: tag,
      textContent: "",
      className: "",
      hidden: false,
      attrs: {},
      children: [],
    });
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }
  append(...children) {
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this.children = children;
  }
}
const box = new Node();
const versions = {};
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  state: { data: {} },
  $: () => box,
  el: (tag, text, cls) => {
    const n = new Node(tag);
    if (text != null) n.textContent = text;
    n.className = cls || "";
    return n;
  },
  button: (text) => {
    const n = new Node("button");
    n.textContent = text;
    return n;
  },
  shortId: (id) => String(id ?? "").slice(0, 8),
  ver: (id) => versions[id],
  showVersion: () => {},
  LABEL: { compose: "成片合成", render: "分镜渲染" },
};
vm.createContext(sandbox);
const source = require("./studio_source.cjs").studioSource();
vm.runInContext(
  source.slice(source.indexOf("// 检索组合依赖（后端"), source.indexOf("function renderMatrix(")),
  sandbox,
);
vm.runInContext(
  source.slice(source.indexOf("// 成片合成版本沿依赖链"), source.indexOf("function versionFiles(")),
  sandbox,
);
const render = (retrieval) => {
  sandbox.state.data = { retrieval };
  vm.runInContext("renderRetrieval()", sandbox);
};

const CONTENT = "948ffaef006da3df9d827c41",
  NEW_CONTENT = "cfbdd3fcccdae82970f426c7";
const RELEASE = "594791409ff1b0fac35c2ff6",
  NEW_RELEASE = "e60e29dc0d45a1ce93ed34b7";
const PERSON = "d045d64f39c2f065026a4538";
const OLD = "检索发布版 59479140（内容索引 948ffaef · 人物快照 d045d64f）";
const NEW = "检索发布版 e60e29dc（内容索引 cfbdd3fc · 人物快照 d045d64f）";
const combo = (extra) => ({
  content_id: CONTENT,
  release_id: RELEASE,
  person_id: PERSON,
  known: true,
  ...extra,
});
const group = (extra) => combo({ shots: 36, current: true, ...extra });

// 缺少后端字段（旧后端）时整块隐藏。
render(undefined);
assert.equal(box.hidden, true);

// 一致：只有一行，写明当前生效、工作台依赖与结论。
render({ active: combo(), used: [group()] });
assert.equal(box.hidden, false);
assert.equal(box.children.length, 1);
assert.equal(box.children[0].textContent, `检索组合：当前生效 ${OLD}；工作台依赖 ${OLD}；一致`);
for (const id of [RELEASE, CONTENT, PERSON])
  assert.ok(box.children[0].attrs.title.includes(id), id);
assert.ok(!box.children[0].className.includes("is-mismatch"));

// 不一致：生效组合已换；依赖多个组合时逐个列出分镜数。
render({
  active: combo({ content_id: NEW_CONTENT, release_id: NEW_RELEASE }),
  used: [
    group({ current: false, shots: 30 }),
    group({ content_id: NEW_CONTENT, release_id: NEW_RELEASE, shots: 6, current: true }),
  ],
});
assert.equal(box.children.length, 1);
assert.ok(
  box.children[0].textContent.startsWith(
    `检索组合：当前生效 ${NEW}；工作台依赖 ${OLD}，30 个分镜 / ${NEW}，6 个分镜；不一致`,
  ),
);
assert.ok(box.children[0].className.includes("is-mismatch"));
render({
  active: combo({ content_id: NEW_CONTENT, release_id: NEW_RELEASE }),
  used: [group({ current: false })],
});
assert.ok(box.children[0].textContent.endsWith("不一致，请重新召回或在设置页“索引版本”恢复"));

// 没有召回结果 / 没有生效索引：不下结论。
render({ active: combo(), used: [] });
assert.ok(box.children[0].textContent.endsWith("工作台依赖 暂无召回结果；无需比较"));
render({ active: null, used: [group()] });
assert.ok(
  box.children[0].textContent.includes("当前生效 无（没有生效的内容索引）") &&
    box.children[0].textContent.endsWith("无需比较"),
);

// 发布版未绑定 / 记录已删除。
render({
  active: combo({ release_id: null, person_id: null, known: false }),
  used: [group({ release_id: "deleted0123456789abcdef", known: false, person_id: null })],
});
assert.ok(
  box.children[0].textContent.includes(
    "当前生效 检索发布版 未绑定（内容索引 948ffaef · 人物快照 未绑定）",
  ),
);
assert.ok(
  box.children[0].textContent.includes(
    "工作台依赖 检索发布版 deleted0（内容索引 948ffaef · 人物快照 未知）",
  ),
);

// 成片合成依赖树：根节点下增加检索组合依赖。
Object.assign(versions, {
  c1: {
    id: "c1",
    stage: "compose",
    shot_id: null,
    inputs: { board: null, s1: "r1" },
    status: "complete",
  },
  r1: { id: "r1", stage: "render", shot_id: "s1", inputs: {}, status: "complete" },
  c2: { id: "c2", stage: "compose", shot_id: null, inputs: {}, status: "complete" },
});
const tree = (id, retrieval) => {
  sandbox.state.data = { retrieval };
  return vm.runInContext(`dependencyTree(${JSON.stringify(id)})`, sandbox);
};
const edges = (node) => node.children.filter((n) => n.className === "dependency-edge");
let root = tree("c1", {
  compose: {
    c1: [
      group({ current: false, shots: 30 }),
      group({ content_id: NEW_CONTENT, release_id: NEW_RELEASE, shots: 6 }),
    ],
  },
});
assert.equal(root.children[0].textContent, "成片合成  · c1");
let [first, second, render1] = edges(root);
assert.equal(first.children[0].textContent, "检索组合");
assert.equal(first.children[1].textContent, `${OLD}，30 个分镜`);
assert.ok(
  first.children[1].attrs.title.includes(RELEASE) && first.children[1].attrs.title.includes(PERSON),
);
assert.equal(first.children[2].textContent, "与当前生效不一致");
assert.equal(second.children.length, 2, "与当前生效一致的组合不加不一致标记");
assert.equal(render1.children[0].textContent, "s1", "原有依赖仍然保留");
// 没有召回依赖时明确说明；非成片合成节点与缺少数据时不增加该项。
root = tree("c2", { compose: { c2: [] } });
assert.equal(edges(root)[0].children[1].textContent, "无召回依赖（分镜均不使用召回结果）");
assert.equal(edges(tree("r1", { compose: { c1: [group()] } })).length, 0);
assert.equal(edges(tree("c1", undefined)).length, 1);

// 页面结构：一行描述位于分镜工作台内、分镜条之前。
const html = fs.readFileSync("frontend/src/studio/studio.html", "utf8");
const at = html.indexOf('id="retrieval-deps"');
assert.ok(at > html.indexOf('id="storyboard-section"') && at < html.indexOf('id="shot-strip"'));
console.log("studio retrieval UI checks passed");
