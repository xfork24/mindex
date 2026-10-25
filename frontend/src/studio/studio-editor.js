/* 创作页 · 逐镜召回设置与分镜编辑、保存与增删镜头。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
const RECALL_MODES = [
["text", MindexI18n.msg("ui.22d82c6044da")],
["image", MindexI18n.msg("ui.07e80d382810")],
["video", MindexI18n.msg("ui.8c5d4d07c1de")]];

const RECALL_MODE_LABEL = { text: MindexI18n.msg("ui.f4d3dab8e0c2"), image: MindexI18n.msg("ui.07e80d382810"), video: MindexI18n.msg("ui.8c5d4d07c1de") };
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
      source_ids: [...(v.filters?.source_ids || [])],
      min_duration: v.filters?.min_duration ?? 0,
      max_duration: v.filters?.max_duration ?? null,
      scene: v.filters?.scene ?? null,
      emotion: v.filters?.emotion ?? null,
      shot_size: v.filters?.shot_size ?? null
    },
    media_id: v.media_id ?? null,
    media_kind: v.media_kind ?? null
  };
}
function recallIsDefault(r) {
  return (
    r.mode === "text" &&
    !r.person_ids.length &&
    r.person_mode === "auto" &&
    r.person_relation === "all" &&
    !r.filters.source_id &&
    !r.filters.source_ids.length &&
    !r.filters.min_duration &&
    !r.filters.max_duration &&
    !r.filters.scene &&
    !r.filters.emotion &&
    !r.filters.shot_size &&
    !r.media_id &&
    !r.media_kind);

}
// 画面来源：素材召回（缺省，不落键）/ 上传片段 / 静态图片 / AI 生成图片 / AI 生成视频 / AI 动画。
const SOURCE_KINDS = [
["recall", MindexI18n.msg("ui.source_recall")],
["clip", MindexI18n.msg("ui.source_clip")],
["image", MindexI18n.msg("ui.source_image")],
["aigc_image", MindexI18n.msg("ui.source_aigc_image_kind")],
["aigc", MindexI18n.msg("ui.source_aigc")],
["animation", MindexI18n.msg("ui.source_animation")],
["tool", MindexI18n.msg("ui.source_tool")]];

const MOTIONS = [
["zoom-in", MindexI18n.msg("ui.motion_zoom_in")],
["zoom-out", MindexI18n.msg("ui.motion_zoom_out")],
["pan-left", MindexI18n.msg("ui.motion_pan_left")],
["pan-right", MindexI18n.msg("ui.motion_pan_right")],
["none", MindexI18n.msg("ui.motion_none")]];

const MOTION_LABEL = Object.fromEntries(MOTIONS);
// 键序与 ClipSource/ImageSource 的字段一致，保证与已存 content 的 dirty 判定稳定。
function sourceEditable(kind, previous) {
  const same = previous?.kind === kind ? previous : null;
  if (kind === "clip") return { kind, media_id: same?.media_id ?? null, start: same?.start ?? null };
  if (kind === "image")
  return { kind, media_id: same?.media_id ?? null, motion: same?.motion || "zoom-in", strength: same?.strength ?? 0.3 };
  if (kind === "aigc") return aigcCanonical({ ...(same || {}), kind });
  if (kind === "aigc_image")
  return { kind, prompt: same?.prompt ?? null, reference_id: same?.reference_id ?? null, seed: same?.seed ?? null,
    prompt_optimizer: same?.prompt_optimizer ?? false, motion: same?.motion || "zoom-in", strength: same?.strength ?? 0.3 };
  if (kind === "animation") return { kind, brief: same?.brief ?? null };
  if (kind === "tool") return toolCanonical({ ...(same || {}), kind });
  return null;
}
// ToolShotSource 的键序（与服务端 dump 一致）：uploads 只在非空时出现，运动参数只对图片产物出现。
function toolCanonical(value) {
  const result = { kind: "tool", tool_id: value.tool_id ?? "", tool: value.tool ?? "", arguments: value.arguments || {} };
  if (value.uploads?.length) result.uploads = value.uploads;
  result.output = value.output || "video";
  if (result.output === "image") Object.assign(result, { motion: value.motion || "zoom-in", strength: value.strength ?? 0.3 });
  return result;
}
// AigcSource 的键序：前五个总在；MiniMax H3 的逐镜选项只在设置时出现（与服务端 dump 省略 None 一致）。
const AIGC_OPTIONAL = ["mode", "last_image_id", "references", "model", "resolution", "ratio", "duration"];
function aigcCanonical(value) {
  const result = { kind: "aigc", prompt: value.prompt ?? null, negative: value.negative ?? null,
    image_id: value.image_id ?? null, seed: value.seed ?? null };
  for (const key of AIGC_OPTIONAL) {
    const v = value[key];
    if (v !== null && v !== undefined && v !== "" && !(Array.isArray(v) && !v.length)) result[key] = v;
  }
  return result;
}
const VIDEO_MODES = [
["", MindexI18n.msg("ui.video_mode_auto")],
["text", MindexI18n.msg("ui.video_mode_text")],
["first_frame", MindexI18n.msg("ui.video_mode_first_frame")],
["last_frame", MindexI18n.msg("ui.video_mode_last_frame")],
["first_last", MindexI18n.msg("ui.video_mode_first_last")],
["reference", MindexI18n.msg("ui.video_mode_reference")]];
const VIDEO_MODE_LABEL = Object.fromEntries(VIDEO_MODES);
function sourceSummary(source) {
  if (source?.kind === "aigc_image")
  return MindexI18n.msg("ui.source_summary_aigc_image", { p0: source.prompt || MindexI18n.msg("ui.source_aigc_default_prompt"),
    p1: MOTION_LABEL[source.motion] || source.motion });
  if (source?.kind === "clip") return MindexI18n.msg("ui.source_summary_clip", { p0: secs(source.start || 0) });
  if (source?.kind === "image")
  return MindexI18n.msg("ui.source_summary_image", { p0: MOTION_LABEL[source.motion] || source.motion, p1: source.strength });
  if (source?.kind === "aigc")
  return MindexI18n.msg("ui.source_summary_aigc", { p0: source.prompt || MindexI18n.msg("ui.source_aigc_default_prompt") }) +
  (source.mode ? ` · ${VIDEO_MODE_LABEL[source.mode]}` : "");
  if (source?.kind === "tool")
  return MindexI18n.msg("ui.source_summary_tool", { p0: source.tool_id || "?", p1: source.tool || "?",
    p2: MindexI18n.msg(source.output === "image" ? "ui.invoke_output_image" : "ui.invoke_output_video") });
  if (source?.kind === "animation")
  return MindexI18n.msg("ui.source_summary_animation", { p0: source.brief || MindexI18n.msg("ui.source_animation_default_brief") });
  return "";
}
// AI 动画来源：画面说明（缺省用画面意象，再缺省用旁白）；源码在“生成动画”阶段详情里查看与编辑。
function renderAnimationEditor(section, draft, markDirty) {
  const value = draft.data.source;
  const fallback = (draft.data.visual || "").trim();
  const brief = field(MindexI18n.msg("ui.source_animation_brief"), value.brief ?? "", "textarea");
  brief.input.maxLength = 2000;
  brief.input.rows = 4;
  brief.input.dataset.shotInput = "true";
  brief.input.dataset.shotSource = "brief";
  MindexI18n.setAttr(brief.input, "placeholder", fallback ?
  MindexI18n.msg("ui.source_aigc_prompt_placeholder", { p0: fallback }) :
  MindexI18n.msg("ui.source_animation_brief_placeholder_text", { p0: draft.data.text || "" }));
  brief.input.addEventListener("input", () => {
    draft.data.source.brief = brief.input.value.trim() ? brief.input.value : null;
    markDirty();
  });
  const briefRow = el("div", "", "source-options");
  briefRow.append(brief.wrap);
  section.append(briefRow);
}
// 第三方工具来源：选择登记的工具源与白名单中的工具（产物类型随工具登记），JSON 参数与（允许时）上传素材。
function renderToolEditor(section, sid, draft, markDirty, redraw) {
  const value = draft.data.source;
  const setSource = (patch) => {draft.data.source = toolCanonical({ ...draft.data.source, ...patch });markDirty();};
  const hint = el("p", "", "muted");
  hint.setAttribute("role", "status");
  const sourceField = field(MindexI18n.msg("ui.tool_source_label"), value.tool_id || "", "text", [["", MindexI18n.msg("ui.tool_loading")]]);
  sourceField.input.dataset.shotInput = "true";
  sourceField.input.dataset.shotSource = "tool_id";
  const toolField = field(MindexI18n.msg("ui.tool_name_label"), value.tool || "", "text", [["", "—"]]);
  toolField.input.dataset.shotInput = "true";
  toolField.input.dataset.shotSource = "tool";
  const line = el("div", "", "source-options");
  line.append(sourceField.wrap, toolField.wrap);
  section.append(line);
  const manage = el("a", MindexI18n.msg("ui.invoke_manage"), "quiet nav-link");
  manage.href = `/studio/tools?job=${encodeURIComponent(state.id)}`;
  section.append(manage, hint);
  const fillTools = (items) => {
    const found = items.find((item) => item.id === draft.data.source.tool_id);
    toolField.input.replaceChildren();
    for (const [v, text] of [["", MindexI18n.msg("ui.tool_choose")], ...(found?.tools || []).map((tool) =>
      [tool.name, `${tool.label || tool.name} · ${MindexI18n.msg(tool.output === "image" ? "ui.invoke_output_image" : "ui.invoke_output_video")}`])]) {
      const option = el("option", text);
      option.value = v;
      toolField.input.append(option);
    }
    toolField.input.value = draft.data.source.tool || "";
    const tool = found?.tools.find((item) => item.name === draft.data.source.tool);
    MindexI18n.setText(hint, !found && draft.data.source.tool_id ? MindexI18n.msg("ui.tool_source_missing") :
      tool ? MindexI18n.msg(tool.allow_upload ? "ui.tool_upload_allowed" : "ui.tool_upload_denied") : "");
    return tool;
  };
  const load = state.toolSources ? Promise.resolve(state.toolSources) :
  api("/api/studio/tool-sources").then((data) => (state.toolSources = data.items));
  load.then((items) => {
    if (!sourceField.input.isConnected) return;
    sourceField.input.replaceChildren();
    for (const [v, text] of [["", items.length ? MindexI18n.msg("ui.tool_choose") : MindexI18n.msg("ui.tool_none_registered")],
      ...items.map((item) => [item.id, item.name])]) {
      const option = el("option", text);
      option.value = v;
      sourceField.input.append(option);
    }
    sourceField.input.value = draft.data.source.tool_id || "";
    fillTools(items);
    sourceField.input.addEventListener("change", () => {setSource({ tool_id: sourceField.input.value, tool: "" });fillTools(items);});
    toolField.input.addEventListener("change", () => {
      const tool = items.find((item) => item.id === draft.data.source.tool_id)?.tools.find((t) => t.name === toolField.input.value);
      setSource({ tool: toolField.input.value, output: tool?.output || "video" });
      redraw();
    });
  }).catch((error) => MindexI18n.setText(hint, shortIds(error.message)));
  const args = field(MindexI18n.msg("ui.tool_arguments"), JSON.stringify(value.arguments || {}, null, 2), "textarea");
  args.input.rows = 5;
  args.input.dataset.shotInput = "true";
  args.input.dataset.shotSource = "arguments";
  const argsError = el("p", "", "warn-text");
  args.input.addEventListener("input", () => {
    try {
      const parsed = JSON.parse(args.input.value || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("object");
      MindexI18n.setText(argsError, "");
      setSource({ arguments: parsed });
    } catch {
      MindexI18n.setText(argsError, MindexI18n.msg("ui.tool_arguments_invalid"));
    }
  });
  section.append(args.wrap, el("p", MindexI18n.msg("ui.tool_arguments_help"), "muted"), argsError);
  const uploads = value.uploads || [];
  const list = el("div", "", "reference-media-list");
  uploads.forEach((ref, index) => {
    const row = el("div", "", "recall-upload-row");
    row.append(el("span", `${MindexI18n.msg("ui.video_ref_" + ref.kind)} · ${ref.media_id.slice(0, 8)}`, "muted"),
    button(MindexI18n.msg("ui.source_aigc_clear_image"), () => {setSource({ uploads: uploads.filter((_, i) => i !== index) });redraw();}));
    list.append(row);
  });
  section.append(el("h4", MindexI18n.msg("ui.tool_uploads", { p0: uploads.length })), list);
  if (uploads.length < 4) for (const kind of ["image", "video"])
  section.append(mediaSlot(sid, draft, "tool", kind, MindexI18n.msg("ui.video_ref_add_" + kind), null,
    (id) => setSource({ uploads: [...(draft.data.source.uploads || []), { kind, media_id: id }] }), redraw));
  if (value.output === "image") section.append(motionOptions(draft, markDirty));
}
// AIGC 来源：提示词（缺省用画面意象，再缺省用召回查询）、反向提示词、种子与可选的首帧参考图。
function renderAigcEditor(section, sid, draft, markDirty, redraw) {
  const value = draft.data.source;
  const fallback = (draft.data.visual || "").trim();
  const prompt = field(MindexI18n.msg("ui.source_aigc_prompt"), value.prompt ?? "", "textarea");
  prompt.input.maxLength = 1000;
  prompt.input.rows = 3;
  prompt.input.dataset.shotInput = "true";
  prompt.input.dataset.shotSource = "prompt";
  MindexI18n.setAttr(prompt.input, "placeholder", fallback ?
  MindexI18n.msg("ui.source_aigc_prompt_placeholder", { p0: fallback }) :
  MindexI18n.msg("ui.source_aigc_prompt_placeholder_query", { p0: draft.data.query || "" }));
  prompt.input.addEventListener("input", () => {
    draft.data.source.prompt = prompt.input.value.trim() ? prompt.input.value : null;
    markDirty();
  });
  const negative = field(MindexI18n.msg("ui.source_aigc_negative"), value.negative ?? "", "textarea");
  negative.input.maxLength = 500;
  negative.input.rows = 3;
  negative.input.dataset.shotInput = "true";
  negative.input.dataset.shotSource = "negative";
  negative.input.addEventListener("input", () => {
    draft.data.source.negative = negative.input.value.trim() ? negative.input.value : null;
    markDirty();
  });
  const seed = field(MindexI18n.msg("ui.source_aigc_seed"), value.seed ?? "", "number");
  seed.input.min = "0";
  seed.input.step = "1";
  seed.input.dataset.shotInput = "true";
  seed.input.dataset.shotSource = "seed";
  seed.input.addEventListener("input", () => {
    const raw = seed.input.value.trim(), number = Number(raw);
    draft.data.source.seed = raw === "" || !Number.isInteger(number) || number < 0 ? null : number;
    markDirty();
  });
  const promptRow = el("div", "", "source-options");
  promptRow.append(prompt.wrap, negative.wrap, seed.wrap);
  section.append(promptRow);
  const setSource = (patch) => {
    draft.data.source = aigcCanonical({ ...draft.data.source, ...patch });
    markDirty();
  };
  // 生成方式与 MiniMax H3 逐镜选项（其余视频生成后端只支持文生/首帧，预检会说明）。
  const mode = field(MindexI18n.msg("ui.video_mode"), value.mode || "", "text", VIDEO_MODES);
  mode.input.dataset.shotInput = "true";
  mode.input.dataset.shotSource = "mode";
  mode.input.addEventListener("change", () => {setSource({ mode: mode.input.value || null });redraw();});
  const h3 = el("div", "", "source-options");
  h3.append(mode.wrap);
  for (const [key, label, choices] of [
  ["model", "ui.video_model", [["", MindexI18n.msg("ui.video_follow_profile")], ...["MiniMax-H3", "MiniMax-H3-Highspeed", "MiniMax-H3-Lite", "MiniMax-H3-Max"].map((v) => [v, v])]],
  ["resolution", "ui.video_resolution", [["", "768P"], ["480P", "480P"], ["768P", "768P"], ["2K", "2K"]]],
  ["ratio", "ui.video_ratio", [["", MindexI18n.msg("ui.video_ratio_auto")], ...["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"].map((v) => [v, v])]],
  ["duration", "ui.video_duration", [["", MindexI18n.msg("ui.video_duration_auto")], ...Array.from({ length: 12 }, (_, i) => [String(i + 4), `${i + 4}s`])]]])
  {
    const f = field(MindexI18n.msg(label), value[key] == null ? "" : String(value[key]), "text", choices);
    f.input.dataset.shotInput = "true";
    f.input.dataset.shotSource = key;
    f.input.addEventListener("change", () => setSource({ [key]: f.input.value === "" ? null : key === "duration" ? Number(f.input.value) : f.input.value }));
    h3.append(f.wrap);
  }
  section.append(h3);
  const effective = value.mode || (value.image_id ? "first_frame" : "text");
  const slotLine = (slot) => {const line = el("div", "", "source-line slot-only");line.append(slot);return line;};
  if (["first_frame", "first_last"].includes(effective) || !value.mode)
  section.append(slotLine(mediaSlot(sid, draft, "aigc", "image", MindexI18n.msg("ui.source_aigc_image"), value.image_id,
    (id) => setSource({ image_id: id }), redraw)));
  if (["last_frame", "first_last"].includes(effective))
  section.append(slotLine(mediaSlot(sid, draft, "aigc", "image", MindexI18n.msg("ui.source_aigc_last_image"), value.last_image_id,
    (id) => setSource({ last_image_id: id }), redraw)));
  if (effective === "reference") {
    const refs = value.references || [];
    const list = el("div", "", "reference-media-list");
    refs.forEach((ref, index) => {
      const row = el("div", "", "recall-upload-row");
      row.append(el("span", `${MindexI18n.msg("ui.video_ref_" + ref.kind)} · ${ref.media_id.slice(0, 8)}`, "muted"),
      button(MindexI18n.msg("ui.source_aigc_clear_image"), () => {
        setSource({ references: refs.filter((_, i) => i !== index) });
        redraw();
      }));
      list.append(row);
    });
    section.append(el("h4", MindexI18n.msg("ui.video_references", { p0: refs.length })), list);
    for (const kind of ["image", "video", "audio"])
    section.append(mediaSlot(sid, draft, "aigc", kind, MindexI18n.msg("ui.video_ref_add_" + kind), null,
      (id) => setSource({ references: [...(draft.data.source.references || []), { kind, media_id: id }] }), redraw));
  }
  if (state.data.generators?.video?.backend !== "minimax_h3")
  section.append(el("p", MindexI18n.msg("ui.video_h3_only_hint"), "muted"));
}
// 参考素材上传位：上传到 inputs/（purpose=source），成功后经 onUploaded 写入草稿；有值时显示预览与移除按钮。
const MEDIA_ACCEPT = { image: "image/jpeg,image/png,image/webp", video: "video/mp4,video/quicktime,video/*",
  audio: "audio/mpeg,audio/wav,audio/flac,audio/mp4,audio/*" };
function mediaSlot(sid, draft, sourceKind, kind, label, currentId, onUploaded, redraw, guard) {
  const box = el("div", "", "media-slot");
  const uploadWrap = el("label", label, "form-field recall-file-field");
  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = MEDIA_ACCEPT[kind];
  uploadWrap.append(fileInput);
  const uploadButton = button(MindexI18n.msg("ui.source_upload"), async () => {
    const scope = scopeKey();
    const file = fileInput.files[0];
    if (!file) throw new Error(MindexI18n.msg("ui.7bcc55e19443"));
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind);
    form.append("purpose", "source");
    const data = await api(`${planBase()}/query-media`, "POST", form);
    if (scope !== scopeKey()) return;
    if (guard ? !guard() : state.drafts.get(draftKey(sid)) !== draft || draft.data.source?.kind !== sourceKind) return;
    state.inputs = null;
    (state.previews = state.previews || new Map()).set(data.media_id, data.preview_url);
    onUploaded(data.media_id);
    redraw();
    notice(MindexI18n.msg("ui.source_uploaded"));
  });
  uploadButton.classList.add("recall-upload-button");
  const row = el("div", "", "recall-upload-row");
  row.append(uploadWrap, uploadButton);
  if (currentId) row.append(button(MindexI18n.msg("ui.source_aigc_clear_image"), () => {onUploaded(null);redraw();}));
  box.append(row);
  if (currentId) {
    const host = el("div", "", "query-preview-host");
    box.append(host);
    const show = (url) => host.replaceChildren(kind === "audio" ? media("audio", url) : queryPreviewNode(url, kind));
    if (state.previews?.has(currentId)) show(state.previews.get(currentId));else
    inputUrl(currentId).then((url) => {if (url && host.isConnected) show(url);});
  }
  return box;
}
// AI 生成图片：提示词（缺省画面意象/查询）、可选主体参考图（图生图）、种子、提示词优化，以及与静态图片相同的运动设置。
function renderAiImageEditor(section, sid, draft, markDirty, redraw) {
  const value = draft.data.source;
  const fallback = (draft.data.visual || "").trim();
  const prompt = field(MindexI18n.msg("ui.source_aigc_prompt"), value.prompt ?? "", "textarea");
  prompt.input.maxLength = 1500;
  prompt.input.rows = 3;
  prompt.input.dataset.shotInput = "true";
  prompt.input.dataset.shotSource = "prompt";
  MindexI18n.setAttr(prompt.input, "placeholder", fallback ?
  MindexI18n.msg("ui.source_aigc_prompt_placeholder", { p0: fallback }) :
  MindexI18n.msg("ui.source_aigc_prompt_placeholder_query", { p0: draft.data.query || "" }));
  prompt.input.addEventListener("input", () => {
    draft.data.source.prompt = prompt.input.value.trim() ? prompt.input.value : null;
    markDirty();
  });
  const seed = field(MindexI18n.msg("ui.source_aigc_seed"), value.seed ?? "", "number");
  seed.input.min = "0";
  seed.input.step = "1";
  seed.input.dataset.shotInput = "true";
  seed.input.addEventListener("input", () => {
    const raw = seed.input.value.trim(), number = Number(raw);
    draft.data.source.seed = raw === "" || !Number.isInteger(number) || number < 0 ? null : number;
    markDirty();
  });
  const optimizer = el("label", "", "check");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = Boolean(value.prompt_optimizer);
  box.dataset.shotInput = "true";
  box.addEventListener("change", () => {draft.data.source.prompt_optimizer = box.checked;markDirty();});
  optimizer.append(box, el("span", MindexI18n.msg("ui.image_prompt_optimizer")));
  // 第一行：提示词、随机种子、提示词自动优化；第二行：主体参考图（含上传）、运动方式、运动强度。
  const promptLine = el("div", "", "source-line");
  prompt.wrap.classList.add("is-wide");
  promptLine.append(prompt.wrap, seed.wrap, optimizer);
  const motionLine = el("div", "", "source-line");
  motionLine.append(mediaSlot(sid, draft, "aigc_image", "image", MindexI18n.msg("ui.image_reference"), value.reference_id,
    (id) => {draft.data.source = { ...draft.data.source, reference_id: id };markDirty();}, redraw),
  motionOptions(draft, markDirty, true));
  section.append(promptLine, motionLine);
}
// inline=true 时返回 display:contents 的分组，运动方式/强度直接并入外层 .source-line 同一行。
function motionOptions(draft, markDirty, inline = false) {
  const value = draft.data.source;
  const options = el("div", "", inline ? "source-inline-group" : "source-options");
  const motionField = field(MindexI18n.msg("ui.source_motion"), value.motion || "zoom-in", "text", MOTIONS);
  motionField.input.dataset.shotInput = "true";
  motionField.input.dataset.shotSource = "motion";
  motionField.input.addEventListener("change", () => {
    draft.data.source.motion = motionField.input.value;
    markDirty();
  });
  const strength = value.strength ?? 0.3;
  const strengthWrap = el("label", MindexI18n.msg("ui.source_strength", { p0: strength }), "form-field");
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "1";
  slider.step = "0.05";
  slider.value = String(strength);
  slider.dataset.shotInput = "true";
  slider.dataset.shotSource = "strength";
  MindexI18n.setAttr(slider, "aria-label", MindexI18n.msg("ui.source_strength", { p0: strength }));
  slider.addEventListener("input", () => {
    const next = Number(slider.value);
    draft.data.source.strength = next;
    MindexI18n.setText(strengthWrap.firstChild, MindexI18n.msg("ui.source_strength", { p0: next }));
    MindexI18n.setAttr(slider, "aria-label", MindexI18n.msg("ui.source_strength", { p0: next }));
    markDirty();
  });
  strengthWrap.append(slider);
  options.append(motionField.wrap, strengthWrap);
  return options;
}
function sourcePreviewNode(url, kind, start) {
  if (kind === "image") return queryPreviewNode(url, "image");
  const n = media("video", url);
  // 预览从片段起点开始播放，对应选片窗口的开头。
  if (start) n.addEventListener("loadedmetadata", () => {n.currentTime = Math.min(start, n.duration || start);}, { once: true });
  return n;
}
function renderSourceEditor(body, sid, draft) {
  const section = el("div", "", "source-editor");
  const markDirty = () => {
    draft.dirty = JSON.stringify(draft.data) !== JSON.stringify(draft.original);
    refreshShotCard(sid);
  };
  const redraw = () => {
    invalidatePanel(sid);
    renderMatrix();
  };
  const value = draft.data.source || null;
  const kind = value?.kind || "recall";
  section.append(el("h4", MindexI18n.msg("ui.source_heading")));
  const kindField = field(MindexI18n.msg("ui.source_heading"), kind, "text", SOURCE_KINDS);
  kindField.input.dataset.shotInput = "true";
  kindField.input.dataset.shotSource = "kind";
  kindField.input.addEventListener("change", () => {
    const next = sourceEditable(kindField.input.value, draft.data.source);
    // 素材召回不落键：与未设置来源的旧 content 完全一致。
    if (next) draft.data.source = next;else delete draft.data.source;
    markDirty();
    redraw();
  });
  const row = el("div", "", "source-options");
  row.append(kindField.wrap);
  section.append(row);
  if (kind === "recall") {
    body.append(section);
    return true;
  }
  if (kind === "aigc") {
    renderAigcEditor(section, sid, draft, markDirty, redraw);
    body.append(section);
    return false;
  }
  if (kind === "aigc_image") {
    renderAiImageEditor(section, sid, draft, markDirty, redraw);
    body.append(section);
    return false;
  }
  if (kind === "animation") {
    renderAnimationEditor(section, draft, markDirty);
    body.append(section);
    return false;
  }
  if (kind === "tool") {
    renderToolEditor(section, sid, draft, markDirty, redraw);
    body.append(section);
    return false;
  }
  const uploadWrap = el("label", kind === "clip" ? MindexI18n.msg("ui.source_clip_file") : MindexI18n.msg("ui.source_image_file"),
  "form-field recall-file-field");
  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = kind === "image" ? "image/jpeg,image/png,image/webp" : "video/*";
  uploadWrap.append(fileInput);
  const uploadButton = button(MindexI18n.msg("ui.source_upload"), async () => {
    const scope = scopeKey();
    const file = fileInput.files[0];
    if (!file) throw new Error(MindexI18n.msg("ui.7bcc55e19443"));
    const form = new FormData();
    form.append("file", file);
    form.append("kind", kind === "clip" ? "video" : "image");
    form.append("purpose", "source");
    const data = await api(`${planBase()}/query-media`, "POST", form);
    if (scope !== scopeKey() || state.drafts.get(draftKey(sid)) !== draft || draft.data.source?.kind !== kind) return;
    draft.data.source = { ...draft.data.source, media_id: data.media_id };
    state.inputs = null; // 新文件入列，待重取索引
    draft.sourcePreview = { media_id: data.media_id, url: data.preview_url };
    markDirty();
    redraw();
    notice(MindexI18n.msg("ui.source_uploaded"));
  });
  uploadButton.classList.add("recall-upload-button");
  // 上传文件、上传按钮与起点/运动设置另起一行（窄屏自动换行）。
  const line = el("div", "", "source-line");
  section.append(line);
  line.append(uploadWrap, uploadButton);
  if (kind === "clip") {
    const startField = field(MindexI18n.msg("ui.source_clip_start"), value.start ?? "", "number");
    startField.input.dataset.shotInput = "true";
    startField.input.dataset.shotSource = "start";
    startField.input.min = "0";
    startField.input.step = "0.1";
    startField.input.addEventListener("input", () => {
      const raw = startField.input.value.trim();
      const number = Number(raw);
      draft.data.source.start = raw === "" || !Number.isFinite(number) ? null : Math.max(0, number);
      markDirty();
    });
    line.append(startField.wrap);
  } else {
    line.append(motionOptions(draft, markDirty, true));
  }
  const host = el("div", "", "query-preview-host");
  section.append(host);
  if (!value.media_id) {
    // 尚未上传画面文件：不显示说明文字
  } else if (draft.sourcePreview?.media_id === value.media_id) {
    host.append(sourcePreviewNode(draft.sourcePreview.url, kind, value.start));
  } else {
    host.append(el("p", MindexI18n.msg("ui.e454bbfe7b70"), "muted"));
    inputUrl(value.media_id).then((url) => {
      if (!url || !host.isConnected) return;
      host.replaceChildren(sourcePreviewNode(url, kind, value.start));
    });
  }
  body.append(section);
  return false;
}
function queryPreviewNode(url, kind) {
  if (kind === "image") {
    const n = el("img");
    n.src = url;
    n.loading = "lazy";
    MindexI18n.setAttr(n, "alt", MindexI18n.msg("ui.95012948d933"));
    return n;
  }
  return media("video", url);
}
function scopedRecallPersons(value) {
  const selected = value?.filters?.source_ids?.length ? value.filters.source_ids :
    value?.filters?.source_id ? [value.filters.source_id] : scopedSourceEntries().map((s) => s.id);
  const sources = new Set(selected);
  const works = new Set((state.personSources || []).filter((s) => sources.has(s.id)).map((s) => s.work_id));
  const roles = (state.persons || []).filter((p) => p.kind === "role" &&
    (p.work_ids || []).some((id) => works.has(id)));
  const actors = new Set(roles.flatMap((p) => p.actor_ids || []));
  return [...(state.persons || []).filter((p) => p.kind === "actor" && actors.has(p.id)), ...roles];
}
function renderRecallEditor(body, sid, draft, overrides) {
  const section = el("div", "", "recall-editor");
  const commit = (next) => {
    // 全默认等价于未设置：写回 null 以保持与旧 content 完全一致的 dirty 判定与签名。
    draft.data.recall = recallIsDefault(next) ? null : next;
    draft.dirty =
    JSON.stringify(draft.data) !== JSON.stringify(draft.original);
    refreshShotCard(sid);
  };
  const draw = () => {
    section.replaceChildren(el("h4", MindexI18n.msg("ui.fc0d8a6e8d52")));
    const value = draft.data.recall ? recallEditable(draft.data.recall) : null;
    const mode = value?.mode || "text";
    const modeField = field(MindexI18n.msg("ui.8c94a76f43c5"), mode, "text", RECALL_MODES);
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
    const primaryOptions = el("div", "", "recall-primary-options");
    primaryOptions.append(modeField.wrap, overrides);
    section.append(primaryOptions);
    const personOptions = el("div", "", "recall-person-options");
    let refreshPersons = () => {};
    if (mode === "text") {
      // 与检索页一致：演员和角色两级，角色按已选演员过滤；后端仍只收 person_ids。
      const chosen = new Set(value?.person_ids || []);
      let persons = scopedRecallPersons(value);
      const actorField = multiField(MindexI18n.msg("ui.acc714bb0167"));
      const roleField = multiField(MindexI18n.msg("ui.224f21d3016f"));
      const commitPersons = () => {
        const next = recallEditable(draft.data.recall);
        next.person_ids = [...new Set([...pickedIds(actorField.select), ...pickedIds(roleField.select)])];
        commit(next);
      };
      const roleEntries = () => {
        const actors = new Set(pickedIds(actorField.select));
        return persons.filter((p) => p.kind === "role" && (!actors.size || (p.actor_ids || []).some((id) => actors.has(id))));
      };
      const fillRoles = () => {
        fillAllSelect(roleField.select, roleEntries(), new Set(pickedIds(roleField.select)));
        commitPersons();
      };
      refreshPersons = (next) => {
        persons = scopedRecallPersons(next);
        const selected = new Set(next.person_ids);
        fillAllSelect(actorField.select, persons.filter((p) => p.kind === "actor"), selected);
        fillAllSelect(roleField.select, roleEntries(), selected);
      };
      fillAllSelect(actorField.select, persons.filter((p) => p.kind === "actor"), chosen);
      fillAllSelect(roleField.select, roleEntries(), chosen);
      actorField.select.addEventListener("change", () => {toggleAllOf(actorField.select);fillRoles();});
      roleField.select.addEventListener("change", () => {toggleAllOf(roleField.select);commitPersons();});
      personOptions.append(actorField.wrap, roleField.wrap);
      for (const [key, label, choices] of [
      ["person_mode", MindexI18n.msg("ui.06cf3151c450"), [
      ["auto", MindexI18n.msg("ui.be93a225bcfb")],
      ["onscreen", MindexI18n.msg("ui.bc9173082f3e")],
      ["mentioned", MindexI18n.msg("ui.cbf394311140")]]],

      ["person_relation", MindexI18n.msg("ui.ac954c28b468"), [
      ["all", MindexI18n.msg("ui.859a27c20c56")],
      ["any", MindexI18n.msg("ui.f61923e57f20")]]]])

      {
        const f = field(label, value?.[key] || (key === "person_mode" ? "auto" : "all"), "text", choices);
        f.input.addEventListener("change", () => {
          const next = recallEditable(draft.data.recall);
          next[key] = f.input.value;
          commit(next);
        });
        personOptions.append(f.wrap);
      }
    } else {
      const uploadWrap = el(
        "label",
        mode === "image" ? MindexI18n.msg("ui.22ddb980ec95") : MindexI18n.msg("ui.f753a4b59e3f"),


        "form-field recall-file-field"
      );
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = mode === "image" ? "image/jpeg,image/png,image/webp" : "video/*";
      uploadWrap.append(fileInput);
      const uploadButton = button(MindexI18n.msg("ui.6c130ff2346d"), async () => {
        const scope = scopeKey();
        const file = fileInput.files[0];
        if (!file) throw new Error(MindexI18n.msg("ui.7bcc55e19443"));
        const form = new FormData();
        form.append("file", file);
        form.append("kind", mode);
        const data = await api(`${planBase()}/query-media`, "POST", form);
        if (scope !== scopeKey() || state.drafts.get(draftKey(sid)) !== draft || draft.data.recall?.mode !== mode) return;
        const next = recallEditable(draft.data.recall);
        next.media_id = data.media_id;
        next.media_kind = data.kind;
        state.inputs = null; // 新文件入列，待重取索引
        draft.mediaPreview = { media_id: data.media_id, url: data.preview_url, kind: data.kind };
        commit(next);
        draw();
        notice(MindexI18n.msg("ui.c539109f0cf6"));
      });
      uploadButton.classList.add("recall-upload-button");
      const uploadRow = el("div", "", "recall-upload-row");
      uploadRow.append(uploadWrap, uploadButton);
      section.append(uploadRow);
      if (value?.media_id) {
        const host = el("div", "", "query-preview-host");
        section.append(host);
        if (draft.mediaPreview?.media_id === value.media_id) {
          host.append(queryPreviewNode(draft.mediaPreview.url, draft.mediaPreview.kind));
        } else {
          host.append(el("p", MindexI18n.msg("ui.e454bbfe7b70"), "muted"));
          inputUrl(value.media_id).then((url) => {
            if (!url || !host.isConnected) return;
            host.replaceChildren(queryPreviewNode(url, value.media_kind || mode));
          });
        }
      }
    }
    const sourceField = multiField(MindexI18n.msg("ui.df2bc29adabb"));
    sourceField.select.dataset.recallFilter = "source_ids";
    // 只列任务范围内的素材；越界的旧值（历史数据或手改 API）一并裁掉。
    const entries = scopedSourceEntries();
    const scope = jobScope();
    const sources = value?.filters?.source_ids?.length ? value.filters.source_ids : value?.filters?.source_id ? [value.filters.source_id] : [];
    const chosen = new Set((sources.length ? sources : entries.map((entry) => entry.id)).filter((id) => !scope || scope.has(id)));
    fillAllSelect(sourceField.select, entries, chosen);
    sourceField.wrap.append(el("span", MindexI18n.msg("ui.c1b8365074c7"), "muted"));
    sourceField.select.addEventListener("change", () => {
      toggleAllOf(sourceField.select);
      const picked = pickedIds(sourceField.select);
      const next = recallEditable(draft.data.recall);
      next.filters.source_id = null;
      // 全选 = 不写筛选，签名保持默认，历史召回版本继续复用。
      next.filters.source_ids = picked.length === entries.length ? [] : picked;
      const allowed = new Set(scopedRecallPersons(next).map((p) => p.id));
      next.person_ids = next.person_ids.filter((id) => allowed.has(id));
      // Keep the active material control (focus, search, scroll and resized height) intact.
      // No selection means the full task scope, matching the saved default semantics.
      if (!picked.length) applyAllOf(sourceField.select, true);
      refreshPersons(next);
      commit(next);
    });
    const scopeOptions = el("div", "", "recall-scope-options");
    scopeOptions.append(sourceField.wrap, personOptions);
    section.append(scopeOptions);
    const filters = el("div", "", "recall-filters");
    for (const [key, label, placeholder, type] of [
      ["scene", "ui.625b392c7b8a", "ui.722889427cbf", "text"],
      ["emotion", "ui.61fe61644a75", "ui.d637fe8494d8", "text"],
      ["shot_size", "ui.6867f7b4537a", "ui.79b85267c636", "text"],
      ["min_duration", "ui.26b6f15b345b", null, "number"],
      ["max_duration", "ui.2de8ce6144b7", null, "number"]
    ]) {
      const f = field(MindexI18n.msg(label), value?.filters?.[key] ?? "", type);
      f.input.dataset.recallFilter = key;
      if (placeholder) MindexI18n.setAttr(f.input, "placeholder", MindexI18n.msg(placeholder));
      if (type === "number") {
        f.input.min = key === "min_duration" ? "0" : "0.1";
        f.input.step = "0.1";
      }
      f.input.addEventListener("input", () => {
        const next = recallEditable(draft.data.recall);
        const raw = f.input.value.trim();
        next.filters[key] = type === "number" ? (raw === "" ? (key === "min_duration" ? 0 : null) : Number(raw)) : raw || null;
        commit(next);
      });
      filters.append(f.wrap);
    }
    section.append(filters);
    if (mode !== "text") section.append(el("p", MindexI18n.msg("ui.105e58965eba"), "muted"));
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
  !draft.dirty && draft.baseContent !== plan().shots[sid].content)
  {
    draft = {
      baseContent: plan().shots[sid].content,
      original: { ...original },
      data: { ...original },
      dirty: false
    };
    state.drafts.set(draftKey(sid), draft);
  }
  const fields = el("div", "", "shot-editor-fields");
  const inputs = {};
  for (const [name, label, type] of [
  ["text", MindexI18n.msg("ui.58c9fb9115f5"), "textarea"],
  ["visual", MindexI18n.msg("ui.visual_label"), "textarea"],
  ["query", MindexI18n.msg("ui.recall_keywords_label"), "textarea"],
  ["estimated_seconds", MindexI18n.msg("ui.9e54a98e61db"), "number"]])
  {
    const f = field(label, draft.data[name], type);
    f.input.dataset.shotInput = "true";
    f.input.dataset.shotField = name;
    inputs[name] = f.input;
    // visual 不设 maxLength：浏览器按 UTF-16 计数，与服务端按码点计的 120 字上限不一致，超长交给服务端 422。
    if (name === "visual") f.input.placeholder = MindexI18n.msg("ui.visual_placeholder");else
    if (type === "textarea") f.input.maxLength = name === "text" ? 200 : 400;else
    {
      f.input.min = "0.001";
      f.input.max = "60";
    }
    f.input.addEventListener("input", () => {
      // visual 为空白时删键而非存空串，保证老分镜（无 visual 键）的 dirty 判定与已存内容一致。
      if (name === "visual" && !f.input.value.trim()) delete draft.data.visual;else
      draft.data[name] =
      type === "number" ? Number(f.input.value) : f.input.value;
      draft.dirty =
      JSON.stringify(draft.data) !== JSON.stringify(draft.original);
      refreshShotCard(sid);
    });
    fields.append(f.wrap);
  }
  // 检索粒度/返回数量与检索页同款选项；默认「跟随方案」，选定后仅覆盖本分镜的召回。
  const markDirty = () => {
    draft.dirty = JSON.stringify(draft.data) !== JSON.stringify(draft.original);
    refreshShotCard(sid);
  };
  const planGranularity =
    GRANULARITY_LABEL[state.data.recall_granularity] || state.data.recall_granularity || "—";
  const overrides = el("div", "", "recall-return-options");
  for (const [name, label, choices] of [
  ["granularity", MindexI18n.msg("ui.770c916c8efa"), [
  ["", MindexI18n.msg("ui.2920edc15f73", { p0: planGranularity })],
  ...Object.entries(GRANULARITY_LABEL)]],

  ["recall_limit", MindexI18n.msg("ui.63a1fd1a6eab"), [
  ["", MindexI18n.msg("ui.2920edc15f73", { p0: plan().options.recall_limit })],
  ["20", "20"], ["10", "10"], ["50", "50"]]]])
  {
    const f = field(label, draft.data[name] ?? "", "text", choices);
    f.input.dataset.shotInput = "true";
    f.input.addEventListener("change", () => {
      // 选回「跟随方案」时删键而非置 null，保证 dirty 判定与旧 content 一致。
      if (f.input.value === "") delete draft.data[name];
      else draft.data[name] = name === "recall_limit" ? Number(f.input.value) : f.input.value;
      markDirty();
    });
    overrides.append(f.wrap);
  }
  // 按画面意象重写召回词：只读请求（不加锁、不落盘），因此用普通按钮，不受构建/未保存锁影响；
  // 建议结果只写入草稿，需「保存此镜」后才生效。
  const rewrite = el("div", "", "visual-rewrite");
  const rewriteStatus = el("p", "", "warn-text visual-rewrite-status");
  rewriteStatus.setAttribute("role", "status");
  rewriteStatus.hidden = true;
  const rewriteButton = button(MindexI18n.msg("ui.visual_rewrite_query"), () =>
  rewriteQuery(sid, draft, {
    button: rewriteButton,
    status: rewriteStatus,
    queryInput: inputs.query,
    markDirty
  }));
  rewriteButton.dataset.rewriteQuery = "true";
  rewrite.append(rewriteButton, rewriteStatus);
  fields.append(rewrite);
  body.append(fields);
  // 召回设置（含粒度/数量覆盖）只对素材召回来源生效；其他来源只显示来源设置。
  if (renderSourceEditor(body, sid, draft)) renderRecallEditor(body, sid, draft, overrides);
  const actions = el("div", "", "detail-actions");
  actions.append(
    mutationButton(MindexI18n.msg("ui.761f731d682b"), () => saveShot(sid), false, true),
    mutationButton(MindexI18n.msg("ui.11eecf503986"),

    () => {
      state.drafts.delete(draftKey(sid));
      invalidatePanel(sid);
      renderMatrix();
    },
    false,
    true
    )
  );
  const index = order().indexOf(sid);
  for (const [op, label, disabled] of [
  ["up", MindexI18n.msg("ui.8a0c839791d3"), index === 0],
  ["down", MindexI18n.msg("ui.05c46fa3b77c"), index === order().length - 1],
  ["merge", MindexI18n.msg("ui.c2226d04d16b"), index === 0],
  ["insert", MindexI18n.msg("ui.fa5046a548d6"), false],
  ["delete", MindexI18n.msg("ui.aeef3a6ac335"), order().length === 1]])

  actions.append(
    mutationButton(label, () => changeStructure(sid, op), disabled)
  );
  const history = el("div");
  history.dataset.rowHistory = "content";
  body.append(actions, history);
}
// 合并分镜的画面意象：跳过空值，按顺序用「；」拼接，按 Unicode 码点截断到 120 字（与服务端 len 一致）；全空返回空串。
function joinVisual(...values) {
  const joined = values.
  map((v) => typeof v === "string" ? v.trim() : "").
  filter(Boolean).
  join("；");
  return Array.from(joined).slice(0, 120).join("");
}
// 按画面意象重写召回词：以草稿中当前 visual 请求建议，成功后只替换草稿的 query 并标记未保存；
// 失败时在编辑器状态区显示中文错误，草稿与未保存标记保持不变。请求期间禁用按钮，结束后恢复。
async function rewriteQuery(sid, draft, ui) {
  if (ui.button.disabled) return;
  ui.button.disabled = true;
  MindexI18n.setText(ui.button, MindexI18n.msg("ui.visual_rewrite_busy"));
  ui.status.hidden = true;
  MindexI18n.setText(ui.status, "");
  try {
    const result = await api(`${planBase()}/shots/${sid}/query-suggestion`, "POST", {
      visual: draft.data.visual ?? ""
    });
    draft.data.query = result.query;
    if (ui.queryInput) ui.queryInput.value = result.query;
    ui.markDirty();
  } catch (error) {
    // 服务端错误已由 api() 译为当前语言；fetch 本身失败（网络错误）时补上「召回词重写失败」前缀。
    const message = error?.name === "TypeError" ?
    MindexI18n.msg("ui.visual_rewrite_failed", { p0: error.message }) :
    error?.message || MindexI18n.msg("ui.visual_rewrite_failed", { p0: "" });
    MindexI18n.setText(ui.status, shortIds(message));
    ui.status.hidden = false;
  } finally {
    ui.button.disabled = false;
    MindexI18n.setText(ui.button, MindexI18n.msg("ui.visual_rewrite_query"));
  }
}
function currentBoard() {
  return order().map((sid) => ({ ...ver(plan().shots[sid].content).data }));
}
async function saveShot(sid) {
  const key = draftKey(sid),
    draft = state.drafts.get(key),
    scope = scopeKey();
  if (draft.baseContent !== plan().shots[sid].content)
  throw new Error(MindexI18n.msg("ui.9af274c47a7b"));
  if (["clip", "image"].includes(draft.data.source?.kind) && !draft.data.source.media_id) throw new Error(MindexI18n.msg("ui.source_need_file"));
  const shots = currentBoard();
  const edited = {
    ...draft.data,
    text: draft.data.text.trim(),
    query: draft.data.query.trim()
  };
  // 其他镜由 currentBoard() 带上已存 visual；当前镜去首尾空白，空值删键。
  const visual = (edited.visual ?? "").trim();
  if (visual) edited.visual = visual;else
  delete edited.visual;
  shots[order().indexOf(sid)] = edited;
  const result = await api(planBase(), "PATCH", {
    revision: state.data.revision,
    shots
  });
  state.drafts.delete(key);
  if (scope !== scopeKey()) return;
  invalidatePanel(sid);
  await accept(result);
  notice(MindexI18n.msg("ui.3579af078651"));
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
    estimated_seconds: 3
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
    const visual = joinVisual(shots[index - 1].visual, shots[index].visual);
    if (visual) shots[index - 1].visual = visual;else
    delete shots[index - 1].visual;
    // 保留上一镜召回设置；上一镜为默认文字召回时继承被合并镜的设置。
    if (recallIsDefault(recallEditable(shots[index - 1].recall)) && shots[index].recall)
    shots[index - 1].recall = shots[index].recall;
    // 背景音乐提示点：保留上一镜的；上一镜没有时继承被合并镜的。
    if (!shots[index - 1].music && shots[index].music) shots[index - 1].music = shots[index].music;
    // 转场：保留上一镜的入场，出场与到下一镜的重叠转场取被合并镜的（与智能助手 edit_shots 一致）。
    const first = shots[index - 1].transition || {},
      second = shots[index].transition || {};
    const merged = Object.fromEntries([["enter", first.enter], ["leave", second.leave], ["next", second.next]].filter(([, v]) => v));
    if (Object.keys(merged).length) shots[index - 1].transition = merged;else
    delete shots[index - 1].transition;
    const words = [...new Set([...(shots[index - 1].highlights || []), ...(shots[index].highlights || [])])].slice(0, 10);
    if (words.length) shots[index - 1].highlights = words;
    shots.splice(index, 1);
  }
  const result = await api(planBase(), "PATCH", {
    revision: state.data.revision,
    shots
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
      state.selection.set(scopeKey(), { sid: added, stage: "content" });
      state.revealShot = added;
    }
  }
  if (scope === scopeKey()) await accept(result);
}
