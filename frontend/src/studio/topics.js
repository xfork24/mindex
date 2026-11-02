/* 创作 · 选题挖掘页（/studio/topics）：领域档案、数据源、挖掘、选题卡片、素材采集清单。
   经典脚本，依赖 shared/*（MindexI18n、MindexApi、MindexFeedback）。只经 MindexApi 请求同源 /api/topics；
   Web 端与桌面端共用，桌面专属提示只经 window.MindexDesktop 特性检测。
   外部来源的标题、摘要与模型输出一律作为内容（textContent）显示，不做文案目录匹配。 */
"use strict";
const PROFILE_STORAGE = "mindex.topics.profile";
const WEIGHT_KEYS = ["timeliness", "audience", "coverage", "novelty", "cost"];
const topicsQuery = new URLSearchParams(location.search);
const page = {
  profiles: [],
  profile: null, // 当前档案（服务端视图）
  editing: null, // 对话框正在编辑的档案 ID；null 表示新建
  job: topicsQuery.get("job")
};
const $ = (id) => document.getElementById(id);
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) MindexI18n.setText(node, text);
  if (cls) node.className = cls;
  return node;
}
// 内容节点：用户填写或外部来源的原文，不经文案目录。
function content(tag, text, cls) {
  const node = document.createElement(tag);
  node.textContent = text ?? "";
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
const enc = encodeURIComponent;
const profileUrl = (id, suffix = "") => `/api/topics/profiles/${enc(id)}${suffix}`;
const failed = (error) => notice(error.message ?? error, true);
const splitList = (text) => String(text || "").split(/[,，、\n]+/).map((part) => part.trim()).filter(Boolean);
const splitLines = (text) => String(text || "").split(/\n+/).map((part) => part.trim()).filter(Boolean);

// ------------------------------------------------------------ 领域档案
function rememberProfile(id) {
  try {
    if (id) localStorage.setItem(PROFILE_STORAGE, id);
    else localStorage.removeItem(PROFILE_STORAGE);
  } catch {}
}
function storedProfile() {
  try { return localStorage.getItem(PROFILE_STORAGE); } catch { return null; }
}
function renderProfiles() {
  const select = $("profile-select");
  select.replaceChildren(...page.profiles.map((item) => {
    const option = content("option", `${item.name} · ${item.domain}`);
    option.value = item.id;
    return option;
  }));
  if (page.profile) select.value = page.profile.id;
  const has = Boolean(page.profile);
  $("topics-empty").hidden = has;
  $("topics-workspace").hidden = !has;
  $("topics-bar").hidden = !page.profiles.length;
  $("profile-edit").disabled = $("profile-delete").disabled = !has;
  renderProfileSummary();
}
function renderProfileSummary() {
  const box = $("profile-summary");
  const item = page.profile;
  if (!item) return MindexI18n.setText(box, "");
  const parts = [];
  if (item.subtopics.length) parts.push(item.subtopics.join("、"));
  if (item.audience) parts.push(item.audience);
  const schedule = item.schedule.enabled
    ? MindexI18n.msg("ui.topics_schedule_summary", { p0: item.schedule.time, p1: item.schedule.count })
    : MindexI18n.msg("ui.topics_schedule_off");
  parts.push(schedule);
  MindexI18n.setText(box, parts.join(" · "));
}
async function selectProfile(id) {
  page.profile = page.profiles.find((item) => item.id === id) || null;
  rememberProfile(page.profile?.id);
  renderProfiles();
  syncUrl();
  await profileChanged();
}
// 档案切换后的刷新钩子与助手定位钩子：后续分区（数据源、挖掘、卡片、采集清单）在此登记。
const profileHooks = [];
const focusHooks = [];
// 档案持久化素材范围；组多选取并集，再与手动素材选择取交集。
const topicGroups = MindexGroups.picker("source", [], () => {}, "ui.groups_filter", {listbox: true, defaultAll: true});
const topicSources = document.createElement("select");
topicSources.id = "topic-material-sources";
topicSources.multiple = true;
topicSources.size = 5;
const topicSourceLabel = el("label", MindexI18n.msg("ui.topics_material_sources"));
topicSourceLabel.append(topicSources);
const topicScopeSave = el("button", MindexI18n.msg("ui.topics_material_save"), "quiet");
topicScopeSave.id = "topic-material-save";
topicScopeSave.type = "button";
const topicScopeHint = el("p", MindexI18n.msg("ui.topics_material_hint"), "muted");
const topicScopeReset = el("button", MindexI18n.msg("ui.topics_material_reset"), "quiet");
topicScopeReset.type = "button";
topicScopeReset.id = "topic-material-reset";
topicScopeReset.addEventListener("click", async () => {
  topicSourceSelection = null; topicGroups.setValues([]);
  await topicScopeLink.refresh();
});
const topicCurrentScope = document.createElement("section");
topicCurrentScope.id = "topic-material-current";
topicCurrentScope.className = "topic-material-current";
topicCurrentScope.setAttribute("aria-live", "polite");
$("topic-material-scope").append(topicCurrentScope, topicGroups.wrap, topicSourceLabel, topicScopeSave, topicScopeReset, topicScopeHint);
let savedMaterialScope = null, savedScopeRequest = 0;
function renderSavedMaterialScope() {
  topicCurrentScope.replaceChildren(el("h3", MindexI18n.msg("ui.topics_material_current")));
  if (!savedMaterialScope) return;
  const data = savedMaterialScope;
  const names = new Map(topicSourcePool.map((source) => [source.id, source.name]));
  function line(key, value) {
    const row = document.createElement("p");
    row.append(el("span", MindexI18n.msg(key)), content("span", value));
    topicCurrentScope.append(row);
  }
  const unlimited = MindexI18n.render(MindexI18n.msg("ui.topics_material_unlimited"));
  const groups = new Map(data.groups.map((group) => [group.id, group]));
  line("ui.topics_material_current_groups", data.groupIds.length ? data.groupIds.map((id) => {
    const group = groups.get(id);
    return group?.system ? MindexI18n.render(MindexI18n.msg("ui.groups_ungrouped")) : group?.name || id;
  }).join("、") : unlimited);
  line("ui.topics_material_current_manual", data.sourceIds.length ? data.sourceIds.map((id) => names.get(id) || id).join("、") : unlimited);
  if (data.error) {
    topicCurrentScope.append(el("p", MindexI18n.system(data.error), "warn-text"));
    return;
  }
  const ids = data.effective === null ? topicSourcePool.map((source) => source.id) : data.effective.filter((id) => names.has(id));
  const details = document.createElement("details"); details.id = "topic-material-current-members";
  details.append(el("summary", MindexI18n.msg("ui.topics_material_current_count", {p0: ids.length})));
  if (ids.length) {
    const list = document.createElement("ul");
    for (const id of ids) list.append(content("li", names.get(id) || id));
    details.append(list);
  } else details.append(el("p", MindexI18n.msg("ui.groups_no_sources")));
  topicCurrentScope.append(details);
}
async function loadSavedMaterialScope(profile) {
  const request = ++savedScopeRequest;
  const [groups, scope] = await Promise.allSettled([
    api("/api/admin/groups?type=source"),
    api("/api/admin/groups/resolve", "POST", {source_ids: profile.source_ids || [], source_group_ids: profile.source_group_ids || []})
  ]);
  if (request !== savedScopeRequest || page.profile?.id !== profile.id) return;
  const empty = scope.status === "rejected" && scope.reason.status === 422 &&
    (scope.reason.detail === "所选组与素材范围没有成员，请重新选择" || scope.reason.data?.message_key === "ui.groups_empty_resolve");
  savedMaterialScope = {groupIds: [...(profile.source_group_ids || [])], sourceIds: [...(profile.source_ids || [])],
    groups: groups.status === "fulfilled" ? groups.value.items : [],
    effective: scope.status === "fulfilled" ? scope.value.source_ids : [],
    error: scope.status === "rejected" && !empty ? scope.reason.message : groups.status === "rejected" ? groups.reason.message : null};
  renderSavedMaterialScope();
}
MindexMultiSearch.attach(topicSources);
let topicSourcePool = [], topicSourceSelection = null;
const topicScopeLink = MindexGroups.linkSources(topicGroups, topicSources, {
  entries: () => topicSourcePool, selection: () => topicSourceSelection,
  fill: (entries, selected) => {
    topicSources.replaceChildren(...MindexMultiSearch.sort(entries).map((item) => {
      const option = content("option", item.name);
      option.value = item.id; option.selected = selected.has(item.id);
      return option;
    }));
    MindexMultiSearch.refilter(topicSources);
  }, onError: failed
});
topicSources.addEventListener("change", () => {
  topicSourceSelection = [...topicSources.selectedOptions].map((option) => option.value);
});
topicScopeSave.addEventListener("click", async () => {
  if (!page.profile) return;
  if (topicSourceSelection?.length === 0) return failed(MindexI18n.msg("ui.topics_material_select_required"));
  topicScopeSave.disabled = true;
  try {
    await api(profileUrl(page.profile.id), "PATCH", {
      source_ids: topicSourceSelection || [], source_group_ids: topicGroups.values()
    });
    await loadProfiles(page.profile.id);
    notice(MindexI18n.msg("ui.topics_material_saved"));
  } catch (error) { failed(error); }
  finally { topicScopeSave.disabled = false; }
});
profileHooks.push(async (profile) => {
  $("topic-material-scope").hidden = !profile;
  if (!profile) { savedMaterialScope = null; ++savedScopeRequest; topicCurrentScope.replaceChildren(); return; }
  savedMaterialScope = null; renderSavedMaterialScope();
  topicSourceSelection = profile.source_ids?.length ? profile.source_ids : null;
  topicGroups.setValues(profile.source_group_ids || []);
  topicSourcePool = [];
  let materialPage = 1;
  while (true) {
    const data = await api(`/api/admin/sources?visibility=all&page_size=100&page=${materialPage}`);
    topicSourcePool.push(...data.items);
    if (!data.items.length || topicSourcePool.length >= data.total) break;
    materialPage += 1;
  }
  await topicGroups.refresh();
  await topicScopeLink.refresh();
  await loadSavedMaterialScope(profile);
});

async function profileChanged() {
  for (const hook of profileHooks) await hook(page.profile);
}
async function loadProfiles(preferred) {
  const data = await api("/api/topics/profiles");
  page.profiles = data.items;
  const wanted = [preferred, page.profile?.id, topicsQuery.get("profile"), storedProfile()]
    .find((id) => id && page.profiles.some((item) => item.id === id));
  await selectProfile(wanted || page.profiles[0]?.id || null);
}
function syncUrl() {
  const params = new URLSearchParams();
  if (page.profile) params.set("profile", page.profile.id);
  if (page.card) params.set("card", page.card);
  if (page.job) params.set("job", page.job);
  const search = params.toString();
  history.replaceState(null, "", "/studio/topics" + (search ? `?${search}` : ""));
}

// ------------------------------------------------------------ 档案对话框
function fillProfileForm(item) {
  const value = item || { name: "", domain: "", subtopics: [], audience: "", positioning: "", taboos: [], keywords: [],
    canvas: "16:9",
    weights: { timeliness: 0.25, audience: 0.25, coverage: 0.25, novelty: 0.15, cost: 0.1 },
    schedule: { enabled: false, time: "08:00", count: 10 } };
  $("pf-name").value = value.name;
  $("pf-domain").value = value.domain;
  $("pf-subtopics").value = value.subtopics.join("，");
  $("pf-audience").value = value.audience;
  $("pf-positioning").value = value.positioning;
  $("pf-taboos").value = value.taboos.join("\n");
  $("pf-keywords").value = value.keywords.join("，");
  $("pf-canvas").value = value.canvas;
  for (const key of WEIGHT_KEYS) $(`pf-w-${key}`).value = value.weights[key];
  $("pf-s-enabled").checked = value.schedule.enabled;
  $("pf-s-time").value = value.schedule.time;
  $("pf-s-count").value = value.schedule.count;
  updateWeightSum();
}
function profileFormValues() {
  const weights = Object.fromEntries(WEIGHT_KEYS.map((key) => [key, Number($(`pf-w-${key}`).value || 0)]));
  return {
    name: $("pf-name").value.trim(),
    domain: $("pf-domain").value.trim(),
    subtopics: splitList($("pf-subtopics").value),
    audience: $("pf-audience").value.trim(),
    positioning: $("pf-positioning").value.trim(),
    taboos: splitLines($("pf-taboos").value),
    keywords: splitList($("pf-keywords").value),
    canvas: $("pf-canvas").value,
    weights,
    schedule: { enabled: $("pf-s-enabled").checked, time: $("pf-s-time").value || "08:00",
      count: Number($("pf-s-count").value || 10) }
  };
}
function weightSum() {
  return WEIGHT_KEYS.reduce((sum, key) => sum + Number($(`pf-w-${key}`).value || 0), 0);
}
function updateWeightSum() {
  const total = weightSum();
  const node = $("pf-weights-sum");
  MindexI18n.setText(node, MindexI18n.msg("ui.topics_weights_sum", { p0: total.toFixed(2) }));
  node.classList.toggle("warn-text", Math.abs(total - 1) > 0.001);
}
function openProfileDialog(item) {
  page.editing = item?.id || null;
  MindexI18n.setText($("profile-title"), MindexI18n.msg(item ? "ui.topics_profile_edit_title" : "ui.topics_profile_new_title"));
  fillProfileForm(item);
  showError("pf-error", null);
  $("profile-dialog").showModal();
  $("pf-name").focus();
}
async function saveProfile(event) {
  event.preventDefault();
  const values = profileFormValues();
  if (!values.name || !values.domain) return showError("pf-error", MindexI18n.msg("ui.topics_profile_required"));
  if (Math.abs(weightSum() - 1) > 0.001) return showError("pf-error", MindexI18n.msg("ui.topics_weights_invalid"));
  $("pf-save").disabled = true;
  try {
    const saved = page.editing
      ? await api(profileUrl(page.editing), "PATCH", values)
      : await api("/api/topics/profiles", "POST", values);
    $("profile-dialog").close();
    await loadProfiles(saved.id);
    notice(MindexI18n.msg(page.editing ? "ui.topics_profile_saved" : "ui.topics_profile_created"));
  } catch (error) {
    showError("pf-error", error);
  } finally {
    $("pf-save").disabled = false;
  }
}
async function deleteProfile() {
  const item = page.profile;
  if (!item) return;
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.topics_profile_delete_confirm", { p0: item.name })))) return;
  await api(profileUrl(item.id), "DELETE");
  page.profile = null;
  await loadProfiles();
  notice(MindexI18n.msg("ui.topics_profile_deleted"));
}

// ------------------------------------------------------------ 数据源
// 各类型的表单字段：key 为 config 的键；type 为 url/text/number/checkbox/select/textarea/list。
const SOURCE_FORMS = {
  rss: [{ key: "url", type: "url", label: "ui.topics_source_url", required: true, placeholder: "https://example.com/feed.xml" }],
  hotlist: [
    { key: "preset", type: "select", label: "ui.topics_hot_preset", options: [["custom", "ui.topics_hot_preset_custom"], ["dailyhot", "ui.topics_hot_preset_dailyhot"]] },
    { key: "base_url", type: "url", label: "ui.topics_hot_base_url", placeholder: "http://127.0.0.1:6688", when: ["preset", "dailyhot"], hint: "ui.topics_hot_base_url_hint" },
    { key: "boards", type: "list", label: "ui.topics_hot_boards", rows: 3, default: ["weibo", "zhihu", "36kr"], when: ["preset", "dailyhot"], hint: "ui.topics_hot_boards_hint" },
    { key: "url", type: "url", label: "ui.topics_hot_url", placeholder: "https://example.com/hot.json", when: ["preset", "custom"] },
    { key: "items_path", type: "text", label: "ui.topics_hot_items_path", default: "data", when: ["preset", "custom"] },
    { key: "fields.title", type: "text", label: "ui.topics_hot_field_title", default: "title", when: ["preset", "custom"] },
    { key: "fields.desc", type: "text", label: "ui.topics_hot_field_desc", default: "desc", when: ["preset", "custom"] },
    { key: "fields.url", type: "text", label: "ui.topics_hot_field_url", default: "url", when: ["preset", "custom"] },
    { key: "fields.hot", type: "text", label: "ui.topics_hot_field_hot", default: "hot", when: ["preset", "custom"] },
    { key: "fields.time", type: "text", label: "ui.topics_hot_field_time", default: "timestamp", when: ["preset", "custom"] },
    { key: "limit", type: "number", label: "ui.topics_hot_limit", default: 30, min: 1, max: 100 }
  ],
  brave: [
    { key: "endpoint", type: "select", label: "ui.topics_brave_endpoint", options: [["news", "ui.topics_brave_news"], ["web", "ui.topics_brave_web"]] },
    { key: "freshness", type: "select", label: "ui.topics_brave_freshness", options: [["pd", "ui.topics_brave_pd"], ["pw", "ui.topics_brave_pw"], ["pm", "ui.topics_brave_pm"], ["py", "ui.topics_brave_py"]] },
    { key: "queries", type: "list", label: "ui.topics_brave_queries", rows: 3, hint: "ui.topics_brave_queries_hint" },
    { key: "count", type: "number", label: "ui.topics_brave_count", default: 10, min: 1, max: 20 },
    { key: "country", type: "text", label: "ui.topics_brave_country", placeholder: "CN" },
    { key: "search_lang", type: "text", label: "ui.topics_brave_lang", placeholder: "zh-hans" }
  ],
  calendar: [
    { key: "url", type: "url", label: "ui.topics_calendar_url", placeholder: "https://example.com/calendar.ics", hint: "ui.topics_calendar_url_hint" },
    { key: "days", type: "number", label: "ui.topics_calendar_days", default: 14, min: 1, max: 60 }
  ],
  local: [{ key: "library", type: "checkbox", label: "ui.topics_source_local_library", default: true }]
};
const fieldValue = (config, key) => key.split(".").reduce((value, part) => (value == null ? undefined : value[part]), config);
function setFieldValue(config, key, value) {
  const parts = key.split(".");
  let target = config;
  for (const part of parts.slice(0, -1)) target = target[part] ??= {};
  target[parts.at(-1)] = value;
}
function fieldId(key) {
  return `sf-field-${key.replace(/\./g, "-")}`;
}
function applyFieldConditions() {
  for (const wrap of $("sf-fields").querySelectorAll("[data-when]")) {
    const [key, value] = wrap.dataset.when.split("=");
    wrap.hidden = $(fieldId(key))?.value !== value;
  }
}
const KIND_LABELS = { rss: "ui.topics_kind_rss", hotlist: "ui.topics_kind_hotlist", brave: "ui.topics_kind_brave",
  calendar: "ui.topics_kind_calendar", local: "ui.topics_kind_local" };
const sources = { items: [], kinds: [], editing: null };
const sourceUrl = (id, suffix = "") => `/api/topics/sources/${enc(id)}${suffix}`;
const kindLabel = (kind) => (KIND_LABELS[kind] ? MindexI18n.msg(KIND_LABELS[kind]) : kind);
const kindInfo = (kind) => sources.kinds.find((item) => item.kind === kind) || {};

function fieldInput(field, value) {
  let input;
  if (field.type === "checkbox") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value ?? field.default ?? false;
  } else if (field.type === "select") {
    input = document.createElement("select");
    for (const [optionValue, optionLabel] of field.options) {
      const option = el("option", MindexI18n.msg(optionLabel));
      option.value = optionValue;
      input.append(option);
    }
    input.value = value ?? field.default ?? field.options[0][0];
  } else if (field.type === "textarea" || field.type === "list") {
    input = document.createElement("textarea");
    input.rows = field.rows || 3;
    input.value = field.type === "list" ? (value || field.default || []).join("\n") : (value ?? field.default ?? "");
  } else {
    input = document.createElement("input");
    input.type = field.type === "number" ? "number" : field.type === "url" ? "url" : "text";
    if (field.min != null) input.min = field.min;
    if (field.max != null) input.max = field.max;
    input.value = value ?? field.default ?? "";
  }
  input.id = fieldId(field.key);
  input.dataset.key = field.key;
  input.dataset.type = field.type;
  if (field.placeholder) input.placeholder = field.placeholder;
  return input;
}
function renderSourceFields(kind, config = {}) {
  const box = $("sf-fields");
  box.replaceChildren();
  for (const field of SOURCE_FORMS[kind] || []) {
    const input = fieldInput(field, fieldValue(config, field.key));
    let label;
    if (field.type === "checkbox") {
      label = el("label", null, "check");
      label.append(input, el("span", MindexI18n.msg(field.label)));
    } else {
      label = el("label", MindexI18n.msg(field.label), "form-field");
      label.append(input);
    }
    if (field.hint) label.append(el("span", MindexI18n.msg(field.hint), "muted source-field-hint"));
    if (field.when) label.dataset.when = field.when.join("=");
    if (field.type === "select") input.addEventListener("change", applyFieldConditions);
    box.append(label);
  }
  applyFieldConditions();
  const info = kindInfo(kind);
  $("sf-third-party").hidden = !info.third_party;
  // 需要密钥的类型：编辑时留空表示保留已保存的密钥。
  if (info.secret) {
    const label = el("label", MindexI18n.msg("ui.topics_source_secret"), "form-field");
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.id = "sf-secret";
    const existing = sources.editing && sources.items.find((item) => item.id === sources.editing);
    MindexI18n.setAttr(input, "placeholder", existing?.has_secret
      ? MindexI18n.msg("ui.topics_source_secret_keep", { p0: existing.secret_mask }) : MindexI18n.msg("ui.topics_source_secret_placeholder"));
    label.append(input);
    box.append(label);
  }
}
function sourceFormConfig(kind) {
  const config = {};
  for (const field of SOURCE_FORMS[kind] || []) {
    const input = $(fieldId(field.key));
    if (!input || input.closest("[data-when]")?.hidden) continue;
    if (field.type === "checkbox") setFieldValue(config, field.key, input.checked);
    else if (field.type === "number") { if (input.value !== "") setFieldValue(config, field.key, Number(input.value)); }
    else if (field.type === "list") setFieldValue(config, field.key, splitLines(input.value));
    else if (input.value.trim() !== "") setFieldValue(config, field.key, input.value.trim());
  }
  return config;
}
function resetSourceForm() {
  sources.editing = null;
  $("sf-kind").disabled = false;
  $("sf-name").value = "";
  $("sf-enabled").checked = true;
  MindexI18n.setText($("sf-title"), MindexI18n.msg("ui.topics_source_add_title"));
  MindexI18n.setText($("sf-save"), MindexI18n.msg("ui.topics_source_add"));
  $("sf-cancel").hidden = true;
  showError("sf-error", null);
  renderSourceFields($("sf-kind").value);
}
function renderKindOptions() {
  const select = $("sf-kind");
  const current = select.value;
  const known = new Set(sources.kinds.map((item) => item.kind));
  select.replaceChildren(...Object.keys(SOURCE_FORMS).filter((kind) => known.has(kind)).map((kind) => {
    const option = el("option", kindLabel(kind));
    option.value = kind;
    return option;
  }));
  if (current && SOURCE_FORMS[current]) select.value = current;
}
function sourceStatus(item) {
  if (!item.last_fetch_at) return MindexI18n.msg("ui.topics_source_never");
  return MindexI18n.msg("ui.topics_source_last", { p0: MindexI18n.date(item.last_fetch_at), p1: item.last_count ?? 0 });
}
function renderSources() {
  const box = $("source-list");
  box.replaceChildren();
  if (!sources.items.length) box.append(el("p", MindexI18n.msg("ui.topics_sources_empty"), "muted source-empty"));
  for (const item of sources.items) {
    const row = el("div", null, "source-row" + (item.enabled ? "" : " is-disabled"));
    row.dataset.id = item.id;
    const head = el("div", null, "source-head");
    head.append(content("strong", item.name, "source-name"), el("span", kindLabel(item.kind), "template-tag"),
      el("span", MindexI18n.msg(item.enabled ? "ui.topics_source_on" : "ui.topics_source_off"), "muted"));
    const meta = el("div", sourceStatus(item), "muted source-meta");
    row.append(head, meta);
    const target = item.config?.url || item.config?.base_url;
    if (target) row.append(content("div", item.config.boards?.length ? `${target} · ${item.config.boards.join(", ")}` : target,
      "muted source-url"));
    if (item.last_error) row.append(el("div", MindexI18n.system(item.last_error), "warn-text source-error"));
    if (item.kind === "calendar") row.append(el("div", MindexI18n.msg("ui.topics_calendar_events", { p0: item.events ?? 0 }), "muted source-meta"));
    const actions = el("div", null, "source-actions");
    const button = (label, handler, cls = "quiet") => {
      const node = el("button", MindexI18n.msg(label), cls);
      node.type = "button";
      node.addEventListener("click", () => handler().catch(failed));
      return node;
    };
    actions.append(
      button("ui.topics_source_test", () => testSource(item)),
      button("ui.topics_source_edit", async () => editSource(item)),
      button(item.enabled ? "ui.topics_source_disable" : "ui.topics_source_enable", () => toggleSource(item)),
      button("ui.topics_source_remove", () => removeSource(item), "quiet danger")
    );
    row.append(actions);
    box.append(row);
  }
  renderCalendarImport();
  const on = sources.items.filter((item) => item.enabled).length;
  MindexI18n.setText($("sources-summary"), MindexI18n.msg("ui.topics_sources_summary", { p0: sources.items.length, p1: on }));
}
async function loadSources(profile) {
  $("sources-section").hidden = $("items-section").hidden = !profile;
  if (!profile) return;
  const data = await api(profileUrl(profile.id, "/sources"));
  sources.items = data.items;
  sources.kinds = data.kinds;
  renderKindOptions();
  renderSources();
  if (!sources.editing) resetSourceForm();
  // 新档案还没有数据源时默认展开，引导配置。
  if (!sources.items.length) $("sources-section").open = true;
}
function editSource(item) {
  sources.editing = item.id;
  $("sf-kind").value = item.kind;
  $("sf-kind").disabled = true;
  $("sf-name").value = item.name;
  $("sf-enabled").checked = item.enabled;
  MindexI18n.setText($("sf-title"), MindexI18n.msg("ui.topics_source_edit_title"));
  MindexI18n.setText($("sf-save"), MindexI18n.msg("ui.topics_source_save"));
  $("sf-cancel").hidden = false;
  showError("sf-error", null);
  renderSourceFields(item.kind, item.config);
  $("sources-section").open = true;
  $("sf-name").focus();
}
async function saveSource(event) {
  event.preventDefault();
  const kind = $("sf-kind").value;
  const name = $("sf-name").value.trim();
  if (!name) return showError("sf-error", MindexI18n.msg("ui.topics_source_name_required"));
  const body = { name, enabled: $("sf-enabled").checked, config: sourceFormConfig(kind) };
  const secret = $("sf-secret")?.value;
  if (secret) body.secret = secret;
  $("sf-save").disabled = true;
  try {
    if (sources.editing) await api(sourceUrl(sources.editing), "PATCH", body);
    else await api(profileUrl(page.profile.id, "/sources"), "POST", { kind, ...body });
    notice(MindexI18n.msg(sources.editing ? "ui.topics_source_saved" : "ui.topics_source_added"));
    sources.editing = null;
    await loadSources(page.profile);
  } catch (error) {
    showError("sf-error", error);
  } finally {
    $("sf-save").disabled = false;
  }
}
async function toggleSource(item) {
  await api(sourceUrl(item.id), "PATCH", { enabled: !item.enabled });
  await loadSources(page.profile);
}
async function removeSource(item) {
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.topics_source_remove_confirm", { p0: item.name })))) return;
  await api(sourceUrl(item.id), "DELETE");
  if (sources.editing === item.id) sources.editing = null;
  await loadSources(page.profile);
  notice(MindexI18n.msg("ui.topics_source_removed"));
}
async function testSource(item) {
  const box = $("source-test");
  box.hidden = false;
  box.replaceChildren(el("p", MindexI18n.msg("ui.topics_source_testing", { p0: item.name }), "muted"));
  const result = await api(sourceUrl(item.id, "/test"), "POST");
  box.replaceChildren();
  if (!result.ok) {
    box.append(el("p", MindexI18n.msg("ui.topics_source_test_failed", { p0: item.name, p1: MindexI18n.system(result.error) }), "warn-text"));
    return;
  }
  box.append(el("p", MindexI18n.msg("ui.topics_source_test_ok", { p0: item.name, p1: result.count }), "muted"));
  const list = el("ul", null, "item-list");
  for (const entry of result.items) list.append(itemRow(entry));
  box.append(list);
}
// ------------------------------------------------------------ 日历导入
const calendars = { samples: null };
function renderCalendarImport() {
  const items = sources.items.filter((item) => item.kind === "calendar");
  $("calendar-import").hidden = !items.length;
  const select = $("calendar-source");
  const current = select.value;
  select.replaceChildren(...items.map((item) => {
    const option = content("option", item.name);
    option.value = item.id;
    return option;
  }));
  if (items.some((item) => item.id === current)) select.value = current;
  if (items.length && !calendars.samples) {
    calendars.samples = [];
    api("/api/topics/calendar-samples").then((data) => {
      calendars.samples = data.items;
      renderCalendarSamples();
    }).catch(failed);
  }
  renderCalendarSamples();
}
function renderCalendarSamples() {
  const select = $("calendar-sample");
  const current = select.value;
  const none = el("option", MindexI18n.msg("ui.topics_calendar_sample_none"));
  none.value = "";
  select.replaceChildren(none, ...(calendars.samples || []).map((item) => {
    const option = el("option", MindexI18n.msg(`ui.topics_calendar_sample_${item.name}`, { p0: item.events }));
    option.value = item.name;
    return option;
  }));
  select.value = current;
}
async function importCalendar() {
  const source = $("calendar-source").value;
  if (!source) return;
  const file = $("calendar-file").files[0];
  const sample = $("calendar-sample").value;
  const mode = $("calendar-append").checked ? "append" : "replace";
  if (!file && !sample) return MindexI18n.setText($("calendar-status"), MindexI18n.msg("ui.topics_calendar_need_input"));
  $("calendar-run").disabled = true;
  try {
    let result;
    if (file) {
      const form = new FormData();
      form.append("file", file);
      form.append("mode", mode);
      result = await api(sourceUrl(source, "/calendar"), "POST", form);
    } else {
      result = await api(sourceUrl(source, `/calendar-sample/${enc(sample)}?mode=${mode}`), "POST");
    }
    $("calendar-file").value = "";
    MindexI18n.setText($("calendar-status"), MindexI18n.msg("ui.topics_calendar_done", { p0: result.imported, p1: result.total }));
    await loadSources(page.profile);
  } catch (error) {
    MindexI18n.setText($("calendar-status"), MindexI18n.system(error.message ?? error));
  } finally {
    $("calendar-run").disabled = false;
  }
}
$("calendar-run").addEventListener("click", () => importCalendar().catch(failed));
profileHooks.push((profile) => { sources.editing = null; $("source-test").hidden = true; return loadSources(profile); });

// ------------------------------------------------------------ 候选条目与立即抓取
function itemRow(entry) {
  const li = el("li", null, "item-row");
  const title = entry.url ? content("a", entry.title, "item-title") : content("span", entry.title, "item-title");
  if (entry.url) {
    title.href = entry.url;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
  }
  li.append(title);
  const when = entry.event_at || entry.published_at;
  const meta = [];
  if (when) meta.push(MindexI18n.date(when));
  if (entry.hot != null) meta.push(MindexI18n.msg("ui.topics_item_hot", { p0: entry.hot }));
  if (meta.length) li.append(el("span", meta.join(" · "), "muted item-meta"));
  if (entry.summary) li.append(content("p", entry.summary, "item-summary"));
  return li;
}
async function loadItems(profile) {
  if (!profile) return;
  const data = await api(profileUrl(profile.id, "/items?limit=30"));
  const list = $("item-list");
  list.replaceChildren(...data.items.map(itemRow));
  if (!data.items.length) list.append(el("li", MindexI18n.msg("ui.topics_items_empty"), "muted"));
  MindexI18n.setText($("items-summary"), MindexI18n.msg("ui.topics_items_summary", { p0: data.items.length }));
}
async function fetchNow() {
  if (!page.profile) return;
  $("fetch-run").disabled = true;
  MindexI18n.setText($("fetch-status"), MindexI18n.msg("ui.topics_fetch_running"));
  try {
    const result = await api(profileUrl(page.profile.id, "/fetch"), "POST");
    await Promise.all([loadSources(page.profile), loadItems(page.profile)]);
    MindexI18n.setText($("fetch-status"), MindexI18n.msg(result.errors ? "ui.topics_fetch_done_errors" : "ui.topics_fetch_done",
      { p0: result.new, p1: result.errors }));
  } catch (error) {
    MindexI18n.setText($("fetch-status"), "");
    failed(error);
  } finally {
    $("fetch-run").disabled = false;
  }
}
profileHooks.push(loadItems);

// ------------------------------------------------------------ 挖掘任务
const RUN_POLL_MS = 1500;
const ACTIVE = new Set(["queued", "running"]);
const STAGE_LABELS = { fetch: "ui.topics_stage_fetch", embed: "ui.topics_stage_embed", cluster: "ui.topics_stage_cluster",
  summarize: "ui.topics_stage_summarize", score: "ui.topics_stage_score", outline: "ui.topics_stage_outline",
  done: "ui.topics_stage_done" };
const runs = { current: null, timer: null };
const runUrl = (id, suffix = "") => `/api/topics/runs/${enc(id)}${suffix}`;

function renderRun() {
  const run = runs.current;
  const status = $("run-status");
  const meter = $("run-meter");
  const active = Boolean(run && ACTIVE.has(run.status));
  $("run-start").disabled = active;
  $("run-cancel").hidden = !active;
  $("run-retry").hidden = !run || !["failed", "interrupted", "cancelled"].includes(run.status);
  meter.hidden = !active;
  const warnings = $("run-warnings");
  warnings.replaceChildren();
  warnings.hidden = true;
  if (!run) return MindexI18n.setText(status, MindexI18n.msg("ui.topics_run_none"));
  const progress = run.progress || {};
  if (active) {
    const stage = STAGE_LABELS[progress.stage] ? MindexI18n.msg(STAGE_LABELS[progress.stage]) : "";
    MindexI18n.setText(status, MindexI18n.msg("ui.topics_run_active", { p0: stage, p1: progress.message ? MindexI18n.system(progress.message) : "" }));
    if (progress.total) {
      meter.max = progress.total;
      meter.value = progress.done || 0;
    } else meter.removeAttribute("value");
    return;
  }
  const result = run.result || {};
  if (run.status === "complete") {
    MindexI18n.setText(status, MindexI18n.msg("ui.topics_run_complete", { p0: MindexI18n.date(run.created_at), p1: result.cards ?? 0,
      p2: result.outlined ?? 0 }));
  } else {
    MindexI18n.setText(status, MindexI18n.msg("ui.topics_run_failed", { p0: MindexI18n.msg(`ui.topics_run_status_${run.status}`),
      p1: MindexI18n.system(run.error || "") }));
  }
  for (const warning of result.warnings || []) warnings.append(el("li", MindexI18n.system(warning)));
  warnings.hidden = !warnings.children.length;
}
function pollRun() {
  clearTimeout(runs.timer);
  if (!runs.current || !ACTIVE.has(runs.current.status)) return;
  runs.timer = setTimeout(async () => {
    try {
      const run = await api(runUrl(runs.current.id));
      const finished = !ACTIVE.has(run.status);
      runs.current = run;
      renderRun();
      if (finished) await Promise.all([loadCards(page.profile), loadGaps(page.profile), loadItems(page.profile),
        loadSchedule(page.profile)]);
      else pollRun();
    } catch (error) {
      failed(error);
    }
  }, RUN_POLL_MS);
}
async function loadRuns(profile) {
  clearTimeout(runs.timer);
  $("runs-bar").hidden = !profile;
  runs.current = null;
  if (!profile) return;
  const data = await api(profileUrl(profile.id, "/runs?limit=1"));
  runs.current = data.items[0] || null;
  renderRun();
  pollRun();
}
async function startRun() {
  if (!page.profile) return;
  $("run-start").disabled = true;
  try {
    runs.current = await api(profileUrl(page.profile.id, "/runs"), "POST", { skip_fetch: $("run-skip-fetch").checked });
    renderRun();
    pollRun();
  } catch (error) {
    $("run-start").disabled = false;
    throw error;
  }
}
async function manageRun(action) {
  if (!runs.current) return;
  runs.current = await api(runUrl(runs.current.id, `/${action}`), "POST");
  renderRun();
  pollRun();
}
profileHooks.push(loadRuns);

// ------------------------------------------------------------ 选题卡片
const SCORE_KEYS = [["timeliness", "ui.topics_weight_timeliness"], ["audience", "ui.topics_weight_audience"],
  ["coverage", "ui.topics_weight_coverage"], ["novelty", "ui.topics_weight_novelty"], ["cost", "ui.topics_weight_cost"]];
const cards = { items: [], counts: {}, selected: null, saved: null, templates: [], saving: false };
page.card = topicsQuery.get("card");
const cardUrl = (id, suffix = "") => `/api/topics/cards/${enc(id)}${suffix}`;
const percent = (value) => value == null ? "—" : `${Math.round(value * 100)}`;

function cardMatches(item, needle) {
  if (!needle) return true;
  return [item.title, item.angle, item.reason].join("\n").toLowerCase().includes(needle);
}
function renderCards() {
  const box = $("card-list");
  const needle = $("card-filter").value.trim().toLowerCase();
  box.replaceChildren();
  const visible = cards.items.filter((item) => cardMatches(item, needle));
  if (!visible.length) {
    box.append(el("p", MindexI18n.msg(cards.items.length ? "ui.topics_cards_filter_empty" : "ui.topics_cards_empty"),
      "muted topics-list-empty"));
  }
  const list = el("ul", null, "template-items");
  for (const item of visible) {
    const li = el("li");
    const entry = el("button", null, "job-item template-item topic-item");
    entry.type = "button";
    entry.dataset.id = item.id;
    if (cards.selected?.id === item.id) {
      entry.classList.add("active");
      entry.setAttribute("aria-current", "true");
    }
    const head = el("div", null, "topic-item-head");
    head.append(content("span", percent(item.total), "topic-score-badge"), content("span", item.title, "job-title"));
    const sub = el("div", null, "job-sub");
    const coverage = item.coverage || {};
    sub.append(el("span", coverage.total ? MindexI18n.msg("ui.topics_card_coverage_short", { p0: coverage.hits, p1: coverage.total })
      : MindexI18n.msg("ui.topics_card_coverage_none")));
    sub.append(el("span", MindexI18n.msg(item.outline ? "ui.topics_card_has_outline" : "ui.topics_card_no_outline")));
    if (item.status !== "new") sub.append(el("span", MindexI18n.msg(`ui.topics_status_${item.status}`)));
    entry.append(head, sub);
    entry.addEventListener("click", () => selectCard(item.id).catch(failed));
    li.append(entry);
    list.append(li);
  }
  if (visible.length) box.append(list);
  const status = $("card-status");
  for (const option of status.options) {
    const count = option.value === "all" ? Object.values(cards.counts).reduce((a, b) => a + b, 0) : cards.counts[option.value];
    option.dataset.count = count ?? "";
  }
}
function scoreBar(key, label, value) {
  const row = el("div", null, "topic-score");
  row.dataset.key = key;
  const name = el("span", MindexI18n.msg(label), "topic-score-name");
  const bar = el("span", null, "topic-score-bar");
  const fill = el("span", null, "topic-score-fill");
  fill.style.width = `${percent(value)}%`;
  bar.append(fill);
  row.append(name, bar, content("span", value == null ? "—" : Number(value).toFixed(2), "topic-score-value"));
  return row;
}
function renderCoverage(card) {
  const report = card.coverage || {};
  const list = $("coverage-list");
  list.replaceChildren();
  if (!report.total) {
    MindexI18n.setText($("coverage-summary"), report.reason ? MindexI18n.system(report.reason) : MindexI18n.msg("ui.topics_coverage_none"));
    return;
  }
  const summary = [MindexI18n.msg("ui.topics_coverage_summary", { p0: report.hits, p1: report.total })];
  if (report.reason) summary.push(MindexI18n.system(report.reason));
  MindexI18n.setText($("coverage-summary"), summary.join(" · "));
  for (const row of report.visuals || []) {
    const li = el("li", null, "coverage-row" + (row.hit ? " is-hit" : " is-gap"));
    const head = el("div", null, "coverage-head");
    head.append(el("span", MindexI18n.msg(row.hit ? "ui.topics_coverage_hit" : "ui.topics_coverage_gap"), "coverage-mark"),
      content("span", row.visual, "coverage-visual"));
    li.append(head);
    if (row.hit) {
      const clips = el("div", null, "coverage-clips");
      for (const clip of row.clips.slice(0, 3)) {
        const link = document.createElement("a");
        link.className = "coverage-clip";
        link.href = `/admin?source=${enc(clip.source_id)}&segment=${enc(clip.segment_id)}`;
        link.target = "_blank";
        link.rel = "noopener";
        link.dataset.segment = clip.segment_id;
        const image = document.createElement("img");
        image.loading = "lazy";
        image.src = clip.thumbnail_url;
        image.alt = "";
        const caption = content("span", `${clip.source_name || ""} ${Number(clip.start).toFixed(1)}–${Number(clip.end).toFixed(1)}s`
          + (clip.similarity != null ? ` · ${Number(clip.similarity).toFixed(2)}` : ""), "coverage-caption");
        link.append(image, caption);
        MindexI18n.setAttr(link, "aria-label", MindexI18n.msg("ui.topics_coverage_clip_aria", { p0: row.visual, p1: clip.segment_id }));
        clips.append(link);
      }
      li.append(clips);
    }
    list.append(li);
  }
}
function renderTemplates(selected) {
  const select = $("card-template");
  select.replaceChildren(...cards.templates.filter((item) => item.kind === "outline").map((item) => {
    const option = el("option", templateLabel(item));
    option.value = item.id;
    return option;
  }));
  select.value = cards.templates.some((item) => item.id === selected) ? selected : "base";
}
function cardFormValues() {
  return { outline: $("card-outline").value, template_id: $("card-template").value };
}
function cardDirty() {
  return Boolean(cards.selected && cards.saved) && JSON.stringify(cardFormValues()) !== JSON.stringify(cards.saved);
}
function updateCardForm() {
  const changed = cardDirty();
  $("card-dirty").hidden = !changed;
  $("card-save").disabled = $("card-revert").disabled = !changed || cards.saving;
  MindexI18n.setText($("card-outline-count"), MindexI18n.msg("ui.topics_outline_count", { p0: $("card-outline").value.length }));
}
function fillCardForm(card) {
  $("card-outline").value = card.outline || "";
  renderTemplates(card.template_id);
  cards.saved = cardFormValues();
  updateCardForm();
}
function renderCard() {
  const card = cards.selected;
  $("card-empty").hidden = Boolean(card);
  $("card-view").hidden = !card;
  if (!card) return;
  $("card-title").textContent = card.title;
  const tags = $("card-tags");
  tags.replaceChildren(el("span", MindexI18n.msg("ui.topics_card_total", { p0: percent(card.total) }), "template-tag topic-total"),
    el("span", MindexI18n.msg(`ui.topics_status_${card.status}`), "template-tag"));
  $("card-angle").textContent = card.angle || "";
  $("card-reason").textContent = card.reason || "";
  $("card-scores").replaceChildren(...SCORE_KEYS.map(([key, label]) => scoreBar(key, label, card.scores?.[key])));
  renderCoverage(card);
  const adopted = card.status === "adopted";
  MindexI18n.setText($("card-dismiss"), MindexI18n.msg(card.status === "dismissed" ? "ui.topics_card_restore" : "ui.topics_card_dismiss"));
  $("card-dismiss").hidden = adopted;
  $("card-items").replaceChildren(...(card.items || []).map((entry) => {
    const row = itemRow(entry);
    if (entry.source_name) row.prepend(content("span", entry.source_name, "template-tag item-source"));
    return row;
  }));
  const warnings = $("card-warnings");
  warnings.hidden = !(card.warnings || []).length;
  MindexI18n.setText(warnings, (card.warnings || []).map((w) => MindexI18n.system(w)).join("; "));
  showError("card-error", null);
  for (const hook of cardHooks) hook(card);
}
const cardHooks = [];
function confirmCardDiscard() {
  return !cardDirty() || window.confirm(MindexI18n.render(MindexI18n.msg("ui.topics_card_discard_confirm")));
}
async function selectCard(id, { force = false } = {}) {
  if (!force && cards.selected?.id === id) return;
  if (!force && !confirmCardDiscard()) return;
  cards.selected = id ? await api(cardUrl(id)) : null;
  page.card = cards.selected?.id || null;
  if (cards.selected) fillCardForm(cards.selected);
  else cards.saved = null;
  renderCards();
  renderCard();
  syncUrl();
}
async function loadCards(profile) {
  if (!profile) {
    cards.items = [];
    cards.selected = cards.saved = null;
    return;
  }
  if (!cards.templates.length) cards.templates = (await api("/api/studio/templates")).items;
  const data = await api(profileUrl(profile.id, `/cards?status=${enc($("card-status").value)}&limit=200`));
  cards.items = data.items;
  cards.counts = data.counts;
  const keep = [cards.selected?.id, page.card].find((id) => id && cards.items.some((item) => item.id === id));
  if (keep) await selectCard(keep, { force: !cardDirty() });
  else if (!cardDirty()) {
    // 未指定选题时默认选中评分最高的一条，右侧直接展示其细节。
    const best = [...cards.items].sort((a, b) => (b.total ?? -Infinity) - (a.total ?? -Infinity))[0];
    if (best) await selectCard(best.id, { force: true });
    else {
      cards.selected = cards.saved = null;
      page.card = null;
      renderCard();
      syncUrl();
    }
  }
  renderCards();
}
async function saveCard() {
  if (!cardDirty() || cards.saving) return;
  cards.saving = true;
  updateCardForm();
  try {
    cards.selected = await api(cardUrl(cards.selected.id), "PATCH", cardFormValues());
    fillCardForm(cards.selected);
    replaceCardItem(cards.selected);
    renderCard();
    notice(MindexI18n.msg("ui.topics_card_saved"));
  } catch (error) {
    showError("card-error", error);
  } finally {
    cards.saving = false;
    updateCardForm();
  }
}
function replaceCardItem(card) {
  cards.items = cards.items.map((item) => (item.id === card.id ? { ...card, coverage: item.coverage && card.coverage
    ? { ...item.coverage, hits: card.coverage.hits, total: card.coverage.total } : card.coverage } : item));
  renderCards();
}
async function cardAction(button, request, message) {
  button.disabled = true;
  try {
    const card = await request();
    cards.selected = card;
    if (!cardDirty() || button.id === "card-outline-run") fillCardForm(card);
    renderCard();
    await loadCards(page.profile);
    if (message) notice(MindexI18n.msg(message));
  } finally {
    button.disabled = false;
  }
}
const rescore = async () => {
  await cardAction($("card-score"), () => api(cardUrl(cards.selected.id, "/score"), "POST"), "ui.topics_card_rescored");
  await loadGaps(page.profile);
};
async function writeOutline() {
  if (cards.selected?.outline && cards.saved && !window.confirm(MindexI18n.render(MindexI18n.msg("ui.topics_outline_overwrite_confirm"))))
    return;
  MindexI18n.setText($("card-outline-count"), MindexI18n.msg("ui.topics_outline_running"));
  await cardAction($("card-outline-run"), () => api(cardUrl(cards.selected.id, "/outline"), "POST"), "ui.topics_outline_done");
}
const toggleDismiss = () => cardAction($("card-dismiss"), () => api(cardUrl(cards.selected.id), "PATCH",
  { status: cards.selected.status === "dismissed" ? "new" : "dismissed" }));
profileHooks.push((profile) => {
  if (page.profile?.id !== profile?.id) page.card = null;
  return loadCards(profile);
});

// ------------------------------------------------------------ 建创作任务
function renderJobBar(card) {
  const adopted = card.status === "adopted" && card.job_id;
  const link = $("card-job-link");
  link.hidden = !adopted;
  if (adopted) link.href = `/?job=${enc(card.job_id)}`;
  MindexI18n.setText($("card-job"), MindexI18n.msg(adopted ? "ui.topics_card_create_job_again" : "ui.topics_card_create_job"));
  $("card-job").className = adopted ? "quiet" : "submit";
  MindexI18n.setText($("card-job-note"), adopted ? MindexI18n.msg("ui.topics_card_job_note", { p0: card.job_id })
    : card.outline ? MindexI18n.msg("ui.topics_card_job_hint") : MindexI18n.msg("ui.topics_card_job_no_outline"));
}
cardHooks.push(renderJobBar);
async function createJob() {
  const card = cards.selected;
  if (!card) return;
  if (cardDirty()) await saveCard();
  if (cardDirty()) return;
  const again = card.status === "adopted";
  if (again && !window.confirm(MindexI18n.render(MindexI18n.msg("ui.topics_card_create_job_again_confirm")))) return;
  $("card-job").disabled = true;
  try {
    const result = await api(cardUrl(card.id, "/job"), "POST", { force: again, language: MindexI18n.language });
    location.href = `/?job=${enc(result.job_id)}`;
  } finally {
    $("card-job").disabled = false;
  }
}
$("card-job").addEventListener("click", () => createJob().catch(failed));

// ------------------------------------------------------------ 素材采集清单
async function loadGaps(profile) {
  $("gaps-section").hidden = !profile;
  if (!profile) return;
  const status = $("gaps-status").value;
  $("gaps-export").href = profileUrl(profile.id, `/gaps.csv?status=${enc(status)}`);
  const data = await api(profileUrl(profile.id, `/gaps?status=${enc(status)}`));
  const list = $("gap-list");
  list.replaceChildren();
  if (!data.items.length) list.append(el("li", MindexI18n.msg("ui.topics_gaps_empty"), "muted"));
  for (const gap of data.items) {
    const li = el("li", null, "gap-row" + (gap.status === "done" ? " is-done" : ""));
    li.dataset.id = gap.id;
    const head = el("div", null, "gap-head");
    head.append(content("strong", gap.visual, "gap-visual"),
      el("span", MindexI18n.msg("ui.topics_gap_count", { p0: gap.count }), "template-tag"));
    const action = el("button", MindexI18n.msg(gap.status === "done" ? "ui.topics_gap_reopen" : "ui.topics_gap_mark_done"), "quiet");
    action.type = "button";
    action.addEventListener("click", async () => {
      action.disabled = true;
      try {
        await api(`/api/topics/gaps/${enc(gap.id)}`, "PATCH", { status: gap.status === "done" ? "open" : "done" });
        await loadGaps(page.profile);
      } catch (error) {
        action.disabled = false;
        failed(error);
      }
    });
    head.append(action);
    li.append(head);
    if (gap.cards.length) {
      const links = el("div", null, "gap-cards muted");
      for (const card of gap.cards.slice(0, 5)) {
        const link = content("a", card.title || card.id, "gap-card");
        link.href = `/studio/topics?profile=${enc(profile.id)}&card=${enc(card.id)}`;
        link.addEventListener("click", (event) => {
          event.preventDefault();
          selectCardAnywhere(card.id).catch(failed);
        });
        links.append(link);
      }
      li.append(links);
    }
    list.append(li);
  }
  const open = status === "open" ? data.items.length : null;
  MindexI18n.setText($("gaps-summary"), open == null ? "" : MindexI18n.msg("ui.topics_gaps_summary", { p0: open }));
}
async function selectCardAnywhere(id) {
  if (!cards.items.some((item) => item.id === id)) {
    $("card-status").value = "all";
    await loadCards(page.profile);
  }
  await selectCard(id);
  $("card-view").scrollIntoView({ block: "start", behavior: "smooth" });
}
profileHooks.push(loadGaps);
$("gaps-status").addEventListener("change", () => loadGaps(page.profile).catch(failed));

// ------------------------------------------------------------ 定时状态
async function loadSchedule(profile) {
  const box = $("schedule-status");
  if (!profile) return MindexI18n.setText(box, "");
  const data = await api("/api/topics/schedule");
  const item = data.items.find((entry) => entry.profile_id === profile.id);
  if (!item || !item.enabled) return MindexI18n.setText(box, MindexI18n.msg("ui.topics_schedule_off_hint"));
  const parts = [MindexI18n.msg("ui.topics_schedule_summary", { p0: item.time, p1: item.count })];
  if (!data.scheduler) parts.push(MindexI18n.msg("ui.topics_schedule_inactive"));
  else if (item.next_run) parts.push(MindexI18n.msg("ui.topics_schedule_next", { p0: MindexI18n.date(item.next_run) }));
  parts.push(MindexI18n.msg("ui.topics_schedule_today", { p0: item.today.cards }));
  // 桌面版：关闭窗口后留在托盘/菜单栏，定时照常执行（只经 window.MindexDesktop 特性检测，Web 端不显示）。
  if (data.scheduler && globalThis.window?.MindexDesktop?.background)
    parts.push(MindexI18n.msg("ui.topics_schedule_background"));
  MindexI18n.setText(box, parts.join(" · "));
}
profileHooks.push(loadSchedule);

// ------------------------------------------------------------ 事件与启动
$("run-start").addEventListener("click", () => startRun().catch(failed));
$("run-cancel").addEventListener("click", () => manageRun("cancel").catch(failed));
$("run-retry").addEventListener("click", () => manageRun("retry").catch(failed));
$("card-status").addEventListener("change", () => loadCards(page.profile).catch(failed));
$("card-filter").addEventListener("input", renderCards);
$("card-outline").addEventListener("input", updateCardForm);
$("card-template").addEventListener("change", updateCardForm);
$("card-save").addEventListener("click", () => saveCard().catch(failed));
$("card-revert").addEventListener("click", () => { fillCardForm(cards.selected); showError("card-error", null); });
$("card-score").addEventListener("click", () => rescore().catch(failed));
$("card-outline-run").addEventListener("click", () => writeOutline().catch(failed));
$("card-dismiss").addEventListener("click", () => toggleDismiss().catch(failed));
$("card-outline").addEventListener("keydown", (event) => {
  if (event.key === "s" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    saveCard().catch(failed);
  }
});
window.addEventListener("beforeunload", (event) => {
  if (!cardDirty()) return;
  event.preventDefault();
  event.returnValue = "";
});
focusHooks.push(async (focus, passive) => {
  if (focus.card_id && !passive && focus.card_id !== cards.selected?.id) {
    if (!cards.items.some((item) => item.id === focus.card_id)) {
      $("card-status").value = "all";
      await loadCards(page.profile);
    }
    await selectCard(focus.card_id).catch(() => {});
  }
  if (focus.section === "card" || focus.card_id) {
    window.MindexAgent?.highlight($("card-view"));
    return true;
  }
  return false;
});
$("sf-kind").addEventListener("change", () => renderSourceFields($("sf-kind").value));
$("source-form").addEventListener("submit", (event) => saveSource(event).catch(failed));
$("sf-cancel").addEventListener("click", resetSourceForm);
$("fetch-run").addEventListener("click", () => fetchNow().catch(failed));
$("profile-select").addEventListener("change", (event) => selectProfile(event.target.value).catch(failed));
$("profile-new").addEventListener("click", () => openProfileDialog(null));
$("profile-new-empty").addEventListener("click", () => openProfileDialog(null));
$("profile-edit").addEventListener("click", () => page.profile && openProfileDialog(page.profile));
$("profile-delete").addEventListener("click", () => deleteProfile().catch(failed));
$("profile-close").addEventListener("click", () => $("profile-dialog").close());
$("profile-form").addEventListener("submit", (event) => saveProfile(event).catch(failed));
for (const key of WEIGHT_KEYS) $(`pf-w-${key}`).addEventListener("input", updateWeightSum);
$("reload").addEventListener("click", async () => {
  $("reload").disabled = true;
  try {
    await loadProfiles();
    notice(MindexI18n.msg("ui.topics_reloaded"));
  } catch (error) {
    failed(error);
  } finally {
    $("reload").disabled = false;
  }
});
window.addEventListener("mindex:languagechange", () => {
  renderSavedMaterialScope();
  renderProfiles();
  renderKindOptions();
  renderSources();
  renderRun();
  renderCards();
  if (cards.selected) {
    const values = cardFormValues();
    renderCard();
    renderTemplates(values.template_id);
    updateCardForm();
  }
});
// 助手或其他来源改动了选题数据：重新读取档案（并经钩子刷新各分区）。
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
  if (detail.groups) {
    Promise.all([topicGroups.refresh().then(() => topicScopeLink.refresh()), loadCards(page.profile),
      page.profile ? loadSavedMaterialScope(page.profile) : Promise.resolve()]).catch(failed);
    return;
  }
  if (detail.domain !== "topics") return;
  loadProfiles(detail.profile_id && page.profiles.some((p) => p.id === detail.profile_id) ? page.profile?.id : undefined)
    .catch(() => {});
});
window.MindexAgentContext = () => ({ page: "topics", profile_id: page.profile?.id || null, card_id: page.card || null,
  source_group_ids: topicGroups.values(), source_ids: topicSourceSelection });
// 助手定位：切换档案、打开卡片并高亮对应分区；建出创作任务时回到创作页。
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  if (focus.page === "studio" && focus.job_id) {
    if (!passive) location.href = `/?job=${enc(focus.job_id)}`;
    return true;
  }
  if (focus.profile_id && focus.profile_id !== page.profile?.id && !passive) {
    if (!page.profiles.some((item) => item.id === focus.profile_id)) await loadProfiles(focus.profile_id);
    else await selectProfile(focus.profile_id);
  }
  for (const hook of focusHooks) if (await hook(focus, passive)) return true;
  const target = document.querySelector(`[data-capability="${CSS.escape(focus.section || "profiles")}"]`) || $("topics-bar");
  window.MindexAgent?.highlight(target);
  return true;
};
if (page.job) {
  $("studio-jobs-tab").href = `/?job=${enc(page.job)}`;
  $("studio-templates-tab").href = `/studio/templates?job=${enc(page.job)}`;
}
loadProfiles().catch(failed);
