/* 创作 · 品牌模板管理页（/studio/brands）：浏览品牌模板、编辑自定义模板的字幕样式、文字层样式、片头片尾与水印。
   经典脚本，依赖 shared/*（MindexI18n、MindexApi、MindexFeedback）与 studio-template-text.js（内置模板的名称与说明按语言显示）。
   内置模板只读，复制为自定义模板后才能修改。
   全部经 /api/studio/brands 与 /api/studio/brand-assets（与 CLI、智能助手、MCP 同一接口）；Web 与桌面版共用。 */
"use strict";
const query = new URLSearchParams(location.search);
const page = { items: [], selected: null, draft: null, saved: null, job: query.get("job") };
const $ = (id) => document.getElementById(id);
const api = (url, method = "GET", body) => MindexApi.json(url, method, body);
const brandUrl = (id, suffix = "") => `/api/studio/brands/${encodeURIComponent(id)}${suffix}`;
const SUBTITLE_PRESETS = {
  classic: {},
  outline: { bold: true, outline: 2.5, shadow: 0 },
  box: { box: true, outline: 0, shadow: 0, box_opacity: 0.6 },
  highlight: { bold: true, outline: 2.0, shadow: 0.5, highlight: "#FFD400" }
};
const SUBTITLE_DEFAULT = { position: "bottom", font_size: null, bold: false, primary: "#FFFFFF", outline_color: "#000000",
  outline: 1.0, shadow: 0.5, box: false, box_color: "#000000", box_opacity: 0.6, highlight: "#FFD400", margin: null };
const OVERLAY_DEFAULT = { font: null, primary: "#FFFFFF", accent: "#FFB000", background: "#000000", background_opacity: 0.55 };
const POSITIONS = ["top-left", "top-right", "bottom-left", "bottom-right"];

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
}
const editable = () => Boolean(page.selected && !page.selected.builtin);
const dirty = () => editable() && page.saved !== null && JSON.stringify(page.draft) !== page.saved;
function control(label, value, type, onChange, extra = {}) {
  const wrap = el("label", label, "form-field");
  const input = document.createElement(extra.choices ? "select" : "input");
  if (extra.choices) for (const [v, text] of extra.choices) {
    const option = el("option", text);
    option.value = v;
    input.append(option);
  } else input.type = type;
  if (type === "checkbox") input.checked = Boolean(value);else input.value = value ?? "";
  for (const key of ["min", "max", "step"]) if (extra[key] != null) input[key] = String(extra[key]);
  input.disabled = !editable();
  input.addEventListener(type === "checkbox" || extra.choices ? "change" : "input", () => {
    onChange(type === "checkbox" ? input.checked : type === "number" ? input.value === "" ? null : Number(input.value) : input.value);
    updateDirty();
  });
  wrap.append(input);
  return wrap;
}
function grid(...nodes) {
  const box = el("div", "", "template-fields");
  box.append(...nodes);
  return box;
}

// ------------------------------------------------------------ 列表
function renderList() {
  const box = $("brand-list");
  box.replaceChildren();
  for (const [title, items] of [["ui.brands_group_builtin", page.items.filter((item) => item.builtin)],
    ["ui.brands_group_user", page.items.filter((item) => !item.builtin)]]) {
    const group = el("div", null, "template-group");
    const heading = el("h3", MindexI18n.msg(title), "template-group-title");
    heading.append(el("span", String(items.length), "muted"));
    group.append(heading);
    const list = el("ul", null, "template-items");
    for (const item of items) {
      const li = el("li");
      const entry = el("button", null, "job-item template-item");
      entry.type = "button";
      entry.dataset.id = item.id;
      if (page.selected?.id === item.id) entry.classList.add("active");
      entry.append(el("div", brandName(item), "job-title"));
      const marks = ["intro", "outro", "watermark"].filter((key) => item[key]).map((key) => MindexI18n.msg(`ui.brands_asset_${key}`));
      entry.append(el("div", marks.join(" · ") || MindexI18n.msg("ui.brands_no_assets"), "job-sub"));
      entry.addEventListener("click", () => select(item.id).catch(failed));
      li.append(entry);
      list.append(li);
    }
    if (items.length) group.append(list);else group.append(el("p", MindexI18n.msg("ui.brands_user_empty"), "muted template-group-empty"));
    box.append(group);
  }
}

// ------------------------------------------------------------ 详情
function editableCopy(item) {
  return JSON.parse(JSON.stringify({ name: item.name, description: item.description || "", subtitle: item.subtitle,
    overlay: item.overlay, intro: item.intro, outro: item.outro, watermark: item.watermark }));
}
function renderSubtitle() {
  const box = $("brand-subtitle");
  box.replaceChildren(el("h3", MindexI18n.msg("ui.brands_subtitle_heading")));
  const value = page.draft.subtitle;
  const presets = el("div", "", "detail-actions");
  presets.append(el("span", MindexI18n.msg("ui.brands_subtitle_preset"), "muted"));
  for (const name of Object.keys(SUBTITLE_PRESETS)) {
    const b = el("button", MindexI18n.msg(`ui.plan_subtitle_style_${name}`), "quiet");
    b.type = "button";
    b.disabled = !editable();
    b.addEventListener("click", () => {page.draft.subtitle = { ...SUBTITLE_DEFAULT, ...SUBTITLE_PRESETS[name] };renderSubtitle();updateDirty();});
    presets.append(b);
  }
  const none = el("button", MindexI18n.msg("ui.brands_subtitle_none"), "quiet");
  none.type = "button";
  none.disabled = !editable();
  none.addEventListener("click", () => {page.draft.subtitle = null;renderSubtitle();updateDirty();});
  presets.append(none);
  box.append(presets);
  if (!value) {
    box.append(el("p", MindexI18n.msg("ui.brands_subtitle_follow"), "muted"));
    return;
  }
  const set = (key) => (v) => {value[key] = v;};
  box.append(grid(
    control(MindexI18n.msg("ui.brands_position"), value.position, "text", set("position"),
    { choices: ["bottom", "middle", "top"].map((v) => [v, MindexI18n.msg(`ui.brands_position_${v}`)]) }),
    control(MindexI18n.msg("ui.brands_font_size"), value.font_size, "number", set("font_size"), { min: 3, max: 40, step: 0.5 }),
    control(MindexI18n.msg("ui.brands_primary"), value.primary, "color", set("primary")),
    control(MindexI18n.msg("ui.brands_highlight"), value.highlight, "color", set("highlight")),
    control(MindexI18n.msg("ui.brands_outline_color"), value.outline_color, "color", set("outline_color")),
    control(MindexI18n.msg("ui.brands_outline"), value.outline, "number", set("outline"), { min: 0, max: 10, step: 0.5 }),
    control(MindexI18n.msg("ui.brands_shadow"), value.shadow, "number", set("shadow"), { min: 0, max: 10, step: 0.5 }),
    control(MindexI18n.msg("ui.brands_bold"), value.bold, "checkbox", set("bold")),
    control(MindexI18n.msg("ui.brands_box"), value.box, "checkbox", set("box")),
    control(MindexI18n.msg("ui.brands_box_color"), value.box_color, "color", set("box_color")),
    control(MindexI18n.msg("ui.brands_box_opacity"), value.box_opacity, "number", set("box_opacity"), { min: 0, max: 1, step: 0.05 })));
}
function renderOverlay() {
  const box = $("brand-overlay");
  box.replaceChildren(el("h3", MindexI18n.msg("ui.brands_overlay_heading")));
  const value = page.draft.overlay || (page.draft.overlay = { ...OVERLAY_DEFAULT });
  const set = (key) => (v) => {value[key] = v;};
  box.append(grid(
    control(MindexI18n.msg("ui.brands_primary"), value.primary, "color", set("primary")),
    control(MindexI18n.msg("ui.brands_accent"), value.accent, "color", set("accent")),
    control(MindexI18n.msg("ui.brands_background"), value.background, "color", set("background")),
    control(MindexI18n.msg("ui.brands_background_opacity"), value.background_opacity, "number", set("background_opacity"),
    { min: 0, max: 1, step: 0.05 })));
}
function assetUrl(asset) {
  return `/api/studio/brand-assets/${asset.sha256}${asset.suffix}`;
}
async function uploadAsset(accept) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  const file = await new Promise((resolve) => {
    input.addEventListener("change", () => resolve(input.files?.[0] || null), { once: true });
    input.click();
  });
  if (!file) return null;
  const form = new FormData();
  form.append("file", file, file.name);
  notice(MindexI18n.msg("ui.brands_uploading"));
  const result = await api("/api/studio/brand-assets", "POST", form);
  return { sha256: result.sha256, suffix: result.suffix, kind: result.kind, name: result.name };
}
function assetRow(key, kind) {
  const row = el("div", "", "brand-asset");
  row.dataset.brandAsset = key;
  row.append(el("h4", MindexI18n.msg(`ui.brands_asset_${key}`)));
  const value = page.draft[key];
  if (value) {
    const preview = document.createElement(kind === "image" ? "img" : "video");
    preview.src = assetUrl(value.asset);
    if (kind === "video") {preview.controls = true;preview.preload = "metadata";} else preview.alt = value.asset.name || "";
    preview.className = "brand-preview";
    row.append(preview, el("p", value.asset.name || value.asset.sha256.slice(0, 12), "muted"));
  } else row.append(el("p", MindexI18n.msg("ui.brands_asset_none"), "muted"));
  const actions = el("div", "", "detail-actions");
  const upload = el("button", MindexI18n.msg(value ? "ui.brands_asset_replace" : "ui.brands_asset_upload"), "quiet");
  upload.type = "button";
  upload.disabled = !editable();
  upload.addEventListener("click", async () => {
    try {
      const asset = await uploadAsset(kind === "image" ? "image/png,image/jpeg,image/webp" : "video/mp4,video/quicktime,video/webm,video/x-matroska");
      if (!asset) return;
      page.draft[key] = kind === "image" ? { position: "top-right", opacity: 0.8, scale: 0.12, margin: 0.03, include_bookends: false,
        ...(page.draft[key] || {}), asset } : { volume: 1, ...(page.draft[key] || {}), asset };
      renderAssets();
      updateDirty();
      notice(MindexI18n.msg("ui.brands_uploaded"));
    } catch (error) {failed(error);}
  });
  actions.append(upload);
  if (value) {
    const remove = el("button", MindexI18n.msg("ui.brands_asset_remove"), "quiet");
    remove.type = "button";
    remove.disabled = !editable();
    remove.addEventListener("click", () => {page.draft[key] = null;renderAssets();updateDirty();});
    actions.append(remove);
  }
  row.append(actions);
  return row;
}
function renderAssets() {
  const bookends = $("brand-bookends");
  bookends.replaceChildren(el("h3", MindexI18n.msg("ui.brands_bookends_heading")), assetRow("intro", "video"), assetRow("outro", "video"));
  for (const key of ["intro", "outro"]) {
    const value = page.draft[key];
    if (value) bookends.querySelector(`[data-brand-asset="${key}"]`).append(control(MindexI18n.msg("ui.brands_volume"),
      value.volume, "number", (v) => {value.volume = v ?? 1;}, { min: 0, max: 1, step: 0.05 }));
  }
  const box = $("brand-watermark");
  box.replaceChildren(el("h3", MindexI18n.msg("ui.brands_watermark_heading")), assetRow("watermark", "image"));
  const mark = page.draft.watermark;
  if (mark) {
    const set = (key) => (v) => {mark[key] = v;};
    box.append(grid(
      control(MindexI18n.msg("ui.brands_position"), mark.position, "text", set("position"),
      { choices: POSITIONS.map((v) => [v, MindexI18n.msg(`ui.brands_corner_${v.replace("-", "_")}`)]) }),
      control(MindexI18n.msg("ui.brands_opacity"), mark.opacity, "number", set("opacity"), { min: 0.05, max: 1, step: 0.05 }),
      control(MindexI18n.msg("ui.brands_scale"), mark.scale, "number", set("scale"), { min: 0.02, max: 0.5, step: 0.01 }),
      control(MindexI18n.msg("ui.brands_margin"), mark.margin, "number", set("margin"), { min: 0, max: 0.2, step: 0.01 }),
      control(MindexI18n.msg("ui.brands_include_bookends"), mark.include_bookends, "checkbox", set("include_bookends"))));
  }
}
function renderDetail() {
  const item = page.selected;
  $("brand-empty").hidden = Boolean(item);
  $("brand-view").hidden = !item;
  if (!item) return;
  MindexI18n.setText($("brand-title"), brandName(item));
  MindexI18n.setText($("brand-tags"), MindexI18n.msg(item.builtin ? "ui.brands_builtin" : "ui.brands_user"));
  MindexI18n.setText($("brand-note"), MindexI18n.msg(item.builtin ? "ui.brands_builtin_note" : "ui.brands_user_note"));
  $("brand-delete").hidden = item.builtin;
  // 内置模板的名称与说明按当前语言显示（只读）；自定义模板显示用户填写的原文。
  $("bf-name").value = item.builtin ? MindexI18n.render(brandName(item)) : page.draft.name;
  $("bf-description").value = item.builtin ? MindexI18n.render(brandDescription(item)) : page.draft.description;
  for (const id of ["bf-name", "bf-description", "brand-save", "brand-revert"]) $(id).disabled = !editable();
  renderSubtitle();
  renderOverlay();
  renderAssets();
  MindexI18n.setText($("brand-meta"), MindexI18n.msg("ui.brands_meta", { p0: item.id, p1: item.version, p2: item.hash.slice(0, 12) }));
  updateDirty();
}
function updateDirty() {
  if (!page.draft) return;
  page.draft.name = $("bf-name").value;
  page.draft.description = $("bf-description").value;
  $("brand-dirty").hidden = !dirty();
}
function confirmDiscard() {
  return !dirty() || window.confirm(MindexI18n.render(MindexI18n.msg("ui.templates_discard_confirm")));
}
async function select(id) {
  if (page.selected?.id !== id && !confirmDiscard()) return;
  page.selected = await api(brandUrl(id));
  page.draft = editableCopy(page.selected);
  page.saved = editable() ? JSON.stringify(page.draft) : null;
  history.replaceState(null, "", `/studio/brands?id=${encodeURIComponent(id)}${page.job ? `&job=${encodeURIComponent(page.job)}` : ""}`);
  renderList();
  renderDetail();
}
async function load(preferred) {
  page.items = (await api("/api/studio/brands")).items;
  renderList();
  const id = preferred || page.selected?.id;
  if (id && page.items.some((item) => item.id === id)) await select(id);
}
async function save(event) {
  event?.preventDefault();
  if (!editable()) return;
  updateDirty();
  $("brand-error").hidden = true;
  try {
    const body = { ...page.draft, name: page.draft.name.trim(), description: page.draft.description.trim() };
    page.selected = await api(brandUrl(page.selected.id), "PUT", body);
  } catch (error) {
    MindexI18n.setText($("brand-error"), error.message);
    $("brand-error").hidden = false;
    throw error;
  }
  page.draft = editableCopy(page.selected);
  page.saved = JSON.stringify(page.draft);
  page.items = page.items.map((item) => (item.id === page.selected.id ? page.selected : item));
  renderList();
  renderDetail();
  notice(MindexI18n.msg("ui.brands_saved"));
}
// 复制后直接选中新模板并聚焦名称输入框（桌面版 Electron 不支持 window.prompt，不弹输入框）。
async function copy() {
  if (!page.selected || !confirmDiscard()) return;
  const name = MindexI18n.render(MindexI18n.msg("ui.brands_copy_name", { p0: brandName(page.selected) })).slice(0, 40);
  const item = await api(brandUrl(page.selected.id, "/copy"), "POST", { name });
  page.saved = null;
  await load(item.id);
  notice(MindexI18n.msg("ui.brands_copied"));
  $("bf-name").focus();
  $("bf-name").select();
}
async function remove() {
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.brands_delete_confirm", { p0: brandName(page.selected) })))) return;
  await api(brandUrl(page.selected.id), "DELETE");
  page.selected = page.draft = page.saved = null;
  await load();
  renderDetail();
  notice(MindexI18n.msg("ui.brands_deleted"));
}
function revert() {
  page.draft = editableCopy(page.selected);
  renderDetail();
}

const failed = (error) => notice(error.message, true);
$("brand-form").addEventListener("submit", (event) => save(event).catch(() => {}));
$("bf-name").addEventListener("input", updateDirty);
$("bf-description").addEventListener("input", updateDirty);
$("brand-revert").addEventListener("click", revert);
$("brand-copy").addEventListener("click", () => copy().catch(failed));
$("brand-delete").addEventListener("click", () => remove().catch(failed));
$("reload").addEventListener("click", () => {
  if (confirmDiscard()) {page.saved = null;load().then(() => notice(MindexI18n.msg("ui.brands_reloaded"))).catch(failed);}
});
window.addEventListener("beforeunload", (event) => {
  if (!dirty()) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("mindex:languagechange", () => {renderList();if (page.selected) renderDetail();});
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
  if (detail.domain === "studio" && detail.brands && !dirty()) load().catch(() => {});
});
window.MindexAgentContext = () => ({ page: "studio", section: "brands", brand_id: page.selected?.id || null });
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  if (focus.job_id && !passive) location.href = `/?job=${encodeURIComponent(focus.job_id)}`;
  return true;
};
if (page.job) $("studio-jobs-tab").href = `/?job=${encodeURIComponent(page.job)}`;
load(query.get("id") || "plain").catch(failed);
