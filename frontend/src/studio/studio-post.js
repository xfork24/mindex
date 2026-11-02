/* 创作页 · 分镜后期：转场（镜内 enter/leave、到下一镜的重叠转场 next）、字幕高亮词与文字层。显示在“分镜渲染”阶段详情里，
   保存经方案 PATCH 写入分镜内容（与智能助手 edit_shots、CLI studio shot set 走同一接口）。
   按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
const EDGE_KINDS = ["fade", "white", "zoom", "slide"];
const OVERLAP_KINDS = ["fade", "dissolve", "fadeblack", "fadewhite", "wipeleft", "wiperight", "slideleft", "slideright",
"circleopen", "circleclose", "smoothleft", "smoothright"];
function shotTransition(sid) {
  return ver(plan().shots[sid]?.content)?.data?.transition || null;
}
function edgeText(edge) {
  return MindexI18n.msg("ui.post_edge_value", { p0: MindexI18n.msg(`ui.post_edge_${edge.kind}`), p1: edge.duration });
}
// 一行转场摘要（分镜渲染列与阶段详情共用）；硬切返回空字符串。
function transitionSummary(sid) {
  const value = shotTransition(sid);
  if (!value) return "";
  const parts = [];
  if (value.enter) parts.push(MindexI18n.msg("ui.post_summary_enter", { p0: edgeText(value.enter) }));
  if (value.leave) parts.push(MindexI18n.msg("ui.post_summary_leave", { p0: edgeText(value.leave) }));
  if (value.next) parts.push(MindexI18n.msg("ui.post_summary_next", { p0: value.next.kind || "fade", p1: value.next.duration }));
  return parts.length ? MindexI18n.msg("ui.post_summary", { p0: parts.join(" · ") }) : "";
}
function transitionEditable(value) {
  const copy = JSON.parse(JSON.stringify(value || {}));
  return { enter: copy.enter || null, leave: copy.leave || null, next: copy.next || null };
}
// 一个转场位置的编辑行：类型下拉（“无”表示不设置）+ 时长输入。
function transitionRow(label, kinds, current, labelOf, onChange) {
  const row = el("div", "", "post-row");
  const choices = [["", MindexI18n.msg("ui.post_none")], ...kinds.map((kind) => [kind, labelOf(kind)])];
  const kind = field(label, current?.kind || "", "text", choices);
  const seconds = field(MindexI18n.msg("ui.post_duration"), current?.duration ?? 0.5, "number");
  seconds.input.min = "0.1";
  seconds.input.max = "2";
  seconds.input.step = "0.1";
  seconds.wrap.hidden = !current;
  const emit = () => {
    const chosen = kind.input.value;
    seconds.wrap.hidden = !chosen;
    const duration = Math.min(2, Math.max(0.1, Number(seconds.input.value) || 0.5));
    onChange(chosen ? { kind: chosen, duration } : null);
  };
  kind.input.addEventListener("change", emit);
  seconds.input.addEventListener("change", emit);
  row.append(kind.wrap, seconds.wrap);
  return row;
}
function renderPostSection(body, sid) {
  const section = el("div", "", "post-editor");
  section.dataset.postEditor = sid;
  section.append(el("h4", MindexI18n.msg("ui.post_heading")));
  section.append(el("p", transitionSummary(sid) || MindexI18n.msg("ui.post_hard_cut"), "muted"));
  const key = draftKey(sid);
  state.postDrafts = state.postDrafts || new Map();
  let draft = state.postDrafts.get(key);
  const base = plan().shots[sid].content;
  if (!draft || draft.base !== base) {
    draft = { base, data: transitionEditable(shotTransition(sid)),
      highlights: [...(ver(base)?.data?.highlights || [])],
      overlays: JSON.parse(JSON.stringify(ver(base)?.data?.overlays || [])) };
    state.postDrafts.set(key, draft);
  }
  const value = draft.data;
  const edgeLabel = (kind) => MindexI18n.msg(`ui.post_edge_${kind}`);
  const transitions = el("div", "", "post-transitions");
  transitions.append(
    transitionRow(MindexI18n.msg("ui.post_enter"), EDGE_KINDS, value.enter, edgeLabel, (edge) => {value.enter = edge;}),
    transitionRow(MindexI18n.msg("ui.post_leave"), EDGE_KINDS, value.leave, edgeLabel, (edge) => {value.leave = edge;}),
    transitionRow(MindexI18n.msg("ui.post_next"), OVERLAP_KINDS, value.next, (kind) => kind, (edge) => {value.next = edge;})
  );
  section.append(transitions);
  section.append(el("p", MindexI18n.msg("ui.post_help"), "muted"));
  const words = field(MindexI18n.msg("ui.post_highlights"), draft.highlights.join("，"));
  words.input.dataset.postHighlights = sid;
  MindexI18n.setAttr(words.input, "placeholder", MindexI18n.msg("ui.post_highlights_placeholder"));
  words.input.addEventListener("input", () => {
    draft.highlights = [...new Set(words.input.value.split(/[,，、\s]+/).map((word) => word.trim()).filter(Boolean))];
  });
  section.append(words.wrap, el("p", MindexI18n.msg(plan().options.burn ? "ui.post_highlights_help" :
  "ui.post_highlights_soft"), "muted"));
  renderOverlayEditor(section, sid, draft);
  if (shotStages(sid).includes("overlay")) renderOverlayStage(section, sid);
  const last = order().indexOf(sid) === order().length - 1;
  if (last && value.next) section.append(el("p", MindexI18n.msg("ui.post_last_next"), "warn-text"));
  const notes = ver(plan().shots[sid].render)?.data?.transition_notes;
  if (notes?.length) section.append(el("p", notes.map((note) => MindexI18n.system(note)).join("；"), "warn-text"));
  const actions = el("div", "", "detail-actions");
  actions.append(mutationButton(MindexI18n.msg("ui.post_save"), () => savePost(sid, draft)));
  section.append(actions);
  body.append(section);
}
async function savePost(sid, draft) {
  if (state.drafts.get(draftKey(sid))?.dirty) throw new Error(MindexI18n.msg("ui.music_content_dirty"));
  const shots = currentBoard();
  const shot = shots[order().indexOf(sid)];
  const transition = Object.fromEntries(Object.entries(draft.data).filter(([, edge]) => edge));
  if (Object.keys(transition).length) shot.transition = transition;else delete shot.transition;
  if (draft.highlights.length) shot.highlights = draft.highlights;else delete shot.highlights;
  const layers = draft.overlays.map(cleanOverlay);
  if (layers.length) shot.overlays = layers;else delete shot.overlays;
  const scope = scopeKey();
  const result = await api(planBase(), "PATCH", { revision: state.data.revision, shots });
  state.postDrafts?.delete(draftKey(sid));
  if (scope !== scopeKey()) return;
  invalidatePanel(sid);
  await accept(result);
  notice(MindexI18n.msg("ui.post_saved"));
}

const OVERLAY_KINDS = ["title", "lower_third", "data_card", "custom"];
// 提交前去掉空字段（与服务端 ShotOverlay 的 dump 一致）；数据行由“标签=值”文本解析。
function cleanOverlay(layer) {
  const result = { engine: layer.kind === "custom" ? "html" : layer.engine || "ass", kind: layer.kind, effect: layer.effect || "fade" };
  for (const key of ["title", "subtitle", "brief"]) if (layer[key]?.trim()) result[key] = layer[key].trim();
  for (const key of ["start", "end"]) if (layer[key] !== null && layer[key] !== undefined && layer[key] !== "") result[key] = Number(layer[key]);
  if (layer.items?.length) result.items = layer.items;
  return result;
}
function itemsText(items) {
  return (items || []).map((item) => `${item.label}=${item.value}`).join("\n");
}
function parseItems(text) {
  return text.split(/\n+/).map((line) => line.trim()).filter(Boolean).map((line) => {
    const [label, ...rest] = line.split("=");
    return { label: label.trim(), value: rest.join("=").trim() };
  }).filter((item) => item.label && item.value);
}
function renderOverlayEditor(section, sid, draft) {
  const box = el("div", "", "overlay-editor");
  box.dataset.overlayEditor = sid;
  box.append(el("h5", MindexI18n.msg("ui.overlay_heading")));
  const redraw = () => {
    const next = el("div");
    renderOverlayEditor(next, sid, draft);
    box.replaceWith(next.firstChild);
  };
  draft.overlays.forEach((layer, index) => {
    const card = el("div", "", "overlay-item");
    const kind = field(MindexI18n.msg("ui.overlay_kind"), layer.kind, "text",
    OVERLAY_KINDS.map((name) => [name, MindexI18n.msg(`ui.overlay_kind_${name}`)]));
    kind.input.addEventListener("change", () => {layer.kind = kind.input.value;redraw();});
    const engine = field(MindexI18n.msg("ui.overlay_engine"), layer.kind === "custom" ? "html" : layer.engine || "ass", "text",
    [["ass", MindexI18n.msg("ui.overlay_engine_ass")], ["html", MindexI18n.msg("ui.overlay_engine_html")]]);
    engine.input.disabled = layer.kind === "custom";
    engine.input.addEventListener("change", () => {layer.engine = engine.input.value;});
    const effect = field(MindexI18n.msg("ui.overlay_effect"), layer.effect || "fade", "text",
    ["none", "fade", "slide"].map((name) => [name, MindexI18n.msg(`ui.overlay_effect_${name}`)]));
    effect.input.addEventListener("change", () => {layer.effect = effect.input.value;});
    const row = el("div", "", "post-row");
    row.append(kind.wrap, engine.wrap, effect.wrap);
    card.append(row);
    const text = (key, label, max) => {
      const f = field(label, layer[key] ?? "");
      f.input.maxLength = max;
      f.input.addEventListener("input", () => {layer[key] = f.input.value;});
      return f.wrap;
    };
    if (layer.kind === "custom") {
      const brief = field(MindexI18n.msg("ui.overlay_brief"), layer.brief ?? "", "textarea");
      brief.input.maxLength = 1000;
      brief.input.addEventListener("input", () => {layer.brief = brief.input.value;});
      card.append(brief.wrap);
    } else {
      card.append(text("title", MindexI18n.msg("ui.overlay_title"), 60));
      if (layer.kind !== "data_card") card.append(text("subtitle", MindexI18n.msg("ui.overlay_subtitle"), 80));
    }
    if (layer.kind === "data_card") {
      const items = field(MindexI18n.msg("ui.overlay_items"), itemsText(layer.items), "textarea");
      MindexI18n.setAttr(items.input, "placeholder", MindexI18n.msg("ui.overlay_items_placeholder"));
      items.input.addEventListener("input", () => {layer.items = parseItems(items.input.value);});
      card.append(items.wrap);
    }
    const times = el("div", "", "post-row");
    for (const key of ["start", "end"]) {
      const f = field(MindexI18n.msg(`ui.overlay_${key}`), layer[key] ?? "", "number");
      f.input.min = "0";
      f.input.step = "0.1";
      f.input.addEventListener("change", () => {layer[key] = f.input.value === "" ? null : Number(f.input.value);});
      times.append(f.wrap);
    }
    card.append(times, button(MindexI18n.msg("ui.overlay_remove"), () => {draft.overlays.splice(index, 1);redraw();}));
    box.append(card);
  });
  if (draft.overlays.length < 4)
  box.append(button(MindexI18n.msg("ui.overlay_add"), () => {
    draft.overlays.push({ engine: "ass", kind: "title", title: "", effect: "fade" });
    redraw();
  }));
  box.append(el("p", MindexI18n.msg("ui.overlay_help"), "muted"));
  section.append(box);
}

// HTML 文字层的叠加层源码：生成 / 重新生成（overlay 阶段单节点执行），逐层查看与人工编辑（人工版本被钉住）。
function renderOverlayStage(section, sid) {
  const box = el("div", "", "overlay-stage");
  box.dataset.overlayStage = sid;
  box.append(el("h5", MindexI18n.msg("ui.overlay_stage_heading")));
  const item = ver(plan().shots[sid].overlay);
  if (!item) box.append(el("p", MindexI18n.msg("ui.overlay_stage_none"), "muted"));else
  {
    const origin = item.data?.origin === "manual" ? MindexI18n.msg("ui.overlay_stage_manual") : MindexI18n.msg("ui.overlay_stage_generated");
    box.append(el("p", `${origin} · ${statusText(item.status)}`, item.status === "failed" ? "warn-text" : "muted"));
    if (item.error) box.append(el("p", shortIds(MindexI18n.system(item.error)), "warn-text"));
  }
  const actions = el("div", "", "detail-actions");
  actions.append(mutationButton(MindexI18n.msg("ui.fb03cbd3e2da"), () => runNode("overlay", sid, false)),
  mutationButton(MindexI18n.msg("ui.c6a70df5d5ee"), () => runNode("overlay", sid, true)));
  const layers = ver(plan().shots[sid].content)?.data?.overlays || [];
  layers.forEach((layer, index) => {
    if (layer.engine !== "html") return;
    actions.append(mutationButton(MindexI18n.msg("ui.overlay_edit_source", { p0: index + 1 }), () => openOverlayEditor(sid, index)));
  });
  box.append(actions);
  section.append(box);
}
async function openOverlayEditor(sid, index) {
  const vid = plan().shots[sid].overlay, item = ver(vid);
  if (!item?.files?.[`overlay-${index}`]) throw new Error(MindexI18n.msg("ui.overlay_source_missing"));
  const response = await fetch(`${base()}/versions/${vid}/overlay-${index}.html`);
  if (!response.ok) throw new Error(MindexI18n.msg("ui.animate_load_failed", { p0: response.status }));
  const source = await response.text();
  const scope = scopeKey();
  const body = showDialog(MindexI18n.msg("ui.overlay_editor_title", { p0: shortId(sid), p1: index + 1 }));
  body.append(el("p", MindexI18n.msg("ui.overlay_editor_hint"), "muted"));
  const editor = field(MindexI18n.msg("ui.animate_source_label"), "", "textarea");
  editor.input.value = source;
  editor.input.rows = 24;
  editor.input.spellcheck = false;
  editor.input.classList.add("code-editor");
  const actions = el("div", "", "detail-actions");
  actions.append(
    button(MindexI18n.msg("ui.animate_save"), async () => {
      if (scope !== scopeKey()) throw new Error(MindexI18n.msg("ui.animate_scope_changed"));
      const data = await api(`${planBase()}/shots/${sid}/overlay`, "PATCH",
      { revision: state.data.revision, index, html: editor.input.value });
      closeDialog();
      await accept(data);
      notice(MindexI18n.msg("ui.overlay_saved"));
    }),
    button(MindexI18n.msg("ui.4d0b4688c787"), closeDialog)
  );
  body.append(editor.wrap, actions);
}
