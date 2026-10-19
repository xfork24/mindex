"use strict";
const STAGES = ["recall", "tts", "select", "render"];
// 多选框顶部的“全部选择”占位值，不是一个真实人物 ID。
const ALL_VALUE = "__all__";
function allOptionOf(select) {return [...select.options].find((o) => o.value === ALL_VALUE);}
function itemOptionsOf(select) {return [...select.options].filter((o) => o.value !== ALL_VALUE);}
function syncAllOf(select) {
  const all = allOptionOf(select);
  if (!all) return;
  const items = itemOptionsOf(select);
  all.selected = items.length > 0 && items.every((o) => o.selected);
  select.dataset.allSelected = all.selected ? "1" : "0";
}
function applyAllOf(select, on) {for (const o of itemOptionsOf(select)) o.selected = on;syncAllOf(select);}
// change 事件分不清是点了“全部选择”还是普通项，用上一次的全选状态判断意图。
function toggleAllOf(select) {
  const all = allOptionOf(select);
  if (all && all.selected !== (select.dataset.allSelected === "1")) applyAllOf(select, all.selected);else
  syncAllOf(select);
}
function pickedIds(select) {return [...select.selectedOptions].map((o) => o.value).filter((v) => v !== ALL_VALUE);}
function fillAllSelect(select, entries, chosen) {
  select.replaceChildren();
  const all = el("option", MindexI18n.msg("ui.3e26ad9407ad"));
  all.value = ALL_VALUE;
  select.append(all);
  for (const entry of MindexMultiSearch.sort(entries)) {
    const o = el("option", `${entry.name} · ${entry.id.slice(0, 8)}`);
    o.value = entry.id;
    o.selected = chosen.has(entry.id);
    select.append(o);
  }
  MindexMultiSearch.attach(select);
  MindexMultiSearch.refilter(select);
  syncAllOf(select);
}
function multiField(label) {
  const wrap = el("label", label, "form-field");
  const select = document.createElement("select");
  select.multiple = true;
  MindexI18n.setAttr(select, "aria-label", label);
  wrap.append(select);
  return { wrap, select };
}
const LABEL = {
  content: MindexI18n.msg("ui.5727d4463f7c"),
  board: MindexI18n.msg("ui.4d7765b7c123"),
  split: MindexI18n.msg("ui.e08119699ac5"),
  recall: MindexI18n.msg("ui.d06847315d0d"),
  tts: MindexI18n.msg("ui.0f10bb37a946"),
  select: MindexI18n.msg("ui.7a88bdc950ad"),
  render: MindexI18n.msg("ui.d458415b7971"),
  compose: MindexI18n.msg("ui.47badcfc4cad")
};
const STATUS = {
  running: MindexI18n.msg("ui.1f425b6bf0a4"),
  partial: MindexI18n.msg("ui.53951802211e"),
  failed: MindexI18n.msg("ui.3e3c8068bb0e"),
  blocked: MindexI18n.msg("ui.a00db105bd49"),
  pending: MindexI18n.msg("ui.25d23b92b225"),
  complete: MindexI18n.msg("ui.33246f6a5e5b"),
  reused: MindexI18n.msg("ui.68f7d028b705"),
  skipped: MindexI18n.msg("ui.31a98593f16d")
};
const state = {
  id: null,
  data: null,
  jobs: [],
  timer: null,
  token: 0,
  submitting: false,
  expanded: new Map(),
  drafts: new Map(),
  windowDrafts: new Map(),
  matrixRows: new Map(),
  matrixScope: null,
  logViews: new Map(),
  finalUrl: null,
  persons: null, // 懒加载的人物身份（演员/角色；作品不参与选择）
  sources: null, // 懒加载的素材列表
  inputs: null // 查询文件 media_id → 产物条目索引
};
const $ = (id) => document.getElementById(id);
function shortIds(value) {
  return String(value ?? "").
  replace(/\b([a-f0-9]{8})-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b/gi, "$1").
  replace(/\b(shot-)([a-f0-9]{9,64})\b/gi, (_, prefix, id) => prefix + id.slice(0, 8)).
  replace(/(?<![a-z0-9_.])(?:[0-9]{32}|[0-9]{64})(?![a-z0-9_])/g, (id) => id.slice(0, 8)).
  replace(/(?<![a-z0-9_.])(?=[a-f0-9]{9,64}(?![a-z0-9_]))(?=[a-f0-9]*[a-f])[a-f0-9]{9,64}(?![a-z0-9_])/gi,
  (id) => id.slice(0, 8));
}
function shortId(value) {
  const id = String(value ?? "");
  return /^[a-f0-9]{9,64}$/i.test(id) ? id.slice(0, 8) : shortIds(id);
}
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) MindexI18n.setText(n, text);
  if (cls) n.className = cls;
  return n;
}
function button(text, action, disabled = false) {
  const n = el("button", text, "quiet");
  n.type = "button";
  n.disabled = disabled;
  n.addEventListener("click", () => {
    $("dialog-error")?.remove();
    Promise.resolve().then(action).catch(fail);
  });
  return n;
}
function link(text, url, download = false) {
  const n = el("a", shortIds(text), "quiet nav-link");
  n.href =
  url + (download ? `${url.includes("?") ? "&" : "?"}download=true` : "");
  return n;
}
function notice(text, error = false) {
  MindexI18n.setText($("notice"), shortIds(error ? MindexI18n.system(text) : text));
  $("notice").dataset.type = error ? "error" : "info";
}
function fail(error) {
  notice(error.message, true);
  if ($("work-dialog").open) {
    $("dialog-error")?.remove();
    const message = el("p", shortIds(error.message), "warn-text");
    message.id = "dialog-error";
    message.setAttribute("role", "alert");
    $("dialog-content").prepend(message);
  }
}
async function api(url, method = "GET", body) {
  const r = await fetch(url, {
    method,
    ...(body ?
    {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    } :
    {})
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok)
  throw new Error(MindexI18n.system(data));
  return data;
}
const base = () => `/api/studio/jobs/${state.id}`;
const plan = () => state.data.plans[state.data.active_plan];
const planBase = () => `${base()}/plans/${plan().id}`;
const ver = (id) => state.data.versions[id];
const order = () => plan().board ? ver(plan().board).data.order : [];
const busy = () =>
state.submitting || state.data?.builds.some((b) => b.status === "running");
const secs = (n) => `${Number(n || 0).toFixed(3)}s`;
function showDialog(title) {
  MindexI18n.setText($("dialog-title"), shortIds(title));
  const body = $("dialog-content");
  body.replaceChildren();
  if (!$("work-dialog").open) $("work-dialog").showModal();
  return body;
}
function closeDialog() {
  $("work-dialog").
  querySelectorAll("video,audio").
  forEach((n) => n.pause());
  $("work-dialog").close();
}
function field(label, value, type = "text", choices = null) {
  const wrap = el("label", label, "form-field");
  const n = document.createElement(
    choices ? "select" : type === "textarea" ? "textarea" : "input"
  );
  MindexI18n.setAttr(n, "aria-label", label);
  if (choices) {
    for (const [v, text] of choices) {
      const o = el("option", text);
      o.value = v;
      n.append(o);
    }
  } else if (type !== "textarea") n.type = type;
  n.value = MindexI18n.render(value ?? "");
  if (type === "number") n.step = "0.001";
  wrap.append(n);
  return { wrap, input: n };
}
function jsonView(value) {
  const pre = el("pre", shortIds(JSON.stringify(value, null, 2)), "json-view");
  return pre;
}
function media(tag, url) {
  const n = document.createElement(tag);
  n.controls = true;
  n.preload = "metadata";
  const playlist = tag === "video" ? ShotPlayer.playlistURL(url) : null;
  if(playlist) queueMicrotask(() => ShotPlayer.attach(n, playlist));else n.src = url;
  return n;
}
async function loadJobs() {
  const data = await api("/api/studio/jobs");
  state.jobs = data.items;
  const list = $("job-list");
  list.replaceChildren();
  for (const job of data.items) {
    const b = button(job.title, () => selectJob(job.id));
    b.className = `job-item${job.id === state.id ? " active" : ""}`;
    b.append(
      el("div", MindexI18n.date(new Date(job.created_at), false), "job-sub")
    );
    list.append(b);
  }
}
async function selectJob(id, { reloadJobs = true } = {}) {
  state.token++;
  clearTimeout(state.timer);
  state.id = id;
  state.data = null;
  state.finalUrl = null;
  state.matrixScope = null;
  state.persons = state.inputs = null;
  history.replaceState(null, "", `/studio?job=${id}`);
  closeDialog();
  await refresh();
  if (id !== state.id) return;
  if (reloadJobs) loadJobs().catch(fail);
  loadArtifacts().catch(fail);
  loadRecallOptions().catch((error) => notice(MindexI18n.msg("ui.867f562167d6", { p0: error.message }), true));
}
async function loadRecallOptions() {
  const id = state.id;
  const persons = await api("/api/persons");
  if (id !== state.id) return;
  if (persons?.entities) {
    state.persons = persons.entities.filter((e) => ["actor", "role"].includes(e.kind));
    state.personSources = persons.sources || [];
  }
  await loadSources();
  if (id !== state.id) return;
  for (const entry of state.matrixRows.values()) entry.panelKey = null;
  renderMatrix();
}
// 素材是库级列表，创建表单也要用，所以跨任务保留，不随 selectJob 清空。
async function loadSources() {
  const status = await api("/api/status");
  if (status?.sources) {
    state.sources = status.sources;
    fillCreateSources();
    // 素材名要靠这份列表，元信息面板可能早于它渲染，到货后补一次。
    if (state.data) renderJobMeta();
  }
  return state.sources;
}
function fillCreateSources() {
  const select = $("create-sources");
  if (!select) return;
  const sources = state.sources || [];
  fillAllSelect(select, sources, new Set(sources.map((s) => s.id)));
  select.disabled = sources.length === 0;
  $("create-sources-empty").hidden = sources.length > 0;
}
// 创建时选中的素材；全选等价于「不限」，因此不传 source_ids。
function createSourceScope() {
  const select = $("create-sources");
  const sources = state.sources || [];
  if (!sources.length) return null;
  const picked = pickedIds(select);
  return picked.length === sources.length ? null : picked;
}
// 任务级素材范围：null 表示不限。逐镜编辑器只能在这个范围内二次选择。
function jobScope() {
  const ids = state.data?.job?.source_ids;
  return ids?.length ? new Set(ids) : null;
}
function scopedSourceEntries() {
  const scope = jobScope();
  return (state.sources || []).filter((s) => !scope || scope.has(s.id));
}
function personName(id) {
  return state.persons?.find((p) => p.id === id)?.name || shortId(id);
}
function sourceName(id) {
  return state.sources?.find((s) => s.id === id)?.name || shortId(id);
}
// 任务元信息只读：把创建任务时用的参数原样摊开，便于复盘和确认可用素材。
function renderJobMeta() {
  const job = state.data.job, o = job.options || {}, body = $("job-meta-body");
  const audioModes = {
    tts_only: MindexI18n.msg("ui.1801c99c1d7d"),
    ducking: MindexI18n.msg("ui.b49af7ac7ca1")
  };
  const rows = [
    // 文案保留非空行，素材列表独占整行并逐项展示。
    ["ui.7407393d52c7", (job.input_text || "").split(/\r?\n/).filter((line) => line.trim()).join("\n"), true],
    ["ui.84e3802f60a7", MindexI18n.date(job.created_at)],
    ["ui.f711792f098b", (job.source_ids?.length ? job.source_ids : (state.sources || []).map((s) => s.id)).map(sourceName).join("\n") || MindexI18n.msg("ui.0a9eab1a4d36"), true],
    ["ui.d19e8611d29b", audioModes[o.audio_mode] || o.audio_mode],
    ["ui.aaa5f2e9f006", o.burn ? MindexI18n.msg("ui.3f4dbcbdf0ab") : MindexI18n.msg("ui.ea2d04f98746")],
    ["ui.88af53b5b584", o.recall_limit]
  ];
  body.replaceChildren();
  for (const [label, value, wide] of rows) {
    const row = el("div", undefined, wide ? "job-meta-row wide" : "job-meta-row"),
      term = el("dt", MindexI18n.msg(label)),
      detail = el("dd", value == null ? "—" : String(value));
    row.append(term, detail);
    body.append(row);
  }
}
async function inputUrl(mediaId) {
  const id = state.id;
  if (!state.inputs) {
    const data = await api(`${base()}/artifacts`);
    if (id !== state.id) return null;
    state.inputs = new Map(
      data.items.
      filter((item) => item.path.startsWith("inputs/")).
      map((item) => [item.path.slice("inputs/".length).split(".")[0], item])
    );
  }
  return state.inputs.get(mediaId)?.url || null;
}
async function refresh() {
  const token = ++state.token,
    id = state.id;
  if (!id) return;
  const data = await api(`${base()}/workspace`);
  if (token !== state.token || id !== state.id) return;
  const wasBusy = state.data?.builds.some((b) => b.status === "running");
  state.data = data;
  render();
  await refreshLogs().catch((error) =>
  notice(MindexI18n.msg("ui.5c458a043061", { p0: error.message }), true)
  );
  clearTimeout(state.timer);
  if (busy()) state.timer = setTimeout(() => refresh().catch(fail), 1200);else
  if (wasBusy) {
    await loadArtifacts();
    await loadJobs();
  }
}
async function accept(data) {
  if (data.job.id !== state.id) return;
  state.data = data;
  render();
  await refreshLogs().catch((error) =>
  notice(MindexI18n.msg("ui.5c458a043061", { p0: error.message }), true)
  );
}
function render() {
  const p = plan(),
    job = state.data.job,
    locked = busy();
  $("no-job").hidden = true;
  $("job-workspace").hidden = false;
  const header = $("job-header");
  header.replaceChildren();
  const title = field(MindexI18n.msg("ui.6eae640bc412"), job.title);
  title.input.maxLength = 60;
  header.append(
    title.wrap,
    button(MindexI18n.msg("ui.fe0e9e6e4fad"),

    async () => {
      await api(`${base()}/title`, "PATCH", {
        title: title.input.value.trim()
      });
      await refresh();
      await loadJobs();
    },
    locked
    ),
    el("p", MindexI18n.msg("ui.1c83d271dc5a", { p0: job.id }), "muted")
  );
  const select = $("plan-select");
  renderJobMeta();
  select.replaceChildren();
  for (const item of Object.values(state.data.plans)) {
    const o = el("option", item.name);
    o.value = item.id;
    o.selected = item.id === p.id;
    select.append(o);
  }
  MindexI18n.setText($("shot-count"), MindexI18n.msg("ui.afd372b9b45d", { p0: order().length }));
  const original = job.input_text.replace(/\s/g, "");
  const texts = order().
  map((sid) => ver(p.shots[sid].content).data.text).
  join("").
  replace(/\s/g, "");
  MindexI18n.setText($("storyboard-warning"),
  p.board && original !== texts ? MindexI18n.msg("ui.34f1df936c8a") : "");
  renderMatrix();
  renderFinal();
  renderBuilds();
  renderCurrentArtifacts();
  renderLogShell();
  updateLocks();
}
function dependencyTree(id, seen = new Set()) {
  const item = ver(id);
  if (!item) return el("p", MindexI18n.msg("ui.4adbd05991ae", { p0: id }), "warn-text");
  const d = el("details", "", "dependency");
  d.append(
    el(
      "summary",
      `${LABEL[item.stage]} ${shortId(item.shot_id)} · ${id.slice(0, 8)}${item.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : ""}`
    )
  );
  d.append(button(MindexI18n.msg("ui.c56d640f8fde"), () => showVersion(id)));
  if (seen.has(id)) return d;
  const next = new Set(seen).add(id);
  for (const [name, dep] of Object.entries(item.inputs))
  if (dep) {
    const row = el("div", "", "dependency-edge");
    row.append(el("span", shortId(name), "muted"), dependencyTree(dep, next));
    d.append(row);
  }
  return d;
}
function versionFiles(body, item) {
  for (const [name, metadata] of Object.entries(item.files)) {
    if (metadata.url) body.append(link(MindexI18n.msg("ui.1a179261cfb7", { p0: name }), metadata.url, true));
  }
}
function showVersion(id) {
  const item = ver(id),
    body = showDialog(
      `${LABEL[item.stage]} · ${id.slice(0, 8)} · ${STATUS[item.status]}`
    );
  if (item.incomplete)
  body.append(
    el(
      "p", MindexI18n.msg("ui.ddb0dcb38944"),

      "warn-text"
    )
  );
  if (MindexI18n.system(item.error)) body.append(el("p", shortIds(MindexI18n.system(item.error)), "warn-text"));
  if (item.files.audio?.url) body.append(media("audio", item.files.audio.url));
  if (item.files.preview?.url || item.files.video?.url)
  body.append(
    media("video", item.files.preview?.url || item.files.video.url)
  );
  if (item.files.mp4?.url) body.append(media("video", item.files.mp4.url));
  if (item.stage === "select" && !item.incomplete)
  body.append(
    button(MindexI18n.msg("ui.176d14ef6dfa"), async () => {
      const r = await api(`${base()}/versions/${id}/preview`);
      body.append(media("video", r.url));
    })
  );
  versionFiles(body, item);
  body.append(
    el("h3", MindexI18n.msg("ui.27f7d5b1ba5a")),
    jsonView({
      inputs: item.inputs,
      parameters: item.parameters,
      data:
      item.stage === "compose" ?
      { ...item.data, graph: undefined, plan: undefined } :
      item.data
    })
  );
  body.append(el("h3", MindexI18n.msg("ui.1ac5e6f7e826")), dependencyTree(id));
  if (item.stage === "compose" && item.data.plan)
  body.append(
    button(MindexI18n.msg("ui.0e649bf77acc"), () => createPlan("restore", id), busy())
  );
}
function scopeKey() {
  return `${state.id}/${plan().id}`;
}
function draftKey(sid) {
  return `${scopeKey()}/${sid}`;
}
function expandedRows() {
  if (!state.expanded.has(scopeKey()))
  state.expanded.set(scopeKey(), new Map());
  return state.expanded.get(scopeKey());
}
function hasDirtyDraft() {
  const prefix = `${scopeKey()}/`;
  return [...state.drafts].some(
    ([key, draft]) => key.startsWith(prefix) && draft.dirty
  );
}
function mutationButton(text, action, unavailable = false, allowDirty = false) {
  const n = button(text, () => {
    if (busy() || !allowDirty && hasDirtyDraft()) return;
    return action();
  });
  n.dataset.mutating = "true";
  n.dataset.unavailable = String(unavailable);
  n.dataset.allowDirty = String(allowDirty);
  return n;
}
function updateLocks() {
  if (!state.data) return;
  const locked = busy(),
    dirty = hasDirtyDraft();
  const collapse = $("collapse-all-rows");
  collapse.disabled = !order().length;
  MindexI18n.setText(collapse, MindexI18n.msg(expandedRows().size ? "ui.57f2d43c7d09" : "ui.expand_all_rows"));
  for (const id of [
  "plan-select",
  "new-plan",
  "copy-plan",
  "rename-plan",
  "plan-settings",
  "run-all"])
  {
    $(id).disabled = locked || dirty && id !== "plan-select";
    MindexI18n.setAttr($(id), "title", dirty && id !== "plan-select" ? MindexI18n.msg("ui.9f185b0cc351") : "");
  }
  document.querySelectorAll(".regenerate-column, .execute-column").forEach((n) => {
    n.disabled =
    locked || dirty || n.dataset.stage !== "split" && !order().length;
    MindexI18n.setAttr(n, "title", dirty ? MindexI18n.msg("ui.9f185b0cc351") :

    n.dataset.stage === "split" ? MindexI18n.msg("ui.cddf7b77b6a7") : MindexI18n.msg("ui.319e0a3a0458"));


  });
  $("matrix-body").
  querySelectorAll("[data-mutating]").
  forEach((n) => {
    n.disabled =
    locked ||
    n.dataset.unavailable === "true" ||
    n.dataset.requiresTts && !plan().shots[n.dataset.requiresTts]?.tts ||
    dirty && n.dataset.allowDirty !== "true";
  });
  $("matrix-body").
  querySelectorAll("[data-shot-input]").
  forEach((n) => {
    n.disabled = locked;
  });
}
function pauseMedia(node) {
  node.querySelectorAll("audio,video").forEach((n) => n.pause());
}
function toggleCell(sid, stage) {
  const expanded = expandedRows();
  expanded.get(sid) === stage ? expanded.delete(sid) : expanded.set(sid, stage);
  renderMatrix();
}
function collapseAllRows() {
  if (!state.data) return;
  const expanded = expandedRows();
  if (expanded.size) expanded.clear();
  else for (const sid of order()) expanded.set(sid, "content");
  renderMatrix();
}
function invalidatePanel(sid) {
  const row = state.matrixRows.get(sid);
  if (row) row.panelKey = null;
}
function latestNode(stage, sid) {
  const build = [...state.data.builds].reverse().find((b) =>
  b.plan_id === plan().id &&
  b.nodes.some((n) => n.stage === stage && n.shot_id === sid)
  );
  return build?.nodes.find((n) => n.stage === stage && n.shot_id === sid);
}
function selectionWithin(node) {
  const selection = window.getSelection();
  return selection && !selection.isCollapsed && (
  node.contains(selection.anchorNode) || node.contains(selection.focusNode));
}
function executionError(stage, sid) {
  const node = latestNode(stage, sid);
  if (node?.status !== "failed") return "";
  const attempted = ver(node.version),
    tts = attempted?.parameters.tts,
    detail = node.error || attempted?.error || MindexI18n.msg("ui.6d38d4b01891");
  const context = stage === "tts" && tts ? MindexI18n.msg("ui.710dfeaa9bc9", { p0:
    tts.model, p1: tts.voice }) : "";
  const advice = stage === "tts" && MindexI18n.render(detail).includes("voice id not exist") ? MindexI18n.msg("ui.bdc1a8e66ba1") :
  "";
  const changed = stage === "tts" && tts && state.data.tts_settings &&
  tts.voice !== state.data.tts_settings.voice;
  return shortIds(MindexI18n.msg("ui.853971a580b9", { p0: context, p1: MindexI18n.system(detail), p2: changed ? MindexI18n.msg("ui.249b7096f10b", { p0:
      state.data.tts_settings.voice }) :
    advice }));
}
function cellSummary(sid, stage) {
  const selected = plan().shots[sid][stage],
    item = ver(selected);
  const cell = el("td"),
    trigger = el("div", "", "cell-trigger");
  trigger.setAttribute("role", "button");
  trigger.tabIndex = 0;
  MindexI18n.setAttr(trigger, "aria-label", MindexI18n.msg("ui.c09c857f141c", { p0: shortId(sid), p1: LABEL[stage] }));
  trigger.setAttribute(
    "aria-expanded",
    String(expandedRows().get(sid) === stage)
  );
  trigger.setAttribute("aria-controls", `shot-detail-${sid}`);
  trigger.append(
    el(
      "span",
      selected ? MindexI18n.msg("ui.ac3d0b42ecd9", { p0: selected.slice(0, 8) }) : MindexI18n.msg("ui.53e2db70167f"),
      `badge${selected ? " good" : ""}`
    )
  );
  if (item) {
    const d = item.data;
    if (stage === "content") {
      trigger.append(
        el("span", d.text, "cell-summary"),
        el("span", d.query, "cell-summary muted")
      );
      if (state.drafts.get(draftKey(sid))?.dirty)
      trigger.append(el("span", MindexI18n.msg("ui.70ac7eb1c08c"), "badge warn"));
    } else {
      const summary =
      stage === "recall" ? MindexI18n.msg("ui.3b8b0abb1f35", { p0:
        RECALL_MODE_LABEL[d.mode || "text"], p1: d.results?.length || 0 }) :
      stage === "tts" ?
      secs(d.duration) :
      stage === "select" ?
      `${d.event.video.source_name || shortId(d.event.video.source_id)} ${secs(d.event.video.start)}–${secs(d.event.video.end)}` : MindexI18n.msg("ui.a8e554e0bc5a", { p0:
        d.frames || "?", p1: d.fps || "?" });
      trigger.append(el("span", summary, "cell-summary"));
    }
  }
  const messages = state.data.conflicts[`${stage}:${sid}`] || [];
  if (messages.length)
  trigger.append(el("span", shortIds(MindexI18n.msg("ui.e5d4ec45ff67", { p0: messages.join("；") })), "cell-summary warn-text"));
  const node = latestNode(stage, sid);
  if (node)
  trigger.append(
    el("span", MindexI18n.msg("ui.7f95d89a3924", { p0: STATUS[node.status] }), "cell-status")
  );
  const error = executionError(stage, sid);
  if (error) trigger.append(el("span", error, "cell-summary warn-text"));
  trigger.append(
    el(
      "span",
      expandedRows().get(sid) === stage ? MindexI18n.msg("ui.7831f9512e6b") : MindexI18n.msg("ui.bb066c91ece7"),
      "cell-expand"
    )
  );
  cell.addEventListener("click", (event) => {
    if (event.detail > 1 || selectionWithin(cell)) return;
    toggleCell(sid, stage);
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    toggleCell(sid, stage);
  });
  cell.append(trigger);
  return cell;
}
function renderMatrix() {
  const body = $("matrix-body"),
    scope = scopeKey();
  if (state.matrixScope !== scope) {
    pauseMedia(body);
    body.replaceChildren();
    state.matrixRows.clear();
    state.matrixScope = scope;
  }
  const ids = order(),
    wanted = [];
  for (const [index, sid] of ids.entries()) {
    let entry = state.matrixRows.get(sid);
    if (!entry) {
      const summary = el("tr", "", "shot-summary-row"),
        detail = el("tr", "", "shot-detail-row"),
        cell = el("td");
      cell.colSpan = 6;
      const panel = el("div", "", "shot-detail-panel");
      panel.id = `shot-detail-${sid}`;
      cell.append(panel);
      detail.append(cell);
      entry = { summary, detail, panel, panelKey: null };
      state.matrixRows.set(sid, entry);
    }
    const head = el("th", `${index + 1} · ${shortId(sid)}`);
    head.scope = "row";
    const summaryKey = JSON.stringify([
    index, plan().shots[sid],
    STAGES.map((stage) => [state.data.conflicts[`${stage}:${sid}`], latestNode(stage, sid)]),
    state.drafts.get(draftKey(sid))?.dirty]
    );
    if (entry.summaryKey !== summaryKey && !selectionWithin(entry.summary)) {
      entry.summary.replaceChildren(
        head,
        ...["content", ...STAGES].map((stage) => cellSummary(sid, stage))
      );
      entry.summaryKey = summaryKey;
      entry.summaryPending = false;
    } else if (entry.summaryKey !== summaryKey) {
      entry.summaryPending = true;
    }
    ["content", ...STAGES].forEach((stage, column) => {
      const trigger = entry.summary.children[column + 1].firstElementChild;
      const expanded = expandedRows().get(sid) === stage;
      trigger.setAttribute("aria-expanded", String(expanded));
      const hint = expanded ? MindexI18n.msg("ui.7831f9512e6b") : MindexI18n.msg("ui.bb066c91ece7");
      if (trigger.lastElementChild.textContent !== hint)
      MindexI18n.setText(trigger.lastElementChild, hint);
    });
    wanted.push(entry.summary);
    const stage = expandedRows().get(sid);
    if (stage) {
      const selected = plan().shots[sid][stage];
      const history = Object.values(state.data.versions).
      filter((v) => v.stage === stage && v.shot_id === sid).
      map((v) => v.id);
      const key = JSON.stringify([
      stage,
      selected,
      stage === "content" ? null : plan().options,
      stage === "content" ? ids : null,
      stage === "tts" ? state.data.tts_settings : null,
      stage === "select" ? state.data.revision : null,
      ["recall", "select"].includes(stage) ? [plan().anchors?.[sid], plan().shots[sid].recall, plan().shots[sid].tts, plan().ranges[sid]] : null]
      );
      if (entry.panelKey !== key) {
        pauseMedia(entry.panel);
        entry.panel.replaceChildren();
        const heading = el("div", "", "detail-heading");
        heading.append(
          el("h3", MindexI18n.msg("ui.4d2801d57965", { p0: index + 1, p1: LABEL[stage], p2: shortId(sid) })),
          button(MindexI18n.msg("ui.5d5815647c76"), () => toggleCell(sid, stage))
        );
        entry.panel.append(heading);
        stage === "content" ?
        renderShotEditor(entry.panel, sid) :
        renderStageDetails(entry.panel, sid, stage);
        entry.panelKey = key;
        entry.historyKey = null;
      }
      MindexI18n.setText(entry.panel.querySelector("h3"), MindexI18n.msg("ui.4d2801d57965", { p0:
        index + 1, p1: LABEL[stage], p2: shortId(sid) }));
      const result = entry.panel.querySelector("[data-node-result]");
      if (result) {
        const message = executionError(stage, sid);
        if (result.textContent !== message) MindexI18n.setText(result, message);
        result.hidden = !message;
      }
      const historyKey = JSON.stringify(history);
      if (entry.historyKey !== historyKey) {
        const historyBox = entry.panel.querySelector("[data-row-history]");
        const preferred = historyBox.querySelector("select")?.value;
        historyBox.replaceChildren(historyCell(stage, sid, preferred));
        entry.historyKey = historyKey;
      }
      wanted.push(entry.detail);
    } else if (entry.detail.parentNode) {
      pauseMedia(entry.detail);
      entry.detail.remove();
    }
  }
  for (const [sid, entry] of state.matrixRows)
  if (!ids.includes(sid)) {
    pauseMedia(entry.detail);
    entry.summary.remove();
    entry.detail.remove();
    state.matrixRows.delete(sid);
  }
  if (!ids.length) {
    body.replaceChildren();
    const row = el("tr"),
      cell = el("td");
    cell.colSpan = 6;
    cell.append(
      el(
        "p", MindexI18n.msg("ui.d26826ed782c"),

        "muted"
      ),
      mutationButton(MindexI18n.msg("ui.1c9d6d4917d0"), () => changeStructure(null, "insert"))
    );
    row.append(cell);
    body.append(row);
  } else {
    for (const child of [...body.children])
    if (!wanted.includes(child)) child.remove();
    wanted.forEach((node, index) => {
      if (body.children[index] !== node)
      body.insertBefore(node, body.children[index] || null);
    });
  }
  updateLocks();
}
document.addEventListener("selectionchange", () => {
  if (state.data && [...state.matrixRows.values()].some((entry) =>
  entry.summaryPending && !selectionWithin(entry.summary)
  )) renderMatrix();
});
const RECALL_MODES = [
["text", MindexI18n.msg("ui.22d82c6044da")],
["image", MindexI18n.msg("ui.07e80d382810")],
["video", MindexI18n.msg("ui.8c5d4d07c1de")]];

const RECALL_MODE_LABEL = { text: MindexI18n.msg("ui.f4d3dab8e0c2"), image: MindexI18n.msg("ui.07e80d382810"), video: MindexI18n.msg("ui.8c5d4d07c1de") };
function recallEditable(value) {
  // 键序与 ShotRecall 模型字段一致，保证 JSON.stringify 的 dirty 判定稳定。
  const v = value || {};
  return {
    mode: v.mode || "text",
    person_ids: [...(v.person_ids || [])],
    person_mode: v.person_mode || "auto",
    person_relation: v.person_relation || "all",
    filters: {
      source_id: v.filters?.source_id ?? null,
      source_ids: [...(v.filters?.source_ids || [])],
      min_duration: v.filters?.min_duration ?? 0,
      max_duration: v.filters?.max_duration ?? null,
      scene: v.filters?.scene ?? null,
      emotion: v.filters?.emotion ?? null,
      shot_size: v.filters?.shot_size ?? null
    },
    media_id: v.media_id ?? null,
    media_kind: v.media_kind ?? null
  };
}
function recallIsDefault(r) {
  return (
    r.mode === "text" &&
    !r.person_ids.length &&
    r.person_mode === "auto" &&
    r.person_relation === "all" &&
    !r.filters.source_id &&
    !r.filters.source_ids.length &&
    !r.filters.min_duration &&
    !r.filters.max_duration &&
    !r.filters.scene &&
    !r.filters.emotion &&
    !r.filters.shot_size &&
    !r.media_id &&
    !r.media_kind);

}
function queryPreviewNode(url, kind) {
  if (kind === "image") {
    const n = el("img");
    n.src = url;
    n.loading = "lazy";
    MindexI18n.setAttr(n, "alt", MindexI18n.msg("ui.95012948d933"));
    return n;
  }
  return media("video", url);
}
function scopedRecallPersons(value) {
  const selected = value?.filters?.source_ids?.length ? value.filters.source_ids :
    value?.filters?.source_id ? [value.filters.source_id] : scopedSourceEntries().map((s) => s.id);
  const sources = new Set(selected);
  const works = new Set((state.personSources || []).filter((s) => sources.has(s.id)).map((s) => s.work_id));
  const roles = (state.persons || []).filter((p) => p.kind === "role" &&
    (p.work_ids || []).some((id) => works.has(id)));
  const actors = new Set(roles.flatMap((p) => p.actor_ids || []));
  return [...(state.persons || []).filter((p) => p.kind === "actor" && actors.has(p.id)), ...roles];
}
function renderRecallEditor(body, sid, draft) {
  const section = el("div", "", "recall-editor");
  const commit = (next) => {
    // 全默认等价于未设置：写回 null 以保持与旧 content 完全一致的 dirty 判定与签名。
    draft.data.recall = recallIsDefault(next) ? null : next;
    draft.dirty =
    JSON.stringify(draft.data) !== JSON.stringify(draft.original);
    const entry = state.matrixRows.get(sid);
    entry?.summary.children[1]?.replaceWith(cellSummary(sid, "content"));
    updateLocks();
  };
  const draw = () => {
    section.replaceChildren(el("h4", MindexI18n.msg("ui.fc0d8a6e8d52")));
    const value = draft.data.recall ? recallEditable(draft.data.recall) : null;
    const mode = value?.mode || "text";
    const modeField = field(MindexI18n.msg("ui.8c94a76f43c5"), mode, "text", RECALL_MODES);
    modeField.input.addEventListener("change", () => {
      const next = recallEditable(draft.data.recall);
      next.mode = modeField.input.value;
      if (next.mode !== "text") {
        next.person_ids = [];
        next.person_mode = "auto";
        next.person_relation = "all";
        next.media_kind = next.mode;
        next.media_id = null;
      } else {
        next.media_id = null;
        next.media_kind = null;
      }
      commit(next);
      draw();
    });
    section.append(modeField.wrap);
    if (mode === "text") {
      // 与检索页一致：演员和角色两级，角色按已选演员过滤；后端仍只收 person_ids。
      const chosen = new Set(value?.person_ids || []);
      const persons = scopedRecallPersons(value);
      const actorField = multiField(MindexI18n.msg("ui.acc714bb0167"));
      const roleField = multiField(MindexI18n.msg("ui.224f21d3016f"));
      const commitPersons = () => {
        const next = recallEditable(draft.data.recall);
        next.person_ids = [...new Set([...pickedIds(actorField.select), ...pickedIds(roleField.select)])];
        commit(next);
      };
      const roleEntries = () => {
        const actors = new Set(pickedIds(actorField.select));
        return persons.filter((p) => p.kind === "role" && (!actors.size || (p.actor_ids || []).some((id) => actors.has(id))));
      };
      const fillRoles = () => {
        fillAllSelect(roleField.select, roleEntries(), new Set(pickedIds(roleField.select)));
        commitPersons();
      };
      fillAllSelect(actorField.select, persons.filter((p) => p.kind === "actor"), chosen);
      fillAllSelect(roleField.select, roleEntries(), chosen);
      actorField.select.addEventListener("change", () => {toggleAllOf(actorField.select);fillRoles();});
      roleField.select.addEventListener("change", () => {toggleAllOf(roleField.select);commitPersons();});
      section.append(actorField.wrap, roleField.wrap);
      for (const [key, label, choices] of [
      ["person_mode", MindexI18n.msg("ui.06cf3151c450"), [
      ["auto", MindexI18n.msg("ui.be93a225bcfb")],
      ["onscreen", MindexI18n.msg("ui.bc9173082f3e")],
      ["mentioned", MindexI18n.msg("ui.cbf394311140")]]],

      ["person_relation", MindexI18n.msg("ui.ac954c28b468"), [
      ["all", MindexI18n.msg("ui.859a27c20c56")],
      ["any", MindexI18n.msg("ui.f61923e57f20")]]]])

      {
        const f = field(label, value?.[key] || (key === "person_mode" ? "auto" : "all"), "text", choices);
        f.input.addEventListener("change", () => {
          const next = recallEditable(draft.data.recall);
          next[key] = f.input.value;
          commit(next);
        });
        section.append(f.wrap);
      }
    } else {
      const uploadWrap = el(
        "label",
        mode === "image" ? MindexI18n.msg("ui.22ddb980ec95") : MindexI18n.msg("ui.f753a4b59e3f"),


        "form-field recall-file-field"
      );
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = mode === "image" ? "image/jpeg,image/png,image/webp" : "video/*";
      uploadWrap.append(fileInput);
      const uploadButton = button(MindexI18n.msg("ui.6c130ff2346d"), async () => {
        const scope = scopeKey();
        const file = fileInput.files[0];
        if (!file) throw new Error(MindexI18n.msg("ui.7bcc55e19443"));
        const form = new FormData();
        form.append("file", file);
        form.append("kind", mode);
        const r = await fetch(`${planBase()}/query-media`, { method: "POST", body: form });
        const data = await r.json().catch(() => ({}));
        if (!r.ok)
        throw new Error(MindexI18n.system(data));
        if (scope !== scopeKey() || state.drafts.get(draftKey(sid)) !== draft || draft.data.recall?.mode !== mode) return;
        const next = recallEditable(draft.data.recall);
        next.media_id = data.media_id;
        next.media_kind = data.kind;
        state.inputs = null; // 新文件入列，待重取索引
        draft.mediaPreview = { media_id: data.media_id, url: data.preview_url, kind: data.kind };
        commit(next);
        draw();
        notice(MindexI18n.msg("ui.c539109f0cf6"));
      });
      uploadButton.classList.add("recall-upload-button");
      const uploadRow = el("div", "", "recall-upload-row");
      uploadRow.append(uploadWrap, uploadButton);
      section.append(uploadRow);
      if (value?.media_id) {
        const host = el("div", "", "query-preview-host");
        section.append(host);
        if (draft.mediaPreview?.media_id === value.media_id) {
          host.append(queryPreviewNode(draft.mediaPreview.url, draft.mediaPreview.kind));
        } else {
          host.append(el("p", MindexI18n.msg("ui.e454bbfe7b70"), "muted"));
          inputUrl(value.media_id).then((url) => {
            if (!url || !host.isConnected) return;
            host.replaceChildren(queryPreviewNode(url, value.media_kind || mode));
          });
        }
      }
    }
    const sourceField = multiField(MindexI18n.msg("ui.df2bc29adabb"));
    sourceField.select.dataset.recallFilter = "source_ids";
    // 只列任务范围内的素材；越界的旧值（历史数据或手改 API）一并裁掉。
    const entries = scopedSourceEntries();
    const scope = jobScope();
    const sources = value?.filters?.source_ids?.length ? value.filters.source_ids : value?.filters?.source_id ? [value.filters.source_id] : [];
    const chosen = new Set((sources.length ? sources : entries.map((entry) => entry.id)).filter((id) => !scope || scope.has(id)));
    fillAllSelect(sourceField.select, entries, chosen);
    sourceField.wrap.append(el("span", MindexI18n.msg("ui.c1b8365074c7"), "muted"));
    sourceField.select.addEventListener("change", () => {
      toggleAllOf(sourceField.select);
      const picked = pickedIds(sourceField.select);
      const next = recallEditable(draft.data.recall);
      next.filters.source_id = null;
      // 全选 = 不写筛选，签名保持默认，历史召回版本继续复用。
      next.filters.source_ids = picked.length === entries.length ? [] : picked;
      const allowed = new Set(scopedRecallPersons(next).map((p) => p.id));
      next.person_ids = next.person_ids.filter((id) => allowed.has(id));
      commit(next);
      draw();
    });
    const filters = el("div", "", "recall-filters");
    filters.append(sourceField.wrap);
    for (const [key, label, placeholder, type] of [
      ["scene", "ui.625b392c7b8a", "ui.722889427cbf", "text"],
      ["emotion", "ui.61fe61644a75", "ui.d637fe8494d8", "text"],
      ["shot_size", "ui.6867f7b4537a", "ui.79b85267c636", "text"],
      ["min_duration", "ui.26b6f15b345b", null, "number"],
      ["max_duration", "ui.2de8ce6144b7", null, "number"]
    ]) {
      const f = field(MindexI18n.msg(label), value?.filters?.[key] ?? "", type);
      f.input.dataset.recallFilter = key;
      if (placeholder) MindexI18n.setAttr(f.input, "placeholder", MindexI18n.msg(placeholder));
      if (type === "number") {
        f.input.min = key === "min_duration" ? "0" : "0.1";
        f.input.step = "0.1";
      }
      f.input.addEventListener("input", () => {
        const next = recallEditable(draft.data.recall);
        const raw = f.input.value.trim();
        next.filters[key] = type === "number" ? (raw === "" ? (key === "min_duration" ? 0 : null) : Number(raw)) : raw || null;
        commit(next);
      });
      filters.append(f.wrap);
    }
    section.append(filters);
    section.append(
      el(
        "p",
        mode === "text" ? MindexI18n.msg("ui.beb454185f50") : MindexI18n.msg("ui.105e58965eba"),


        "muted"
      )
    );
  };
  draw();
  body.append(section);
}
function renderShotEditor(body, sid) {
  const content = ver(plan().shots[sid].content).data;
  const recall = recallEditable(content.recall);
  const original = { ...content, recall: recallIsDefault(recall) ? null : recall };
  let draft = state.drafts.get(draftKey(sid));
  if (
  !draft ||
  !draft.dirty && draft.baseContent !== plan().shots[sid].content)
  {
    draft = {
      baseContent: plan().shots[sid].content,
      original: { ...original },
      data: { ...original },
      dirty: false
    };
    state.drafts.set(draftKey(sid), draft);
  }
  const fields = el("div", "", "shot-editor-fields");
  for (const [name, label, type] of [
  ["text", MindexI18n.msg("ui.58c9fb9115f5"), "textarea"],
  ["query", MindexI18n.msg("ui.711363c42474"), "textarea"],
  ["estimated_seconds", MindexI18n.msg("ui.9e54a98e61db"), "number"]])
  {
    const f = field(label, draft.data[name], type);
    f.input.dataset.shotInput = "true";
    if (type === "textarea") f.input.maxLength = name === "text" ? 200 : 400;else
    {
      f.input.min = "0.001";
      f.input.max = "60";
    }
    f.input.addEventListener("input", () => {
      draft.data[name] =
      type === "number" ? Number(f.input.value) : f.input.value;
      draft.dirty =
      JSON.stringify(draft.data) !== JSON.stringify(draft.original);
      const entry = state.matrixRows.get(sid);
      entry.summary.children[1].replaceWith(cellSummary(sid, "content"));
      updateLocks();
    });
    fields.append(f.wrap);
  }
  body.append(fields);
  renderRecallEditor(body, sid, draft);
  const actions = el("div", "", "detail-actions");
  actions.append(
    mutationButton(MindexI18n.msg("ui.761f731d682b"), () => saveShot(sid), false, true),
    mutationButton(MindexI18n.msg("ui.11eecf503986"),

    () => {
      state.drafts.delete(draftKey(sid));
      invalidatePanel(sid);
      renderMatrix();
    },
    false,
    true
    )
  );
  const index = order().indexOf(sid);
  for (const [op, label, disabled] of [
  ["up", MindexI18n.msg("ui.8a0c839791d3"), index === 0],
  ["down", MindexI18n.msg("ui.05c46fa3b77c"), index === order().length - 1],
  ["merge", MindexI18n.msg("ui.c2226d04d16b"), index === 0],
  ["insert", MindexI18n.msg("ui.fa5046a548d6"), false],
  ["delete", MindexI18n.msg("ui.aeef3a6ac335"), order().length === 1]])

  actions.append(
    mutationButton(label, () => changeStructure(sid, op), disabled)
  );
  const history = el("div");
  history.dataset.rowHistory = "content";
  body.append(actions, history);
}
function currentBoard() {
  return order().map((sid) => ({ ...ver(plan().shots[sid].content).data }));
}
async function saveShot(sid) {
  const key = draftKey(sid),
    draft = state.drafts.get(key),
    scope = scopeKey();
  if (draft.baseContent !== plan().shots[sid].content)
  throw new Error(MindexI18n.msg("ui.9af274c47a7b"));
  const shots = currentBoard();
  shots[order().indexOf(sid)] = {
    ...draft.data,
    text: draft.data.text.trim(),
    query: draft.data.query.trim()
  };
  const result = await api(planBase(), "PATCH", {
    revision: state.data.revision,
    shots
  });
  state.drafts.delete(key);
  if (scope !== scopeKey()) return;
  invalidatePanel(sid);
  await accept(result);
  notice(MindexI18n.msg("ui.3579af078651"));
}
async function changeStructure(sid, op) {
  const shots = currentBoard(),
    oldIds = order(),
    index = oldIds.indexOf(sid),
    scope = scopeKey();
  if (op === "insert")
  shots.splice(index + 1, 0, {
    id: null,
    text: "新旁白",
    query: "新画面",
    estimated_seconds: 3
  });
  if (op === "delete") shots.splice(index, 1);
  if (op === "up" || op === "down") {
    const target = index + (op === "up" ? -1 : 1);
    [shots[index], shots[target]] = [shots[target], shots[index]];
  }
  if (op === "merge") {
    shots[index - 1].text += shots[index].text;
    shots[index - 1].query += `；${shots[index].query}`;
    shots[index - 1].estimated_seconds += shots[index].estimated_seconds;
    // 保留上一镜召回设置；上一镜为默认文字召回时继承被合并镜的设置。
    if (recallIsDefault(recallEditable(shots[index - 1].recall)) && shots[index].recall)
    shots[index - 1].recall = shots[index].recall;
    shots.splice(index, 1);
  }
  const result = await api(planBase(), "PATCH", {
    revision: state.data.revision,
    shots
  });
  if (scope === scopeKey()) {
    if (sid) state.drafts.delete(draftKey(sid));
    if (op === "merge") {
      state.drafts.delete(draftKey(oldIds[index - 1]));
      invalidatePanel(oldIds[index - 1]);
    }
    if (op === "insert") {
      const p = result.plans[result.active_plan];
      const added = Object.keys(p.shots).find((id) => !oldIds.includes(id));
      expandedRows().set(added, "content");
    }
  }
  if (scope === scopeKey()) await accept(result);
}
function renderStageDetails(body, sid, stage) {
  const vid = plan().shots[sid][stage],
    item = ver(vid),
    actions = el("div", "", "detail-actions");
  actions.append(
    mutationButton(stage === "select" ? MindexI18n.msg("ui.5d9c0feb3855") : MindexI18n.msg("ui.fb03cbd3e2da"), () => runNode(stage, sid, false)),
    mutationButton(stage === "select" ? MindexI18n.msg("ui.2c2583899752") : MindexI18n.msg("ui.c6a70df5d5ee"), () => runNode(stage, sid, true))
  );
  if (item) actions.append(button(MindexI18n.msg("ui.ae6c80665732"), () => showVersion(vid)));
  const result = el("p", "", "warn-text");
  result.dataset.nodeResult = "true";
  result.setAttribute("role", "status");
  body.append(actions, result);
  if (stage === "recall") body.append(el("p", MindexI18n.msg("ui.796ebaa8b64a"),
  "muted"));
  if (stage === "select") body.append(el("p", MindexI18n.msg("ui.6ad63039cb96"),
  "muted"));
  if (stage === "tts") {
    const current = state.data.tts_settings;
    body.append(el("p", current ? MindexI18n.msg("ui.905f065f533b", { p0:
      current.voice_mode === "inherit" ? MindexI18n.msg("ui.79cde58eb13b") : MindexI18n.msg("ui.7a506144d437"), p1: current.voice, p2: current.model }) : MindexI18n.msg("ui.b695402e923b", { p0:
      plan().options.voice || MindexI18n.msg("ui.79cde58eb13b") }), "muted"));
  }
  if (!item) body.append(el("p", MindexI18n.msg("ui.f72548296145"), "muted"));else
  {
    if (item.incomplete)
    body.append(
      el("p", MindexI18n.msg("ui.6ca99f1c90fb"), "warn-text")
    );
    if (stage === "recall") {
      const recall = item.data.recall,
        meta = item.data.meta || {};
      body.append(
        el(
          "p", MindexI18n.msg("ui.ee6cc02c2e4c", { p0:
            RECALL_MODE_LABEL[item.data.mode || "text"], p1: item.data.query || MindexI18n.msg("ui.f1b46a8c5d6c"), p2: item.data.results?.length || 0 })
        )
      );
      if (recall?.person_ids?.length)
      body.append(
        el(
          "p", MindexI18n.msg("ui.e2ee09bf74bf", { p0:
            recall.person_ids.map(personName).join("、"), p1: recall.person_mode === "onscreen" ? MindexI18n.msg("ui.0b697429bd52") : recall.person_mode === "mentioned" ? MindexI18n.msg("ui.9f7ab435be94") : MindexI18n.msg("ui.4afad877551a"), p2: recall.person_relation === "any" ? MindexI18n.msg("ui.31af9a60c654") : MindexI18n.msg("ui.3bd33bf40e37") })
        )
      );
      const sources = recall?.filters?.source_ids?.length ? recall.filters.source_ids : recall?.filters?.source_id ? [recall.filters.source_id] : [];
      const jobMaterials = jobScope();
      if (sources.length)
      body.append(el("p", MindexI18n.msg("ui.81f420e6a6d2", { p0: sources.map(sourceName).join("、") })));
      else if (jobMaterials)
      body.append(el("p", MindexI18n.msg("ui.9213d98274d9", { n: jobMaterials.size }), "muted"));
      for (const [key, label] of [["scene", "ui.625b392c7b8a"], ["emotion", "ui.61fe61644a75"], ["shot_size", "ui.6867f7b4537a"], ["min_duration", "ui.26b6f15b345b"], ["max_duration", "ui.2de8ce6144b7"]]) {
        if (recall?.filters?.[key]) body.append(el("p", `${MindexI18n.msg(label)}：${recall.filters[key]}`));
      }
      if (meta.referenced_people?.length)
      body.append(el("p", MindexI18n.msg("ui.b607edb41246", { p0: [...new Set(meta.referenced_people.map(p => p.name))].join('、') }), "muted"));
      if (meta.person_coverage_missing?.length)
      body.append(
        el(
          "p", MindexI18n.msg("ui.75671397b702", { p0:
            meta.person_coverage_missing.map(sourceName).join("、") }),
          "warn-text"
        )
      );
      if (recall?.media_id) {
        const host = el("div", "", "query-preview-host");
        body.append(host);
        inputUrl(recall.media_id).then((url) => {
          if (!url || !host.isConnected) return;
          host.replaceChildren(queryPreviewNode(url, recall.media_kind || "image"));
        });
      }
      renderCandidatePicker(body, sid);
    } else if (stage === "tts") {
      body.append(
        el(
          "p", MindexI18n.msg("ui.bfc2b2b2a536", { p0:
            item.parameters.voice || MindexI18n.msg("ui.9865b5e9b32d"), p1: secs(item.data.duration) })
        )
      );
      if (item.files.audio) body.append(media("audio", item.files.audio.url));
    } else if (stage === "select") {
      const d = item.data,
        range = d.range,
        actual = d.actual_range || d.event.video;
      body.append(
        el(
          "p", MindexI18n.msg("ui.5e5e44f7ea35", { p0:
            d.event.video.source_name || shortId(d.event.video.source_id) })
        )
      );
      if (d.anchor)
      body.append(
        el(
          "p", MindexI18n.msg("ui.f0786353b3bb", { p0:
            secs(d.anchor.start), p1: secs(d.anchor.end), p2: d.anchor.rank || 0 })
        )
      );
      if (range)
      body.append(
        el(
          "p", MindexI18n.msg("ui.32fb7a7b38fb", { p0:
            secs(range.start), p1: secs(range.end), p2: secs(actual.start), p3: secs(actual.end) })
        )
      );
      body.append(
        el(
          "p", MindexI18n.msg("ui.630a26be62ed", { p0:
            d.event.video.segment_ids.length, p1: (d.atempo || d.event.audio.atempo).toFixed(3), p2: secs(d.trim_seconds), p3: secs(d.freeze_seconds) })
        )
      );
      if (d.extension)
      body.append(
        el(
          "p", MindexI18n.msg("ui.453085df275b", { p0:
            secs(d.extension.before), p1: secs(d.extension.after) })
        )
      );
      if (d.event.warning) body.append(el("p", shortIds(d.event.warning), "warn-text"));
    } else if (stage === "render") {
      body.append(
        el("p", MindexI18n.msg("ui.a8e554e0bc5a", { p0: item.data.frames || "?", p1: item.data.fps || "?" }))
      );
      if (item.files.preview?.url || item.files.video?.url)
      body.append(
        media("video", item.files.preview?.url || item.files.video.url)
      );
      if (item.files.audio?.url)
      body.append(media("audio", item.files.audio.url));
    }
  }
  if (stage === "select") {
    const host = el("div", "", "manual-range-host");
    body.append(host);
    if (!busy()) manualRange(sid, host).catch((error) => {
      if (host.isConnected) host.replaceChildren(el("p", shortIds(error.message), "warn-text"));
    });else
    body.append(button(MindexI18n.msg("ui.1e81d450a0cb"), () => manualRange(sid, host)));

  }
  const history = el("div");
  history.dataset.rowHistory = stage;
  body.append(el("h4", MindexI18n.msg("ui.a3bc41e89f3b")), history);
}
async function regenerateColumn(stage, force = true) {
  if (busy() || hasDirtyDraft()) return;
  const inputs_by_shot = {};
  if (stage !== "split")
  for (const sid of order()) inputs_by_shot[sid] = nodeInputs(stage, sid);
  const url = planBase(),
    body = { revision: state.data.revision, stage, inputs_by_shot, force };
  state.submitting = true;
  render();
  try {
    await api(`${url}/${force ? "regenerate-column" : "execute-column"}`, "POST", body);
    notice(MindexI18n.msg(force ? "ui.92157547f1b7" : "ui.execute_column_submitted", { p0: LABEL[stage] }));
  } finally {
    state.submitting = false;
    await refresh();
  }
}
function renderLogShell() {
  const key = scopeKey(),
    build = [...state.data.builds].
    reverse().
    find((b) => b.plan_id === plan().id);
  let view = state.logViews.get(key);
  if (!view || view.buildId !== build?.id) {
    view = { buildId: build?.id, seq: 0, lines: [], legacy: false };
    state.logViews.set(key, view);
  }
  const box = $("execution-log");
  if (box.dataset.scope !== `${key}/${view.buildId || ""}`) {
    box.dataset.scope = `${key}/${view.buildId || ""}`;
    MindexI18n.setText(box,
    view.lines.join("\n") || (
    build ? MindexI18n.msg("ui.7e123855bc1a") : MindexI18n.msg("ui.27b3fd17f4de")));
    box.scrollTop = box.scrollHeight;
  }
}
async function refreshLogs() {
  if (!state.data) return;
  const key = scopeKey(),
    view = state.logViews.get(key),
    id = state.id;
  if (!view?.buildId) return;
  let more = true;
  while (more) {
    const response = await api(
      `/api/studio/jobs/${id}/builds/${view.buildId}/logs?after=${view.seq}`
    );
    if (
    id !== state.id ||
    key !== scopeKey() ||
    state.logViews.get(key) !== view)

    return;
    const box = $("execution-log"),
      follow = box.scrollHeight - box.clientHeight - box.scrollTop < 32;
    for (const item of response.items)
    view.lines.push(
      shortIds(`${MindexI18n.date(new Date(item.at), true)}  ${item.shot_id ? MindexI18n.msg("ui.fdf052132bde", { p0: item.shot_id }) : ""}${item.stage ? `${LABEL[item.stage]} · ` : ""}${MindexI18n.system(item)}`)
    );
    view.seq = response.next_seq;
    view.legacy = response.legacy;
    if (response.items.length || !view.lines.length)
    MindexI18n.setText(box,
    view.lines.join("\n") || (
    response.legacy ? MindexI18n.msg("ui.0cbb72d3287e") : MindexI18n.msg("ui.29c54863449f")));
    if (follow) box.scrollTop = box.scrollHeight;
    more = response.has_more;
  }
}

function historyCell(stage, sid, preferred = null) {
  const box = el("div", "", "version-history");
  const items = Object.values(state.data.versions).
  filter((v) => v.stage === stage && v.shot_id === sid).
  reverse();
  const select = document.createElement("select");
  MindexI18n.setAttr(select, "aria-label", MindexI18n.msg("ui.3339e0dc57f7", { p0: shortId(sid), p1: LABEL[stage] }));
  for (const item of items) {
    const o = el(
      "option",
      `${item.id.slice(0, 8)} · ${STATUS[item.status]} · ${MindexI18n.date(new Date(item.created_at), false)}`
    );
    o.value = item.id;
    select.append(o);
  }
  if (!items.length) return el("p", MindexI18n.msg("ui.482a16ecfa18"), "muted");
  if (items.some((item) => item.id === preferred)) select.value = preferred;
  const choose = mutationButton(MindexI18n.msg("ui.1dd40d446b45"),

  async () => {
    await accept(
      await api(`${planBase()}/select-version`, "POST", {
        revision: state.data.revision,
        version_id: select.value,
        stage,
        shot_id: sid
      })
    );
    notice(MindexI18n.msg("ui.e401d5ed1d5c"));
  },
  ver(select.value).status !== "complete"
  );
  select.addEventListener("change", () => {
    choose.dataset.unavailable = String(
      ver(select.value).status !== "complete"
    );
    updateLocks();
  });
  box.append(
    select,
    button(MindexI18n.msg("ui.753c35ac28e9"), () => showVersion(select.value)),
    choose
  );
  return box;
}
function nodeInputs(stage, sid) {
  const s = plan().shots[sid];
  if (stage === "split") return {};
  const inputs = { content: s.content };
  if (stage === "select") {
    inputs.tts = s.tts || null;
    if (!plan().ranges[sid] || plan().ranges[sid].anchor_ref) inputs.recall = s.recall || null;
  }
  if (stage === "render")
  Object.assign(inputs, { select: s.select || null, tts: s.tts || null });
  return inputs;
}
async function runNode(stage, sid, force) {
  const body = {
    stage,
    shot_id: sid || null,
    force,
    revision: state.data.revision,
    inputs: nodeInputs(stage, sid)
  };
  state.submitting = true;
  render();
  try {
    await api(`${planBase()}/nodes`, "POST", body);
    notice(MindexI18n.msg("ui.f775fd5ade05", { p0: LABEL[stage] }));
  } finally {
    state.submitting = false;
    await refresh();
  }
}
function createPlan(mode, finalId = null) {
  const body = showDialog(mode === "restore" ? MindexI18n.msg("ui.8fc235b8dfed") : MindexI18n.msg("ui.373241734247"));
  const f = field(MindexI18n.msg("ui.5fd61b9aa9cf"),

  mode === "new" ? MindexI18n.msg("ui.e2135c621b66") : MindexI18n.msg("ui.dff07ba564ad", { p0: plan().name })
  );
  body.append(
    f.wrap,
    button(MindexI18n.msg("ui.ede701c8b470"), async () => {
      await accept(
        await api(`${base()}/plans`, "POST", {
          name: f.input.value,
          ...(mode === "copy" ? { copy_from: plan().id } : {}),
          ...(mode === "restore" ? { from_final: finalId } : {})
        })
      );
      closeDialog();
    })
  );
}
function renamePlan() {
  const body = showDialog(MindexI18n.msg("ui.a2397a951f7f")),
    f = field(MindexI18n.msg("ui.5fd61b9aa9cf"), plan().name);
  body.append(
    f.wrap,
    button(MindexI18n.msg("ui.fadf24dbc5a9"), async () => {
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          name: f.input.value
        })
      );
      closeDialog();
    })
  );
}
function voiceChoices(catalog, current) {
  if (!catalog) return null;
  const choices = [];
  const seen = new Set();
  const push = (value, label) => {
    if (!value || seen.has(value)) return;
    seen.add(value);
    choices.push([value, label]);
  };
  const known = new Set([
  ...catalog.groups.flatMap((g) => g.voices.map((v) => v.voice_id)),
  ...catalog.library.map((v) => v.voice_id)]
  );
  if (current && !known.has(current)) push(current, MindexI18n.msg("ui.4819f1bac31a", { p0: current }));
  for (const group of catalog.groups)
  for (const voice of group.voices) push(voice.voice_id, `${MindexI18n.builtin(group.label)} · ${MindexI18n.voice(voice)}`);
  for (const item of catalog.library)
  push(item.voice_id, `${item.kind === "clone" ? MindexI18n.msg("ui.6e60458a4727") : MindexI18n.msg("ui.4512b3b0e457")} · ${item.name}`);
  return choices;
}

async function editSettings() {
  let catalog = null;
  try {
    catalog = await api("/api/settings/voices/catalog");
  } catch {
    catalog = null; // 目录不可用时降级为文本输入
  }
  const body = showDialog(MindexI18n.msg("ui.b876f5881f16")),
    fields = {},
    o = plan().options;
  const defaultVoice = state.data.tts_settings?.default_voice || catalog?.default_voice || "";
  const definitions = [
  ["voice_mode", MindexI18n.msg("ui.301d4d5a24ef"), "text", [
  ["inherit", MindexI18n.msg("ui.28223da9fbc7", { p0: defaultVoice ? `（${defaultVoice}）` : "" })],
  ["custom", MindexI18n.msg("ui.f0899e6a1685")]]],

  ["voice", MindexI18n.msg("ui.4d5b0b9ed4bd"), "text", voiceChoices(catalog, o.voice || defaultVoice)],
  ["recall_limit", MindexI18n.msg("ui.4f891e5b3cf5"), "number"],
  [
  "audio_mode", MindexI18n.msg("ui.b47469b59094"),

  "text",
  [
  ["tts_only", MindexI18n.msg("ui.2d45e91b9c1f")],
  ["ducking", MindexI18n.msg("ui.f3f417d1a846")]]],


  ["duck_gain", MindexI18n.msg("ui.cd1a77eec0a1"), "number"],
  [
  "burn", MindexI18n.msg("ui.5f7b81907a7c"),

  "text",
  [
  ["true", MindexI18n.msg("ui.caac27bd4051")],
  ["false", MindexI18n.msg("ui.2cf51a1af708")]]],


  ["atempo", MindexI18n.msg("ui.8a1c10056984"), "number"],
  [
  "alignment", MindexI18n.msg("ui.91e681dfd6f2"),

  "text",
  [
  ["auto", MindexI18n.msg("ui.3a5dfc431b89")],
  ["freeze", MindexI18n.msg("ui.e1137203359d")],
  ["strict", MindexI18n.msg("ui.d79a48ac1479")]]]];



  for (const [key, label, type, choices] of definitions) {
    const value = key === "voice_mode" ? o.voice_mode || "inherit" :
    key === "voice" ? o.voice || defaultVoice : o[key];
    const f = field(label, value, type, choices);
    fields[key] = f.input;
    body.append(f.wrap);
  }
  const updateVoiceMode = () => {
    fields.voice.disabled = fields.voice_mode.value === "inherit";
  };
  fields.voice_mode.addEventListener("change", updateVoiceMode);
  updateVoiceMode();
  body.append(el("p", MindexI18n.msg("ui.3ae0d387c4c2"), "muted"));
  if (catalog?.clone?.supported && catalog.clone.mode === "upload") {
    const box = el("div", undefined, "voice-clone-box");
    box.append(el("p", MindexI18n.msg("ui.1cf2530bc2aa"), "muted"));
    const name = document.createElement("input");
    MindexI18n.setAttr(name, "placeholder", MindexI18n.msg("ui.ba037516abcf"));
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".wav,.mp3,.m4a,audio/*";
    const row = el("div", undefined, "voice-clone-row");
    row.append(name, file);
    box.append(row);
    let refText = null;
    if (catalog.clone.ref_text) {
      refText = document.createElement("input");
      MindexI18n.setAttr(refText, "placeholder", MindexI18n.msg("ui.326a027039db"));
      box.append(refText);
    }
    const consent = document.createElement("label");
    consent.className = "check";
    const consentBox = document.createElement("input");
    consentBox.type = "checkbox";
    consent.append(consentBox, MindexI18n.msg("ui.343ed6c33dbf"));
    box.append(consent);
    const error = el("p", undefined, "dialog-error");
    box.append(
      button(MindexI18n.msg("ui.81a3c25a3221"), async () => {
        MindexI18n.setText(error, "");
        if (!name.value.trim() || !file.files.length) {
          MindexI18n.setText(error, MindexI18n.msg("ui.41d899ab4e51"));
          return;
        }
        if (!consentBox.checked) {
          MindexI18n.setText(error, MindexI18n.msg("ui.9a3ba17e62b1"));
          return;
        }
        const form = new FormData();
        form.append("name", name.value.trim());
        form.append("consent", "true");
        if (refText) form.append("ref_text", refText.value.trim());
        form.append("file", file.files[0]);
        const response = await fetch("/api/settings/voices/clone", { method: "POST", body: form });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          MindexI18n.setText(error, MindexI18n.system(data));
          return;
        }
        const option = el("option", MindexI18n.msg("ui.ae48f78f11ac", { p0: data.name }));
        option.value = data.voice_id;
        fields.voice.append(option);
        fields.voice.value = data.voice_id;
        fields.voice_mode.value = "custom";
        updateVoiceMode();
        name.value = "";
        file.value = "";
        notice(MindexI18n.msg("ui.e7018a4b2e89", { p0: data.name }));
      }),
      error
    );
    body.append(box);
  }
  body.append(
    button(MindexI18n.msg("ui.3fab71cb32ac"), async () => {
      const options = { ...o };
      for (const key of Object.keys(fields)) options[key] = fields[key].value;
      for (const key of ["recall_limit", "duck_gain"])
      options[key] = Number(options[key]);
      options.atempo =
      fields.atempo.value === "" ? null : Number(fields.atempo.value);
      options.burn = options.burn === "true";
      options.voice = options.voice_mode === "inherit" ? null : fields.voice.value.trim();
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          options
        })
      );
      closeDialog();
      notice(MindexI18n.msg("ui.1e3dd518b146"));
    })
  );
}
function renderCandidatePicker(body, sid) {
  const recall = plan().shots[sid].recall,
    item = ver(recall),
    choice = plan().anchors?.[sid] || { mode: "auto" };
  const picker = el("div", "", "candidate-picker");
  picker.append(el("h4", MindexI18n.msg("ui.29e3f1f8a2c7")));
  const automatic = mutationButton(
    choice.mode === "auto" ? MindexI18n.msg("ui.ea5bc0e50986") : MindexI18n.msg("ui.fe0e2299285c"),
    () => selectAnchor(sid, { mode: "auto" })
  );
  automatic.setAttribute("aria-pressed", String(choice.mode === "auto"));
  picker.append(automatic, el("p", choice.mode === "auto" ? MindexI18n.msg("ui.3907d07b3bee") : MindexI18n.msg("ui.b955e3930f7a"),

  "muted"));
  if (state.data.anchor_conflicts?.[sid])
  picker.append(el("p", shortIds(state.data.anchor_conflicts[sid]), "warn-text"));
  const pool = el("div", "", "detail-candidates");
  for (const [index, c] of (item?.data.results || []).entries()) {
    const selected = choice.mode === "manual" && choice.recall_version === recall && choice.candidate_id === (c.id || c.segment_id);
    const card = el("div", "", `detail-candidate${selected ? " chosen-candidate" : ""}`);
    const preview = el("div", "", "candidate-image");
    if (c.thumbnail_url) {
      const img = el("img");
      img.src = c.thumbnail_url;
      MindexI18n.setAttr(img, "alt", c.source_name || shortId(c.source_id));
      img.loading = "lazy";
      preview.append(img);
    }
    preview.append(el("span", String(c.rank || index + 1), "candidate-rank"));
    if (selected) preview.append(el("span", MindexI18n.msg("ui.anchor_selected"), "candidate-selection"));
    card.dataset.candidateId = c.id || c.segment_id;
    card.append(preview);
    // 每个演员—角色组合只显示一次，优先保留带匹配参考图的最高分证据。
    const identities = new Map();
    const score = (e) => e.data?.original_score ?? e.data?.score ?? 0;
    for (const evidence of c.person_evidence || []) {
      const key = JSON.stringify([evidence.actor_id ?? evidence.actor_name ?? null, evidence.role_id ?? evidence.role_name ?? null]);
      const previous = identities.get(key);
      const hasReference = Boolean(evidence.reference_url), previousHasReference = Boolean(previous?.reference_url);
      if (!previous || hasReference && !previousHasReference ||
      hasReference === previousHasReference && score(evidence) > score(previous)) identities.set(key, evidence);
    }
    for (const evidence of identities.values()) {
      const chip = el(
        "a",
        `${evidence.actor_name || MindexI18n.msg("ui.1b37b5539364")}${evidence.role_name ? ` / ${evidence.role_name}` : ""}`,
        "evidence-chip"
      );
      chip.href = `/persons?evidence=${evidence.id}`;
      chip.target = "_blank";
      chip.rel = "noopener";
      if (evidence.reference_url) {
        const reference = el("img");
        reference.src = evidence.reference_url;
        MindexI18n.setAttr(reference, "alt", MindexI18n.msg("ui.5ad7ccccc4d1", { p0: evidence.role_name || evidence.actor_name || "" }));
        reference.loading = "lazy";
        reference.addEventListener("error", () => reference.remove(), { once: true });
        chip.prepend(reference);
      }
      card.append(chip);
    }
    card.append(el("p", c.source_name || shortId(c.source_id), "candidate-source"),
    el("p", `${secs(c.start)}–${secs(c.end)}`, "candidate-time"),
    button(MindexI18n.msg("ui.de61aa8e1cbc"), () => play(c.media_url, MindexI18n.msg("ui.212e53d576f5"))));
    const choose = mutationButton(selected ? MindexI18n.msg("ui.fca821feca6c") : MindexI18n.msg("ui.218dbf597c7c"), () => selectAnchor(sid, {
      mode: "manual", recall_version: recall, candidate_id: c.id || c.segment_id
    }));
    choose.setAttribute("aria-pressed", String(selected));
    card.append(choose);
    pool.append(card);
  }
  picker.append(pool);
  body.append(picker);
  if (choice.mode === "auto") {
    const scope = scopeKey();
    api(`${planBase()}/shots/${encodeURIComponent(sid)}/selection-anchor`).then((result) => {
      if (!picker.isConnected || scope !== scopeKey() || plan().shots[sid].recall !== recall ||
          (plan().anchors?.[sid]?.mode || "auto") !== "auto") return;
      for (const card of pool.children) {
        if (card.dataset.candidateId === result.candidate_id)
          card.querySelector(".candidate-image").append(el("span", MindexI18n.msg("ui.anchor_automatic"), "candidate-selection"));
      }
    }).catch((error) => {
      if (picker.isConnected) picker.append(el("p", shortIds(error.message), "warn-text"));
    });
  }
}
async function selectAnchor(sid, choice) {
  await accept(await api(`${planBase()}/anchor`, "POST", {
    revision: state.data.revision, shot_id: sid, ...choice
  }));
  notice(choice.mode === "auto" ? MindexI18n.msg("ui.fd2f00253aa2") : MindexI18n.msg("ui.72722ed97d92")

  );
}
async function manualRange(sid, host = null) {
  const scope = scopeKey(),url = planBase(),
    body = host || showDialog(MindexI18n.msg("ui.7c411f978f53", { p0: sid }));
  body.replaceChildren(el("p", MindexI18n.msg("ui.1fb33bf9acd4"), "muted"));
  const ctx = await api(`${url}/shots/${encodeURIComponent(sid)}/selection-context`);
  if (scope !== scopeKey() || !body.isConnected) return;
  body.replaceChildren();
  if (!ctx.anchor) {
    body.append(el("p", shortIds(MindexI18n.system(ctx.reason)), "muted"));
    return;
  }
  const lo = ctx.context_range.start,hi = ctx.context_range.end,
    length = ctx.window_duration || 0,
    key = JSON.stringify([scope, sid, ctx.anchor_ref, ctx.tts_version, ctx.atempo]),
    saved = state.windowDrafts.get(key),
    existing = plan().ranges[sid];
  let draft = saved || {
    start: existing?.anchor_ref?.candidate_id === ctx.anchor_ref.candidate_id &&
    existing?.anchor_ref?.recall_version === ctx.anchor_ref.recall_version &&
    existing?.tts_version === ctx.tts_version && existing?.atempo === ctx.atempo ?
    existing.start : ctx.initial_start,
    zoom: 1, preview: null, contextToken: ctx.context_token
  };
  if (draft.contextToken !== ctx.context_token) draft.preview = null;
  draft.contextToken = ctx.context_token;
  draft.start = Math.min(ctx.legal_start.max, Math.max(lo, draft.start));
  state.windowDrafts.set(key, draft);
  body.append(el("p", MindexI18n.msg("ui.cf3132a895df", { p0: ctx.anchor.rank || "?", p1: secs(ctx.anchor.start), p2: secs(ctx.anchor.end), p3: secs(lo), p4: secs(hi) }), "muted"));
  if (ctx.extension.before || ctx.extension.after)
  body.append(el("p", MindexI18n.msg("ui.7f0b259b81e5", { p0: ctx.extension.before, p1: ctx.extension.after }), "muted"));
  if (!ctx.ready) body.append(el("p", shortIds(MindexI18n.system(ctx.reason)), "warn-text"));
  const video = media("video", ctx.video_url);
  video.classList.add("window-video");
  video.muted = true;
  const readout = el("p", "", "window-readout"),
    viewport = el("div", "", "window-viewport"),
    track = el("div", "", "window-track"),
    strip = el("img", null, "window-thumbnails"),
    windowBox = el("div", "", "fixed-window"),
    result = el("div");
  strip.src = ctx.thumbnails_url;
  MindexI18n.setAttr(strip, "alt", MindexI18n.msg("ui.718ffac629d9"));
  strip.draggable = false;
  track.append(strip);
  for (const row of ctx.shots) {
    const boundary = el("div", "", "window-boundary");
    boundary.style.left = `${(row.start - lo) / (hi - lo) * 100}%`;
    MindexI18n.setAttr(boundary, "title", `${shortId(row.id)} · ${secs(row.start)}–${secs(row.end)}`);
    boundary.append(el("span", secs(row.start)));
    track.append(boundary);
  }
  const anchorMark = el("div", "", "window-anchor");
  anchorMark.style.left = `${(ctx.anchor.start - lo) / (hi - lo) * 100}%`;
  anchorMark.style.width = `${(ctx.anchor.end - ctx.anchor.start) / (hi - lo) * 100}%`;
  MindexI18n.setAttr(anchorMark, "title", MindexI18n.msg("ui.f2e05c849853"));
  track.append(anchorMark, windowBox);
  viewport.append(track);
  windowBox.tabIndex = ctx.ready ? 0 : -1;
  windowBox.setAttribute("role", "slider");
  MindexI18n.setAttr(windowBox, "aria-label", MindexI18n.msg("ui.aba7cdd1af8f"));
  windowBox.setAttribute("aria-valuemin", lo);
  windowBox.setAttribute("aria-valuemax", ctx.legal_start.max);
  windowBox.setAttribute("aria-disabled", String(!ctx.ready));
  windowBox.hidden = !ctx.ready;
  let playbackFrame = null,drag = null,saving = false,previewing = false;
  const saveStatus = el("p", "", "muted");
  saveStatus.setAttribute("role", "status");
  const confirm = mutationButton(MindexI18n.msg("ui.3ee4a0fe14a0"), async () => {
    if (saving || previewing || !ctx.ready) return;
    const sent = request();
    saving = true;
    update(false);
    try {
      MindexI18n.setText(saveStatus, draft.preview ? MindexI18n.msg("ui.55f2030f3f50") : MindexI18n.msg("ui.fa1e98c506ad"));
      const preview = draft.preview || (await api(`${url}/range-preview`, "POST", {
        ...sent, render_preview: false
      }));
      if (!isCurrent(sent)) throw new Error(MindexI18n.msg("ui.ebf7d2633b0e"));
      draft.preview = preview;
      MindexI18n.setText(saveStatus, MindexI18n.msg("ui.55f2030f3f50"));
      const response = await api(`${url}/range-confirm`, "POST", {
        ...sent, preview_token: preview.preview_token
      });
      draft.preview = null;
      if (scope === scopeKey()) await accept(response);
      if (!host) closeDialog();
      notice(MindexI18n.msg("ui.7ff45d499de7"));
    } catch (error) {
      draft.preview = null;
      MindexI18n.setText(saveStatus, shortIds(MindexI18n.msg("ui.1a1cb2ffaaae", { p0: error.message })));
      throw error;
    } finally {
      saving = false;
      update(false);
    }
  }, !ctx.ready);
  function request() {
    return { revision: ctx.revision, shot_id: sid, context_token: ctx.context_token,
      anchor_ref: ctx.anchor_ref, tts_version: ctx.tts_version, start: draft.start };
  }
  function isCurrent(sent) {
    return scope === scopeKey() && body.isConnected && draft.start === sent.start &&
    state.data.revision === ctx.revision;
  }
  function seek() {
    const position = draft.start - lo;
    if (video.readyState) video.currentTime = Math.max(0, Math.min(video.duration || hi - lo, position));
  }
  function update(seekFrame = true) {
    windowBox.style.left = `${(draft.start - lo) / (hi - lo) * 100}%`;
    windowBox.style.width = `${length / (hi - lo) * 100}%`;
    windowBox.setAttribute("aria-valuenow", draft.start);
    MindexI18n.setAttr(windowBox, "aria-valuetext", MindexI18n.msg("ui.47f8790ecd63", { p0: secs(draft.start), p1: secs(draft.start + length), p2: secs(length) }));
    MindexI18n.setText(readout, ctx.ready ? MindexI18n.msg("ui.03ed6969b5f6", { p0:
      secs(draft.start), p1: secs(draft.start + length), p2: secs(length), p3: ctx.atempo.toFixed(4) }) :
    length ? MindexI18n.msg("ui.bbfcf52e0955", { p0: secs(length), p1: secs(hi - lo) }) : MindexI18n.msg("ui.49ead685ffa8"));
    confirm.dataset.unavailable = String(!ctx.ready || saving || previewing);
    confirm.disabled = !ctx.ready || saving || previewing || busy();
    MindexI18n.setText(confirm, saving ? MindexI18n.msg("ui.9ca32f988d16") : MindexI18n.msg("ui.3ee4a0fe14a0"));
    serverPreview.dataset.unavailable = String(!ctx.ready || saving || previewing);
    serverPreview.disabled = !ctx.ready || saving || previewing || busy();
    if (seekFrame) seek();
  }
  function move(value) {
    if (!ctx.ready || busy() || saving) return;
    video.pause();
    draft.start = Math.min(ctx.legal_start.max, Math.max(lo, value));
    draft.preview = null;
    result.replaceChildren();
    MindexI18n.setText(saveStatus, MindexI18n.msg("ui.44c8084f4efc"));
    update();
  }
  video.addEventListener("loadedmetadata", () => seek(), { once: true });
  function enforcePlaybackBounds() {
    if (video.paused) return false;
    const start = draft.start - lo,end = start + length;
    if (!ctx.ready || video.currentTime >= end) {
      video.pause();
      if (ctx.ready) video.currentTime = end;
      return false;
    }
    if (video.currentTime < start) video.currentTime = start;
    return true;
  }
  function monitorPlayback() {
    playbackFrame = null;
    if (body.isConnected && enforcePlaybackBounds())
    playbackFrame = requestAnimationFrame(monitorPlayback);
  }
  video.addEventListener("play", () => {
    const start = draft.start - lo,end = start + length;
    if (video.currentTime < start || video.currentTime >= end) seek();
    if (playbackFrame !== null) cancelAnimationFrame(playbackFrame);
    monitorPlayback();
  });
  video.addEventListener("pause", () => {
    if (playbackFrame !== null) cancelAnimationFrame(playbackFrame);
    playbackFrame = null;
  });
  video.addEventListener("timeupdate", enforcePlaybackBounds);
  video.addEventListener("seeking", enforcePlaybackBounds);
  track.addEventListener("pointerdown", (e) => {
    if (!ctx.ready || busy() || e.button !== 0) return;
    e.preventDefault();
    const rect = track.getBoundingClientRect(),time = lo + (e.clientX - rect.left) / rect.width * (hi - lo);
    drag = { pointer: e.pointerId, offset: e.target === windowBox ? time - draft.start : length / 2 };
    track.setPointerCapture(e.pointerId);
    windowBox.focus({ preventScroll: true });
    move(time - drag.offset);
  });
  track.addEventListener("pointermove", (e) => {
    if (!drag || drag.pointer !== e.pointerId) return;
    const rect = track.getBoundingClientRect();
    move(lo + (e.clientX - rect.left) / rect.width * (hi - lo) - drag.offset);
  });
  const stop = () => {drag = null;};
  track.addEventListener("pointerup", stop);
  track.addEventListener("pointercancel", stop);
  windowBox.addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      move(e.key === "Home" ? lo : e.key === "End" ? ctx.legal_start.max :
      draft.start + (e.key === "ArrowLeft" ? -1 : 1) * ctx.frame_step * (e.shiftKey ? 10 : 1));
    }
  });
  const zoom = field(MindexI18n.msg("ui.33158b575eaf"), draft.zoom, "range");
  zoom.input.min = 1;zoom.input.max = 8;zoom.input.step = 0.25;
  function setZoom() {
    draft.zoom = Number(zoom.input.value);
    track.style.width = `${draft.zoom * 100}%`;
  }
  zoom.input.addEventListener("input", setZoom);
  setZoom();
  const serverPreview = mutationButton(MindexI18n.msg("ui.724ea6be9a18"), async () => {
    if (saving || previewing || !ctx.ready) return;
    const sent = request();
    previewing = true;
    update(false);
    MindexI18n.setText(saveStatus, MindexI18n.msg("ui.b31cb820fd4f"));
    try {
      const preview = await api(`${url}/range-preview`, "POST", sent);
      if (!isCurrent(sent)) return;
      draft.preview = preview;
      result.replaceChildren(media("video", preview.preview_url));
      MindexI18n.setText(saveStatus, MindexI18n.msg("ui.25bc411a8e4c"));
    } catch (error) {
      MindexI18n.setText(saveStatus, shortIds(MindexI18n.msg("ui.3bb02d7c43b1", { p0: error.message })));
      throw error;
    } finally {
      previewing = false;
      update(false);
    }
  }, !ctx.ready);
  serverPreview.dataset.unavailable = String(!ctx.ready);
  body.append(video, readout, zoom.wrap, viewport,
  el("p", MindexI18n.msg("ui.ba185e8b854c"), "muted"),
  confirm, serverPreview,
  el("p", MindexI18n.msg("ui.128f77bcdc14"), "muted"),
  saveStatus, result);
  if (draft.preview) result.append(media("video", draft.preview.preview_url));
  update(false);
  updateLocks();
}

async function preflight(force = false) {
  const id = state.id,
    pid = plan().id;
  const checked = await api(`${planBase()}/preflight?force=${force}`);
  if (id !== state.id || pid !== plan().id) return;
  const body = showDialog(
    force ? MindexI18n.msg("ui.cae32e3f53cc") : MindexI18n.msg("ui.68d9d6d0c6f8")
  );
  const actionLabel = {
    reuse: MindexI18n.msg("ui.68f7d028b705"),
    rebuild: MindexI18n.msg("ui.f05cd1f881c7"),
    blocked: MindexI18n.msg("ui.a00db105bd49"),
    skip: MindexI18n.msg("ui.af2604e64b4c")
  };
  for (const node of checked.nodes)
  body.append(
    el(
      "p",
      shortIds(`${actionLabel[node.action]} · ${LABEL[node.stage]} ${node.shot_id || ""} · ${MindexI18n.system(node.reason)}${node.version ? ` · ${node.version.slice(0, 8)}` : ""}`),
      node.action === "blocked" ? "warn-text" : ""
    )
  );
  body.append(
    el(
      "p", MindexI18n.msg("ui.2011fb6e1206"),

      "muted"
    ),
    button(MindexI18n.msg("ui.10cdc19001e3"),

    async () => {
      await api(`${planBase()}/build`, "POST", {
        token: checked.token,
        force
      });
      closeDialog();
      await refresh();
    },
    !plan().board
    )
  );
}
function renderFinal() {
  const p = plan(),
    selected = ver(p.final);
  MindexI18n.setText($("final-hint"), selected ? MindexI18n.msg("ui.dcfddbc47c7d", { p0:
    selected.id.slice(0, 8), p1: selected.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : "" }) : MindexI18n.msg("ui.6da8b19d8be1"));

  const player = $("final-player"),
    url = selected?.files.mp4?.url;
  if (state.finalUrl !== url) {
    state.finalUrl = url;
    player.replaceChildren();
    if (url) {
      const video = media("video", url);
      video.id = "final-video";
      if (selected.files.vtt?.url && !selected.data.burn) {
        const track = document.createElement("track");
        Object.assign(track, {
          kind: "subtitles",
          srclang: "zh",
          label: "中文",
          default: true,
          src: selected.files.vtt.url
        });
        video.append(track);
      }
      player.append(video);
      versionFiles(player, selected);
    }
  }
  const history = $("final-history");
  history.replaceChildren();
  const d = el("details");
  d.append(el("summary", MindexI18n.msg("ui.cde52b54fb7d")));
  for (const item of Object.values(state.data.versions).
  filter((v) => v.stage === "compose").
  reverse()) {
    const row = el("div", "", "final-row");
    row.append(
      el("span", `${item.id.slice(0, 8)} · ${STATUS[item.status]}`),
      button(MindexI18n.msg("ui.23a597a9dc81"), () => showVersion(item.id))
    );
    if (item.status === "complete") {
      row.append(
        button(MindexI18n.msg("ui.00db442ae6c8"),

        async () => {
          await accept(
            await api(`${planBase()}/select-version`, "POST", {
              revision: state.data.revision,
              version_id: item.id,
              stage: "compose"
            })
          );
        },
        busy()
        )
      );
      if (item.data.plan)
      row.append(
        button(MindexI18n.msg("ui.8a061f3f2726"), () => createPlan("restore", item.id), busy())
      );
      versionFiles(row, item);
    }
    d.append(row);
  }
  if (selected) d.append(dependencyTree(selected.id));
  history.append(d);
}
function renderBuilds() {
  const view = $("build-history");
  view.replaceChildren();
  for (const b of [...state.data.builds].reverse()) {
    const d = el("details");
    d.append(
      el(
        "summary", MindexI18n.msg("ui.9610eb026792", { p0:
          shortId(b.id), p1: state.data.plans[b.plan_id]?.name || shortId(b.plan_id), p2: STATUS[b.status] })
      )
    );
    for (const node of b.nodes)
    d.append(
      el(
        "p",
        shortIds(`${LABEL[node.stage]} ${node.shot_id || ""} · ${STATUS[node.status]}${node.error ? ` · ${MindexI18n.system(node.error)}` : ""}`)
      )
    );
    view.append(d);
  }
}
function currentVersionEntries() {
  const p = plan();
  if (!p) return [];
  const entries = [];
  if (p.board) entries.push([p.board, LABEL.board]);
  for (const sid of order()) {
    const s = p.shots[sid] || {};
    for (const stage of ["content", "recall", "tts", "select", "render"])
    if (s[stage]) entries.push([s[stage], `${LABEL[stage]} · ${shortId(sid)}`]);
  }
  if (p.final) entries.push([p.final, LABEL.compose]);
  return entries.filter(([id]) => ver(id));
}
function selectedCandidate(sid, recallId) {
  // “当前选中”：人工固定且锚定在当前召回版本的参考锚点优先，其次当前选片实际使用的锚点。
  const results = ver(recallId)?.data.results || [];
  if (!results.length) return null;
  const pinned = plan().anchors?.[sid];
  if (pinned?.mode === "manual" && pinned.recall_version === recallId) {
    const hit = results.find((c) => (c.id || c.segment_id) === pinned.candidate_id);
    if (hit) return hit;
  }
  const anchor = ver(plan().shots[sid]?.select)?.data?.anchor;
  const used = anchor && results.find((c) => c.segment_id === anchor.segment_id);
  return used || null;
}
function stagePreviewToggle(id, stage, chosen) {
  // select 无自身媒体，经 /versions/{id}/preview 合成画面与配音；render 直接用版本文件；
  // recall 预览当前选中的候选分镜原片。
  let host = null;
  const node = button(
    stage === "recall" ? MindexI18n.msg("ui.d869c54677a8") : MindexI18n.msg("ui.f31ed5bdb27d"),
    async () => {
      if (host?.isConnected) {
        host.hidden = !host.hidden;
        return;
      }
      host = el("div", "", "inline-preview");
      if (stage === "select") {
        const r = await api(`${base()}/versions/${id}/preview`);
        host.append(media("video", r.url), link(MindexI18n.msg("ui.e04b78659234"), r.url, true));
      } else if (stage === "recall") {
        host.append(
          el(
            "p", MindexI18n.msg("ui.c2fcd4e57b8b", { p0:
              chosen.rank || "?", p1: chosen.source_name || shortId(chosen.source_id), p2: secs(chosen.start), p3: secs(chosen.end) }),
            "muted"
          ),
          media("video", chosen.media_url)
        );
      } else {
        const files = ver(id).files;
        host.append(media("video", files.preview?.url || files.video.url));
        if (files.audio?.url) host.append(media("audio", files.audio.url));
      }
      node.after(host);
    }
  );
  return node;
}
function currentArtifactRow(id, label, stage, sid) {
  const item = ver(id);
  const row = el("div", "", "artifact-row");
  row.append(el("span", `${label} · ${shortId(id)}${item.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : ""}`));
  versionFiles(row, item);
  const files = item.files || {};
  if (stage === "select" && !item.incomplete) row.append(stagePreviewToggle(id, stage));
  if (stage === "render" && (files.preview?.url || files.video?.url))
  row.append(stagePreviewToggle(id, stage));
  if (stage === "recall" && sid) {
    const chosen = selectedCandidate(sid, id);
    if (chosen?.media_url) row.append(stagePreviewToggle(id, stage, chosen));
  }
  return row;
}
function renderCurrentArtifacts() {
  const view = $("current-artifacts");
  view.replaceChildren();
  const entries = currentVersionEntries();
  MindexI18n.setText($("current-artifacts-hint"), entries.length ? MindexI18n.msg("ui.fd2983a531cb", { p0:
    entries.length }) : MindexI18n.msg("ui.c22d7acfe02e"));

  const p = plan();
  if (p.board && ver(p.board)) {
    const board = el("details", "", "dependency");
    board.open = true;
    board.append(el("summary", `${LABEL.board} · ${shortId(p.board)}`));
    board.append(currentArtifactRow(p.board, MindexI18n.msg("ui.c8ca780ce823")));
    const shots = el("details", "", "dependency");
    shots.open = true;
    shots.append(el("summary", MindexI18n.msg("ui.ae9da1d3ed8c", { p0: order().length })));
    order().forEach((sid, index) => {
      const s = p.shots[sid] || {};
      const stages = ["content", "recall", "tts", "select", "render"].filter((k) => s[k] && ver(s[k]));
      if (!stages.length) return;
      const shot = el("details", "", "dependency-edge");
      shot.append(el("summary", MindexI18n.msg("ui.09c83d435a6c", { p0: index + 1, p1: shortId(sid), p2: stages.length })));
      for (const stage of stages)
      shot.append(currentArtifactRow(s[stage], LABEL[stage], stage, sid));
      shots.append(shot);
    });
    board.append(shots);
    view.append(board);
  }
  if (p.final && ver(p.final)) view.append(currentArtifactRow(p.final, LABEL.compose));
}
const RESIDUAL_LABELS = {
  previews: MindexI18n.msg("ui.a6d8932e1c71"),
  attempts: MindexI18n.msg("ui.4417c6e95e4c"),
  "migration-v2": MindexI18n.msg("ui.20fdefac205f"),
  builds: MindexI18n.msg("ui.80cf66e7dd79")
};
function artifactFileRow(item) {
  const row = el("div", "", "artifact-row");
  row.append(
    link(item.path, item.url, true),
    el("small", `${(item.bytes / 1024).toFixed(1)} KiB`)
  );
  return row;
}
function historyVersionRow(item, debugFiles) {
  const row = el("div", "", "artifact-row");
  const label = el(
    "span",
    `${LABEL[item.stage] || item.stage} · ${shortId(item.id)} · ${STATUS[item.status] || item.status}` +
    `${item.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : ""}`
  );
  if (MindexI18n.system(item.error)) MindexI18n.setAttr(label, "title", shortIds(MindexI18n.system(item.error)));
  row.append(label);
  versionFiles(row, item);
  // 失败版本会把 debug 文件记进 files；嵌套调试目录只列尚未出现的文件，避免重复。
  const covered = new Set(Object.values(item.files || {}).map((f) => f.path));
  const extra = debugFiles.filter((f) => !covered.has(f.path));
  if (extra.length) {
    const d = el("details", "", "dependency-edge");
    d.append(el("summary", MindexI18n.msg("ui.b2b855ddd8d4", { p0: extra.length })));
    for (const f of extra) d.append(artifactFileRow(f));
    row.append(d);
  }
  return row;
}
function renderHistoryArtifacts(items) {
  const view = $("artifact-view");
  view.replaceChildren();
  const current = new Set(currentVersionEntries().map(([vid]) => vid));
  const history = Object.values(state.data.versions).filter((v) => !current.has(v.id));
  const debugOf = new Map();
  const currentDebug = new Map();
  const residual = new Map();
  const residualGroup = (name) => {
    if (!residual.has(name)) residual.set(name, []);
    return residual.get(name);
  };
  for (const item of items) {
    const versionId = item.path.match(/^versions\/([0-9a-f]+)\//)?.[1];
    if (versionId) {
      if (item.path.includes("/debug/")) {
        const bucket = current.has(versionId) ? currentDebug : debugOf;
        if (!bucket.has(versionId)) bucket.set(versionId, []);
        bucket.get(versionId).push(item);
      } else if (!state.data.versions[versionId]) {
        residualGroup(MindexI18n.msg("ui.a4e74e681f51")).push(item);
      }
      continue;
    }
    const top = item.path.includes("/") ? item.path.split("/")[0] : MindexI18n.msg("ui.8f4e93dc3dd4");
    residualGroup(RESIDUAL_LABELS[top] || top).push(item);
  }
  const archive = el("details", "", "dependency");
  archive.open = true;
  archive.append(
    el("summary", MindexI18n.msg("ui.74fe1bd6e010", { p0: history.length, p1: items.length }))
  );
  const rank = { content: 0, recall: 1, tts: 2, select: 3, render: 4 };
  const byShot = new Map();
  for (const v of history)
  if (v.shot_id && v.stage in rank) {
    if (!byShot.has(v.shot_id)) byShot.set(v.shot_id, []);
    byShot.get(v.shot_id).push(v);
  }
  const boardOrder = order();
  const shotIds = [
  ...boardOrder.filter((sid) => byShot.has(sid)),
  ...[...byShot.keys()].filter((sid) => !boardOrder.includes(sid))];

  if (shotIds.length) {
    const list = el("details", "", "dependency-edge");
    list.open = true;
    list.append(el("summary", MindexI18n.msg("ui.ae9da1d3ed8c", { p0: shotIds.length })));
    shotIds.forEach((sid) => {
      const versions = byShot.get(sid);
      const shot = el("details", "", "dependency-edge");
      const index = boardOrder.indexOf(sid);
      let prefix = index >= 0 ? MindexI18n.msg("ui.640c031a4b90", { p0: index + 1 }) : "";
      if (index < 0) {
        // 已不在当前分镜表中的历史镜头，用其文本片段帮助识别。
        const content = versions.find((v) => v.stage === "content");
        const text = (content?.data?.text || "").replace(/\s+/g, "");
        prefix += text ? `「${text.slice(0, 12)}${text.length > 12 ? "…" : ""}」 · ` : "";
      }
      shot.append(
        el("summary", MindexI18n.msg("ui.f12a0cd9968e", { p0: prefix, p1: shortId(sid), p2: versions.length }))
      );
      versions.
      sort(
        (a, b) => rank[a.stage] - rank[b.stage] || b.created_at.localeCompare(a.created_at)
      ).
      forEach((v) => shot.append(historyVersionRow(v, debugOf.get(v.id) || [])));
      list.append(shot);
    });
    archive.append(list);
  }
  for (const [stage, label] of [
  ["split", MindexI18n.msg("ui.e842286c8f23")],
  ["board", MindexI18n.msg("ui.83fb89d6c204")],
  ["compose", MindexI18n.msg("ui.79c352c0be68")]])
  {
    const versions = history.
    filter((v) => v.stage === stage).
    sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (!versions.length) continue;
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", MindexI18n.msg("ui.8d0b5b4633ec", { p0: label, p1: versions.length })));
    for (const v of versions) group.append(historyVersionRow(v, debugOf.get(v.id) || []));
    archive.append(group);
  }
  if (currentDebug.size) {
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", MindexI18n.msg("ui.4ab0dfbd5e4f", { p0: currentDebug.size })));
    for (const [vid, files] of currentDebug) {
      const v = state.data.versions[vid];
      const d = el("details", "", "dependency-edge");
      d.append(
        el(
          "summary",
          `${LABEL[v.stage] || v.stage}${v.shot_id ? ` · ${shortId(v.shot_id)}` : ""} · ${shortId(vid)}`
        )
      );
      for (const f of files) d.append(artifactFileRow(f));
      group.append(d);
    }
    archive.append(group);
  }
  for (const [name, files] of residual) {
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", MindexI18n.msg("ui.49a0f32d789c", { p0: name, p1: files.length })));
    for (const f of files) group.append(artifactFileRow(f));
    archive.append(group);
  }
  view.append(archive);
}
async function loadArtifacts() {
  const id = state.id;
  if (!id) return;
  const data = await api(`${base()}/artifacts`);
  if (id !== state.id) return;
  $("download-archive").href = `${base()}/archive`;
  renderHistoryArtifacts(data.items);
}
function play(url, title) {
  MindexI18n.setText($("play-title"), shortIds(title));
  const playlist = ShotPlayer.playlistURL(url);
  if(playlist) ShotPlayer.attach($("play-video"), playlist);else $("play-video").src = url;
  $("play-dialog").showModal();
}
$("play-close").addEventListener("click", () => {
  $("play-video").pause();
  $("play-dialog").close();
});
$("dialog-close").addEventListener("click", closeDialog);
$("work-dialog").addEventListener("close", () =>
$("work-dialog").
querySelectorAll("video,audio").
forEach((n) => n.pause())
);
$("play-dialog").addEventListener("close", () => $("play-video").pause());
$("reload").addEventListener("click", () => loadJobs().catch(fail));
$("reload-artifacts").addEventListener("click", () =>
loadArtifacts().catch(fail)
);
$("plan-select").addEventListener("change", () =>
api(`${base()}/plans/${$("plan-select").value}/activate`, "POST").
then(accept).
catch(fail)
);
$("new-plan").addEventListener("click", () => createPlan("new"));
$("copy-plan").addEventListener("click", () => createPlan("copy"));
$("rename-plan").addEventListener("click", renamePlan);
$("plan-settings").addEventListener("click", editSettings);
$("run-all").addEventListener("click", () => preflight().catch(fail));
$("collapse-all-rows").addEventListener("click", collapseAllRows);
document.
querySelectorAll(".regenerate-column, .execute-column").
forEach((n) =>
n.addEventListener("click", () =>
regenerateColumn(n.dataset.stage, !n.classList.contains("execute-column")).catch(fail)
)
);
const matrixResize = new ResizeObserver((entries) => {
  for (const entry of entries)
  entry.target.style.setProperty(
    "--matrix-viewport-width",
    `${entry.contentRect.width}px`
  );
});
matrixResize.observe(document.querySelector(".matrix-scroll"));
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("create-button").disabled = true;
  try {
    const scope = createSourceScope();
    if (scope && !scope.length)
      throw new Error(MindexI18n.msg("ui.0fb24191cd2c"));
    const job = await api("/api/studio/jobs", "POST", {
      text: $("create-text").value,
      options: {
        audio_mode: $("create-audio-mode").value,
        burn: $("create-burn").value === "true",
        recall_limit: Number($("create-recall-limit").value),
        // 全选时 scope 为 null：不限素材，后续新增素材自动可用。
        ...(scope ? { source_ids: scope } : {})
      }
    });
    await selectJob(job.id);
    $("create-text").value = "";
    notice(MindexI18n.msg("ui.34fa75919260"));
  } catch (error) {
    fail(error);
  } finally {
    $("create-button").disabled = false;
  }
});
async function boot() {
  const id = new URLSearchParams(location.search).get("job");
  // 创建表单在选任务之前就要列出素材；和任务列表一样不阻塞工作台首屏。
  loadSources().catch(() => {});
  await Promise.all([
    loadJobs(),
    id ? selectJob(id, { reloadJobs: false }) : Promise.resolve()
  ]);
}
boot().catch(fail);
