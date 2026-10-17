"use strict";
const STAGES = ["recall", "tts", "select", "render"];
const LABEL = {
  content: "文本与查询",
  board: "分镜表",
  split: "拆分镜",
  recall: "召回",
  tts: "配音",
  select: "选片与对齐",
  render: "分镜渲染",
  compose: "成片合成",
};
const STATUS = {
  running: "执行中",
  partial: "部分完成",
  failed: "失败",
  blocked: "阻塞",
  pending: "等待",
  complete: "完成",
  reused: "复用",
  skipped: "跳过",
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
  persons: null, // 懒加载的人物身份（演员/角色/阶段；作品不参与选择）
  sources: null, // 懒加载的素材列表
  inputs: null, // 查询文件 media_id → 产物条目索引
};
const $ = (id) => document.getElementById(id);
function shortIds(value) {
  return String(value ?? "")
    .replace(/\b([a-f0-9]{8})-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b/gi, "$1")
    .replace(/\b(shot-)([a-f0-9]{9,64})\b/gi, (_, prefix, id) => prefix + id.slice(0, 8))
    .replace(/(?<![a-z0-9_.])(?:[0-9]{32}|[0-9]{64})(?![a-z0-9_])/g, id => id.slice(0, 8))
    .replace(/(?<![a-z0-9_.])(?=[a-f0-9]{9,64}(?![a-z0-9_]))(?=[a-f0-9]*[a-f])[a-f0-9]{9,64}(?![a-z0-9_])/gi,
      id => id.slice(0, 8));
}
function shortId(value) {
  const id = String(value ?? "");
  return /^[a-f0-9]{9,64}$/i.test(id) ? id.slice(0, 8) : shortIds(id);
}
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
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
  $("notice").textContent = shortIds(text);
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
    ...(body
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok)
    throw new Error(shortIds(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || r.status),
    ));
  return data;
}
const base = () => `/api/studio/jobs/${state.id}`;
const plan = () => state.data.plans[state.data.active_plan];
const planBase = () => `${base()}/plans/${plan().id}`;
const ver = (id) => state.data.versions[id];
const order = () => (plan().board ? ver(plan().board).data.order : []);
const busy = () =>
  state.submitting || state.data?.builds.some((b) => b.status === "running");
const secs = (n) => `${Number(n || 0).toFixed(3)}s`;
function showDialog(title) {
  $("dialog-title").textContent = shortIds(title);
  const body = $("dialog-content");
  body.replaceChildren();
  if (!$("work-dialog").open) $("work-dialog").showModal();
  return body;
}
function closeDialog() {
  $("work-dialog")
    .querySelectorAll("video,audio")
    .forEach((n) => n.pause());
  $("work-dialog").close();
}
function field(label, value, type = "text", choices = null) {
  const wrap = el("label", label, "form-field");
  const n = document.createElement(
    choices ? "select" : type === "textarea" ? "textarea" : "input",
  );
  n.setAttribute("aria-label", label);
  if (choices) {
    for (const [v, text] of choices) {
      const o = el("option", text);
      o.value = v;
      n.append(o);
    }
  } else if (type !== "textarea") n.type = type;
  n.value = value ?? "";
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
  n.src = url;
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
      el("div", new Date(job.created_at).toLocaleString("zh-CN"), "job-sub"),
    );
    list.append(b);
  }
}
async function selectJob(id) {
  state.token++;
  clearTimeout(state.timer);
  state.id = id;
  state.data = null;
  state.finalUrl = null;
  state.matrixScope = null;
  state.persons = state.sources = state.inputs = null;
  history.replaceState(null, "", `/studio?job=${id}`);
  closeDialog();
  await refresh();
  await loadJobs();
  await loadArtifacts();
  loadRecallOptions().catch((error) => notice(`召回选项读取失败：${error.message}`, true));
}
async function loadRecallOptions() {
  const id = state.id;
  const [persons, status] = await Promise.all([
    api("/api/persons"),
    api("/api/status"),
  ]);
  if (id !== state.id) return;
  if (persons?.entities) state.persons = persons.entities.filter((e) => e.kind !== "work");
  if (status?.sources) state.sources = status.sources;
  for (const entry of state.matrixRows.values()) entry.panelKey = null;
  renderMatrix();
}
function personName(id) {
  return state.persons?.find((p) => p.id === id)?.name || shortId(id);
}
function sourceName(id) {
  return state.sources?.find((s) => s.id === id)?.name || shortId(id);
}
async function inputUrl(mediaId) {
  const id = state.id;
  if (!state.inputs) {
    const data = await api(`${base()}/artifacts`);
    if (id !== state.id) return null;
    state.inputs = new Map(
      data.items
        .filter((item) => item.path.startsWith("inputs/"))
        .map((item) => [item.path.slice("inputs/".length).split(".")[0], item]),
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
    notice(`读取工作日志失败：${error.message}`, true),
  );
  clearTimeout(state.timer);
  if (busy()) state.timer = setTimeout(() => refresh().catch(fail), 1200);
  else if (wasBusy) {
    await loadArtifacts();
    await loadJobs();
  }
}
async function accept(data) {
  if (data.job.id !== state.id) return;
  state.data = data;
  render();
  await refreshLogs().catch((error) =>
    notice(`读取工作日志失败：${error.message}`, true),
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
  const title = field("任务标题", job.title);
  title.input.maxLength = 60;
  header.append(
    title.wrap,
    button(
      "保存标题",
      async () => {
        await api(`${base()}/title`, "PATCH", {
          title: title.input.value.trim(),
        });
        await refresh();
        await loadJobs();
      },
      locked,
    ),
    el("p", `任务 ${job.id}`, "muted"),
  );
  const select = $("plan-select");
  select.replaceChildren();
  for (const item of Object.values(state.data.plans)) {
    const o = el("option", item.name);
    o.value = item.id;
    o.selected = item.id === p.id;
    select.append(o);
  }
  $("shot-count").textContent = `${order().length} 镜`;
  const original = job.input_text.replace(/\s/g, "");
  const texts = order()
    .map((sid) => ver(p.shots[sid].content).data.text)
    .join("")
    .replace(/\s/g, "");
  $("storyboard-warning").textContent =
    p.board && original !== texts ? "分镜未按顺序完整覆盖原文，请核对。" : "";
  renderMatrix();
  renderFinal();
  renderBuilds();
  renderCurrentArtifacts();
  renderLogShell();
  updateLocks();
}
function dependencyTree(id, seen = new Set()) {
  const item = ver(id);
  if (!item) return el("p", `缺失记录 ${id}`, "warn-text");
  const d = el("details", "", "dependency");
  d.append(
    el(
      "summary",
      `${LABEL[item.stage]} ${shortId(item.shot_id)} · ${id.slice(0, 8)}${item.incomplete ? " · 记录不完整" : ""}`,
    ),
  );
  d.append(button("查看节点详情", () => showVersion(id)));
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
    if (metadata.url) body.append(link(`下载 ${name}`, metadata.url, true));
  }
}
function showVersion(id) {
  const item = ver(id),
    body = showDialog(
      `${LABEL[item.stage]} · ${id.slice(0, 8)} · ${STATUS[item.status]}`,
    );
  if (item.incomplete)
    body.append(
      el(
        "p",
        "历史记录不完整，未知关系未推断。原文件可查看和下载。",
        "warn-text",
      ),
    );
  if (item.error) body.append(el("p", shortIds(item.error), "warn-text"));
  if (item.files.audio?.url) body.append(media("audio", item.files.audio.url));
  if (item.files.preview?.url || item.files.video?.url)
    body.append(
      media("video", item.files.preview?.url || item.files.video.url),
    );
  if (item.files.mp4?.url) body.append(media("video", item.files.mp4.url));
  if (item.stage === "select" && !item.incomplete)
    body.append(
      button("预览此版本（画面与配音）", async () => {
        const r = await api(`${base()}/versions/${id}/preview`);
        body.append(media("video", r.url));
      }),
    );
  versionFiles(body, item);
  body.append(
    el("h3", "实际输入与参数"),
    jsonView({
      inputs: item.inputs,
      parameters: item.parameters,
      data:
        item.stage === "compose"
          ? { ...item.data, graph: undefined, plan: undefined }
          : item.data,
    }),
  );
  body.append(el("h3", "依赖树"), dependencyTree(id));
  if (item.stage === "compose" && item.data.plan)
    body.append(
      button("复制此成片为新方案", () => createPlan("restore", id), busy()),
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
    ([key, draft]) => key.startsWith(prefix) && draft.dirty,
  );
}
function mutationButton(text, action, unavailable = false, allowDirty = false) {
  const n = button(text, () => {
    if (busy() || (!allowDirty && hasDirtyDraft())) return;
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
  $("collapse-all-rows").disabled = !expandedRows().size;
  for (const id of [
    "plan-select",
    "new-plan",
    "copy-plan",
    "rename-plan",
    "plan-settings",
    "run-all",
  ]) {
    $(id).disabled = locked || (dirty && id !== "plan-select");
    $(id).title = dirty && id !== "plan-select" ? "请先保存或取消分镜修改" : "";
  }
  document.querySelectorAll(".regenerate-column").forEach((n) => {
    n.disabled =
      locked || dirty || (n.dataset.stage !== "split" && !order().length);
    n.title = dirty
      ? "请先保存或取消分镜修改"
      : n.dataset.stage === "split"
        ? "按原始文案重新生成整份分镜"
        : "只重新生成本列全部分镜";
  });
  $("matrix-body")
    .querySelectorAll("[data-mutating]")
    .forEach((n) => {
      n.disabled =
        locked ||
        n.dataset.unavailable === "true" ||
        (n.dataset.requiresTts && !plan().shots[n.dataset.requiresTts]?.tts) ||
        (dirty && n.dataset.allowDirty !== "true");
    });
  $("matrix-body")
    .querySelectorAll("[data-shot-input]")
    .forEach((n) => {
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
  expandedRows().clear();
  renderMatrix();
}
function invalidatePanel(sid) {
  const row = state.matrixRows.get(sid);
  if (row) row.panelKey = null;
}
function latestNode(stage, sid) {
  const build = [...state.data.builds].reverse().find((b) =>
    b.plan_id === plan().id &&
    b.nodes.some((n) => n.stage === stage && n.shot_id === sid),
  );
  return build?.nodes.find((n) => n.stage === stage && n.shot_id === sid);
}
function selectionWithin(node) {
  const selection = window.getSelection();
  return selection && !selection.isCollapsed &&
    (node.contains(selection.anchorNode) || node.contains(selection.focusNode));
}
function executionError(stage, sid) {
  const node = latestNode(stage, sid);
  if (node?.status !== "failed") return "";
  const attempted = ver(node.version),
    tts = attempted?.parameters.tts,
    detail = node.error || attempted?.error || "请查看执行日志";
  const context = stage === "tts" && tts
    ? `（${tts.model} · 音色 ${tts.voice}）` : "";
  const advice = stage === "tts" && detail.includes("voice id not exist")
    ? "；请在“方案参数”选择当前配音服务支持的音色并保存后重试。" : "";
  const changed = stage === "tts" && tts && state.data.tts_settings &&
    tts.voice !== state.data.tts_settings.voice;
  return shortIds(`最近执行失败${context}：${detail}${changed
    ? `；当前生效音色已变为 ${state.data.tts_settings.voice}，重新执行将使用新音色。`
    : advice}`);
}
function cellSummary(sid, stage) {
  const selected = plan().shots[sid][stage],
    item = ver(selected);
  const cell = el("td"),
    trigger = el("div", "", "cell-trigger");
  trigger.setAttribute("role", "button");
  trigger.tabIndex = 0;
  trigger.setAttribute("aria-label", `${shortId(sid)} · ${LABEL[stage]}详情`);
  trigger.setAttribute(
    "aria-expanded",
    String(expandedRows().get(sid) === stage),
  );
  trigger.setAttribute("aria-controls", `shot-detail-${sid}`);
  trigger.append(
    el(
      "span",
      selected ? `选用 ${selected.slice(0, 8)}` : "未选择",
      `badge${selected ? " good" : ""}`,
    ),
  );
  if (item) {
    const d = item.data;
    if (stage === "content") {
      trigger.append(
        el("span", d.text, "cell-summary"),
        el("span", d.query, "cell-summary muted"),
      );
      if (state.drafts.get(draftKey(sid))?.dirty)
        trigger.append(el("span", "未保存修改", "badge warn"));
    } else {
      const summary =
        stage === "recall"
          ? `${RECALL_MODE_LABEL[d.mode || "text"]} · ${d.results?.length || 0} 个候选`
          : stage === "tts"
            ? secs(d.duration)
            : stage === "select"
              ? `${d.event.video.source_name || shortId(d.event.video.source_id)} ${secs(d.event.video.start)}–${secs(d.event.video.end)}`
              : `${d.frames || "?"} 帧 · ${d.fps || "?"} fps`;
      trigger.append(el("span", summary, "cell-summary"));
    }
  }
  const messages = state.data.conflicts[`${stage}:${sid}`] || [];
  if (messages.length)
    trigger.append(el("span", shortIds(`选用版本兼容性：${messages.join("；")}`), "cell-summary warn-text"));
  const node = latestNode(stage, sid);
  if (node)
    trigger.append(
      el("span", `最近执行：${STATUS[node.status]}`, "cell-status"),
    );
  const error = executionError(stage, sid);
  if (error) trigger.append(el("span", error, "cell-summary warn-text"));
  trigger.append(
    el(
      "span",
      expandedRows().get(sid) === stage ? "收起详情 ▴" : "展开详情 ▾",
      "cell-expand",
    ),
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
      state.drafts.get(draftKey(sid))?.dirty,
    ]);
    if (entry.summaryKey !== summaryKey && !selectionWithin(entry.summary)) {
      entry.summary.replaceChildren(
        head,
        ...["content", ...STAGES].map((stage) => cellSummary(sid, stage)),
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
      const hint = expanded ? "收起详情 ▴" : "展开详情 ▾";
      if (trigger.lastElementChild.textContent !== hint)
        trigger.lastElementChild.textContent = hint;
    });
    wanted.push(entry.summary);
    const stage = expandedRows().get(sid);
    if (stage) {
      const selected = plan().shots[sid][stage];
      const history = Object.values(state.data.versions)
        .filter((v) => v.stage === stage && v.shot_id === sid)
        .map((v) => v.id);
      const key = JSON.stringify([
        stage,
        selected,
        stage === "content" ? null : plan().options,
        stage === "content" ? ids : null,
        stage === "tts" ? state.data.tts_settings : null,
        stage === "select" ? state.data.revision : null,
        ["recall", "select"].includes(stage) ? [plan().anchors?.[sid], plan().shots[sid].recall, plan().shots[sid].tts, plan().ranges[sid]] : null,
      ]);
      if (entry.panelKey !== key) {
        pauseMedia(entry.panel);
        entry.panel.replaceChildren();
        const heading = el("div", "", "detail-heading");
        heading.append(
          el("h3", `分镜 ${index + 1} · ${LABEL[stage]} · ${shortId(sid)}`),
          button("收起", () => toggleCell(sid, stage)),
        );
        entry.panel.append(heading);
        stage === "content"
          ? renderShotEditor(entry.panel, sid)
          : renderStageDetails(entry.panel, sid, stage);
        entry.panelKey = key;
        entry.historyKey = null;
      }
      entry.panel.querySelector("h3").textContent =
        `分镜 ${index + 1} · ${LABEL[stage]} · ${shortId(sid)}`;
      const result = entry.panel.querySelector("[data-node-result]");
      if (result) {
        const message = executionError(stage, sid);
        if (result.textContent !== message) result.textContent = message;
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
        "p",
        "使用“文本与查询”列的“全部重新生成”生成分镜，或手动添加第一镜。",
        "muted",
      ),
      mutationButton("添加分镜", () => changeStructure(null, "insert")),
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
    entry.summaryPending && !selectionWithin(entry.summary),
  )) renderMatrix();
});
const RECALL_MODES = [
  ["text", "文字 + 人物"],
  ["image", "图片找视频"],
  ["video", "视频找相似"],
];
const RECALL_MODE_LABEL = { text: "文字", image: "图片找视频", video: "视频找相似" };
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
      min_duration: v.filters?.min_duration ?? 0,
      max_duration: v.filters?.max_duration ?? null,
      scene: v.filters?.scene ?? null,
      emotion: v.filters?.emotion ?? null,
      shot_size: v.filters?.shot_size ?? null,
    },
    media_id: v.media_id ?? null,
    media_kind: v.media_kind ?? null,
  };
}
function recallIsDefault(r) {
  return (
    r.mode === "text" &&
    !r.person_ids.length &&
    r.person_mode === "auto" &&
    r.person_relation === "all" &&
    !r.filters.source_id &&
    !r.filters.min_duration &&
    !r.filters.max_duration &&
    !r.filters.scene &&
    !r.filters.emotion &&
    !r.filters.shot_size &&
    !r.media_id &&
    !r.media_kind
  );
}
function queryPreviewNode(url, kind) {
  if (kind === "image") {
    const n = el("img");
    n.src = url;
    n.loading = "lazy";
    n.alt = "查询图片预览";
    return n;
  }
  return media("video", url);
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
    section.replaceChildren(el("h4", "召回设置"));
    const value = draft.data.recall ? recallEditable(draft.data.recall) : null;
    const mode = value?.mode || "text";
    const modeField = field("召回方式", mode, "text", RECALL_MODES);
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
      const personWrap = el("label", "人物（可多选）", "form-field");
      const personSelect = document.createElement("select");
      personSelect.multiple = true;
      personSelect.setAttribute("aria-label", "人物（可多选）");
      const chosen = new Set(value?.person_ids || []);
      for (const p of state.persons || []) {
        const o = el(
          "option",
          `${p.name} · ${p.kind === "actor" ? "演员" : p.kind === "role" ? "角色" : "阶段"} · ${p.id.slice(0, 8)}`,
        );
        o.value = p.id;
        o.selected = chosen.has(p.id);
        personSelect.append(o);
      }
      personSelect.addEventListener("change", () => {
        const next = recallEditable(draft.data.recall);
        next.person_ids = [...personSelect.selectedOptions].map((o) => o.value);
        commit(next);
      });
      personWrap.append(personSelect);
      section.append(personWrap);
      for (const [key, label, choices] of [
        ["person_mode", "人物检索方式", [
          ["auto", "自动识别姓名 · 默认出镜"],
          ["onscreen", "人物出镜"],
          ["mentioned", "对白提及"],
        ]],
        ["person_relation", "多人物关系", [
          ["all", "同时出镜 / 全部提及"],
          ["any", "任一人物"],
        ]],
      ]) {
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
        mode === "image"
          ? "查询图片（JPEG、PNG、WebP 静态图片 · 最大 20MB）"
          : "查询视频（最长 30 秒 · 最大 100MB）",
        "form-field",
      );
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = mode === "image" ? "image/jpeg,image/png,image/webp" : "video/*";
      uploadWrap.append(fileInput);
      section.append(uploadWrap, button("上传查询文件", async () => {
        const scope = scopeKey();
        const file = fileInput.files[0];
        if (!file) throw new Error("请先选择查询文件。");
        const form = new FormData();
        form.append("file", file);
        form.append("kind", mode);
        const r = await fetch(`${planBase()}/query-media`, { method: "POST", body: form });
        const data = await r.json().catch(() => ({}));
        if (!r.ok)
          throw new Error(shortIds(
            typeof data.detail === "string"
              ? data.detail
              : JSON.stringify(data.detail || r.status),
          ));
        if (scope !== scopeKey() || state.drafts.get(draftKey(sid)) !== draft || draft.data.recall?.mode !== mode) return;
        const next = recallEditable(draft.data.recall);
        next.media_id = data.media_id;
        next.media_kind = data.kind;
        state.inputs = null; // 新文件入列，待重取索引
        draft.mediaPreview = { media_id: data.media_id, url: data.preview_url, kind: data.kind };
        commit(next);
        draw();
        notice("查询文件已上传；保存分镜后生效。");
      }));
      if (value?.media_id) {
        const host = el("div", "", "query-preview-host");
        section.append(host);
        if (draft.mediaPreview?.media_id === value.media_id) {
          host.append(queryPreviewNode(draft.mediaPreview.url, draft.mediaPreview.kind));
        } else {
          host.append(el("p", "正在定位查询文件…", "muted"));
          inputUrl(value.media_id).then((url) => {
            if (!url || !host.isConnected) return;
            host.replaceChildren(queryPreviewNode(url, value.media_kind || mode));
          });
        }
      }
    }
    const sourceChoices = [
      ["", "全部素材"],
      ...(state.sources || []).map((s) => [s.id, s.name]),
    ];
    const sourceField = field("素材范围", value?.filters?.source_id || "", "text", sourceChoices);
    sourceField.input.addEventListener("change", () => {
      const next = recallEditable(draft.data.recall);
      next.filters.source_id = sourceField.input.value || null;
      commit(next);
    });
    section.append(sourceField.wrap);
    section.append(
      el(
        "p",
        mode === "text"
          ? "人物与素材筛选仅约束此镜召回。"
          : "图片/视频查询按画面相似度召回，不支持人物筛选；上传后保存分镜生效。",
        "muted",
      ),
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
    (!draft.dirty && draft.baseContent !== plan().shots[sid].content)
  ) {
    draft = {
      baseContent: plan().shots[sid].content,
      original: { ...original },
      data: { ...original },
      dirty: false,
    };
    state.drafts.set(draftKey(sid), draft);
  }
  const fields = el("div", "", "shot-editor-fields");
  for (const [name, label, type] of [
    ["text", "旁白与字幕", "textarea"],
    ["query", "查询", "textarea"],
    ["estimated_seconds", "预估秒数", "number"],
  ]) {
    const f = field(label, draft.data[name], type);
    f.input.dataset.shotInput = "true";
    if (type === "textarea") f.input.maxLength = name === "text" ? 200 : 400;
    else {
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
    mutationButton("保存此镜", () => saveShot(sid), false, true),
    mutationButton(
      "取消修改",
      () => {
        state.drafts.delete(draftKey(sid));
        invalidatePanel(sid);
        renderMatrix();
      },
      false,
      true,
    ),
  );
  const index = order().indexOf(sid);
  for (const [op, label, disabled] of [
    ["up", "上移", index === 0],
    ["down", "下移", index === order().length - 1],
    ["merge", "合并到上一镜", index === 0],
    ["insert", "其后插入", false],
    ["delete", "删除此镜", order().length === 1],
  ])
    actions.append(
      mutationButton(label, () => changeStructure(sid, op), disabled),
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
    throw new Error("此分镜已变化，请取消修改后重新编辑。");
  const shots = currentBoard();
  shots[order().indexOf(sid)] = {
    ...draft.data,
    text: draft.data.text.trim(),
    query: draft.data.query.trim(),
  };
  const result = await api(planBase(), "PATCH", {
    revision: state.data.revision,
    shots,
  });
  state.drafts.delete(key);
  if (scope !== scopeKey()) return;
  invalidatePanel(sid);
  await accept(result);
  notice("当前分镜已保存，其他分镜保持原内容。");
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
      estimated_seconds: 3,
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
    shots,
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
    mutationButton(stage === "select" ? "按当前输入选片 / 复用" : "执行 / 复用", () => runNode(stage, sid, false)),
    mutationButton(stage === "select" ? "重新生成选片版本" : "强制重新生成", () => runNode(stage, sid, true)),
  );
  if (item) actions.append(button("查看完整版本", () => showVersion(vid)));
  const result = el("p", "", "warn-text");
  result.dataset.nodeResult = "true";
  result.setAttribute("role", "status");
  body.append(actions, result);
  if (stage === "recall") body.append(el("p",
    "“执行 / 复用”优先选用输入与设置匹配、文件完好的成功召回版本，没有时才重新检索；“强制重新生成”每次重新检索并新增召回版本。两者只处理召回，不自动配音、选片或渲染。", "muted"));
  if (stage === "select") body.append(el("p",
    "拖动窗口后，点击下方“保存当前窗口”保存位置。“按当前输入选片 / 复用”会复用匹配的成功选片，没有时按当前锚点或已保存范围选片；“重新生成选片版本”跳过复用并新增版本。这两个按钮不保存未提交的窗口草稿，也不渲染视频或合成成片。", "muted"));
  if (stage === "tts") {
    const current = state.data.tts_settings;
    body.append(el("p", current
      ? `${current.voice_mode === "inherit" ? "跟随当前默认音色" : "方案指定音色"}：${current.voice} · 模型 ${current.model}`
      : `当前方案音色：${plan().options.voice || "跟随当前默认音色"}`, "muted"));
  }
  if (!item) body.append(el("p", "此镜尚未选择此阶段的成功版本。", "muted"));
  else {
    if (item.incomplete)
      body.append(
        el("p", "此历史版本记录不完整，已知文件仍可查看。", "warn-text"),
      );
    if (stage === "recall") {
      const recall = item.data.recall,
        meta = item.data.meta || {};
      body.append(
        el(
          "p",
          `方式：${RECALL_MODE_LABEL[item.data.mode || "text"]} · 查询：${item.data.query || "未记录"} · ${item.data.results?.length || 0} 个候选`,
        ),
      );
      if (recall?.person_ids?.length)
        body.append(
          el(
            "p",
            `人物：${recall.person_ids.map(personName).join("、")}（${recall.person_mode === "onscreen" ? "出镜" : recall.person_mode === "mentioned" ? "提及" : "自动"} / ${recall.person_relation === "any" ? "任一" : "同时"}）`,
          ),
        );
      if (recall?.filters?.source_id)
        body.append(el("p", `素材限定：${sourceName(recall.filters.source_id)}`));
      if (meta.person_coverage_missing?.length)
        body.append(
          el(
            "p",
            `人物覆盖缺失：${meta.person_coverage_missing.map(sourceName).join("、")}，出镜筛选可能遗漏。`,
            "warn-text",
          ),
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
          "p",
          `已选版本音色：${item.parameters.voice || "历史未记录"} · 时长 ${secs(item.data.duration)}`,
        ),
      );
      if (item.files.audio) body.append(media("audio", item.files.audio.url));
    } else if (stage === "select") {
      const d = item.data,
        range = d.range,
        actual = d.actual_range || d.event.video;
      body.append(
        el(
          "p",
          `素材：${d.event.video.source_name || shortId(d.event.video.source_id)}`,
        ),
      );
      if (d.anchor)
        body.append(
          el(
            "p",
            `产物参考锚点：${secs(d.anchor.start)}–${secs(d.anchor.end)} · 候选第 ${d.anchor.rank || 0} 名`,
          ),
        );
      if (range)
        body.append(
          el(
            "p",
            `选定范围：${secs(range.start)}–${secs(range.end)}；实际使用：${secs(actual.start)}–${secs(actual.end)}`,
          ),
        );
      body.append(
        el(
          "p",
          `涉及 ${d.event.video.segment_ids.length} 个镜头 · 配音速度 ${(d.atempo || d.event.audio.atempo).toFixed(3)}× · 裁剪 ${secs(d.trim_seconds)} · 冻结 ${secs(d.freeze_seconds)}`,
        ),
      );
      if (d.extension)
        body.append(
          el(
            "p",
            `向前扩展 ${secs(d.extension.before)}，向后扩展 ${secs(d.extension.after)}`,
          ),
        );
      if (d.event.warning) body.append(el("p", shortIds(d.event.warning), "warn-text"));
    } else if (stage === "render") {
      body.append(
        el("p", `${item.data.frames || "?"} 帧 · ${item.data.fps || "?"} fps`),
      );
      if (item.files.preview?.url || item.files.video?.url)
        body.append(
          media("video", item.files.preview?.url || item.files.video.url),
        );
      if (item.files.audio?.url)
        body.append(media("audio", item.files.audio.url));
    }
  }
  if (stage === "select") {
    const host = el("div", "", "manual-range-host");
    renderCandidatePicker(body, sid);
    body.append(host);
    if (!busy()) manualRange(sid, host).catch((error) => {
      if (host.isConnected) host.replaceChildren(el("p", shortIds(error.message), "warn-text"));
    });
    else body.append(button("打开定长窗口", () => manualRange(sid, host)));

  }
  const history = el("div");
  history.dataset.rowHistory = stage;
  body.append(el("h4", "此镜历史版本"), history);
}
async function regenerateColumn(stage) {
  if (busy() || hasDirtyDraft()) return;
  const inputs_by_shot = {};
  if (stage !== "split")
    for (const sid of order()) inputs_by_shot[sid] = nodeInputs(stage, sid);
  const url = planBase(),
    body = { revision: state.data.revision, stage, inputs_by_shot };
  state.submitting = true;
  render();
  try {
    await api(`${url}/regenerate-column`, "POST", body);
    notice(`${LABEL[stage]}列已提交全部重新生成。`);
  } finally {
    state.submitting = false;
    await refresh();
  }
}
function renderLogShell() {
  const key = scopeKey(),
    build = [...state.data.builds]
      .reverse()
      .find((b) => b.plan_id === plan().id);
  let view = state.logViews.get(key);
  if (!view || view.buildId !== build?.id) {
    view = { buildId: build?.id, seq: 0, lines: [], legacy: false };
    state.logViews.set(key, view);
  }
  const box = $("execution-log");
  if (box.dataset.scope !== `${key}/${view.buildId || ""}`) {
    box.dataset.scope = `${key}/${view.buildId || ""}`;
    box.textContent =
      view.lines.join("\n") ||
      (build ? "正在读取执行日志…" : "当前方案暂无执行日志。");
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
      `/api/studio/jobs/${id}/builds/${view.buildId}/logs?after=${view.seq}`,
    );
    if (
      id !== state.id ||
      key !== scopeKey() ||
      state.logViews.get(key) !== view
    )
      return;
    const box = $("execution-log"),
      follow = box.scrollHeight - box.clientHeight - box.scrollTop < 32;
    for (const item of response.items)
      view.lines.push(
        shortIds(`${new Date(item.at).toLocaleTimeString("zh-CN")}  ${item.shot_id ? `分镜 ${item.shot_id} · ` : ""}${item.stage ? `${LABEL[item.stage]} · ` : ""}${item.message}`),
      );
    view.seq = response.next_seq;
    view.legacy = response.legacy;
    if (response.items.length || !view.lines.length)
      box.textContent =
        view.lines.join("\n") ||
        (response.legacy ? "此历史执行没有详细工作日志。" : "等待执行日志…");
    if (follow) box.scrollTop = box.scrollHeight;
    more = response.has_more;
  }
}

function historyCell(stage, sid, preferred = null) {
  const box = el("div", "", "version-history");
  const items = Object.values(state.data.versions)
    .filter((v) => v.stage === stage && v.shot_id === sid)
    .reverse();
  const select = document.createElement("select");
  select.setAttribute("aria-label", `${shortId(sid)} ${LABEL[stage]}历史版本`);
  for (const item of items) {
    const o = el(
      "option",
      `${item.id.slice(0, 8)} · ${STATUS[item.status]} · ${new Date(item.created_at).toLocaleString("zh-CN")}`,
    );
    o.value = item.id;
    select.append(o);
  }
  if (!items.length) return el("p", "暂无历史", "muted");
  if (items.some((item) => item.id === preferred)) select.value = preferred;
  const choose = mutationButton(
    "选为构建输入",
    async () => {
      await accept(
        await api(`${planBase()}/select-version`, "POST", {
          revision: state.data.revision,
          version_id: select.value,
          stage,
          shot_id: sid,
        }),
      );
      notice("选择已保存。兼容性提示已更新；尚未执行构建。");
    },
    ver(select.value).status !== "complete",
  );
  select.addEventListener("change", () => {
    choose.dataset.unavailable = String(
      ver(select.value).status !== "complete",
    );
    updateLocks();
  });
  box.append(
    select,
    button("查看历史版本", () => showVersion(select.value)),
    choose,
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
    inputs: nodeInputs(stage, sid),
  };
  state.submitting = true;
  render();
  try {
    await api(`${planBase()}/nodes`, "POST", body);
    notice(`${LABEL[stage]}已提交。`);
  } finally {
    state.submitting = false;
    await refresh();
  }
}
function createPlan(mode, finalId = null) {
  const body = showDialog(mode === "restore" ? "从成片恢复方案" : "构建方案");
  const f = field(
    "方案名称",
    mode === "new" ? "新方案" : `${plan().name} 副本`,
  );
  body.append(
    f.wrap,
    button("保存方案", async () => {
      await accept(
        await api(`${base()}/plans`, "POST", {
          name: f.input.value,
          ...(mode === "copy" ? { copy_from: plan().id } : {}),
          ...(mode === "restore" ? { from_final: finalId } : {}),
        }),
      );
      closeDialog();
    }),
  );
}
function renamePlan() {
  const body = showDialog("命名方案"),
    f = field("方案名称", plan().name);
  body.append(
    f.wrap,
    button("保存", async () => {
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          name: f.input.value,
        }),
      );
      closeDialog();
    }),
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
    ...catalog.groups.flatMap(g => g.voices.map(v => v.voice_id)),
    ...catalog.library.map(v => v.voice_id),
  ]);
  if (current && !known.has(current)) push(current, `${current}（当前，不在目录中）`);
  for (const group of catalog.groups)
    for (const voice of group.voices) push(voice.voice_id, `${group.label} · ${voice.label}`);
  for (const item of catalog.library)
    push(item.voice_id, `${item.kind === "clone" ? "克隆" : "已录入"} · ${item.name}`);
  return choices;
}

async function editSettings() {
  let catalog = null;
  try {
    catalog = await api("/api/settings/voices/catalog");
  } catch {
    catalog = null; // 目录不可用时降级为文本输入
  }
  const body = showDialog("方案参数"),
    fields = {},
    o = plan().options;
  const defaultVoice = state.data.tts_settings?.default_voice || catalog?.default_voice || "";
  const definitions = [
    ["voice_mode", "音色方式", "text", [
      ["inherit", `跟随当前默认音色${defaultVoice ? `（${defaultVoice}）` : ""}`],
      ["custom", "为本方案指定音色"],
    ]],
    ["voice", "指定音色", "text", voiceChoices(catalog, o.voice || defaultVoice)],
    ["recall_limit", "召回数量 1–50", "number"],
    [
      "audio_mode",
      "原声混音",
      "text",
      [
        ["tts_only", "仅配音"],
        ["ducking", "配音与原声"],
      ],
    ],
    ["duck_gain", "原声增益 0–1", "number"],
    [
      "burn",
      "字幕模式",
      "text",
      [
        ["true", "烧录字幕"],
        ["false", "软字幕"],
      ],
    ],
    ["atempo", "配音速度 0.75–1.25（留空自动）", "number"],
    [
      "alignment",
      "补齐方式",
      "text",
      [
        ["auto", "自动裁剪 / 变速 / 冻结"],
        ["freeze", "原速配音，裁剪或冻结"],
        ["strict", "严格时长匹配"],
      ],
    ],
  ];
  for (const [key, label, type, choices] of definitions) {
    const value = key === "voice_mode" ? o.voice_mode || "inherit"
      : key === "voice" ? o.voice || defaultVoice : o[key];
    const f = field(label, value, type, choices);
    fields[key] = f.input;
    body.append(f.wrap);
  }
  const updateVoiceMode = () => {
    fields.voice.disabled = fields.voice_mode.value === "inherit";
  };
  fields.voice_mode.addEventListener("change", updateVoiceMode);
  updateVoiceMode();
  body.append(el("p", "跟随默认时，切换配音服务或默认音色会用于下一次执行；历史配音仍保留实际使用的音色。", "muted"));
  if (catalog?.clone?.supported && catalog.clone.mode === "upload") {
    const box = el("div", undefined, "voice-clone-box");
    box.append(el("p", "克隆新音色：上传 3 秒以上清晰人声样本，成功后自动选用（仍需保存参数）。", "muted"));
    const name = document.createElement("input");
    name.placeholder = "音色名称";
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".wav,.mp3,.m4a,audio/*";
    const row = el("div", undefined, "voice-clone-row");
    row.append(name, file);
    box.append(row);
    let refText = null;
    if (catalog.clone.ref_text) {
      refText = document.createElement("input");
      refText.placeholder = "参考文本（可选，填样本朗读内容可提升克隆效果）";
      box.append(refText);
    }
    const consent = document.createElement("label");
    consent.className = "check";
    const consentBox = document.createElement("input");
    consentBox.type = "checkbox";
    consent.append(consentBox, " 我确认拥有该声音的合法使用授权");
    box.append(consent);
    const error = el("p", undefined, "dialog-error");
    box.append(
      button("克隆音色", async () => {
        error.textContent = "";
        if (!name.value.trim() || !file.files.length) {
          error.textContent = "请填写音色名称并选择样本文件";
          return;
        }
        if (!consentBox.checked) {
          error.textContent = "请确认声音使用授权";
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
          error.textContent = shortIds(typeof data.detail === "string" ? data.detail : "克隆失败");
          return;
        }
        const option = el("option", `克隆 · ${data.name}`);
        option.value = data.voice_id;
        fields.voice.append(option);
        fields.voice.value = data.voice_id;
        fields.voice_mode.value = "custom";
        updateVoiceMode();
        name.value = "";
        file.value = "";
        notice(`音色「${data.name}」克隆成功；保存参数后生效。`);
      }),
      error,
    );
    body.append(box);
  }
  body.append(
    button("保存参数", async () => {
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
          options,
        }),
      );
      closeDialog();
      notice("参数已保存；不会自动执行。");
    }),
  );
}
function renderCandidatePicker(body, sid) {
  const recall = plan().shots[sid].recall,
    item = ver(recall),
    choice = plan().anchors?.[sid] || { mode: "auto" };
  const picker = el("div", "", "candidate-picker");
  picker.append(el("h4", "新选片的参考锚点"));
  const automatic = mutationButton(
    choice.mode === "auto" ? "自动选择 · 当前模式" : "恢复自动选择",
    () => selectAnchor(sid, { mode: "auto" }),
  );
  automatic.setAttribute("aria-pressed", String(choice.mode === "auto"));
  picker.append(automatic, el("p", choice.mode === "auto"
    ? "当前为自动模式，未固定某个候选，不默认选择第一名。打开选片窗口或执行选片时，按语义、实际配音时长和素材延长情况综合评分；窗口中会显示实际参考候选。无成功配音时等待配音完成，已有选片和视频保留原依赖。"
    : "当前固定使用标记的参考候选。恢复自动选择只保存自动模式并清除此前人工范围，不会立即执行选片；已有选片和视频保留原依赖。", "muted"));
  if (state.data.anchor_conflicts?.[sid])
    picker.append(el("p", shortIds(state.data.anchor_conflicts[sid]), "warn-text"));
  const pool = el("div", "", "detail-candidates");
  for (const c of item?.data.results || []) {
    const selected = choice.mode === "manual" && choice.recall_version === recall && choice.candidate_id === (c.id || c.segment_id);
    const card = el("div", "", `detail-candidate${selected ? " chosen-candidate" : ""}`);
    if (c.thumbnail_url) {
      const img = el("img");
      img.src = c.thumbnail_url;
      img.alt = c.source_name || shortId(c.source_id);
      img.loading = "lazy";
      card.append(img);
    }
    // 人物出镜证据（最多 3 条）：点击跳人物库分镜审核。
    for (const evidence of (c.person_evidence || []).slice(0, 3)) {
      const chip = el(
        "a",
        `${evidence.actor_name || "未知演员"}${evidence.role_name ? ` / ${evidence.role_name}` : ""}`,
        "evidence-chip",
      );
      chip.href = `/persons?evidence=${evidence.id}`;
      chip.target = "_blank";
      chip.rel = "noopener";
      if (evidence.image_url) {
        const shot = el("img");
        shot.src = evidence.image_url;
        shot.alt = "人物出镜证据";
        shot.loading = "lazy";
        chip.prepend(shot);
      }
      card.append(chip);
    }
    card.append(el("p", `${c.rank || ""} · ${c.source_name || shortId(c.source_id)} · ${secs(c.start)}–${secs(c.end)}`),
      button("预览", () => play(c.media_url, "召回候选")));
    const choose = mutationButton(selected ? "当前参考锚点" : "设为参考锚点", () => selectAnchor(sid, {
      mode: "manual", recall_version: recall, candidate_id: c.id || c.segment_id,
    }));
    choose.setAttribute("aria-pressed", String(selected));
    card.append(choose);
    pool.append(card);
  }
  picker.append(pool);
  body.append(picker);
}
async function selectAnchor(sid, choice) {
  await accept(await api(`${planBase()}/anchor`, "POST", {
    revision: state.data.revision, shot_id: sid, ...choice,
  }));
  notice(choice.mode === "auto"
    ? "已恢复自动模式，尚未固定候选；打开选片窗口或执行选片时综合评分选择，不默认使用第一名。已有选片和视频保留原版本。"
    : "参考锚点已保存；执行选片或保存当前窗口后才会产生新选片版本。");
}
async function manualRange(sid, host = null) {
  const scope = scopeKey(), url = planBase(),
    body = host || showDialog(`定长窗口 · ${sid}`);
  body.replaceChildren(el("p", "正在加载局部连续视频…", "muted"));
  const ctx = await api(`${url}/shots/${encodeURIComponent(sid)}/selection-context`);
  if (scope !== scopeKey() || !body.isConnected) return;
  body.replaceChildren();
  if (!ctx.anchor) {
    body.append(el("p", shortIds(ctx.reason), "muted"));
    return;
  }
  const lo = ctx.context_range.start, hi = ctx.context_range.end,
    length = ctx.window_duration || 0,
    key = JSON.stringify([scope, sid, ctx.anchor_ref, ctx.tts_version, ctx.atempo]),
    saved = state.windowDrafts.get(key),
    existing = plan().ranges[sid];
  let draft = saved || {
    start: existing?.anchor_ref?.candidate_id === ctx.anchor_ref.candidate_id &&
      existing?.anchor_ref?.recall_version === ctx.anchor_ref.recall_version &&
      existing?.tts_version === ctx.tts_version && existing?.atempo === ctx.atempo
      ? existing.start : ctx.initial_start,
    zoom: 1, preview: null, contextToken: ctx.context_token,
  };
  if (draft.contextToken !== ctx.context_token) draft.preview = null;
  draft.contextToken = ctx.context_token;
  draft.start = Math.min(ctx.legal_start.max, Math.max(lo, draft.start));
  state.windowDrafts.set(key, draft);
  body.append(el("p", `参考候选第 ${ctx.anchor.rank || "?"} 名 · ${secs(ctx.anchor.start)}–${secs(ctx.anchor.end)}；局部上下文 ${secs(lo)}–${secs(hi)}`, "muted"));
  if (ctx.extension.before || ctx.extension.after)
    body.append(el("p", `默认范围不足，已补充前 ${ctx.extension.before} 镜、后 ${ctx.extension.after} 镜。`, "muted"));
  if (!ctx.ready) body.append(el("p", shortIds(ctx.reason), "warn-text"));
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
  strip.alt = "局部连续视频缩略图时间轴";
  strip.draggable = false;
  track.append(strip);
  for (const row of ctx.shots) {
    const boundary = el("div", "", "window-boundary");
    boundary.style.left = `${(row.start - lo) / (hi - lo) * 100}%`;
    boundary.title = `${shortId(row.id)} · ${secs(row.start)}–${secs(row.end)}`;
    boundary.append(el("span", secs(row.start)));
    track.append(boundary);
  }
  const anchorMark = el("div", "", "window-anchor");
  anchorMark.style.left = `${(ctx.anchor.start - lo) / (hi - lo) * 100}%`;
  anchorMark.style.width = `${(ctx.anchor.end - ctx.anchor.start) / (hi - lo) * 100}%`;
  anchorMark.title = "参考锚点区间";
  track.append(anchorMark, windowBox);
  viewport.append(track);
  windowBox.tabIndex = ctx.ready ? 0 : -1;
  windowBox.setAttribute("role", "slider");
  windowBox.setAttribute("aria-label", "定长选片窗口，左右方向键逐帧移动");
  windowBox.setAttribute("aria-valuemin", lo);
  windowBox.setAttribute("aria-valuemax", ctx.legal_start.max);
  windowBox.setAttribute("aria-disabled", String(!ctx.ready));
  windowBox.hidden = !ctx.ready;
  let playbackFrame = null, drag = null, saving = false, previewing = false;
  const saveStatus = el("p", "", "muted");
  saveStatus.setAttribute("role", "status");
  const confirm = mutationButton("保存当前窗口", async () => {
    if (saving || previewing || !ctx.ready) return;
    const sent = request();
    saving = true;
    update(false);
    try {
      saveStatus.textContent = draft.preview ? "正在保存当前窗口…" : "正在校验窗口并准备保存…";
      const preview = draft.preview || await api(`${url}/range-preview`, "POST", {
        ...sent, render_preview: false,
      });
      if (!isCurrent(sent)) throw new Error("窗口或方案已变化，请重新打开窗口后保存。");
      draft.preview = preview;
      saveStatus.textContent = "正在保存当前窗口…";
      const response = await api(`${url}/range-confirm`, "POST", {
        ...sent, preview_token: preview.preview_token,
      });
      draft.preview = null;
      if (scope === scopeKey()) await accept(response);
      if (!host) closeDialog();
      notice("当前窗口已保存为新选片版本；分镜渲染列仍保留原视频，需要时再渲染。");
    } catch (error) {
      draft.preview = null;
      saveStatus.textContent = shortIds(`保存失败：${error.message}`);
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
    windowBox.setAttribute("aria-valuetext", `${secs(draft.start)} 至 ${secs(draft.start + length)}，固定 ${secs(length)}`);
    readout.textContent = ctx.ready
      ? `开始 ${secs(draft.start)} · 结束 ${secs(draft.start + length)} · 固定时长 ${secs(length)} · 配音速度 ${ctx.atempo.toFixed(4)}×`
      : length ? `所需固定时长 ${secs(length)} · 连续可用 ${secs(hi - lo)}` : "等待实际配音时长，窗口尚未启用。";
    confirm.dataset.unavailable = String(!ctx.ready || saving || previewing);
    confirm.disabled = !ctx.ready || saving || previewing || busy();
    confirm.textContent = saving ? "正在保存…" : "保存当前窗口";
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
    saveStatus.textContent = "窗口位置尚未保存，点击“保存当前窗口”提交。";
    update();
  }
  video.addEventListener("loadedmetadata", () => seek(), { once: true });
  function enforcePlaybackBounds() {
    if (video.paused) return false;
    const start = draft.start - lo, end = start + length;
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
    const start = draft.start - lo, end = start + length;
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
    const rect = track.getBoundingClientRect(), time = lo + (e.clientX - rect.left) / rect.width * (hi - lo);
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
  const stop = () => { drag = null; };
  track.addEventListener("pointerup", stop);
  track.addEventListener("pointercancel", stop);
  windowBox.addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      move(e.key === "Home" ? lo : e.key === "End" ? ctx.legal_start.max :
        draft.start + (e.key === "ArrowLeft" ? -1 : 1) * ctx.frame_step * (e.shiftKey ? 10 : 1));
    }
  });
  const zoom = field("时间轴缩放", draft.zoom, "range");
  zoom.input.min = 1; zoom.input.max = 8; zoom.input.step = 0.25;
  function setZoom() {
    draft.zoom = Number(zoom.input.value);
    track.style.width = `${draft.zoom * 100}%`;
  }
  zoom.input.addEventListener("input", setZoom);
  setZoom();
  const serverPreview = mutationButton("试听配音与画面", async () => {
    if (saving || previewing || !ctx.ready) return;
    const sent = request();
    previewing = true;
    update(false);
    saveStatus.textContent = "正在生成当前窗口的配音与画面预览…";
    try {
      const preview = await api(`${url}/range-preview`, "POST", sent);
      if (!isCurrent(sent)) return;
      draft.preview = preview;
      result.replaceChildren(media("video", preview.preview_url));
      saveStatus.textContent = "预览已生成；点击“保存当前窗口”才会保存选片。";
    } catch (error) {
      saveStatus.textContent = shortIds(`预览失败：${error.message}；可重试或点击“保存当前窗口”。`);
      throw error;
    } finally {
      previewing = false;
      update(false);
    }
  }, !ctx.ready);
  serverPreview.dataset.unavailable = String(!ctx.ready);
  body.append(video, readout, zoom.wrap, viewport,
    el("p", "拖动整个窗口或点击时间轴定位；← / → 逐帧，Shift 加速，Home / End 到边界。缩放后可横向滚动。", "muted"),
    confirm, serverPreview,
    el("p", "“保存当前窗口”只校验并保存所选位置，不生成视频；“试听配音与画面”生成低清音画预览，相同窗口可复用缓存，正式视频仍在分镜渲染列生成。", "muted"),
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
    force ? "强制重新生成：确认构建" : "构建预检：确认后执行",
  );
  const actionLabel = {
    reuse: "复用",
    rebuild: "重建",
    blocked: "阻塞",
    skip: "无需执行",
  };
  for (const node of checked.nodes)
    body.append(
      el(
        "p",
        shortIds(`${actionLabel[node.action]} · ${LABEL[node.stage]} ${node.shot_id || ""} · ${node.reason}${node.version ? ` · ${node.version.slice(0, 8)}` : ""}`),
        node.action === "blocked" ? "warn-text" : "",
      ),
    );
  body.append(
    el(
      "p",
      "独立节点会继续执行；必需依赖未齐时保留已有成片。重试会重新计算复用清单。",
      "muted",
    ),
    button(
      "确认构建",
      async () => {
        await api(`${planBase()}/build`, "POST", {
          token: checked.token,
          force,
        });
        closeDialog();
        await refresh();
      },
      !plan().board,
    ),
  );
}
function renderFinal() {
  const p = plan(),
    selected = ver(p.final);
  $("final-hint").textContent = selected
    ? `版本 ${selected.id.slice(0, 8)}${selected.incomplete ? " · 记录不完整" : ""}`
    : "尚未生成";
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
          src: selected.files.vtt.url,
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
  d.append(el("summary", "历史成片与完整依赖"));
  for (const item of Object.values(state.data.versions)
    .filter((v) => v.stage === "compose")
    .reverse()) {
    const row = el("div", "", "final-row");
    row.append(
      el("span", `${item.id.slice(0, 8)} · ${STATUS[item.status]}`),
      button("查看与播放", () => showVersion(item.id)),
    );
    if (item.status === "complete") {
      row.append(
        button(
          "选为方案成片",
          async () => {
            await accept(
              await api(`${planBase()}/select-version`, "POST", {
                revision: state.data.revision,
                version_id: item.id,
                stage: "compose",
              }),
            );
          },
          busy(),
        ),
      );
      if (item.data.plan)
        row.append(
          button("复制为新方案", () => createPlan("restore", item.id), busy()),
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
        "summary",
        `构建 ${shortId(b.id)} · ${state.data.plans[b.plan_id]?.name || shortId(b.plan_id)} · ${STATUS[b.status]}`,
      ),
    );
    for (const node of b.nodes)
      d.append(
        el(
          "p",
          shortIds(`${LABEL[node.stage]} ${node.shot_id || ""} · ${STATUS[node.status]}${node.error ? ` · ${node.error}` : ""}`),
        ),
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
    stage === "recall" ? "当前选中分镜预览" : "画面与声音预览",
    async () => {
      if (host?.isConnected) {
        host.hidden = !host.hidden;
        return;
      }
      host = el("div", "", "inline-preview");
      if (stage === "select") {
        const r = await api(`${base()}/versions/${id}/preview`);
        host.append(media("video", r.url), link("下载预览", r.url, true));
      } else if (stage === "recall") {
        host.append(
          el(
            "p",
            `候选第 ${chosen.rank || "?"} 名 · ${chosen.source_name || shortId(chosen.source_id)} · ${secs(chosen.start)}–${secs(chosen.end)}`,
            "muted",
          ),
          media("video", chosen.media_url),
        );
      } else {
        const files = ver(id).files;
        host.append(media("video", files.preview?.url || files.video.url));
        if (files.audio?.url) host.append(media("audio", files.audio.url));
      }
      node.after(host);
    },
  );
  return node;
}
function currentArtifactRow(id, label, stage, sid) {
  const item = ver(id);
  const row = el("div", "", "artifact-row");
  row.append(el("span", `${label} · ${shortId(id)}${item.incomplete ? " · 记录不完整" : ""}`));
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
  $("current-artifacts-hint").textContent = entries.length
    ? `当前方案引用 ${entries.length} 个版本`
    : "当前方案尚无产物";
  const p = plan();
  if (p.board && ver(p.board)) {
    const board = el("details", "", "dependency");
    board.open = true;
    board.append(el("summary", `${LABEL.board} · ${shortId(p.board)}`));
    board.append(currentArtifactRow(p.board, "分镜文件"));
    const shots = el("details", "", "dependency");
    shots.open = true;
    shots.append(el("summary", `分镜列表 · ${order().length} 镜`));
    order().forEach((sid, index) => {
      const s = p.shots[sid] || {};
      const stages = ["content", "recall", "tts", "select", "render"].filter((k) => s[k] && ver(s[k]));
      if (!stages.length) return;
      const shot = el("details", "", "dependency-edge");
      shot.append(el("summary", `镜头 ${index + 1} · ${shortId(sid)} · ${stages.length} 产物`));
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
  previews: "预览缓存",
  attempts: "旧执行尝试",
  "migration-v2": "迁移备份",
  builds: "构建记录",
};
function artifactFileRow(item) {
  const row = el("div", "", "artifact-row");
  row.append(
    link(item.path, item.url, true),
    el("small", `${(item.bytes / 1024).toFixed(1)} KiB`),
  );
  return row;
}
function historyVersionRow(item, debugFiles) {
  const row = el("div", "", "artifact-row");
  const label = el(
    "span",
    `${LABEL[item.stage] || item.stage} · ${shortId(item.id)} · ${STATUS[item.status] || item.status}` +
      `${item.incomplete ? " · 记录不完整" : ""}`,
  );
  if (item.error) label.title = shortIds(item.error);
  row.append(label);
  versionFiles(row, item);
  // 失败版本会把 debug 文件记进 files；嵌套调试目录只列尚未出现的文件，避免重复。
  const covered = new Set(Object.values(item.files || {}).map((f) => f.path));
  const extra = debugFiles.filter((f) => !covered.has(f.path));
  if (extra.length) {
    const d = el("details", "", "dependency-edge");
    d.append(el("summary", `调试记录 · ${extra.length} 件`));
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
        residualGroup("未登记的版本文件").push(item);
      }
      continue;
    }
    const top = item.path.includes("/") ? item.path.split("/")[0] : "任务文件";
    residualGroup(RESIDUAL_LABELS[top] || top).push(item);
  }
  const archive = el("details", "", "dependency");
  archive.open = true;
  archive.append(
    el("summary", `历史产物与调试记录 · ${history.length} 个版本 · ${items.length} 件文件`),
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
    ...[...byShot.keys()].filter((sid) => !boardOrder.includes(sid)),
  ];
  if (shotIds.length) {
    const list = el("details", "", "dependency-edge");
    list.open = true;
    list.append(el("summary", `分镜列表 · ${shotIds.length} 镜`));
    shotIds.forEach((sid) => {
      const versions = byShot.get(sid);
      const shot = el("details", "", "dependency-edge");
      const index = boardOrder.indexOf(sid);
      let prefix = index >= 0 ? `镜头 ${index + 1} · ` : "";
      if (index < 0) {
        // 已不在当前分镜表中的历史镜头，用其文本片段帮助识别。
        const content = versions.find((v) => v.stage === "content");
        const text = (content?.data?.text || "").replace(/\s+/g, "");
        prefix += text ? `「${text.slice(0, 12)}${text.length > 12 ? "…" : ""}」 · ` : "";
      }
      shot.append(
        el("summary", `${prefix}${shortId(sid)} · ${versions.length} 个历史版本`),
      );
      versions
        .sort(
          (a, b) => rank[a.stage] - rank[b.stage] || b.created_at.localeCompare(a.created_at),
        )
        .forEach((v) => shot.append(historyVersionRow(v, debugOf.get(v.id) || [])));
      list.append(shot);
    });
    archive.append(list);
  }
  for (const [stage, label] of [
    ["split", "拆分镜版本"],
    ["board", "分镜表版本"],
    ["compose", "成片版本"],
  ]) {
    const versions = history
      .filter((v) => v.stage === stage)
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (!versions.length) continue;
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", `${label} · ${versions.length} 个`));
    for (const v of versions) group.append(historyVersionRow(v, debugOf.get(v.id) || []));
    archive.append(group);
  }
  if (currentDebug.size) {
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", `当前版本的调试记录 · ${currentDebug.size} 个版本`));
    for (const [vid, files] of currentDebug) {
      const v = state.data.versions[vid];
      const d = el("details", "", "dependency-edge");
      d.append(
        el(
          "summary",
          `${LABEL[v.stage] || v.stage}${v.shot_id ? ` · ${shortId(v.shot_id)}` : ""} · ${shortId(vid)}`,
        ),
      );
      for (const f of files) d.append(artifactFileRow(f));
      group.append(d);
    }
    archive.append(group);
  }
  for (const [name, files] of residual) {
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", `${name} · ${files.length} 件`));
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
  $("play-title").textContent = shortIds(title);
  $("play-video").src = url;
  $("play-dialog").showModal();
}
$("play-close").addEventListener("click", () => {
  $("play-video").pause();
  $("play-dialog").close();
});
$("dialog-close").addEventListener("click", closeDialog);
$("work-dialog").addEventListener("close", () =>
  $("work-dialog")
    .querySelectorAll("video,audio")
    .forEach((n) => n.pause()),
);
$("play-dialog").addEventListener("close", () => $("play-video").pause());
$("reload").addEventListener("click", () => loadJobs().catch(fail));
$("reload-artifacts").addEventListener("click", () =>
  loadArtifacts().catch(fail),
);
$("plan-select").addEventListener("change", () =>
  api(`${base()}/plans/${$("plan-select").value}/activate`, "POST")
    .then(accept)
    .catch(fail),
);
$("new-plan").addEventListener("click", () => createPlan("new"));
$("copy-plan").addEventListener("click", () => createPlan("copy"));
$("rename-plan").addEventListener("click", renamePlan);
$("plan-settings").addEventListener("click", editSettings);
$("run-all").addEventListener("click", () => preflight().catch(fail));
$("collapse-all-rows").addEventListener("click", collapseAllRows);
document
  .querySelectorAll(".regenerate-column")
  .forEach((n) =>
    n.addEventListener("click", () =>
      regenerateColumn(n.dataset.stage).catch(fail),
    ),
  );
const matrixResize = new ResizeObserver((entries) => {
  for (const entry of entries)
    entry.target.style.setProperty(
      "--matrix-viewport-width",
      `${entry.contentRect.width}px`,
    );
});
matrixResize.observe(document.querySelector(".matrix-scroll"));
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("create-button").disabled = true;
  try {
    const job = await api("/api/studio/jobs", "POST", {
      text: $("create-text").value,
      options: {
        audio_mode: $("create-audio-mode").value,
        burn: $("create-burn").value === "true",
        recall_limit: Number($("create-recall-limit").value),
      },
    });
    await selectJob(job.id);
    $("create-text").value = "";
    notice("任务已创建，请先生成分镜。");
  } catch (error) {
    fail(error);
  } finally {
    $("create-button").disabled = false;
  }
});
async function boot() {
  await loadJobs();
  const id = new URLSearchParams(location.search).get("job");
  if (id && state.jobs.some((j) => j.id === id)) await selectJob(id);
}
boot().catch(fail);
