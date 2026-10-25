/* 创作页 · 常量、全局状态、通用 DOM/请求/对话框工具。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
// 全部逐镜阶段（后端注册顺序）；draw（生成图片）只属于 AI 生成图片来源，generate（生成画面）只属于 AI 生成视频来源，
// animate（生成动画）只属于 AI 动画来源；music（背景音乐）只属于背景音乐提示点分镜，显示在配音列里而不单独成列。
const STAGES = ["recall", "tts", "music", "draw", "generate", "animate", "select", "render"];
const RECALL_STAGES = ["recall", "tts", "select", "render"];
// 画面阶段：每个分镜至多一个，统一显示在“召回”列（后端 stages.PICTURE_STAGES）；列操作用虚拟列名 picture。
// 上传片段/图片的分镜没有画面阶段，该列显示上传的素材（虚拟阶段 source，只预览，不执行）。
const PICTURE_STAGES = ["recall", "draw", "generate", "animate"];
const PICTURE_COLUMN = "picture";
// 分镜按画面来源实际包含的逐镜阶段（后端 detail().shot_stages）；缺省为素材召回的四个阶段。
function shotStages(sid) {
  return state.data?.shot_stages?.[sid] || RECALL_STAGES;
}
function pictureStage(sid) {
  return shotStages(sid).find((stage) => PICTURE_STAGES.includes(stage)) || null;
}
// 分镜表固定为五列：文案与查询 + 下面四列；与画面来源无关。
function matrixStages() {
  return RECALL_STAGES;
}
// 某分镜在某列实际显示的阶段：召回列换成该分镜的画面阶段（或上传素材）。
function cellStage(sid, column) {
  if (column !== "recall") return column;
  return pictureStage(sid) || "source";
}
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
  const all = allOptionOf(select), wasAll = select.dataset.allSelected === "1";
  if (all?.selected && !wasAll) applyAllOf(select, true);
  else if (all && !all.selected && wasAll && itemOptionsOf(select).every((o) => o.selected)) applyAllOf(select, false);
  else syncAllOf(select);
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
  script: MindexI18n.msg("ui.script_generate"),
  content: MindexI18n.msg("ui.5727d4463f7c"),
  board: MindexI18n.msg("ui.4d7765b7c123"),
  split: MindexI18n.msg("ui.e08119699ac5"),
  recall: MindexI18n.msg("ui.d06847315d0d"),
  picture: MindexI18n.msg("ui.d06847315d0d"), // 召回列（画面列）的列操作
  source: MindexI18n.msg("ui.source_stage_label"), // 召回列里上传片段/图片分镜的素材预览
  tts: MindexI18n.msg("ui.0f10bb37a946"),
  music: MindexI18n.msg("ui.music_stage_label"),
  draw: MindexI18n.msg("ui.draw_stage_label"),
  generate: MindexI18n.msg("ui.generate_stage_label"),
  animate: MindexI18n.msg("ui.animate_stage_label"),
  select: MindexI18n.msg("ui.7a88bdc950ad"),
  render: MindexI18n.msg("ui.d458415b7971"),
  compose: MindexI18n.msg("ui.47badcfc4cad")
};
// 与检索页（index.html）一致的检索粒度选项文案。
const GRANULARITY_LABEL = {
  moment: MindexI18n.msg("ui.5aca155225fc"),
  shot: MindexI18n.msg("ui.5d649f75d54a"),
  scene: MindexI18n.msg("ui.337ba7da75d2")
};
const STATUS = {
  running: MindexI18n.msg("ui.1f425b6bf0a4"),
  partial: MindexI18n.msg("ui.53951802211e"),
  failed: MindexI18n.msg("ui.3e3c8068bb0e"),
  blocked: MindexI18n.msg("ui.a00db105bd49"),
  pending: MindexI18n.msg("ui.25d23b92b225"),
  complete: MindexI18n.msg("ui.33246f6a5e5b"),
  reused: MindexI18n.msg("ui.68f7d028b705"),
  skipped: MindexI18n.msg("ui.31a98593f16d"),
  cancelled: MindexI18n.msg("ui.a5ffdc95eeb0")
};
// 未知状态（服务端新增取值）回退显示原始值，而不是空白或 undefined。
const statusText = (value) => STATUS[value] || String(value ?? "");
const state = {
  id: null,
  data: null,
  jobs: [],
  timer: null,
  token: 0,
  submitting: false,
  cancelling: null, // 已请求取消的执行："build:<id>" 或 "job:<id>"，用于显示“正在取消…”
  titleEditing: false,
  titleDraft: null,
  expanded: new Map(),
  drafts: new Map(),
  windowDrafts: new Map(),
  matrixRows: new Map(),
  matrixScope: null,
  logViews: new Map(),
  finalUrl: null,
  persons: null, // 懒加载的人物身份（演员/角色；作品不参与选择）
  sources: null, // 懒加载的素材列表
  inputs: null, // 查询文件 media_id → 产物条目索引
  materialDigest: null // 当前任务的素材摘要缓存 {jobId, status, data, open, request}
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
  if (globalThis.MindexFeedback) return globalThis.MindexFeedback.show('notice', shortIds(error ? MindexI18n.system(text) : text), error ? 'error' : 'info');

  MindexI18n.setText($("notice"), shortIds(error ? MindexI18n.system(text) : text));
  $("notice").dataset.type = error ? "error" : "info";
}
function fail(error) {
  if ($("work-dialog").open) {
    $("dialog-error")?.remove();
    const message = el("p", shortIds(error.message), "warn-text");
    message.id = "dialog-error";
    message.setAttribute("role", "alert");
    $("dialog-content").prepend(message);
    return;
  }
  notice(error.message, true);
}
async function api(url, method = "GET", body) {
  return MindexApi.json(url, method, body);
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
