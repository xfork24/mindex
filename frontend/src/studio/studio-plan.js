/* 创作页 · 方案新建/重命名与方案设置。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
const CANVAS_LABELS = {
  "16:9": "ui.plan_canvas_landscape",
  "9:16": "ui.plan_canvas_portrait",
  "1:1": "ui.plan_canvas_square"
};
function createPlan(mode, finalId = null) {
  const body = showDialog(mode === "restore" ? MindexI18n.msg("ui.8fc235b8dfed") : MindexI18n.msg("ui.373241734247"));
  const f = field(MindexI18n.msg("ui.5fd61b9aa9cf"),

  mode === "new" ? MindexI18n.msg("ui.e2135c621b66") : MindexI18n.msg("ui.dff07ba564ad", { p0: plan().name })
  );
  body.append(
    f.wrap,
    button(MindexI18n.msg("ui.ede701c8b470"), async () => {
      await accept(
        await api(`${base()}/plans`, "POST", {
          name: f.input.value,
          ...(mode === "copy" ? { copy_from: plan().id } : {}),
          ...(mode === "restore" ? { from_final: finalId } : {})
        })
      );
      closeDialog();
    })
  );
}
function renamePlan() {
  const body = showDialog(MindexI18n.msg("ui.a2397a951f7f")),
    f = field(MindexI18n.msg("ui.5fd61b9aa9cf"), plan().name);
  body.append(
    f.wrap,
    button(MindexI18n.msg("ui.fadf24dbc5a9"), async () => {
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          name: f.input.value
        })
      );
      closeDialog();
    })
  );
}
function voiceChoices(catalog, current, defaultVoice) {
  const clones = (catalog.library || []).filter((item) => item.kind === "clone");
  const cloneIds = new Set(clones.map((item) => item.voice_id));
  const preset = [["", MindexI18n.msg("ui.28223da9fbc7", {
    p0: defaultVoice ? `（${defaultVoice}）` : ""
  })]];
  const seen = new Set([""]);
  for (const group of catalog.groups || []) {
    for (const voice of group.voices || []) {
      if (!voice.voice_id || cloneIds.has(voice.voice_id) || seen.has(voice.voice_id)) continue;
      seen.add(voice.voice_id);
      preset.push([voice.voice_id, MindexI18n.voice(voice)]);
    }
  }
  if (current && !cloneIds.has(current) && !seen.has(current)) {
    preset.push([current, current]);
  }
  return {
    preset,
    clones: [["", MindexI18n.msg("ui.no_cloned_voice")],
      ...clones.map((item) => [item.voice_id, item.name || item.voice_id])],
    presetValue: cloneIds.has(current) ? "" : current || "",
    cloneValue: cloneIds.has(current || defaultVoice) ? current || defaultVoice : ""
  };
}

async function editSettings() {
  // 立即开对话框：先用占位音色项渲染，再异步拉目录回填，避免被 TTS 服务往返阻塞。
  const loadingLabel = MindexI18n.msg("ui.fcabadb2a772");
  const body = showDialog(MindexI18n.msg("ui.b876f5881f16")),
    fields = {},
    o = plan().options;
  const definitions = [
  ["preset_voice", MindexI18n.msg("ui.preset_voice_select"), "text", [["", loadingLabel]]],
  ["clone_voice", MindexI18n.msg("ui.cloned_voice_select"), "text", [["", loadingLabel]]],
  ["recall_limit", MindexI18n.msg("ui.4f891e5b3cf5"), "number"],
  [
  "audio_mode", MindexI18n.msg("ui.b47469b59094"),

  "text",
  [
  ["tts_only", MindexI18n.msg("ui.2d45e91b9c1f")],
  ["ducking", MindexI18n.msg("ui.f3f417d1a846")]]],


  ["duck_gain", MindexI18n.msg("ui.cd1a77eec0a1"), "number"],
  [
  "burn", MindexI18n.msg("ui.5f7b81907a7c"),

  "text",
  [
  ["true", MindexI18n.msg("ui.caac27bd4051")],
  ["false", MindexI18n.msg("ui.2cf51a1af708")]]],


  ["atempo", MindexI18n.msg("ui.8a1c10056984"), "number"],
  ["canvas", MindexI18n.msg("ui.plan_canvas_label"), "text", ["16:9", "9:16", "1:1"].map((name) => [
    name, MindexI18n.msg(CANVAS_LABELS[name], { p0: (state.data.canvases?.[name] || []).join("×") })])],
  ["fit", MindexI18n.msg("ui.plan_fit_label"), "text", [
    ["pad", MindexI18n.msg("ui.plan_fit_pad")],
    ["crop", MindexI18n.msg("ui.plan_fit_crop")]]]];

  for (const [key, label, type, choices] of definitions) {
    const value = key === "preset_voice" || key === "clone_voice" ? "" :
    key === "atempo" ? o.atempo ?? 1 :
    key === "canvas" ? o.canvas || "16:9" :
    key === "fit" ? o.fit || "pad" : o[key];
    const f = field(label, value, type, choices);
    fields[key] = f.input;
    body.append(f.wrap);
    const helpKey = {
      duck_gain: "ui.original_gain_help",
      atempo: "ui.narration_speed_help",
      fit: "ui.plan_canvas_help"
    }[key];
    if (helpKey) body.append(el("p", MindexI18n.msg(helpKey), "muted"));
  }
  // 音色目录尚未就绪：先禁用音色相关控件与保存，待目录到达再回填与放开。
  fields.preset_voice.disabled = true;
  fields.clone_voice.disabled = true;
  let voiceChanged = false;
  fields.preset_voice.addEventListener("change", () => { voiceChanged = true; });
  fields.clone_voice.addEventListener("change", () => { voiceChanged = true; });
  const updatePresetVoice = () => {
    fields.preset_voice.disabled = Boolean(fields.clone_voice.value);
  };
  fields.clone_voice.addEventListener("change", updatePresetVoice);
  fields.atempo.min = "0.75";
  fields.atempo.max = "1.25";
  const save = button(MindexI18n.msg("ui.3fab71cb32ac"), async () => {
      const options = { ...o };
      for (const key of Object.keys(fields)) {
        if (key !== "preset_voice" && key !== "clone_voice") options[key] = fields[key].value;
      }
      for (const key of ["recall_limit", "duck_gain"])
      options[key] = Number(options[key]);
      options.atempo =
      fields.atempo.value === "" ? 1 : Number(fields.atempo.value);
      options.burn = options.burn === "true";
      if (voiceChanged) {
        options.voice = fields.clone_voice.value || fields.preset_voice.value || null;
        options.voice_mode = options.voice ? "custom" : "inherit";
      }
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          options
        })
      );
      closeDialog();
      notice(MindexI18n.msg("ui.1e3dd518b146"));
    });
  save.disabled = true;
  body.append(save);
  // 异步回填音色下拉：对话框已可见，TTS 服务往返不再阻塞打开动作。
  const dialogBody = body;
  loadVoiceCatalogue().then((catalog) => {
    if ($("dialog-content") !== dialogBody || !$("work-dialog").open) return;
    const defaultVoice = state.data.tts_settings?.default_voice || catalog?.default_voice || "";
    const voices = voiceChoices(catalog, o.voice_mode === "custom" ? o.voice : "", defaultVoice);
    fillSelect(fields.preset_voice, voices.preset, voices.presetValue);
    fillSelect(fields.clone_voice, voices.clones, voices.cloneValue);
    fields.clone_voice.disabled = false;
    updatePresetVoice();
    save.disabled = false;
    if (catalog?.error) notice(catalog.error);
  }).catch(() => {
    if ($("dialog-content") !== dialogBody) return;
    notice(MindexI18n.msg("ui.voice_catalog_unavailable"));
    closeDialog();
  });
}
function fillSelect(select, choices, value) {
  select.replaceChildren();
  for (const [v, text] of choices) {
    const o = el("option", text);
    o.value = v;
    select.append(o);
  }
  select.value = MindexI18n.render(value ?? "");
}
