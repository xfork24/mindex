const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag, text = "", cls = "") {
    Object.assign(this, {
      tagName: tag, textContent: text, className: cls, children: [],
      style: {}, dataset: {}, attrs: {}, events: {},
      classList: { add: () => {} }, isConnected: true, readyState: 4,
      currentTime: 0, duration: 10, paused: true,
    });
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(k, fn) { this.events[k] = fn; }
  getBoundingClientRect() { return { left: 0, width: 1000 }; }
  setPointerCapture() {}
  focus() {}
  pause() { this.paused = true; this.events.pause?.(); }
  async play() { this.paused = false; this.events.play?.(); }
}
const ctx = {
  revision: "r1", plan_id: "p", shot_id: "a", anchor: { rank: 2, start: 2, end: 3 },
  anchor_ref: { recall_version: "recall", candidate_id: "c2" }, tts_version: "tts",
  window_duration: 2, context_range: { start: 1, end: 8 }, legal_start: { min: 1, max: 6 },
  initial_start: 2, atempo: 1, context_token: "token1", frame_step: 1001 / 30000,
  ready: true, extension: { before: 0, after: 0 },
  shots: [{ id: "s1", start: 1, end: 2 }, { id: "s2", start: 2, end: 8 }],
  video_url: "video", thumbnails_url: "thumbs",
};
const requests = [], frames = new Map();
let currentCtx = ctx, scope = "job/p", pendingPreview = null, previewFailure = false, frameID = 0;
const sandbox = {
  MindexI18n: require('./i18n_adapter.cjs'),
  // multiselect.js 未在 vm 中加载；给出与真实组件同接口的桩。
  MindexMultiSearch: {sort: entries => entries.slice().sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh')), attach() {}, refilter() {}},
  console, Node, Math, JSON, Number, String, Map, Promise, Error,
  state: { windowDrafts: new Map(), data: { revision: "r1" } },
  scopeKey: () => scope, planBase: () => "/plan", plan: () => ({ ranges: {} }),
  el: (tag, text, cls) => new Node(tag, text, cls),
  field: (label, value) => ({ wrap: new Node("label", label), input: Object.assign(new Node("input"), { value }) }),
  button: (text, action, disabled = false) => Object.assign(new Node("button", text), { action, disabled }),
  mutationButton: (text, action, disabled = false) => Object.assign(new Node("button", text), { action, disabled }),
  media: (tag, src) => Object.assign(new Node(tag), { src }), secs: n => `${n.toFixed(3)}s`,
  busy: () => false, updateLocks: () => {}, notice: () => {}, accept: async () => {},
  requestAnimationFrame: fn => { frames.set(++frameID, fn); return frameID; },
  cancelAnimationFrame: id => frames.delete(id),
  api: async (url, method, body) => {
    requests.push({ url, method, body });
    if (url.endsWith("selection-context")) return structuredClone(currentCtx);
    if (url.endsWith("range-preview")) {
      if (previewFailure) throw new Error("preview unavailable");
      if (pendingPreview) return await pendingPreview;
      return { preview_token: "preview", preview_url: "preview.mp4" };
    }
    return {};
  },
};
vm.createContext(sandbox);
const source = fs.readFileSync("frontend/src/studio/studio.js", "utf8");
vm.runInContext(source.slice(source.indexOf("function shortIds("), source.indexOf("function el(")), sandbox);
vm.runInContext(source.slice(source.indexOf("async function manualRange("), source.indexOf("\nasync function preflight(")), sandbox);
const find = (node, predicate) => node.children.find(predicate) || node.children.map(n => find(n, predicate)).find(Boolean);
const slider = b => find(b, n => n.attrs.role === "slider");
const track = b => find(b, n => n.className === "window-track");
const control = (b, text) => find(b, n => n.textContent === text);
const event = key => ({ key, preventDefault: () => {} });
function tick() {
  const batch = [...frames.values()]; frames.clear(); batch.forEach(fn => fn());
}

(async () => {
  assert.equal(sandbox.shortIds("分镜 shot-8220c84eed：依赖 0123456789abcdef0123456789abcdef"),
    "分镜 shot-8220c84e：依赖 01234567");
  assert.equal(sandbox.shortIds("版本 12345678-1234-5678-9abc-def012345678"), "版本 12345678");
  assert.equal(sandbox.shortIds("版本 12345678901234567890123456789012"), "版本 12345678");
  assert.equal(sandbox.shortIds("开始 2.0333666666666668 秒，配音 0.540625 秒"),
    "开始 2.0333666666666668 秒，配音 0.540625 秒");
  const original = { shot_id: "shot-8220c84eed", version: "0123456789abcdef0123456789abcdef" };
  sandbox.shortIds(JSON.stringify(original));
  assert.equal(original.shot_id, "shot-8220c84eed");
  assert.equal(original.version, "0123456789abcdef0123456789abcdef");
  const body = new Node("div"); await sandbox.manualRange("a", body);
  const box = slider(body), t = track(body), width = box.style.width;
  assert.equal(control(body, "保存当前窗口").disabled, false);
  for (let i = 0; i < 40; i++) box.events.keydown(event("ArrowRight"));
  assert.equal(box.style.width, width);
  assert.ok(Number(box.attrs["aria-valuenow"]) > 3);
  t.events.pointerdown({ clientX: 700, button: 0, pointerId: 1, target: t, preventDefault: () => {} });
  for (let x = 0; x <= 1000; x += 10) t.events.pointermove({ clientX: x, pointerId: 1 });
  t.events.pointerup();
  assert.equal(box.style.width, width);
  assert.equal(Number(box.attrs["aria-valuenow"]), 6);
  assert.equal(requests.length, 1); // Dragging and clicking never request server work.

  // The video's native controls enforce the selected window bounds.
  const video = find(body, n => n.tagName === "video");
  video.currentTime = 0;
  await video.play(); // Simulates the native video play control.
  assert.equal(video.currentTime, 5); // Source 6 seconds minus context origin 1 second.
  video.currentTime = 7.1; video.events.timeupdate();
  assert.equal(video.paused, true);
  assert.equal(video.currentTime, 7);
  await video.play(); // Replay at the end restarts at the selected start.
  assert.equal(video.currentTime, 5);
  video.currentTime = 0; video.events.seeking();
  assert.equal(video.currentTime, 5);
  video.currentTime = 7.01; tick(); // Frame polling catches the boundary between timeupdate events.
  assert.equal(video.paused, true);
  assert.equal(video.currentTime, 7);
  await video.play();
  assert.equal(video.currentTime, 5);
  video.currentTime = 7.2; video.events.seeking();
  assert.equal(video.paused, true);

  // Direct saving performs preview validation and confirmation, with no manual preview prerequisite.
  const save = control(body, "保存当前窗口");
  await save.action();
  const writes = requests.filter(r => r.method === "POST");
  assert.deepEqual(writes.map(r => r.url), ["/plan/range-preview", "/plan/range-confirm"]);
  assert.equal(writes[0].body.render_preview, false); // Saving must not render media.
  assert.equal(writes[1].body.start, 6);
  assert.equal(writes[1].body.end, undefined);
  assert.equal(writes[1].body.context_token, "token1");
  assert.equal(writes[1].body.anchor_ref.candidate_id, "c2");
  assert.equal(writes[1].body.preview_token, "preview");

  const next = new Node("div"); await sandbox.manualRange("a", next);
  assert.equal(slider(next).attrs["aria-valuenow"], "6");
  await control(next, "试听配音与画面").action();
  assert.equal(control(next, "保存当前窗口").disabled, false);
  slider(next).events.keydown(event("ArrowLeft"));
  assert.equal(control(next, "保存当前窗口").disabled, false); // Moving never makes saving depend on preview.
  const before = requests.length;
  await control(next, "保存当前窗口").action();
  assert.equal(requests.length - before, 2); // A changed window obtains a fresh token before saving.

  currentCtx = { ...ctx, context_token: "token2", revision: "r2" };
  sandbox.state.data.revision = "r2";
  const revised = new Node("div"); await sandbox.manualRange("a", revised);
  assert.equal(slider(revised).attrs["aria-valuenow"], slider(next).attrs["aria-valuenow"]);
  currentCtx = { ...currentCtx, tts_version: "tts2", atempo: 1.25, window_duration: 1.6, context_token: "token3" };
  const changed = new Node("div"); await sandbox.manualRange("a", changed);
  assert.equal(slider(changed).attrs["aria-valuenow"], "2");
  assert.notEqual(slider(changed).style.width, width);

  // A late optional preview cannot become the token for another window position.
  let resolve; pendingPreview = new Promise(r => { resolve = r; });
  const generating = control(changed, "试听配音与画面").action();
  slider(changed).events.keydown(event("ArrowRight"));
  resolve({ preview_token: "late", preview_url: "late.mp4" }); await generating;
  pendingPreview = null;
  const changedSave = control(changed, "保存当前窗口"), writesBefore = requests.length;
  await changedSave.action();
  assert.equal(requests.length - writesBefore, 2);
  assert.equal(requests.at(-1).body.preview_token, "preview");

  // Saving locks the position, propagates errors, and stays retryable after failed validation.
  slider(changed).events.keydown(event("ArrowRight"));
  previewFailure = true;
  await assert.rejects(changedSave.action(), /preview unavailable/);
  assert.equal(changedSave.disabled, false);
  previewFailure = false;
  pendingPreview = new Promise(r => { resolve = r; });
  const saving = changedSave.action(), start = slider(changed).attrs["aria-valuenow"];
  assert.equal(changedSave.disabled, true);
  slider(changed).events.keydown(event("ArrowRight"));
  assert.equal(slider(changed).attrs["aria-valuenow"], start);
  sandbox.state.data.revision = "stale";
  const count = requests.filter(r => r.url.endsWith("range-confirm")).length;
  resolve({ preview_token: "stale", preview_url: "stale.mp4" });
  await assert.rejects(saving, /方案已变化/);
  assert.equal(requests.filter(r => r.url.endsWith("range-confirm")).length, count);

  console.log("Window checks passed: native bounded playback, direct save, fixed length, drafts, stale responses and retry.");
})().catch(error => { console.error(error); process.exit(1); });
