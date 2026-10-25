/* 创作 · 文案模板管理页（/studio/templates）：浏览全部模板、编辑自定义模板、用示例大纲试写。
   经典脚本，依赖 shared/*（MindexI18n、MindexApi、MindexFeedback）与 studio-template-text.js。
   内置模板只读（可查看提示词与试写），复制为自定义模板后才能修改；试写不落库、不涉及任何任务。 */
"use strict";
const PREVIEW_STORAGE = "mindex.templatePreview";
const query = new URLSearchParams(location.search);
const page = {
  items: [],
  selected: null, // 当前模板（服务端视图）
  saved: null, // 选中自定义模板时表单的已保存值，用于判断未保存修改
  job: query.get("job"), // 来源任务：“创作任务”标签回到该任务
  saving: false,
  previewing: false,
  previewTimer: null
};
const $ = (id) => document.getElementById(id);
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) MindexI18n.setText(node, text);
  if (cls) node.className = cls;
  return node;
}
function notice(text, error = false) {
  const value = error ? MindexI18n.system(text) : text;
  if (globalThis.MindexFeedback) return MindexFeedback.show("notice", value, error ? "error" : "info");
  MindexI18n.setText($("notice"), value);
  $("notice").dataset.type = error ? "error" : "info";
}
function showError(id, error) {
  const node = $(id);
  if (!error) {
    node.hidden = true;
    MindexI18n.setText(node, "");
    return;
  }
  MindexI18n.setText(node, MindexI18n.system(error.message ?? error));
  node.hidden = false;
}
const api = (url, method = "GET", body) => MindexApi.json(url, method, body);
const templateUrl = (id, suffix = "") => `/api/studio/templates/${encodeURIComponent(id)}${suffix}`;
const editable = () => Boolean(page.selected && !page.selected.builtin);

// ------------------------------------------------------------ 列表
function matches(item, needle) {
  if (!needle) return true;
  const text = [MindexI18n.render(templateName(item)), MindexI18n.render(templateCategory(item)), item.id,
    MindexI18n.render(templateDescription(item)), item.instructions || ""].join("\n").toLowerCase();
  return text.includes(needle);
}
function renderList() {
  const box = $("template-list");
  const needle = $("template-filter").value.trim().toLowerCase();
  box.replaceChildren();
  const groups = [
    ["ui.templates_group_builtin", page.items.filter((item) => item.builtin)],
    ["ui.templates_group_user", page.items.filter((item) => !item.builtin)]
  ];
  let shown = 0;
  for (const [title, items] of groups) {
    const visible = items.filter((item) => matches(item, needle));
    shown += visible.length;
    const group = el("div", null, "template-group");
    const heading = el("h3", MindexI18n.msg(title, {}), "template-group-title");
    heading.append(el("span", String(items.length), "muted"));
    group.append(heading);
    if (!items.length && !needle && title === "ui.templates_group_user")
      group.append(el("p", MindexI18n.msg("ui.templates_user_empty"), "muted template-group-empty"));
    const list = el("ul", null, "template-items");
    for (const item of visible) {
      const li = el("li");
      const entry = el("button", null, "job-item template-item");
      entry.type = "button";
      entry.dataset.id = item.id;
      if (page.selected?.id === item.id) {
        entry.classList.add("active");
        entry.setAttribute("aria-current", "true");
      }
      entry.append(el("div", templateName(item), "job-title"));
      const sub = el("div", null, "job-sub");
      sub.append(el("span", templateCategory(item)), el("span", templateKind(item.kind)));
      entry.append(sub);
      entry.addEventListener("click", () => select(item.id).catch((error) => notice(error.message, true)));
      li.append(entry);
      list.append(li);
    }
    if (visible.length) group.append(list);
    box.append(group);
  }
  if (needle && !shown) box.append(el("p", MindexI18n.msg("ui.templates_filter_empty"), "muted template-group-empty"));
}

// ------------------------------------------------------------ 详情与编辑
function formValues() {
  const limit = $("tf-max-chars").value.trim();
  return {
    name: $("tf-name").value.trim(),
    category: $("tf-category").value.trim(),
    kind: $("tf-kind").value,
    description: $("tf-description").value.trim(),
    instructions: $("tf-instructions").value,
    max_chars: limit ? Number(limit) : null
  };
}
function itemValues(item) {
  return {
    name: item.name,
    category: item.category,
    kind: item.kind,
    description: item.description || "",
    instructions: item.instructions,
    max_chars: item.constraints?.max_chars ?? null
  };
}
function dirty() {
  return editable() && page.saved !== null && JSON.stringify(formValues()) !== JSON.stringify(page.saved);
}
function fillForm(item) {
  // 内置模板的名称、分类与说明按当前语言显示（只读）；自定义模板显示用户填写的原文。
  const values = itemValues(item);
  $("tf-name").value = item.builtin ? MindexI18n.render(templateName(item)) : values.name;
  $("tf-category").value = item.builtin ? MindexI18n.render(templateCategory(item)) : values.category;
  $("tf-kind").value = values.kind;
  $("tf-max-chars").value = values.max_chars ?? "";
  $("tf-description").value = item.builtin ? MindexI18n.render(templateDescription(item)) : values.description;
  $("tf-instructions").value = values.instructions;
  page.saved = item.builtin ? null : itemValues(item);
}
function renderTags(item) {
  const box = $("template-tags");
  box.replaceChildren(
    el("span", MindexI18n.msg(item.builtin ? "ui.template_builtin" : "ui.template_user"), "template-tag"),
    el("span", templateCategory(item), "template-tag"),
    el("span", templateKind(item.kind), "template-tag")
  );
}
function renderMeta(item) {
  const parts = [MindexI18n.msg("ui.templates_meta", { p0: item.id, p1: item.version, p2: String(item.hash || "").slice(0, 10) })];
  if (item.base_id) {
    const base = page.items.find((entry) => entry.id === item.base_id);
    parts.push(MindexI18n.msg("ui.templates_meta_base", { p0: base ? templateName(base) : item.base_id }));
  }
  if (item.updated_at) parts.push(MindexI18n.msg("ui.templates_meta_updated", { p0: MindexI18n.date(item.updated_at) }));
  MindexI18n.setText($("template-meta"), parts.join(" · "));
}
function renderDetail() {
  const item = page.selected;
  $("template-empty").hidden = Boolean(item);
  $("template-view").hidden = !item;
  if (!item) return;
  MindexI18n.setText($("template-title"), templateName(item));
  renderTags(item);
  MindexI18n.setText($("template-description"), templateDescription(item));
  MindexI18n.setText($("template-note"), MindexI18n.msg(item.builtin ? "ui.templates_builtin_note" : "ui.templates_user_note"));
  $("template-note").classList.toggle("is-readonly", item.builtin);
  for (const id of ["tf-name", "tf-category", "tf-max-chars", "tf-description", "tf-instructions"]) $(id).readOnly = item.builtin;
  $("tf-kind").disabled = item.builtin;
  $("template-delete").hidden = item.builtin;
  $("template-revert").hidden = $("template-save").hidden = item.builtin;
  MindexI18n.setAttr($("template-copy"), "aria-label", MindexI18n.msg("ui.template_copy_aria", { p0: templateName(item) }));
  MindexI18n.setAttr($("template-delete"), "aria-label", MindexI18n.msg("ui.template_delete_aria", { p0: item.name }));
  renderMeta(item);
  showError("template-error", null);
  updateForm();
}
function updateForm() {
  const value = $("tf-instructions").value;
  MindexI18n.setText($("tf-instructions-count"), MindexI18n.msg("ui.templates_instructions_count", { p0: value.length }));
  const changed = dirty();
  $("template-dirty").hidden = !changed;
  $("template-save").disabled = !changed || page.saving;
  $("template-revert").disabled = !changed || page.saving;
  $("template-copy").disabled = $("template-delete").disabled = page.saving;
}
function confirmDiscard() {
  return !dirty() || window.confirm(MindexI18n.render(MindexI18n.msg("ui.templates_discard_confirm")));
}
function syncUrl() {
  const url = templatesPageUrl(page.selected?.id, page.job);
  history.replaceState(null, "", url);
}
async function select(id, { force = false } = {}) {
  if (!force && page.selected?.id === id) return;
  if (!force && !confirmDiscard()) return;
  // 列表视图可能落后于其他标签或助手的修改：选中时按 ID 重新读取完整模板。
  const item = await api(templateUrl(id));
  page.selected = item;
  const index = page.items.findIndex((entry) => entry.id === id);
  if (index >= 0) page.items[index] = item;
  fillForm(item);
  renderList();
  renderDetail();
  syncUrl();
}
async function load(preferred) {
  const data = await api("/api/studio/templates");
  page.items = data.items;
  const keep = preferred || page.selected?.id;
  const target = page.items.find((item) => item.id === keep) ? keep : null;
  if (target) await select(target, { force: true });
  else {
    page.selected = page.saved = null;
    renderList();
    renderDetail();
    syncUrl();
  }
}
async function save(event) {
  event?.preventDefault();
  if (!editable() || !dirty() || page.saving) return;
  const values = formValues();
  if (!values.name || !values.category || !values.instructions.trim()) {
    showError("template-error", MindexI18n.msg("ui.templates_required_fields"));
    return;
  }
  page.saving = true;
  updateForm();
  try {
    const item = await api(templateUrl(page.selected.id), "PUT", values);
    page.selected = item;
    page.items = page.items.map((entry) => (entry.id === item.id ? item : entry));
    fillForm(item);
    renderList();
    renderDetail();
    notice(MindexI18n.msg("ui.template_saved"));
  } catch (error) {
    showError("template-error", error);
  } finally {
    page.saving = false;
    updateForm();
  }
}
async function copy() {
  if (!page.selected || !confirmDiscard()) return;
  const source = page.selected;
  const name = MindexI18n.render(MindexI18n.msg("ui.template_copy_name", { p0: templateName(source) })).slice(0, 40);
  const created = await api(templateUrl(source.id, "/copy"), "POST", { name });
  page.saved = null; // 已确认放弃当前修改
  await load(created.id);
  notice(MindexI18n.msg("ui.template_copied"));
  $("tf-name").focus();
  $("tf-name").select();
}
async function remove() {
  if (!editable()) return;
  const item = page.selected;
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.template_delete_confirm", { p0: item.name })))) return;
  await api(templateUrl(item.id), "DELETE");
  page.selected = page.saved = null;
  await load(item.base_id || "base");
  notice(MindexI18n.msg("ui.template_deleted"));
}
function revert() {
  if (!editable()) return;
  fillForm(page.selected);
  showError("template-error", null);
  updateForm();
}

// ------------------------------------------------------------ 试写
function splitReferences(text) {
  return text.split(/^\s*---\s*$/m).map((part) => part.trim()).filter(Boolean);
}
function rememberPreview() {
  try {
    localStorage.setItem(PREVIEW_STORAGE, JSON.stringify({
      outline: $("preview-outline").value, references: $("preview-references").value
    }));
  } catch {}
}
function restorePreview() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREVIEW_STORAGE) || "null");
    if (saved) {
      $("preview-outline").value = saved.outline || "";
      $("preview-references").value = saved.references || "";
    }
  } catch {}
}
function previewBody() {
  const body = { outline: $("preview-outline").value.trim(), references: splitReferences($("preview-references").value) };
  // 自定义模板总是带上表单里影响生成的字段（含未保存的修改）；内置模板只读，按服务端内容试写。
  if (editable()) {
    const values = formValues();
    Object.assign(body, { kind: values.kind, instructions: values.instructions, max_chars: values.max_chars });
  }
  return body;
}
function setPreviewing(on) {
  page.previewing = on;
  $("preview-run").disabled = on;
  clearInterval(page.previewTimer);
  if (!on) {
    MindexI18n.setText($("preview-status"), "");
    return;
  }
  const started = Date.now();
  const tick = () => MindexI18n.setText($("preview-status"),
    MindexI18n.msg("ui.templates_preview_running", { p0: Math.round((Date.now() - started) / 1000) }));
  tick();
  page.previewTimer = setInterval(tick, 1000);
}
async function runPreview() {
  if (!page.selected || page.previewing) return;
  showError("preview-error", null);
  const body = previewBody();
  const kind = body.kind || page.selected.kind;
  if (!body.outline && !body.references.length) return showError("preview-error", MindexI18n.msg("ui.templates_preview_needs_input"));
  if (kind === "rewrite" && !body.references.length)
    return showError("preview-error", MindexI18n.msg("ui.templates_preview_needs_references"));
  if (editable() && !body.instructions.trim()) return showError("preview-error", MindexI18n.msg("ui.templates_required_fields"));
  rememberPreview();
  const draft = dirty();
  setPreviewing(true);
  try {
    const result = await api(templateUrl(page.selected.id, "/preview"), "POST", body);
    const meta = [MindexI18n.msg("ui.templates_preview_result", {
      p0: result.chars, p1: result.limit, p2: (result.elapsed_ms / 1000).toFixed(1)
    })];
    if (draft) meta.push(MindexI18n.msg("ui.templates_preview_draft"));
    MindexI18n.setText($("preview-meta"), meta.join(" · "));
    $("preview-text").textContent = result.text; // 模型输出是内容，不做文案目录匹配
    $("preview-result").hidden = false;
  } catch (error) {
    showError("preview-error", error);
  } finally {
    setPreviewing(false);
  }
}
async function copyPreview() {
  await navigator.clipboard.writeText($("preview-text").textContent);
  notice(MindexI18n.msg("ui.templates_preview_copied"));
}

// ------------------------------------------------------------ 事件与启动
const failed = (error) => notice(error.message, true);
$("template-filter").addEventListener("input", renderList);
$("template-form").addEventListener("submit", (event) => save(event).catch(failed));
$("template-form").addEventListener("input", updateForm);
$("template-form").addEventListener("change", updateForm);
$("template-form").addEventListener("keydown", (event) => {
  if (event.key === "s" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    save().catch(failed);
  }
});
$("template-revert").addEventListener("click", revert);
$("template-copy").addEventListener("click", () => copy().catch(failed));
$("template-delete").addEventListener("click", () => remove().catch(failed));
$("preview-run").addEventListener("click", () => runPreview().catch(failed));
$("preview-copy").addEventListener("click", () => copyPreview().catch(failed));
$("preview-outline").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
  event.preventDefault();
  runPreview().catch(failed);
});
$("reload").addEventListener("click", async () => {
  if (!confirmDiscard()) return;
  $("reload").disabled = true;
  try {
    page.saved = null;
    await load();
    notice(MindexI18n.msg("ui.templates_reloaded"));
  } catch (error) {
    failed(error);
  } finally {
    $("reload").disabled = false;
  }
});
window.addEventListener("beforeunload", (event) => {
  if (!dirty()) return;
  event.preventDefault();
  event.returnValue = "";
});
// 切换语言：内置模板的只读字段与列表按新语言重绘（用户正在编辑的内容不动）。
window.addEventListener("mindex:languagechange", () => {
  if (page.selected?.builtin) fillForm(page.selected);
  renderList();
});
// 助手或其他来源改动了模板：没有未保存修改时重新读取，否则只刷新列表，保留正在编辑的内容。
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
  if (detail.domain !== "studio" || !detail.templates) return;
  if (dirty()) {
    api("/api/studio/templates").then((data) => {
      page.items = data.items.map((item) => (item.id === page.selected?.id ? page.selected : item));
      renderList();
    }).catch(() => {});
  } else load().catch(() => {});
});
window.MindexAgentContext = () => ({ page: "studio", section: "templates", template_id: page.selected?.id || null });
// 助手要定位到某个创作任务时回到创作页；模板页本身没有可定位的区块。
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  if (focus.job_id && !passive) location.href = `/studio?job=${encodeURIComponent(focus.job_id)}`;
  return true;
};
if (page.job) {
  $("studio-jobs-tab").href = `/studio?job=${encodeURIComponent(page.job)}`;
  $("studio-topics-tab").href = `/studio/topics?job=${encodeURIComponent(page.job)}`;
}
restorePreview();
load(query.get("id") || "base").catch(failed);
