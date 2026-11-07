/* 创作页 · 文案创作、工作台刷新与总渲染、检索依赖树、版本详情。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
// 文案创作：文案是方案级的版本化阶段。文本框里的修改只有保存或重新生成时才形成新版本，
// 未保存的内容按方案暂存，切换方案再回来仍在。
const scriptDrafts = new Map();
const SCRIPT_ORIGIN = {
  generated: "ui.script_origin_generated",
  manual: "ui.script_origin_manual",
  legacy: "ui.script_origin_legacy",
};
function scriptText() {
  return ver(plan().script)?.data.text ?? null;
}
// 统计单词数：按空白切分非空词元；中文等无空格文本整体计为一词。
function wordCount(text) {
  return (text || "").trim().split(/\s+/).filter(Boolean).length;
}
function scriptDirty() {
  if (!state.data) return false;
  const area = $("script-text");
  if (area.dataset.scope !== scopeKey()) return false;
  return area.value.trim() !== (scriptText() ?? "").trim();
}
// 只更新状态文字与按钮可用性，不重建文本框，避免打断输入和光标。
function syncScriptControls() {
  const p = plan(),
    job = state.data.job,
    area = $("script-text"),
    item = ver(p.script),
    locked = busy();
  area.disabled = locked;
  const generate = $("script-generate");
  MindexI18n.setText(
    generate,
    MindexI18n.msg(item ? "ui.script_regenerate" : "ui.script_generate"),
  );
  // 有大纲或参考文稿即可生成（访谈改写模板只靠参考文稿）；能否执行仍以预检为准。
  const source = Boolean((job.outline || "").trim()) || planReferences().length > 0;
  generate.disabled = locked || !source;
  const template = $("script-template");
  if (template) template.disabled = locked || !scriptTemplates.items;
  MindexI18n.setAttr(generate, "title", source ? "" : MindexI18n.msg("ui.script_no_outline"));
  MindexI18n.setText(
    $("script-counter"),
    MindexI18n.msg("ui.script_counter", { p0: wordCount(area.value) }),
  );
}
// 文案区"参考了 N 个素材、M 个镜头描述"：只在所显示文案版本带 data.materials 时显示（需求 4.12）。
function renderScriptMaterials(item) {
  const note = $("script-materials");
  if (!note) return;
  const materials = item?.data?.materials;
  note.hidden = !materials;
  if (materials) {
    MindexI18n.setText(
      note,
      MindexI18n.msg("ui.script_materials_note", {
        p0: materials.sources ?? 0,
        p1: materials.segments ?? 0,
      }),
    );
  }
}
function renderScript() {
  const p = plan(),
    job = state.data.job,
    area = $("script-text"),
    scope = scopeKey(),
    base = p.script || "";
  // 方案或文案版本变化（含生成、保存、切换方案）时重置文本框；同一版本内保留未保存的输入。
  if (area.dataset.scope !== scope || area.dataset.base !== base) {
    const draft = scriptDrafts.get(scope);
    area.dataset.scope = scope;
    area.dataset.base = base;
    area.value = draft && draft.base === base ? draft.value : (scriptText() ?? "");
  }
  const outline = $("script-outline");
  outline.hidden = !job.outline;
  if (job.outline)
    MindexI18n.setText(outline, MindexI18n.msg("ui.script_outline_line", { p0: job.outline }));
  $("script-stale").hidden = !state.data.script_stale;
  // 最近一次生成失败时在这里直接说明原因；之后有了更新的文案版本（重新生成成功或手动输入）就不再显示。
  const failed = latestNode("script", null),
    current = ver(p.script);
  const error =
    failed?.status === "failed" && !(current && current.created_at > (failed.finished_at || ""))
      ? shortIds(MindexI18n.system(failed.error || ""))
      : "";
  $("script-error").hidden = !error;
  if (error)
    MindexI18n.setText($("script-error"), MindexI18n.msg("ui.script_failed", { p0: error }));
  renderScriptMaterials(current);
  renderScriptHistory();
  renderTemplateSelect();
  renderReferences();
  syncScriptControls();
}
// 文案历史版本：与分镜工作台每个步骤一致，可查看并切换历史版本。切换后分镜工作台会同步到
// 依赖此文案版本的最后一次构建结果（没有构建则清空）。
function renderScriptHistory() {
  const box = $("script-history"),
    items = Object.values(state.data.versions)
      .filter((v) => v.stage === "script" && v.shot_id === null)
      .reverse();
  const key = JSON.stringify([items.map((v) => v.id), plan().script, busy()]);
  if (box.dataset.key === key && !selectionWithin(box)) return;
  box.dataset.key = key;
  const prevScript = box.dataset.script,
    current = plan().script || "";
  // 下拉默认跟随当前文案版本；只有当前版本没变时，才保留用户手动浏览的选择。
  const preferred =
    current && current !== prevScript ? current : box.querySelector("select")?.value;
  box.dataset.script = current;
  box.replaceChildren();
  if (!items.length) {
    box.append(el("p", MindexI18n.msg("ui.script_history_empty"), "muted"));
    return;
  }
  const select = document.createElement("select");
  MindexI18n.setAttr(select, "aria-label", MindexI18n.msg("ui.script_history_aria"));
  for (const item of items) {
    const o = el(
      "option",
      `${item.id.slice(0, 8)} · ${MindexI18n.msg(SCRIPT_ORIGIN[item.data.origin] || SCRIPT_ORIGIN.generated)} · ${MindexI18n.date(new Date(item.created_at), false)}`,
    );
    o.value = item.id;
    select.append(o);
  }
  select.value = items.some((item) => item.id === preferred) ? preferred : current || items[0].id;
  const choose = mutationButton(MindexI18n.msg("ui.1dd40d446b45"), async () => {
    await accept(
      await api(`${planBase()}/select-version`, "POST", {
        revision: state.data.revision,
        version_id: select.value,
        stage: "script",
        shot_id: null,
      }),
    );
    notice(MindexI18n.msg("ui.script_history_switched"));
  });
  const syncChoose = () => {
    choose.dataset.unavailable = String(select.value === plan().script);
    choose.disabled = busy() || hasDirtyDraft() || select.value === plan().script;
  };
  select.addEventListener("change", syncChoose);
  box.append(
    select,
    button(MindexI18n.msg("ui.753c35ac28e9"), () => showVersion(select.value)),
    choose,
  );
  syncChoose();
}
async function generateScript() {
  if (busy()) return;
  const hadScript = Boolean(plan().script),
    dirty = scriptDirty();
  // 重新生成会丢弃文本框里未保存的修改，并清空分镜工作台（新文案尚未处理），先说清楚再执行。
  const warning = dirty
    ? "ui.script_discard_confirm"
    : hadScript && plan().board
      ? "ui.script_regenerate_confirm"
      : null;
  if (warning && !window.confirm(MindexI18n.render(MindexI18n.msg(warning)))) return;
  scriptDrafts.delete(scopeKey());
  await runNode("script", null, true);
}
async function saveScript() {
  const text = $("script-text").value.trim();
  if (busy() || !text || !scriptDirty()) return;
  await accept(await api(planBase(), "PATCH", { revision: state.data.revision, script: text }));
  scriptDrafts.delete(scopeKey());
  notice(MindexI18n.msg("ui.script_saved"));
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
  const data = await api(`${base()}/workspace?compact=true`);
  if (token !== state.token || id !== state.id) return;
  const wasBusy = state.data?.builds.some((b) => b.status === "running");
  state.data = data;
  render();
  void refreshLogs().catch((error) =>
    notice(MindexI18n.msg("ui.5c458a043061", { p0: error.message }), true),
  );
  clearTimeout(state.timer);
  // 旧任务的步骤执行不锁工作台，但仍需轮询，以便取消后及时刷新状态。
  if (busy() || legacyRunning()) state.timer = setTimeout(() => refresh().catch(fail), 1200);
  else if (wasBusy) {
    void loadMaterialDigest(id, { fresh: true });
    await loadArtifacts();
    await loadJobs();
  }
}
async function accept(data) {
  if (data.job.id !== state.id) return;
  state.data = data;
  render();
  void refreshLogs().catch((error) =>
    notice(MindexI18n.msg("ui.5c458a043061", { p0: error.message }), true),
  );
}
function render() {
  const p = plan(),
    job = state.data.job,
    locked = busy();
  $("no-job").hidden = true;
  $("legacy-run").hidden = true;
  $("job-workspace").hidden = false;
  const header = $("job-header");
  header.replaceChildren();
  const title = field(MindexI18n.msg("ui.6eae640bc412"), job.title);
  title.wrap.replaceChildren(title.input);
  title.input.id = "job-title";
  title.input.maxLength = 60;
  title.input.disabled = locked || !state.titleEditing;
  title.input.value = state.titleDraft ?? job.title;
  title.input.addEventListener("input", () => {
    state.titleDraft = title.input.value;
  });
  header.append(
    title.wrap,
    button(
      MindexI18n.msg(state.titleEditing ? "ui.fe0e9e6e4fad" : "ui.edit_job_title"),

      async () => {
        if (!state.titleEditing) {
          state.titleEditing = true;
          state.titleDraft = job.title;
          render();
          $("job-title")?.focus();
          return;
        }
        const id = state.id,
          save = header.querySelector("button");
        save.disabled = title.input.disabled = true;
        try {
          await api(`${base()}/title`, "PATCH", { title: title.input.value.trim() });
          if (state.id === id) {
            state.titleEditing = false;
            state.titleDraft = null;
            await refresh();
          }
          await loadJobs();
        } finally {
          if (state.id === id && state.titleEditing) {
            save.disabled = title.input.disabled = busy();
          }
        }
      },
      locked,
    ),
  );
  const select = $("plan-select");
  renderJobMeta();
  renderJobTopic();
  select.replaceChildren();
  for (const item of Object.values(state.data.plans)) {
    const o = el("option", item.name);
    o.value = item.id;
    o.selected = item.id === p.id;
    select.append(o);
  }
  MindexI18n.setText($("shot-count"), MindexI18n.msg("ui.afd372b9b45d", { p0: order().length }));
  // 分镜应完整覆盖当前方案的文案；文案变化后分镜过期，单独提示而不是误报"未完整覆盖"。
  const original = (scriptText() ?? job.input_text ?? "").replace(/\s/g, "");
  const texts = order()
    .map((sid) => ver(p.shots[sid].content).data.text)
    .join("")
    .replace(/\s/g, "");
  MindexI18n.setText(
    $("storyboard-warning"),
    p.board && state.data.script_stale
      ? MindexI18n.msg("ui.script_stale_board")
      : p.board && original !== texts
        ? MindexI18n.msg("ui.34f1df936c8a")
        : "",
  );
  renderScript();
  renderRetrieval();
  renderMatrix();
  renderFinal();
  renderDelivery();
  renderBuilds();
  renderCurrentArtifacts();
  renderLogShell();
  updateLocks();
}
// 成片合成版本沿依赖链（渲染 → 选片 → 召回）用到的检索组合；与当前生效组合不同时标出。
function retrievalEdges(id) {
  const groups = state.data.retrieval?.compose?.[id];
  if (!groups) return [];
  if (!groups.length) {
    const row = el("div", "", "dependency-edge");
    row.append(
      el("span", MindexI18n.msg("ui.retrieval_edge_name"), "muted"),
      el("span", MindexI18n.msg("ui.retrieval_edge_none"), "retrieval-edge"),
    );
    return [row];
  }
  return groups.map((item) => {
    const row = el("div", "", "dependency-edge");
    const text = el(
      "span",
      MindexI18n.msg("ui.retrieval_combo_shots", { p0: retrievalCombo(item), p1: item.shots }),
      "retrieval-edge",
    );
    MindexI18n.setAttr(
      text,
      "title",
      MindexI18n.msg("ui.retrieval_combo_shots", {
        p0: retrievalCombo(item, true),
        p1: item.shots,
      }),
    );
    row.append(el("span", MindexI18n.msg("ui.retrieval_edge_name"), "muted"), text);
    if (!item.current)
      row.append(el("span", MindexI18n.msg("ui.retrieval_edge_diff"), "retrieval-badge warn-text"));
    return row;
  });
}
function dependencyTree(id, seen = new Set()) {
  const item = ver(id);
  if (!item) return el("p", MindexI18n.msg("ui.4adbd05991ae", { p0: id }), "warn-text");
  const d = el("details", "", "dependency");
  d.append(
    el(
      "summary",
      `${LABEL[item.stage]} ${shortId(item.shot_id)} · ${id.slice(0, 8)}${item.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : ""}`,
    ),
  );
  d.append(button(MindexI18n.msg("ui.c56d640f8fde"), () => showVersion(id)));
  if (seen.has(id)) return d;
  const next = new Set(seen).add(id);
  if (item.stage === "compose") for (const row of retrievalEdges(id)) d.append(row);
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
    if (metadata.url)
      body.append(link(MindexI18n.msg("ui.1a179261cfb7", { p0: name }), metadata.url, true));
  }
}
async function showVersion(id) {
  const jobId = state.id;
  if (ver(id)?.compact) {
    const full = await api(`${base()}/versions/${id}`);
    if (state.id !== jobId || !state.data?.versions[id]) return;
    state.data.versions[id] = full;
  }
  const item = ver(id),
    body = showDialog(`${LABEL[item.stage]} · ${id.slice(0, 8)} · ${statusText(item.status)}`);
  if (item.incomplete)
    body.append(
      el(
        "p",
        MindexI18n.msg("ui.ddb0dcb38944"),

        "warn-text",
      ),
    );
  if (MindexI18n.system(item.error))
    body.append(el("p", shortIds(MindexI18n.system(item.error)), "warn-text"));
  if (item.files.audio?.url) body.append(media("audio", item.files.audio.url));
  if (item.files.preview?.url || item.files.video?.url)
    body.append(media("video", item.files.preview?.url || item.files.video.url));
  if (item.files.mp4?.url) body.append(media("video", item.files.mp4.url));
  if (item.stage === "select" && !item.incomplete)
    body.append(
      button(MindexI18n.msg("ui.176d14ef6dfa"), async () => {
        const r = await api(`${base()}/versions/${id}/preview`);
        body.append(media("video", r.url));
      }),
    );
  versionFiles(body, item);
  body.append(
    el("h3", MindexI18n.msg("ui.27f7d5b1ba5a")),
    jsonView({
      inputs: item.inputs,
      parameters: item.parameters,
      data:
        item.stage === "compose" ? { ...item.data, graph: undefined, plan: undefined } : item.data,
    }),
  );
  body.append(el("h3", MindexI18n.msg("ui.1ac5e6f7e826")), dependencyTree(id));
  if (item.stage === "compose" && item.data.plan)
    body.append(button(MindexI18n.msg("ui.0e649bf77acc"), () => createPlan("restore", id), busy()));
}
