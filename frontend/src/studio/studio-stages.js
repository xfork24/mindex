/* 创作页 · 阶段详情、整列执行、执行日志、历史版本与单节点执行。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
function renderStageDetails(body, sid, stage) {
  const vid = plan().shots[sid][stage],
    item = ver(vid),
    actions = el("div", "", "detail-actions");
  actions.append(
    mutationButton(stage === "select" ? MindexI18n.msg("ui.5d9c0feb3855") : MindexI18n.msg("ui.fb03cbd3e2da"), () => runNode(stage, sid, false)),
    mutationButton(stage === "select" ? MindexI18n.msg("ui.2c2583899752") : MindexI18n.msg("ui.c6a70df5d5ee"), () => runNode(stage, sid, true))
  );
  if (item) actions.append(button(MindexI18n.msg("ui.ae6c80665732"), () => showVersion(vid)));
  const result = el("p", "", "warn-text");
  result.dataset.nodeResult = "true";
  result.setAttribute("role", "status");
  body.append(actions, result);
  if (stage === "recall") body.append(el("p", MindexI18n.msg("ui.796ebaa8b64a"),
  "muted"));
  const recallSource = shotStages(sid).includes("recall");
  const aigcSource = shotStages(sid).includes("generate");
  if (stage === "select") body.append(el("p", MindexI18n.msg(recallSource ? "ui.6ad63039cb96" :
  aigcSource ? "ui.select_generated_help" : "ui.select_clip_help"),
  "muted"));
  if (stage === "generate") body.append(el("p", MindexI18n.msg("ui.generate_help"), "muted"));
  if (stage === "draw") body.append(el("p", MindexI18n.msg("ui.draw_help"), "muted"));
  if (stage === "animate") {
    body.append(el("p", MindexI18n.msg("ui.animate_help"), "muted"));
    // 人工编辑源码：没有成功版本时也可从空白模板开始写。
    actions.append(mutationButton(MindexI18n.msg("ui.animate_edit_source"), () => openAnimationEditor(sid)));
  }
  if (stage === "tts") {
    const current = state.data.tts_settings;
    body.append(el("p", current ? MindexI18n.msg("ui.905f065f533b", { p0:
      current.voice_mode === "inherit" ? MindexI18n.msg("ui.79cde58eb13b") : MindexI18n.msg("ui.7a506144d437"), p1: current.voice, p2: current.model }) : MindexI18n.msg("ui.b695402e923b", { p0:
      plan().options.voice || MindexI18n.msg("ui.79cde58eb13b") }), "muted"));
  }
  if (!item) body.append(el("p", MindexI18n.msg("ui.f72548296145"), "muted"));else
  {
    if (item.incomplete)
    body.append(
      el("p", MindexI18n.msg("ui.6ca99f1c90fb"), "warn-text")
    );
    if (stage === "recall") {
      const recall = item.data.recall,
        meta = item.data.meta || {};
      body.append(
        el(
          "p", MindexI18n.msg("ui.ee6cc02c2e4c", { p0:
            RECALL_MODE_LABEL[item.data.mode || "text"], p1: item.data.query || MindexI18n.msg("ui.f1b46a8c5d6c"), p2: item.data.results?.length || 0 })
        )
      );
      if (recall?.person_ids?.length)
      body.append(
        el(
          "p", MindexI18n.msg("ui.e2ee09bf74bf", { p0:
            recall.person_ids.map(personName).join("、"), p1: recall.person_mode === "onscreen" ? MindexI18n.msg("ui.0b697429bd52") : recall.person_mode === "mentioned" ? MindexI18n.msg("ui.9f7ab435be94") : MindexI18n.msg("ui.4afad877551a"), p2: recall.person_relation === "any" ? MindexI18n.msg("ui.31af9a60c654") : MindexI18n.msg("ui.3bd33bf40e37") })
        )
      );
      const sources = recall?.filters?.source_ids?.length ? recall.filters.source_ids : recall?.filters?.source_id ? [recall.filters.source_id] : [];
      const jobMaterials = jobScope();
      if (sources.length)
      body.append(el("p", MindexI18n.msg("ui.81f420e6a6d2", { p0: sources.map(sourceName).join("、") })));
      else if (jobMaterials)
      body.append(el("p", MindexI18n.msg("ui.9213d98274d9", { n: jobMaterials.size }), "muted"));
      for (const [key, label] of [["scene", "ui.625b392c7b8a"], ["emotion", "ui.61fe61644a75"], ["shot_size", "ui.6867f7b4537a"], ["min_duration", "ui.26b6f15b345b"], ["max_duration", "ui.2de8ce6144b7"]]) {
        if (recall?.filters?.[key]) body.append(el("p", `${MindexI18n.msg(label)}：${recall.filters[key]}`));
      }
      if (meta.referenced_people?.length)
      body.append(el("p", MindexI18n.msg("ui.b607edb41246", { p0: [...new Set(meta.referenced_people.map(p => p.name))].join('、') }), "muted"));
      if (meta.person_coverage_missing?.length)
      body.append(
        el(
          "p", MindexI18n.msg("ui.75671397b702", { p0:
            meta.person_coverage_missing.map(sourceName).join("、") }),
          "warn-text"
        )
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
          "p", MindexI18n.msg("ui.bfc2b2b2a536", { p0:
            item.parameters.voice || MindexI18n.msg("ui.9865b5e9b32d"), p1: secs(item.data.duration) })
        )
      );
      if (item.files.audio) body.append(media("audio", item.files.audio.url));
    } else if (stage === "draw") {
      // 生成图片：提示词、尺寸与图片预览；渲染阶段按静态图片推拉/平移。
      const d = item.data;
      body.append(el("p", MindexI18n.msg("ui.generate_prompt_line", { p0: d.prompt || "" })));
      body.append(el("p", MindexI18n.msg("ui.draw_summary", { p0: d.width || "?", p1: d.height || "?" }), "muted"));
      if (item.files.image?.url) body.append(queryPreviewNode(item.files.image.url, "image"));
    } else if (stage === "generate") {
      // 生成画面：提示词、请求/实际时长与生成结果预览（下载后统一为 MP4）。
      const d = item.data;
      body.append(el("p", MindexI18n.msg("ui.generate_prompt_line", { p0: d.prompt || "" })));
      body.append(el("p", MindexI18n.msg("ui.generate_detail", { p0: d.task_id || "?", p1: secs(d.seconds),
        p2: secs(d.duration), p3: d.width || "?", p4: d.height || "?" }), "muted"));
      if (item.files.generated?.url) body.append(media("video", item.files.generated.url));
    } else if (stage === "animate") {
      // 动画源码：来源（模型生成/人工修改）、字符数与时长；沙箱 iframe 预览（严格 CSP，不含 allow-same-origin）。
      const d = item.data;
      body.append(el("p", MindexI18n.msg("ui.animate_detail", { p0: MindexI18n.msg(d.origin === "manual" ? "ui.animate_origin_manual" : "ui.animate_origin_generated"),
        p1: d.chars ?? "?", p2: secs(d.seconds), p3: d.width || "?", p4: d.height || "?" })));
      if (d.brief) body.append(el("p", MindexI18n.msg("ui.animate_brief_line", { p0: d.brief }), "muted"));
      if (item.status === "complete" && item.files.animation) body.append(animationFrame(vid, d.width, d.height));
    } else if (stage === "select" && item.data.event.video.kind === "file") {
      // 上传片段 / 生成画面：窗口是文件内的时间。
      const d = item.data,
        video = d.event.video;
      body.append(el("p", MindexI18n.msg("ui.5e5e44f7ea35", { p0: MindexI18n.msg(d.generated ? "ui.source_aigc" : "ui.source_clip") })));
      body.append(
        el("p", MindexI18n.msg("ui.select_clip_window", { p0: secs(video.start), p1: secs(video.end),
          p2: secs((d.clip || d.generated)?.duration), p3: (d.atempo || d.event.audio.atempo).toFixed(3), p4: secs(d.freeze_seconds) }))
      );
      if (d.event.warning) body.append(el("p", shortIds(d.event.warning), "warn-text"));
    } else if (stage === "select") {
      const d = item.data,
        range = d.range,
        actual = d.actual_range || d.event.video;
      body.append(
        el(
          "p", MindexI18n.msg("ui.5e5e44f7ea35", { p0:
            sourceName(d.event.video.source_id, d.event.video.source_name) })
        )
      );
      if (d.anchor)
      body.append(
        el(
          "p", MindexI18n.msg("ui.f0786353b3bb", { p0:
            secs(d.anchor.start), p1: secs(d.anchor.end), p2: d.anchor.rank || 0 })
        )
      );
      if (range)
      body.append(
        el(
          "p", MindexI18n.msg("ui.32fb7a7b38fb", { p0:
            secs(range.start), p1: secs(range.end), p2: secs(actual.start), p3: secs(actual.end) })
        )
      );
      body.append(
        el(
          "p", MindexI18n.msg("ui.630a26be62ed", { p0:
            d.event.video.segment_ids.length, p1: (d.atempo || d.event.audio.atempo).toFixed(3), p2: secs(d.trim_seconds), p3: secs(d.freeze_seconds) })
        )
      );
      if (d.extension)
      body.append(
        el(
          "p", MindexI18n.msg("ui.453085df275b", { p0:
            secs(d.extension.before), p1: secs(d.extension.after) })
        )
      );
      if (d.event.warning) body.append(el("p", shortIds(d.event.warning), "warn-text"));
    } else if (stage === "render") {
      body.append(
        el("p", MindexI18n.msg("ui.a8e554e0bc5a", { p0: item.data.frames || "?", p1: item.data.fps || "?" }))
      );
      // 只显示带配音与字幕的分镜视频，不再单独列出音频。
      const video = renderPreviewVideo(item);
      if (video) body.append(video);
    }
  }
  if (stage === "select" && recallSource) {
    const host = el("div", "", "manual-range-host");
    body.append(host);
    if (!busy()) manualRange(sid, host).catch((error) => {
      if (host.isConnected) host.replaceChildren(el("p", shortIds(error.message), "warn-text"));
    });else
    body.append(button(MindexI18n.msg("ui.1e81d450a0cb"), () => manualRange(sid, host)));

  }
  if (stage === "tts") renderMusicSection(body, sid);
  const history = el("div");
  history.dataset.rowHistory = stage;
  body.append(el("h4", MindexI18n.msg("ui.a3bc41e89f3b")), history);
}
// 分镜渲染预览：带配音的 preview.mp4（旧版本没有时退回无声 video.mp4），叠加该镜字幕。
// 字幕取版本时间线事件 event.subtitle（相对本镜起点），生成 WebVTT 字幕轨，与成片字幕同一来源。
// 浏览器原生字幕的位置不可精确控制，所以字幕轨设为 hidden，按当前 cue 在视频上叠加一层文字：
// 字幕底边对齐可视画面底边（画幅按 pad 适配时画面上下可能带黑边，取帧检测黑边，字幕放在黑边之上）。
// 视频全屏时叠加层不可见，改用原生字幕显示。
function renderPreviewVideo(item) {
  const url = item.files.preview?.url || item.files.video?.url;
  if (!url) return null;
  const video = media("video", url);
  const wrap = el("div", "", "render-preview");
  wrap.append(video);
  const src = shotSubtitleUrl(item.data?.event?.subtitle);
  if (!src) return wrap;
  const track = document.createElement("track");
  Object.assign(track, { kind: "subtitles", srclang: "zh", src });
  MindexI18n.setAttr(track, "label", MindexI18n.msg("ui.board_subtitle_label"));
  track.addEventListener("load", () => URL.revokeObjectURL(src), { once: true });
  video.append(track);
  const caption = el("div", "", "render-subtitle");
  caption.hidden = true;
  wrap.append(caption);
  const text = track.track;
  text.mode = "hidden";
  const update = () => {
    const full = document.fullscreenElement === video;
    const active = [...text.activeCues || []].map((cue) => cue.text).join("\n");
    if (caption.textContent !== active) caption.textContent = active;
    caption.hidden = !active || full;
    placeCaption(video, caption);
  };
  text.addEventListener("cuechange", update);
  video.addEventListener("loadedmetadata", update);
  video.addEventListener("fullscreenchange", () => {
    text.mode = document.fullscreenElement === video ? "showing" : "hidden";
    update();
  });
  if (typeof ResizeObserver === "function") new ResizeObserver(() => placeCaption(video, caption)).observe(video);
  // 首帧可能尚未解码（preload=metadata）或整帧偏暗（如淡入）：在加载、跳转、播放时反复尝试，直到检测出画面。
  const events = ["loadeddata", "seeked", "playing", "timeupdate"];
  const attempt = () => {
    if (video.readyState < 2) return;
    const bottom = pictureBottom(video, video.videoWidth, video.videoHeight);
    if (bottom == null) return;
    video.dataset.pictureBottom = String(bottom);
    placeCaption(video, caption);
    for (const name of events) video.removeEventListener(name, attempt);
  };
  for (const name of events) video.addEventListener(name, attempt);
  return wrap;
}
// 叠加字幕的位置：按 object-fit: contain 求视频画面在元素中的实际矩形，字幕宽度与画面一致，
// 底边落在画面内“可视画面底边”（pictureBottom，缺省为整帧底边）；字号随画面短边缩放。
function placeCaption(video, caption) {
  const vw = video.videoWidth, vh = video.videoHeight, w = video.clientWidth, h = video.clientHeight;
  if (!vw || !vh || !w || !h) return;
  const scale = Math.min(w / vw, h / vh),
    picW = vw * scale,
    picH = vh * scale;
  const left = video.offsetLeft + (w - picW) / 2,
    top = video.offsetTop + (h - picH) / 2;
  const bottom = Math.min(1, Math.max(0, Number(video.dataset.pictureBottom || 1)));
  const wrapHeight = caption.parentElement?.clientHeight || video.offsetTop + h;
  Object.assign(caption.style, {
    left: `${left}px`,
    width: `${picW}px`,
    bottom: `${Math.max(0, wrapHeight - (top + picH * bottom))}px`,
    fontSize: `${Math.max(11, Math.round(Math.min(picW, picH) * 0.06))}px`
  });
}
// 可视画面底边占整帧高度的比例（0–1）：从下往上找第一行“有画面”的像素行；整帧都偏暗时返回 null。
// 黑边是纯黑（编码后亮度约 16 以下），画面行要求至少 2% 的像素亮度超过阈值。
function pictureBottom(source, width, height) {
  if (!width || !height) return null;
  const w = 64, h = Math.max(1, Math.round(w * height / width));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  try {
    ctx.drawImage(source, 0, 0, w, h);
  } catch {
    return null;
  }
  const { data } = ctx.getImageData(0, 0, w, h);
  const need = Math.max(1, Math.ceil(w * 0.02));
  for (let y = h - 1; y >= 0; y--) {
    let lit = 0;
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      if (0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2] > 28) lit++;
    }
    if (lit >= need) return (y + 1) / h;
  }
  return null;
}
function shotSubtitleUrl(subtitle) {
  if (!subtitle?.text || !(subtitle.end > subtitle.start)) return null;
  const stamp = (seconds) => {
    const ms = Math.max(0, Math.round(seconds * 1000));
    const pad = (n, size = 2) => String(n).padStart(size, "0");
    return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)}.${pad(ms % 1000, 3)}`;
  };
  // 字幕文本里的 "-->" 与空行会破坏 WebVTT 结构，替换掉。
  const text = String(subtitle.text).replace(/-->/g, "→").replace(/\n\s*\n/g, "\n");
  const vtt = `WEBVTT\n\n1\n${stamp(subtitle.start)} --> ${stamp(subtitle.end)}\n${text}\n`;
  return URL.createObjectURL(new Blob([vtt], { type: "text/vtt" }));
}
// 召回列里上传片段/图片分镜：预览上传的素材；更换素材或调整起点在“文案与查询”里进行。
function renderSourceDetails(body, sid) {
  const source = ver(plan().shots[sid].content)?.data?.source || {};
  const kind = source.kind === "image" ? "image" : "video";
  body.append(el("p", sourceSummary(source)));
  const host = el("div", "", "query-preview-host");
  body.append(host);
  if (!source.media_id) host.append(el("p", MindexI18n.msg("ui.source_no_file"), "muted"));else
  {
    host.append(el("p", MindexI18n.msg("ui.e454bbfe7b70"), "muted"));
    inputUrl(source.media_id).then((url) => {
      if (!url || !host.isConnected) return;
      host.replaceChildren(sourcePreviewNode(url, kind, source.start));
    });
  }
  const actions = el("div", "", "detail-actions");
  actions.append(button(MindexI18n.msg("ui.source_cell_edit"), () => openStage(sid, "content")));
  body.append(el("p", MindexI18n.msg("ui.source_cell_hint"), "muted"), actions);
}
// 背景音乐（配音列）：分镜上的提示点从此镜起换成新的背景音乐或停止，之后的分镜顺延；默认没有背景音乐。
function shotCue(sid) {
  return ver(plan().shots[sid]?.content)?.data?.music || null;
}
function musicSummary(sid) {
  const belong = state.data.shot_music?.[sid];
  if (!belong) return "";
  if (belong.stop) return MindexI18n.msg("ui.music_summary_stop");
  if (belong.start) {
    const cue = shotCue(sid);
    const item = ver(plan().shots[sid].music);
    return MindexI18n.msg("ui.music_summary_start", { p0: MindexI18n.msg(cue?.kind === "cover" ? "ui.music_kind_cover" : "ui.music_kind_generate"),
      p1: cue?.prompt || "", p2: item ? statusText(item.status) : MindexI18n.msg("ui.53e2db70167f") });
  }
  return MindexI18n.msg("ui.music_summary_continue", { p0: order().indexOf(belong.from) + 1, p1: shortId(belong.from) });
}
function musicEditable(cue) {
  // 键序与 ShotMusic 字段一致。
  return { kind: cue?.kind || "", prompt: cue?.prompt ?? null, lyrics: cue?.lyrics ?? null,
    instrumental: cue?.instrumental ?? true, audio_id: cue?.audio_id ?? null, volume: cue?.volume ?? 0.25 };
}
function renderMusicSection(body, sid) {
  const section = el("div", "", "music-editor");
  section.append(el("h4", MindexI18n.msg("ui.music_heading")));
  const line = musicSummary(sid);
  section.append(el("p", line || MindexI18n.msg("ui.music_summary_none"), "muted"));
  const key = draftKey(sid);
  state.musicDrafts = state.musicDrafts || new Map();
  let draft = state.musicDrafts.get(key);
  const base = plan().shots[sid].content;
  if (!draft || draft.base !== base) {
    draft = { base, data: musicEditable(shotCue(sid)) };
    state.musicDrafts.set(key, draft);
  }
  const value = draft.data;
  const redraw = () => {invalidatePanel(sid);renderMatrix();};
  const kind = field(MindexI18n.msg("ui.music_kind"), value.kind, "text", [
  ["", MindexI18n.msg("ui.music_kind_continue")], ["generate", MindexI18n.msg("ui.music_kind_generate")],
  ["cover", MindexI18n.msg("ui.music_kind_cover")], ["none", MindexI18n.msg("ui.music_kind_none")]]);
  kind.input.addEventListener("change", () => {value.kind = kind.input.value;redraw();});
  section.append(kind.wrap);
  if (["generate", "cover"].includes(value.kind)) {
    const prompt = field(MindexI18n.msg(value.kind === "cover" ? "ui.music_cover_prompt" : "ui.music_prompt"), value.prompt ?? "", "textarea");
    prompt.input.rows = 2;
    prompt.input.maxLength = value.kind === "cover" ? 300 : 2000;
    MindexI18n.setAttr(prompt.input, "placeholder", MindexI18n.msg("ui.music_prompt_placeholder"));
    prompt.input.addEventListener("input", () => {value.prompt = prompt.input.value.trim() ? prompt.input.value : null;});
    section.append(prompt.wrap);
    if (value.kind === "generate") {
      const wrap = el("label", "", "check");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = value.instrumental !== false;
      box.addEventListener("change", () => {value.instrumental = box.checked;redraw();});
      wrap.append(box, el("span", MindexI18n.msg("ui.music_instrumental")));
      section.append(wrap);
    }
    if (value.kind === "cover" || value.instrumental === false) {
      const lyrics = field(MindexI18n.msg("ui.music_lyrics"), value.lyrics ?? "", "textarea");
      lyrics.input.rows = 6;
      lyrics.input.maxLength = 3500;
      MindexI18n.setAttr(lyrics.input, "placeholder", MindexI18n.msg("ui.music_lyrics_placeholder"));
      lyrics.input.addEventListener("input", () => {value.lyrics = lyrics.input.value.trim() ? lyrics.input.value : null;});
      const status = el("p", "", "muted");
      status.setAttribute("role", "status");
      const write = button(MindexI18n.msg("ui.music_write_lyrics"), async () => {
        write.disabled = true;
        MindexI18n.setText(status, MindexI18n.msg("ui.music_writing_lyrics"));
        try {
          const result = await api(`${base()}/music/lyrics`, "POST", value.lyrics ?
          { mode: "edit", prompt: value.prompt || "", lyrics: value.lyrics } : { mode: "write_full_song", prompt: value.prompt || "" });
          value.lyrics = result.lyrics;
          lyrics.input.value = result.lyrics;
          MindexI18n.setText(status, MindexI18n.msg("ui.music_lyrics_done", { p0: result.title || "", p1: result.style_tags || "" }));
        } catch (error) {
          MindexI18n.setText(status, shortIds(error.message));
        } finally {write.disabled = false;}
      });
      section.append(lyrics.wrap, write, status);
    }
    if (value.kind === "cover")
    section.append(mediaSlot(sid, null, null, "audio", MindexI18n.msg("ui.music_cover_audio"), value.audio_id,
      (id) => {value.audio_id = id;}, redraw, () => state.musicDrafts.get(key) === draft));
    const volumeWrap = el("label", MindexI18n.msg("ui.music_volume", { p0: value.volume }), "form-field");
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.max = "1";
    slider.step = "0.05";
    slider.value = String(value.volume);
    MindexI18n.setAttr(slider, "aria-label", MindexI18n.msg("ui.music_volume", { p0: value.volume }));
    slider.addEventListener("input", () => {
      value.volume = Number(slider.value);
      MindexI18n.setText(volumeWrap.firstChild, MindexI18n.msg("ui.music_volume", { p0: value.volume }));
    });
    volumeWrap.append(slider);
    section.append(volumeWrap);
  }
  const actions = el("div", "", "detail-actions");
  actions.append(mutationButton(MindexI18n.msg("ui.music_save"), () => saveMusic(sid, draft)));
  if (shotStages(sid).includes("music")) {
    actions.append(mutationButton(MindexI18n.msg("ui.fb03cbd3e2da"), () => runNode("music", sid, false)),
    mutationButton(MindexI18n.msg("ui.c6a70df5d5ee"), () => runNode("music", sid, true)));
  }
  section.append(actions);
  const item = ver(plan().shots[sid].music);
  if (item) {
    section.append(el("p", MindexI18n.msg("ui.music_version", { p0: item.id.slice(0, 8), p1: statusText(item.status),
      p2: secs(item.data?.duration) }), item.status === "failed" ? "warn-text" : "muted"));
    if (item.error) section.append(el("p", shortIds(MindexI18n.system(item.error)), "warn-text"));
    if (item.files.music?.url) section.append(media("audio", item.files.music.url));
  }
  if (shotStages(sid).includes("music")) section.append(historyCell("music", sid));
  section.append(el("p", MindexI18n.msg("ui.music_hint"), "muted"));
  body.append(section);
}
async function saveMusic(sid, draft) {
  if (state.drafts.get(draftKey(sid))?.dirty) throw new Error(MindexI18n.msg("ui.music_content_dirty"));
  const value = draft.data;
  const shots = currentBoard();
  const shot = shots[order().indexOf(sid)];
  if (!value.kind) delete shot.music;else
  if (value.kind === "none") shot.music = { kind: "none", prompt: null, lyrics: null, instrumental: true, audio_id: null, volume: value.volume };else
  shot.music = { ...value, lyrics: value.kind === "cover" || value.instrumental === false ? value.lyrics : null,
    audio_id: value.kind === "cover" ? value.audio_id : null };
  const scope = scopeKey();
  const result = await api(planBase(), "PATCH", { revision: state.data.revision, shots });
  state.musicDrafts.delete(draftKey(sid));
  if (scope !== scopeKey()) return;
  invalidatePanel(sid);
  await accept(result);
  notice(MindexI18n.msg("ui.music_saved"));
}
async function regenerateColumn(stage, force = true) {
  if (busy() || hasDirtyDraft()) return;
  const inputs_by_shot = {};
  if (stage === PICTURE_COLUMN) {
    // 召回列：每个分镜执行各自的画面阶段（召回 / 生成图片 / 生成视频 / 生成动画）；上传素材的分镜不参与。
    for (const sid of order()) {
      const picture = pictureStage(sid);
      if (picture) inputs_by_shot[sid] = nodeInputs(picture, sid);
    }
  } else if (stage !== "split")
  // 只绑定包含该阶段的分镜（例如图片分镜没有召回与选片）。
  for (const sid of order()) if (!STAGES.includes(stage) || shotStages(sid).includes(stage)) inputs_by_shot[sid] = nodeInputs(stage, sid);
  const url = planBase(),
    body = { revision: state.data.revision, stage, inputs_by_shot, force };
  state.submitting = true;
  render();
  try {
    await api(`${url}/${force ? "regenerate-column" : "execute-column"}`, "POST", body);
    notice(MindexI18n.msg(force ? "ui.92157547f1b7" : "ui.execute_column_submitted", { p0: LABEL[stage] }));
  } finally {
    state.submitting = false;
    await refresh();
  }
}
function renderLogShell() {
  const key = scopeKey(),
    build = [...state.data.builds].
    reverse().
    find((b) => b.plan_id === plan().id);
  let view = state.logViews.get(key);
  if (!view || view.buildId !== build?.id) {
    view = { buildId: build?.id, seq: 0, lines: [], legacy: false };
    state.logViews.set(key, view);
  }
  const box = $("execution-log");
  if (box.dataset.scope !== `${key}/${view.buildId || ""}`) {
    box.dataset.scope = `${key}/${view.buildId || ""}`;
    MindexI18n.setText(box,
    view.lines.join("\n") || (
    build ? MindexI18n.msg("ui.7e123855bc1a") : MindexI18n.msg("ui.27b3fd17f4de")));
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
      `/api/studio/jobs/${id}/builds/${view.buildId}/logs?after=${view.seq}`
    );
    if (
    id !== state.id ||
    key !== scopeKey() ||
    state.logViews.get(key) !== view)

    return;
    const box = $("execution-log"),
      follow = box.scrollHeight - box.clientHeight - box.scrollTop < 32;
    for (const item of response.items)
    view.lines.push(
      shortIds(`${MindexI18n.date(new Date(item.at), true)}  ${item.shot_id ? MindexI18n.msg("ui.fdf052132bde", { p0: item.shot_id }) : ""}${item.stage ? `${LABEL[item.stage]} · ` : ""}${MindexI18n.system(item)}`)
    );
    view.seq = response.next_seq;
    view.legacy = response.legacy;
    if (response.items.length || !view.lines.length)
    MindexI18n.setText(box,
    view.lines.join("\n") || (
    response.legacy ? MindexI18n.msg("ui.0cbb72d3287e") : MindexI18n.msg("ui.29c54863449f")));
    if (follow) box.scrollTop = box.scrollHeight;
    more = response.has_more;
  }
}

function historyCell(stage, sid, preferred = null) {
  const box = el("div", "", "version-history");
  const items = Object.values(state.data.versions).
  filter((v) => v.stage === stage && v.shot_id === sid).
  reverse();
  const select = document.createElement("select");
  MindexI18n.setAttr(select, "aria-label", MindexI18n.msg("ui.3339e0dc57f7", { p0: shortId(sid), p1: LABEL[stage] }));
  for (const item of items) {
    const o = el(
      "option",
      `${item.id.slice(0, 8)} · ${statusText(item.status)} · ${MindexI18n.date(new Date(item.created_at), false)}`
    );
    o.value = item.id;
    select.append(o);
  }
  if (!items.length) return el("p", MindexI18n.msg("ui.482a16ecfa18"), "muted");
  // 默认显示方案里已选用的版本（刷新或面板重建后不回到最新版本）；调用方给出 preferred 时优先。
  const selected = sid ? plan().shots?.[sid]?.[stage] : null;
  const wanted = items.some((item) => item.id === preferred) ? preferred : selected;
  if (items.some((item) => item.id === wanted)) select.value = wanted;
  const choose = mutationButton(MindexI18n.msg("ui.1dd40d446b45"),

  async () => {
    await accept(
      await api(`${planBase()}/select-version`, "POST", {
        revision: state.data.revision,
        version_id: select.value,
        stage,
        shot_id: sid
      })
    );
    notice(MindexI18n.msg("ui.e401d5ed1d5c"));
  },
  ver(select.value).status !== "complete"
  );
  select.addEventListener("change", () => {
    choose.dataset.unavailable = String(
      ver(select.value).status !== "complete"
    );
    updateLocks();
  });
  box.append(
    select,
    button(MindexI18n.msg("ui.753c35ac28e9"), () => showVersion(select.value)),
    choose
  );
  return box;
}
function nodeInputs(stage, sid) {
  if (stage === "script") return {};
  if (stage === "split") return { script: plan().script || null };
  const s = plan().shots[sid];
  const inputs = { content: s.content };
  const present = shotStages(sid);
  if (stage === "generate" || stage === "animate") inputs.tts = s.tts || null;
  if (stage === "draw" || stage === "music") return inputs;
  if (stage === "select") {
    inputs.tts = s.tts || null;
    if (present.includes("generate")) inputs.generate = s.generate || null;
    if (present.includes("recall") && (!plan().ranges[sid] || plan().ranges[sid].anchor_ref)) inputs.recall = s.recall || null;
  }
  if (stage === "render") {
    Object.assign(inputs, present.includes("select") ? { select: s.select || null, tts: s.tts || null } : { tts: s.tts || null });
    if (present.includes("animate")) inputs.animate = s.animate || null;
    if (present.includes("draw")) inputs.draw = s.draw || null;
  }
  return inputs;
}
async function runNode(stage, sid, force) {
  const body = {
    stage,
    shot_id: sid || null,
    force,
    revision: state.data.revision,
    inputs: nodeInputs(stage, sid)
  };
  state.submitting = true;
  render();
  try {
    await api(`${planBase()}/nodes`, "POST", body);
    notice(MindexI18n.msg("ui.f775fd5ade05", { p0: LABEL[stage] }));
  } finally {
    state.submitting = false;
    await refresh();
  }
}

// AI 动画源码：服务端以严格 CSP 返回 text/html；iframe 只给 allow-scripts（不含 allow-same-origin），
// 动画脚本运行在不透明源里，拿不到本站 Cookie、存储与父页面 DOM。按画布尺寸渲染后等比缩放到预览宽度。
const ANIMATION_PREVIEW_WIDTH = 360;
function animationUrl(vid) {
  return `${base()}/versions/${vid}/animation.html`;
}
function animationFrame(vid, width, height) {
  const w = Number(width) || 1280, h = Number(height) || 720;
  const scale = Math.min(1, ANIMATION_PREVIEW_WIDTH / w);
  const box = el("div", "", "animation-preview");
  box.style.width = `${Math.round(w * scale)}px`;
  box.style.height = `${Math.round(h * scale)}px`;
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.loading = "lazy";
  MindexI18n.setAttr(frame, "title", MindexI18n.msg("ui.animate_preview_title"));
  frame.width = String(w);
  frame.height = String(h);
  frame.style.transform = `scale(${scale})`;
  frame.src = animationUrl(vid);
  const replay = button(MindexI18n.msg("ui.animate_replay"), () => {frame.src = animationUrl(vid);});
  const wrap = el("div", "", "animation-preview-wrap");
  box.append(frame);
  wrap.append(box, replay);
  return wrap;
}
const ANIMATION_TEMPLATE = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
html, body { margin: 0; padding: 0; overflow: hidden; background: #ffffff; font-family: sans-serif; }
</style></head><body>
<script>
requestAnimationFrame(function frame(t) { /* t：毫秒 */ requestAnimationFrame(frame); });
</script>
</body></html>`;
// 源码编辑对话框：等宽文本框，保存 → PATCH（服务端静态校验，未通过时在对话框顶部显示违规项）。
async function openAnimationEditor(sid) {
  const vid = plan().shots[sid].animate, item = ver(vid);
  let source = ANIMATION_TEMPLATE;
  if (item?.status === "complete" && item.files.animation) {
    const response = await fetch(animationUrl(vid));
    if (!response.ok) throw new Error(MindexI18n.msg("ui.animate_load_failed", { p0: response.status }));
    source = await response.text();
  }
  const scope = scopeKey();
  const body = showDialog(MindexI18n.msg("ui.animate_editor_title", { p0: shortId(sid) }));
  body.append(el("p", MindexI18n.msg("ui.animate_editor_hint"), "muted"));
  const editor = field(MindexI18n.msg("ui.animate_source_label"), "", "textarea");
  editor.input.value = source;
  editor.input.rows = 24;
  editor.input.spellcheck = false;
  editor.input.classList.add("code-editor");
  const counter = el("p", "", "muted");
  const count = () => MindexI18n.setText(counter, MindexI18n.msg("ui.animate_chars", { p0: editor.input.value.length }));
  editor.input.addEventListener("input", count);
  count();
  const actions = el("div", "", "detail-actions");
  actions.append(
    button(MindexI18n.msg("ui.animate_save"), async () => {
      if (scope !== scopeKey()) throw new Error(MindexI18n.msg("ui.animate_scope_changed"));
      const data = await api(`${planBase()}/shots/${sid}/animation`, "PATCH", { revision: state.data.revision, html: editor.input.value });
      closeDialog();
      await accept(data);
      notice(MindexI18n.msg("ui.animate_saved"));
    }),
    button(MindexI18n.msg("ui.4d0b4688c787"), closeDialog)
  );
  body.append(editor.wrap, counter, actions);
}
