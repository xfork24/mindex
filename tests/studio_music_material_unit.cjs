// 分镜渲染阶段列出背景音乐素材：每个分镜显示自己适用的背景音乐（切入 / 顺延 / 停止 / 没有），
// 在对应提示点分镜切换；成片面板列出实际混入的各段。真实创作页脚本 + 轻量桩，不依赖浏览器。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const dir = path.join(__dirname, "../frontend/src/studio");
const read = name => fs.readFileSync(path.join(dir, name), "utf8");
const slice = (text, start, end) => text.slice(text.indexOf(start), text.indexOf(end));
const sandbox = {
  MindexI18n: require("./i18n_adapter.cjs"),
  console, Map, Set, Promise, Error, JSON, String, Boolean, Object, Date, Array, Number, RegExp,
  document: { getElementById: () => null },
};
vm.createContext(sandbox);
vm.runInContext(read("studio-state.js"), sandbox);
const stages = read("studio-stages.js");
vm.runInContext(slice(stages, "function shotCue(", "function musicEditable("), sandbox);
vm.runInContext(slice(stages, "function musicMaterial(", "function renderMusicSection("), sandbox);
vm.runInContext(slice(read("studio-build.js"), "function renderFinalMusic(", "function renderFinal("), sandbox);
// 用普通对象代替 DOM 节点。
sandbox.__node = props => ({ ...props, children: [], append(...nodes) { this.children.push(...nodes); } });
vm.runInContext(`
  el = (tag, text, cls) => __node({ tag, text: text ?? "", cls: cls || "" });
  media = (tag, url) => __node({ tag, url });
`, sandbox);
const run = code => vm.runInContext(code, sandbox);
const texts = node => [node.text, ...(node.children || []).flatMap(texts)].filter(Boolean);
const collect = (node, predicate) => [...(predicate(node) ? [node] : []), ...(node.children || []).flatMap(child => collect(child, predicate))];

// a 没有背景音乐；b 切入音乐 A，c 顺延；d 停止；e 切入音乐 B（尚未生成）。
const content = (sid, music) => ({ [`c-${sid}`]: { data: { music } } });
const shots = ["a", "b", "c", "d", "e"].reduce((all, sid) => ({
  ...all, [sid]: { content: `c-${sid}`, tts: `t-${sid}`, music: { b: "m-b", e: "m-e" }[sid] ?? null },
}), {});
run(`state.id = "job"; state.data = ${JSON.stringify({
  revision: "rev", active_plan: "p1", builds: [],
  plans: { p1: { id: "p1", board: "board", shots, ranges: {}, options: {} } },
  versions: {
    board: { data: { order: ["a", "b", "c", "d", "e"] } },
    ...content("a", null), ...content("b", { kind: "generate", prompt: "轻快电子", volume: 0.5 }),
    ...content("c", null), ...content("d", { kind: "none", volume: 0.25 }),
    ...content("e", { kind: "cover", prompt: "温暖流行风格翻唱版本", volume: 0.3 }),
    "m-b": { id: "m-b-0000000000", status: "complete", data: { duration: 12 }, files: { music: { url: "/m/b.mp3" } } },
    "m-e": { id: "m-e-0000000000", status: "running", data: {}, files: {} },
  },
  shot_music: { b: { from: "b", start: true }, c: { from: "b", start: false }, d: { stop: true }, e: { from: "e", start: true } },
})};`);

const render = sid => { const body = sandbox.__node({}); run(`renderMusicMaterial`)(body, sid); return body; };
const all = sid => render(sid).children.flatMap(texts).join("\n");

// 1. 分镜 a 没有背景音乐；列表里没有音频。
assert.match(all("a"), /本镜没有背景音乐/);
assert.equal(collect(render("a"), n => n.tag === "audio").length, 0);
// 2. b 是提示点：切入新的背景音乐，列出描述、音量、版本，并带试听。
assert.match(all("b"), /本镜切入新的背景音乐（.*）：轻快电子/);
assert.match(all("b"), /混音音量 0\.5 · 音乐版本 m-b-0000/);
assert.deepEqual(collect(render("b"), n => n.tag === "audio").map(n => n.url), ["/m/b.mp3"]);
// 3. c 没有自己的提示点：顺延 b 的音乐（同一素材），而不是显示“没有”。
assert.match(all("c"), /本镜顺延 #2 .*切入的背景音乐.*：轻快电子/);
assert.deepEqual(collect(render("c"), n => n.tag === "audio").map(n => n.url), ["/m/b.mp3"]);
// 4. d 停止背景音乐：在这一镜切断，不再显示顺延的 b。
assert.match(all("d"), /背景音乐在本镜停止/);
assert.equal(collect(render("d"), n => n.tag === "audio").length, 0);
// 5. e 切入另一段音乐；尚未生成成功时给出警告而不是静默。
assert.match(all("e"), /本镜切入新的背景音乐（.*）：温暖流行风格翻唱版本/);
assert.match(all("e"), /背景音乐尚未成功生成/);
assert.ok(collect(render("e"), n => n.cls === "warn-text").length >= 1);
// 6. 缓存键随着归属与音乐版本状态变化：切换提示点、音乐生成完成后分镜渲染详情会重建。
const key = sid => JSON.stringify(run(`musicMaterialKey(${JSON.stringify(sid)})`));
assert.notEqual(key("b"), key("c"));
const before = key("e");
run(`state.data.versions["m-e"].status = "complete"`);
assert.notEqual(before, key("e"));

// 7. 成片面板列出实际混入的各段：分镜序号范围、成片时间、音量、描述与试听。
const final = {
  data: {
    boundaries: ["a", "b", "c", "d", "e"].map(shot_id => ({ shot_id })),
    music: [
      { cue: "b", shots: ["b", "c"], version: "m-b", volume: 0.5, prompt: "轻快电子", start: 1.5, end: 4.25 },
      { cue: "e", shots: ["e"], version: "m-e", volume: 0.3, prompt: "温暖流行风格翻唱版本", start: 6, end: 8 },
    ],
  },
};
const host = sandbox.__node({});
run(`renderFinalMusic`)(host, final);
const listing = host.children.flatMap(texts);
assert.ok(listing.some(line => /成片背景音乐/.test(line)));
assert.ok(listing.some(line => line.includes("第 2–3 镜") && line.includes("1.500s") && line.includes("音量 0.5")));
assert.ok(listing.some(line => line.includes("第 5–5 镜") && line.includes("音量 0.3")));
assert.deepEqual(collect(host, n => n.tag === "audio").map(n => n.url), ["/m/b.mp3"]);
// 没有背景音乐的成片不出现该区域。
const empty = sandbox.__node({});
run(`renderFinalMusic`)(empty, { data: { boundaries: [] } });
assert.equal(empty.children.length, 0);
console.log("studio music material ok");
