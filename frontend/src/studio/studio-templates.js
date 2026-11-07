/* 创作页 · 文案模板：文案区的模板选择（方案参数 template_id）与参考文稿。模板的浏览、编辑与试写在独立的
   模板管理页（/studio/templates，templates.js）。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
// 模板目录在打开页面、页面重新可见（可能刚在管理页改过模板）时重新读取。
const scriptTemplates = { items: null, loading: null, failed: false };
function planTemplateId() {
  return plan().options.template_id || "base";
}
async function loadScriptTemplates(force = false) {
  if (scriptTemplates.items && !force) return scriptTemplates.items;
  if (!scriptTemplates.loading || force) {
    scriptTemplates.loading = api("/api/studio/templates")
      .then((data) => {
        scriptTemplates.items = data.items;
        scriptTemplates.failed = false;
        return data.items;
      })
      .finally(() => {
        scriptTemplates.loading = null;
      });
  }
  return scriptTemplates.loading;
}
// 文案区的模板下拉：只在目录或所选模板变化时重建选项，避免打断键盘操作。
function renderTemplateSelect() {
  const select = $("script-template");
  if (!select || !state.data) return;
  const manage = $("script-template-manage");
  if (manage) manage.href = templatesPageUrl(planTemplateId(), state.id);
  const tab = $("studio-templates-tab");
  if (tab) tab.href = templatesPageUrl(null, state.id);
  const topicsTab = $("studio-topics-tab");
  if (topicsTab)
    topicsTab.href = state.id
      ? `/studio/topics?job=${encodeURIComponent(state.id)}`
      : "/studio/topics";
  const changed = $("script-template-changed");
  if (changed) changed.hidden = !state.data.script_template_changed;
  const scopeChanged = $("script-material-scope-changed");
  if (scopeChanged) scopeChanged.hidden = !state.data.script_material_scope_changed;
  const profileChanged = $("script-profile-changed");
  if (profileChanged) profileChanged.hidden = !state.data.script_profile_changed;
  select.disabled = busy() || !scriptTemplates.items;
  if (!scriptTemplates.items) {
    // 读取失败只提示一次，不在每次重绘时重试；页面重新可见时会重新读取。
    if (!scriptTemplates.failed && !scriptTemplates.loading) {
      loadScriptTemplates()
        .then(() => state.data && renderTemplateSelect())
        .catch((error) => {
          scriptTemplates.failed = true;
          fail(error);
        });
    }
    return;
  }
  const current = planTemplateId();
  const choices = scriptTemplates.items.map((item) => [item.id, templateLabel(item)]);
  // 方案引用的模板不在目录里（理论上已被删除保护拦住）时仍显示其 ID，避免下拉悄悄改值。
  if (!choices.some(([id]) => id === current)) choices.push([current, current]);
  const key = JSON.stringify([
    choices.map(([id]) => id),
    scriptTemplates.items.map((i) => i.updated_at),
    current,
  ]);
  if (select.dataset.key !== key) {
    select.dataset.key = key;
    fillSelect(select, choices, current);
  }
  const item = scriptTemplates.items.find((entry) => entry.id === current);
  MindexI18n.setAttr(select, "title", item ? templateDescription(item) : "");
}
async function chooseTemplate(id) {
  if (busy() || id === planTemplateId()) return;
  try {
    await accept(
      await api(planBase(), "PATCH", {
        revision: state.data.revision,
        options: { ...plan().options, template_id: id },
      }),
    );
    notice(MindexI18n.msg("ui.script_template_switched"));
  } finally {
    $("script-template").dataset.key = "";
    renderTemplateSelect();
  }
}
async function refreshTemplates() {
  await loadScriptTemplates(true);
  $("script-template").dataset.key = "";
  renderTemplateSelect();
}
// 参考文稿：方案级登记（options.references），只经 /references 接口增删。名称是用户内容，原样显示。
function planReferences() {
  return plan().options.references || [];
}
function renderReferences() {
  const box = $("script-references");
  if (!box || !state.data) return;
  const items = planReferences(),
    limit = state.data.reference_limit || 0,
    locked = busy();
  const total = items.reduce((sum, item) => sum + item.chars, 0);
  const key = JSON.stringify([plan().id, items, limit, locked]);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren();
  const head = el("div", null, "script-references-head");
  head.append(
    el("strong", MindexI18n.msg("ui.reference_title")),
    el("span", MindexI18n.msg("ui.reference_total", { p0: total, p1: limit }), "muted"),
  );
  box.append(head);
  if (!items.length) box.append(el("p", MindexI18n.msg("ui.reference_empty"), "muted"));
  const list = el("ul", null, "reference-list");
  for (const item of items) {
    const row = el("li", null, "reference-row");
    const remove = mutationButton(MindexI18n.msg("ui.reference_remove"), () =>
      removeReference(item),
    );
    MindexI18n.setAttr(
      remove,
      "aria-label",
      MindexI18n.msg("ui.reference_remove_aria", { p0: item.name }),
    );
    remove.disabled = locked;
    row.append(
      el("span", item.name),
      el("span", MindexI18n.msg("ui.reference_chars", { p0: item.chars }), "muted"),
      remove,
    );
    list.append(row);
  }
  if (items.length) box.append(list);
  const actions = el("div", null, "reference-actions");
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".txt,.md,.markdown,text/plain,text/markdown";
  input.hidden = true;
  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";
    if (file) uploadReference(file).catch(fail);
  });
  const upload = mutationButton(MindexI18n.msg("ui.reference_upload"), () => input.click());
  const paste = mutationButton(MindexI18n.msg("ui.reference_paste"), () => pasteReference());
  upload.disabled = paste.disabled = locked;
  actions.append(input, upload, paste);
  box.append(actions);
}
async function uploadReference(file) {
  if (busy()) return;
  const form = new FormData();
  form.append("file", file);
  form.append("revision", state.data.revision);
  await accept(await api(`${planBase()}/references`, "POST", form));
  notice(MindexI18n.msg("ui.reference_added"));
}
function pasteReference() {
  const body = showDialog(MindexI18n.msg("ui.reference_paste_title"));
  const name = field(MindexI18n.msg("ui.reference_field_name"), "");
  name.input.maxLength = 200;
  const text = field(MindexI18n.msg("ui.reference_field_text"), "", "textarea");
  text.input.rows = 12;
  text.input.required = true;
  const limit = state.data.reference_limit || 0;
  body.append(
    name.wrap,
    text.wrap,
    el("p", MindexI18n.msg("ui.reference_limit_note", { p0: limit }), "muted"),
  );
  const save = button(MindexI18n.msg("ui.reference_save"), async () => {
    if (!text.input.value.trim()) throw new Error(MindexI18n.msg("ui.reference_empty_error"));
    await accept(
      await api(`${planBase()}/references`, "POST", {
        revision: state.data.revision,
        name: name.input.value.trim() || null,
        text: text.input.value,
      }),
    );
    closeDialog();
    notice(MindexI18n.msg("ui.reference_added"));
  });
  body.append(save);
  text.input.focus();
}
async function removeReference(item) {
  if (busy()) return;
  const query = `?revision=${encodeURIComponent(state.data.revision)}`;
  await accept(
    await api(`${planBase()}/references/${encodeURIComponent(item.id)}${query}`, "DELETE"),
  );
  notice(MindexI18n.msg("ui.reference_removed"));
}
// 选题来源与账号档案快照（job.topic，见 /studio/topics）：显示来源选题、档案是否已更新，并可同步。
const jobTopic = { key: null, data: null, loading: null };
function renderJobTopic() {
  const box = $("job-topic");
  if (!box || !state.data) return;
  const topic = state.data.job.topic;
  if (!topic) {
    box.hidden = true;
    box.replaceChildren();
    jobTopic.key = null;
    return;
  }
  const key = `${state.id}:${topic.profile_hash}`;
  if (jobTopic.key !== key && !jobTopic.loading) {
    jobTopic.loading = api(`/api/topics/jobs/${encodeURIComponent(state.id)}/profile`)
      .then((data) => {
        jobTopic.key = key;
        jobTopic.data = data;
      })
      .catch((error) => {
        jobTopic.key = key;
        jobTopic.data = null;
        fail(error);
      })
      .finally(() => {
        jobTopic.loading = null;
        renderJobTopic();
      });
  }
  const data = jobTopic.key === key ? jobTopic.data : null;
  box.hidden = false;
  box.replaceChildren();
  const link = document.createElement("a");
  link.className = "quiet nav-link";
  const params = new URLSearchParams({
    profile: topic.profile_id,
    card: topic.card_id,
    job: state.id,
  });
  link.href = `/studio/topics?${params}`;
  link.textContent = topic.title || topic.card_id;
  const line = el("p", null, "job-topic-line");
  line.append(el("span", MindexI18n.msg("ui.job_topic_from")), link);
  if (data?.profile_name)
    line.append(
      el("span", MindexI18n.msg("ui.job_topic_profile", { p0: data.profile_name }), "muted"),
    );
  box.append(line);
  if (!data) return;
  if (!data.profile_exists)
    box.append(el("p", MindexI18n.msg("ui.job_topic_profile_deleted"), "muted"));
  else if (data.outdated) {
    const row = el("p", null, "job-topic-outdated");
    const sync = mutationButton(MindexI18n.msg("ui.job_topic_sync"), () => syncJobTopic());
    sync.disabled = busy();
    row.append(el("span", MindexI18n.msg("ui.job_topic_outdated"), "warn-text"), sync);
    box.append(row);
  }
}
async function syncJobTopic() {
  if (busy() || !state.id) return;
  await api(`/api/topics/jobs/${encodeURIComponent(state.id)}/profile/sync`, "POST");
  jobTopic.key = null;
  await refresh();
  notice(MindexI18n.msg("ui.job_topic_synced"));
}
