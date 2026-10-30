/* 创作页 · 交付：多画幅导出（快速派生 / 重新渲染）与“构建全部画幅”。后续的平台封面、发布元数据与发布包也在这里。
   全部经方案 PATCH 与构建接口（与 CLI studio export、智能助手 set_exports / run_build 同一接口）；Web 与桌面版共用。
   按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
const DELIVERY_CANVASES = ["16:9", "9:16", "1:1"];
function exportsOf(p) {
  return p.options.exports || [];
}
function finalOf(p, key) {
  return ver((p.finals || {})[key]);
}
function renderDelivery() {
  const box = $("delivery-body");
  if (!box) return;
  const p = plan();
  const key = JSON.stringify([scopeKey(), p.options.canvas, p.options.exports, p.options.publish, p.finals, p.final,
  state.data.revision,
  Object.values(state.data.plans).filter((item) => item.variant_of?.plan === p.id).map((item) => [item.id, item.final])]);
  if (state.deliveryKey === key) return;
  state.deliveryKey = key;
  pauseMedia(box);
  box.replaceChildren();
  renderExports(box, p);
  renderPublish(box, p);
}
function renderExports(box, p) {
  const section = el("div", "", "delivery-exports");
  section.dataset.deliveryExports = "true";
  section.append(el("h3", MindexI18n.msg("ui.delivery_exports_heading")));
  section.append(el("p", MindexI18n.msg("ui.delivery_exports_help"), "muted"));
  const current = Object.fromEntries(exportsOf(p).map((item) => [item.canvas, { ...item }]));
  const own = p.options.canvas || "16:9";
  for (const canvas of DELIVERY_CANVASES.filter((name) => name !== own)) {
    const row = el("div", "", "post-row");
    const value = current[canvas] || { canvas, fit: "crop", mode: "" };
    const mode = field(MindexI18n.msg("ui.delivery_export_mode", { p0: canvas }), value.mode || "", "text", [
    ["", MindexI18n.msg("ui.delivery_export_off")], ["derive", MindexI18n.msg("ui.delivery_export_derive")],
    ["rerender", MindexI18n.msg("ui.delivery_export_rerender")]]);
    const fit = field(MindexI18n.msg("ui.plan_fit_label"), value.fit, "text", [["crop", MindexI18n.msg("ui.plan_fit_crop")],
    ["pad", MindexI18n.msg("ui.plan_fit_pad")]]);
    mode.input.addEventListener("change", () => {value.mode = mode.input.value;current[canvas] = value;});
    fit.input.addEventListener("change", () => {value.fit = fit.input.value;current[canvas] = value;});
    row.append(mode.wrap, fit.wrap);
    section.append(row);
    const made = finalOf(p, `export:${canvas}`);
    if (made?.files?.mp4?.url) {
      const result = el("div", "", "delivery-result");
      result.append(el("p", MindexI18n.msg("ui.delivery_export_result", { p0: canvas, p1: made.data?.mode === "rerender" ?
        MindexI18n.msg("ui.delivery_export_rerender") : MindexI18n.msg("ui.delivery_export_derive"), p2: (made.data?.size || []).join("×") }),
      "muted"));
      result.append(media("video", made.files.mp4.url));
      const download = el("a", MindexI18n.msg("ui.delivery_download"), "quiet nav-link");
      download.href = `${made.files.mp4.url}?download=true`;
      download.setAttribute("download", "");
      result.append(download);
      section.append(result);
    }
  }
  const actions = el("div", "", "detail-actions");
  actions.append(mutationButton(MindexI18n.msg("ui.delivery_exports_save"), async () => {
    const exports = Object.values(current).filter((item) => item.mode).map(({ canvas, fit, mode }) => ({ canvas, fit, mode }));
    const options = { ...p.options, exports: exports.length ? exports : null };
    await accept(await api(planBase(), "PATCH", { revision: state.data.revision, options }));
    notice(MindexI18n.msg("ui.delivery_exports_saved"));
  }));
  if (exportsOf(p).some((item) => item.mode === "rerender"))
  actions.append(mutationButton(MindexI18n.msg("ui.delivery_build_all"), () => buildAllCanvases()));
  section.append(actions);
  box.append(section);
}
// 构建全部画幅：子方案（重新渲染）在前、主方案在后，一次构建完成；预检清单按方案分组列出要重建的节点数。
async function buildAllCanvases() {
  const checked = await api(`${planBase()}/preflight?all_canvases=true`);
  const rebuild = checked.nodes.filter((node) => node.action === "rebuild").length;
  const blocked = checked.nodes.filter((node) => node.action === "blocked").length;
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.delivery_build_all_confirm", { p0: rebuild, p1: blocked })))) return;
  await api(`${planBase()}/build`, "POST", { token: checked.token, all_canvases: true });
  await refresh();
}

// ------------------------------------------------------------ 平台发布：平台、封面、发布元数据与发布包
const PLATFORM_IDS = ["bilibili", "douyin", "youtube"];
function renderPublish(box, p) {
  const section = el("div", "", "delivery-publish");
  section.dataset.deliveryPublish = "true";
  section.append(el("h3", MindexI18n.msg("ui.publish_heading")));
  section.append(el("p", MindexI18n.msg("ui.publish_help"), "muted"));
  const current = p.options.publish || { platforms: [], cover: {} };
  const draft = { platforms: new Set(current.platforms), cover: { source: "frame", text: true, ...current.cover } };
  const picks = el("div", "", "detail-actions");
  for (const id of PLATFORM_IDS) {
    const wrap = el("label", "", "check");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = draft.platforms.has(id);
    box.addEventListener("change", () => {box.checked ? draft.platforms.add(id) : draft.platforms.delete(id);});
    wrap.append(box, el("span", MindexI18n.msg(`ui.publish_platform_${id}`)));
    picks.append(wrap);
  }
  section.append(picks);
  const cover = draft.cover;
  const source = field(MindexI18n.msg("ui.publish_cover_source"), cover.source, "text",
  ["frame", "aigc", "upload"].map((name) => [name, MindexI18n.msg(`ui.publish_cover_${name}`)]));
  source.input.addEventListener("change", () => {cover.source = source.input.value;});
  const at = field(MindexI18n.msg("ui.publish_cover_time"), cover.time ?? "", "number");
  at.input.min = "0";
  at.input.step = "0.1";
  at.input.addEventListener("change", () => {cover.time = at.input.value === "" ? null : Number(at.input.value);});
  const prompt = field(MindexI18n.msg("ui.publish_cover_prompt"), cover.prompt ?? "", "textarea");
  prompt.input.rows = 2;
  prompt.input.addEventListener("input", () => {cover.prompt = prompt.input.value.trim() || null;});
  const title = field(MindexI18n.msg("ui.publish_cover_title"), cover.title ?? "");
  title.input.maxLength = 40;
  title.input.addEventListener("input", () => {cover.title = title.input.value.trim() || null;});
  const subtitle = field(MindexI18n.msg("ui.publish_cover_subtitle"), cover.subtitle ?? "");
  subtitle.input.maxLength = 60;
  subtitle.input.addEventListener("input", () => {cover.subtitle = subtitle.input.value.trim() || null;});
  const row = el("div", "", "post-row");
  row.append(source.wrap, at.wrap);
  section.append(row, prompt.wrap);
  const textRow = el("div", "", "post-row");
  textRow.append(title.wrap, subtitle.wrap);
  section.append(textRow);
  const upload = button(MindexI18n.msg(cover.media_id ? "ui.publish_cover_replace" : "ui.publish_cover_upload"), async () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/png,image/jpeg,image/webp";
    const file = await new Promise((resolve) => {input.addEventListener("change", () => resolve(input.files?.[0] || null), { once: true });input.click();});
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("kind", "image");
    form.append("purpose", "source");
    const result = await api(`${planBase()}/query-media`, "POST", form);
    cover.media_id = result.media_id;
    cover.source = "upload";
    source.input.value = "upload";
    notice(MindexI18n.msg("ui.publish_cover_uploaded"));
  });
  const candidates = el("div", "", "cover-candidates");
  const pick = button(MindexI18n.msg("ui.publish_cover_candidates"), async () => {
    const first = [...draft.platforms][0] || current.platforms[0];
    if (!first) throw new Error(MindexI18n.msg("ui.publish_need_platform"));
    const data = await api(`${planBase()}/cover-candidates?platform=${first}`);
    candidates.replaceChildren();
    for (const item of data.items) {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.className = "quiet cover-candidate";
      const img = document.createElement("img");
      img.src = item.url;
      MindexI18n.setAttr(img, "alt", MindexI18n.msg("ui.publish_cover_at", { p0: item.time }));
      choice.append(img, el("span", MindexI18n.msg("ui.publish_cover_at", { p0: item.time }), "muted"));
      choice.addEventListener("click", () => {cover.source = "frame";cover.time = item.time;source.input.value = "frame";at.input.value = item.time;});
      candidates.append(choice);
    }
  });
  const actions = el("div", "", "detail-actions");
  actions.append(upload, pick, mutationButton(MindexI18n.msg("ui.publish_save"), async () => {
    const platforms = PLATFORM_IDS.filter((id) => draft.platforms.has(id));
    const options = { ...p.options, publish: platforms.length ? { platforms, cover } : null };
    await accept(await api(planBase(), "PATCH", { revision: state.data.revision, options }));
    notice(MindexI18n.msg("ui.publish_saved"));
  }));
  if (current.platforms.length) actions.append(mutationButton(MindexI18n.msg("ui.publish_build"), () =>
  exportsOf(p).some((item) => item.mode === "rerender") ? buildAllCanvases() : preflight(false)));
  section.append(actions, candidates);
  for (const id of current.platforms) section.append(platformResult(p, id));
  box.append(section);
}
function platformResult(p, id) {
  const card = el("div", "", "publish-result");
  card.dataset.publishPlatform = id;
  card.append(el("h4", MindexI18n.msg(`ui.publish_platform_${id}`)));
  const cover = finalOf(p, `cover:${id}`), meta = finalOf(p, `metadata:${id}`), pack = finalOf(p, `package:${id}`);
  if (!meta && !cover && !pack) {
    card.append(el("p", MindexI18n.msg("ui.publish_not_built"), "muted"));
    return card;
  }
  if (cover?.files?.cover?.url) {
    const img = document.createElement("img");
    img.src = cover.files.cover.url;
    img.className = "publish-cover";
    MindexI18n.setAttr(img, "alt", MindexI18n.msg("ui.publish_cover_alt"));
    card.append(img);
  }
  if (meta?.data) {
    const value = { title: meta.data.title, description: meta.data.body ?? "", tags: (meta.data.tags || []).join("，") };
    const title = field(MindexI18n.msg("ui.publish_meta_title"), value.title);
    const description = field(MindexI18n.msg("ui.publish_meta_description"), value.description, "textarea");
    description.input.rows = 4;
    const tags = field(MindexI18n.msg("ui.publish_meta_tags"), value.tags);
    card.append(title.wrap, description.wrap, tags.wrap);
    if (meta.data.chapters?.length)
    card.append(el("p", MindexI18n.msg("ui.publish_meta_chapters", { p0: meta.data.chapters.length }), "muted"));
    if (meta.data.notes?.length) card.append(el("p", meta.data.notes.map((note) => MindexI18n.system(note)).join("；"), "warn-text"));
    card.append(el("p", MindexI18n.msg(meta.data.origin === "manual" ? "ui.publish_meta_manual" : "ui.publish_meta_generated"), "muted"));
    card.append(mutationButton(MindexI18n.msg("ui.publish_meta_save"), async () => {
      const body = { revision: state.data.revision, title: title.input.value.trim(), description: description.input.value,
        tags: tags.input.value.split(/[,，、\s]+/).map((tag) => tag.trim()).filter(Boolean) };
      await accept(await api(`${planBase()}/publish-metadata/${id}`, "PATCH", body));
      notice(MindexI18n.msg("ui.publish_meta_saved"));
    }));
  }
  if (pack?.files?.zip?.url) {
    const download = el("a", MindexI18n.msg("ui.publish_download", { p0: Math.round((pack.data?.bytes || 0) / 1024 / 1024 * 10) / 10 }), "quiet nav-link");
    download.href = `${pack.files.zip.url}?download=true`;
    download.setAttribute("download", "");
    card.append(download);
    if (pack.data?.mismatch) card.append(el("p", MindexI18n.msg("ui.publish_mismatch"), "warn-text"));
  }
  return card;
}
