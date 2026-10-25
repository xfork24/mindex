/* 创作页 · 任务列表与切换、召回与音色目录、素材范围、任务元信息与素材摘要。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
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
  // 标题后台生成：有待生成任务时轻量轮询刷新，完成后自动替换占位标题。
  const pending = data.items.some((job) => job.title_pending);
  clearTimeout(state.titlePollTimer);
  if (pending) {
    state.titlePollTimer = setTimeout(() => {
      loadJobs().catch(() => {});
      // 当前打开的任务若正在等待标题，刷新其头部展示。
      if (state.id && state.jobs?.find((j) => j.id === state.id && j.title_pending)) {
        refresh().catch(() => {});
      }
    }, 1500);
  }
}
async function selectJob(id, { reloadJobs = true } = {}) {
  state.token++;
  clearTimeout(state.timer);
  state.id = id;
  state.titleEditing = false;
  state.titleDraft = null;
  state.data = null;
  state.finalUrl = null;
  state.boardScope = null; // 下一次渲染时重置分镜条与详情区
  state.persons = state.inputs = state.materialDigest = null; // 素材摘要在 renderJobMeta 中按任务重新获取
  history.replaceState(null, "", `/studio?job=${id}`);
  closeDialog();
  loadRecallOptions().catch((error) => notice(MindexI18n.msg("ui.867f562167d6", { p0: error.message }), true));
  try {
    await refresh();
  } catch (error) {
    // 旧任务步骤执行期间工作台被锁（409）：改为显示执行状态与“取消执行”。
    if (id === state.id && await legacyRunView(id)) {
      if (reloadJobs) loadJobs().catch(fail);
      return;
    }
    throw error;
  }
  if (id !== state.id) return;
  if (reloadJobs) loadJobs().catch(fail);
  loadArtifacts().catch(fail);
}
let recallCatalogue = null, recallCatalogueTime = 0, recallCatalogueRequest = null;
async function loadCatalogue() {
  if (recallCatalogue && Date.now() - recallCatalogueTime < 15000) return recallCatalogue;
  if (!recallCatalogueRequest) {
    recallCatalogueRequest = api("/api/persons/filters").then((data) => {
      recallCatalogue = data;recallCatalogueTime = Date.now();return data;
    }).finally(() => {recallCatalogueRequest = null;});
  }
  return recallCatalogueRequest;
}
// 音色目录需要实时查询 TTS 服务，开销大；短 TTL 缓存 + 合并在途请求，避免每次开“方案参数”都阻塞一次往返。
let voiceCatalogue = null, voiceCatalogueTime = 0, voiceCatalogueRequest = null;
function loadVoiceCatalogue({ fresh = false } = {}) {
  if (!fresh && voiceCatalogue && Date.now() - voiceCatalogueTime < 30000)
    return Promise.resolve(voiceCatalogue);
  if (!voiceCatalogueRequest) {
    voiceCatalogueRequest = api("/api/settings/voices/catalog").then((data) => {
      voiceCatalogue = data;voiceCatalogueTime = Date.now();return data;
    }).finally(() => {voiceCatalogueRequest = null;});
  }
  return voiceCatalogueRequest;
}
async function loadRecallOptions() {
  const id = state.id;
  const persons = await loadCatalogue();
  if (id !== state.id) return;
  state.persons = persons.entities;
  state.personSources = persons.sources;
  applySources(persons.sources);
  invalidatePanel();
  if (state.data) renderMatrix();
}
// Shared by the creation form and per-shot recall editors.
function applySources(sources) {
  state.sources = sources;
  fillCreateSources();
  if (state.data) renderJobMeta();
}
async function loadSources() {
  const catalogue = await loadCatalogue();
  applySources(catalogue.sources);
  return state.sources;
}
function fillCreateSources() {
  const select = $("create-sources");
  if (!select) return;
  const sources = state.sources || [];
  const chosen = select.options.length ? new Set(pickedIds(select)) : new Set(sources.map((s) => s.id));
  fillAllSelect(select, sources, chosen);
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
function sourceName(id, fallback) {
  return state.sources?.find((s) => s.id === id)?.name || fallback || shortId(id);
}
// 任务元信息只读：只摊开创建后不可变的输入（文案、素材范围）；
// 音轨/字幕/召回数量属于方案参数，可在方案参数里动态调整，不在此重复展示。
function renderJobMeta() {
  const job = state.data.job, body = $("job-meta-body");
  // 新任务显示用户的原始输入（智能助手创建时为用户原话；视频大纲在下方“文案创作”里显示）；
  // 早于原始输入字段的任务没有原话，退回显示大纲。老任务的 input_text 就是创建时填写的文案。
  const legacyText = !job.outline && job.input_text;
  const rows = [
    // 原始输入/文案保留非空行，素材列表独占整行并逐项展示。
    [legacyText ? "ui.7407393d52c7" : "ui.job_original_input_label",
    (job.original_input || job.outline || job.input_text || "").split(/\r?\n/).filter((line) => line.trim()).join("\n"), true],
    ["ui.84e3802f60a7", MindexI18n.date(job.created_at)],
    ["ui.f711792f098b", (job.source_ids?.length ? job.source_ids : (state.sources || []).map((s) => s.id)).map(sourceName).join("\n") || MindexI18n.msg("ui.0a9eab1a4d36"), true]
  ];
  body.replaceChildren();
  for (const [label, value, wide] of rows) {
    const row = el("div", undefined, wide ? "job-meta-row wide" : "job-meta-row"),
      term = el("dt", MindexI18n.msg(label)),
      detail = el("dd", value == null ? "—" : String(value));
    row.append(term, detail);
    body.append(row);
  }
  body.append(materialDigestNode(state.materialDigest?.jobId === job.id ? state.materialDigest : null));
  if (state.materialDigest?.jobId !== job.id) void loadMaterialDigest(job.id);
}
// 可用素材摘要：只读预览，按任务缓存；切换任务或构建结束刷新工作台时重新获取。
// 接口返回的为空原因是固定的中文取值，界面按条目翻译，不直接显示原文。
const DIGEST_EMPTY_REASON = {
  "预算为 0": "ui.material_digest_empty_budget",
  "无生效索引": "ui.material_digest_empty_index",
  "无已分析镜头": "ui.material_digest_empty_segments"
};
function loadMaterialDigest(jobId, { fresh = false } = {}) {
  const cached = state.materialDigest;
  if (!fresh && cached?.jobId === jobId) return cached.request;
  const entry = { jobId, status: "loading", data: null, open: cached?.jobId === jobId && cached.open };
  entry.request = api(`/api/studio/jobs/${jobId}/material-digest`).then(
    (data) => {entry.status = "ok";entry.data = data;},
    () => {entry.status = "error";}
  ).then(() => {
    if (state.materialDigest === entry && state.id === jobId && state.data) renderJobMeta();
  });
  state.materialDigest = entry;
  return entry.request;
}
// entry 为 null 或 loading 时显示加载中；成功时 summary 显示计数（为空时显示原因），展开显示正文。
function materialDigestNode(entry) {
  const box = document.createElement("details");
  box.className = "material-digest";
  box.open = Boolean(entry?.open);
  box.addEventListener("toggle", () => {if (entry) entry.open = box.open;});
  const summary = el("summary", MindexI18n.msg("ui.material_digest_title"));
  const status = el("span", undefined, "material-digest-status");
  summary.append(" · ", status);
  box.append(summary);
  if (!entry || entry.status === "loading") {
    MindexI18n.setText(status, MindexI18n.msg("ui.material_digest_loading"));
  } else if (entry.status === "error") {
    status.classList.add("warn-text");
    MindexI18n.setText(status, MindexI18n.msg("ui.material_digest_failed"));
  } else if (entry.data.empty_reason || !entry.data.text) {
    const key = DIGEST_EMPTY_REASON[entry.data.empty_reason];
    MindexI18n.setText(status, key ? MindexI18n.msg(key) : String(entry.data.empty_reason || "—"));
  } else {
    const stats = entry.data.stats || {};
    MindexI18n.setText(status, MindexI18n.msg("ui.material_digest_counts", { p0: stats.sources ?? 0, p1: stats.segments ?? 0 }));
    box.append(el("pre", entry.data.text, "material-digest-text"));
  }
  return box;
}
