/* 创作页 · 常量、全局状态、通用 DOM/请求/对话框工具。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
// 全部逐镜阶段（后端注册顺序）；draw（生成图片）只属于 AI 生成图片来源，generate（生成画面）只属于 AI 生成视频来源，
// animate（生成动画）只属于 AI 动画来源，invoke（调用第三方工具）只属于第三方工具来源；music（背景音乐）只属于背景音乐提示点分镜，显示在配音列里而不单独成列。
const STAGES = ["recall", "tts", "music", "draw", "generate", "animate", "invoke", "select", "render"];
const RECALL_STAGES = ["recall", "tts", "select", "render"];
// 画面阶段：每个分镜至多一个，统一显示在“召回”列（后端 stages.PICTURE_STAGES）；列操作用虚拟列名 picture。
// 上传片段/图片的分镜没有画面阶段，该列显示上传的素材（虚拟阶段 source，只预览，不执行）。
const PICTURE_STAGES = ["recall", "draw", "generate", "animate", "invoke"];
const PICTURE_COLUMN = "picture";
// 分镜按画面来源实际包含的逐镜阶段（后端 detail().shot_stages）；缺省为素材召回的四个阶段。
function shotStages(sid) {
  return state.data?.shot_stages?.[sid] || RECALL_STAGES;
}
function pictureStage(sid) {
  return shotStages(sid).find((stage) => PICTURE_STAGES.includes(stage)) || null;
}
// 批量操作的阶段（原分镜表的四列）：文案与查询之外的四个阶段，与画面来源无关。
function matrixStages() {
  return RECALL_STAGES;
}
// 某分镜在某列实际显示的阶段：召回列换成该分镜的画面阶段（或上传素材）。
function cellStage(sid, column) {
  if (column !== "recall") return column;
  return pictureStage(sid) || "source";
}
// 分镜卡片可打开的阶段：文案与查询、画面阶段（无画面阶段时为上传素材预览 source），再加该分镜包含的配音/选片/渲染。
// 背景音乐（music）并入配音阶段显示，不单独列出。
function barStages(sid) {
  const present = shotStages(sid);
  return ["content", cellStage(sid, "recall"), ...["tts", "select", "render"].filter((stage) => present.includes(stage))];
}
// 阶段是否已有产物：普通阶段看方案里选用的版本；上传素材预览看文案与查询里是否有上传文件。
function stageHasVersion(sid, stage) {
  const shot = plan().shots[sid] || {};
  if (stage === "source") return !!ver(shot.content)?.data?.source?.media_id;
  return !!shot[stage];
}
// 阶段状态：running > failed > stale > complete > none。
// stale = 已选版本但后端重算签名后有冲突；failed/running 取最近一次包含该节点的构建。
function stageState(sid, stage) {
  if (stage === "source") return stageHasVersion(sid, stage) ? "complete" : "none";
  const builds = state.data?.builds || [];
  const build = [...builds].reverse().find((b) => b.plan_id === plan().id &&
  b.nodes.some((n) => n.stage === stage && n.shot_id === sid));
  const node = build?.nodes.find((n) => n.stage === stage && n.shot_id === sid);
  if (build?.status === "running" && ["pending", "running"].includes(node?.status)) return "running";
  if (node?.status === "failed") return "failed";
  if (!stageHasVersion(sid, stage)) return "none";
  return state.data?.conflicts?.[`${stage}:${sid}`]?.length ? "stale" : "complete";
}
// 卡片显示的阶段：第一个已过期的阶段；没有时为最后一个已有产物的阶段；都没有时为文案与查询。
// 失败或执行中但仍保留旧版本的阶段：只要旧版本过期就算过期。
function cardStage(sid) {
  const stages = barStages(sid);
  const stale = stages.find((stage) => stageHasVersion(sid, stage) &&
  stage !== "source" && state.data?.conflicts?.[`${stage}:${sid}`]?.length);
  if (stale) return stale;
  return [...stages].reverse().find((stage) => stageHasVersion(sid, stage)) || "content";
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
  invoke: MindexI18n.msg("ui.invoke_stage_label"),
  overlay: MindexI18n.msg("ui.overlay_stage_label"),
  select: MindexI18n.msg("ui.7a88bdc950ad"),
  render: MindexI18n.msg("ui.d458415b7971"),
  compose: MindexI18n.msg("ui.47badcfc4cad"),
  export: MindexI18n.msg("ui.export_stage_label"),
  metadata: MindexI18n.msg("ui.metadata_stage_label"),
  cover: MindexI18n.msg("ui.cover_stage_label"),
  package: MindexI18n.msg("ui.package_stage_label")
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
  selection: new Map(), // 任务/方案 → 当前打开的 {sid, stage}
  drafts: new Map(),
  windowDrafts: new Map(),
  shotCards: new Map(), // sid → 分镜卡片缓存 {card, body, mediaBox, key, mediaKey}
  boardScope: null,
  stagePanel: { key: null, historyKey: null }, // 阶段详情区的重建键
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
