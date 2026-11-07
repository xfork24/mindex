/* 创作页 · 候选选择、锚点与手动时间窗对齐。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
function renderCandidatePicker(body, sid) {
  const recall = plan().shots[sid].recall,
    item = ver(recall),
    choice = plan().anchors?.[sid] || { mode: "auto" };
  const picker = el("div", "", "candidate-picker");
  picker.append(el("h4", MindexI18n.msg("ui.29e3f1f8a2c7")));
  const automatic = mutationButton(
    choice.mode === "auto" ? MindexI18n.msg("ui.ea5bc0e50986") : MindexI18n.msg("ui.fe0e2299285c"),
    () => selectAnchor(sid, { mode: "auto" }),
  );
  automatic.setAttribute("aria-pressed", String(choice.mode === "auto"));
  picker.append(
    automatic,
    el(
      "p",
      choice.mode === "auto"
        ? MindexI18n.msg("ui.3907d07b3bee")
        : MindexI18n.msg("ui.b955e3930f7a"),

      "muted",
    ),
  );
  if (state.data.anchor_conflicts?.[sid])
    picker.append(el("p", shortIds(state.data.anchor_conflicts[sid]), "warn-text"));
  const pool = el("div", "", "detail-candidates");
  for (const [index, c] of (item?.data.results || []).entries()) {
    const selected =
      choice.mode === "manual" &&
      choice.recall_version === recall &&
      choice.candidate_id === (c.id || c.segment_id);
    const card = el("div", "", `detail-candidate${selected ? " chosen-candidate" : ""}`);
    const preview = el("div", "", "candidate-image");
    if (c.thumbnail_url) {
      const img = el("img");
      img.src = c.thumbnail_url;
      MindexI18n.setAttr(img, "alt", sourceName(c.source_id, c.source_name));
      img.loading = "lazy";
      preview.append(img);
    }
    preview.append(el("span", String(c.rank || index + 1), "candidate-rank"));
    if (selected)
      preview.append(el("span", MindexI18n.msg("ui.anchor_selected"), "candidate-selection"));
    card.dataset.candidateId = c.id || c.segment_id;
    card.append(preview);
    // 每个演员—角色组合只显示一次，优先保留带匹配参考图的最高分证据。
    const identities = new Map();
    const score = (e) => e.data?.original_score ?? e.data?.score ?? 0;
    for (const evidence of c.person_evidence || []) {
      const key = JSON.stringify([
        evidence.actor_id ?? evidence.actor_name ?? null,
        evidence.role_id ?? evidence.role_name ?? null,
      ]);
      const previous = identities.get(key);
      const hasReference = Boolean(evidence.reference_url),
        previousHasReference = Boolean(previous?.reference_url);
      if (
        !previous ||
        (hasReference && !previousHasReference) ||
        (hasReference === previousHasReference && score(evidence) > score(previous))
      )
        identities.set(key, evidence);
    }
    for (const evidence of identities.values()) {
      const chip = el(
        "a",
        `${evidence.actor_name || MindexI18n.msg("ui.1b37b5539364")}${evidence.role_name ? ` / ${evidence.role_name}` : ""}`,
        "evidence-chip",
      );
      chip.href = `/persons?evidence=${evidence.id}`;
      chip.target = "_blank";
      chip.rel = "noopener";
      if (evidence.reference_url) {
        const reference = el("img");
        reference.src = evidence.reference_url;
        MindexI18n.setAttr(
          reference,
          "alt",
          MindexI18n.msg("ui.5ad7ccccc4d1", {
            p0: evidence.role_name || evidence.actor_name || "",
          }),
        );
        reference.loading = "lazy";
        reference.addEventListener("error", () => reference.remove(), { once: true });
        chip.prepend(reference);
      }
      card.append(chip);
    }
    card.append(
      el("p", sourceName(c.source_id, c.source_name), "candidate-source"),
      el("p", `${secs(c.start)}–${secs(c.end)}`, "candidate-time"),
      button(MindexI18n.msg("ui.de61aa8e1cbc"), () =>
        play(c.media_url, MindexI18n.msg("ui.212e53d576f5")),
      ),
    );
    const choose = mutationButton(
      selected ? MindexI18n.msg("ui.fca821feca6c") : MindexI18n.msg("ui.218dbf597c7c"),
      () =>
        selectAnchor(sid, {
          mode: "manual",
          recall_version: recall,
          candidate_id: c.id || c.segment_id,
        }),
    );
    choose.setAttribute("aria-pressed", String(selected));
    card.append(choose);
    pool.append(card);
  }
  picker.append(pool);
  body.append(picker);
  if (choice.mode === "auto") {
    const scope = scopeKey();
    api(`${planBase()}/shots/${encodeURIComponent(sid)}/selection-anchor`)
      .then((result) => {
        if (
          !picker.isConnected ||
          scope !== scopeKey() ||
          plan().shots[sid].recall !== recall ||
          (plan().anchors?.[sid]?.mode || "auto") !== "auto"
        )
          return;
        for (const card of pool.children) {
          if (card.dataset.candidateId === result.candidate_id)
            card
              .querySelector(".candidate-image")
              .append(el("span", MindexI18n.msg("ui.anchor_automatic"), "candidate-selection"));
        }
      })
      .catch((error) => {
        if (picker.isConnected) picker.append(el("p", shortIds(error.message), "warn-text"));
      });
  }
}
async function selectAnchor(sid, choice) {
  await accept(
    await api(`${planBase()}/anchor`, "POST", {
      revision: state.data.revision,
      shot_id: sid,
      ...choice,
    }),
  );
  notice(
    choice.mode === "auto" ? MindexI18n.msg("ui.fd2f00253aa2") : MindexI18n.msg("ui.72722ed97d92"),
  );
}
async function manualRange(sid, host = null) {
  const scope = scopeKey(),
    url = planBase(),
    body = host || showDialog(MindexI18n.msg("ui.7c411f978f53", { p0: sid }));
  body.replaceChildren(el("p", MindexI18n.msg("ui.1fb33bf9acd4"), "muted"));
  const ctx = await api(`${url}/shots/${encodeURIComponent(sid)}/selection-context`);
  if (scope !== scopeKey() || !body.isConnected) return;
  body.replaceChildren();
  if (!ctx.anchor) {
    body.append(el("p", shortIds(MindexI18n.system(ctx.reason)), "muted"));
    return;
  }
  const lo = ctx.context_range.start,
    hi = ctx.context_range.end,
    length = ctx.window_duration || 0,
    key = JSON.stringify([scope, sid, ctx.anchor_ref, ctx.tts_version, ctx.atempo]),
    saved = state.windowDrafts.get(key),
    existing = plan().ranges[sid];
  let draft = saved || {
    start:
      existing?.anchor_ref?.candidate_id === ctx.anchor_ref.candidate_id &&
      existing?.anchor_ref?.recall_version === ctx.anchor_ref.recall_version &&
      existing?.tts_version === ctx.tts_version &&
      existing?.atempo === ctx.atempo
        ? existing.start
        : ctx.initial_start,
    zoom: 1,
    preview: null,
    contextToken: ctx.context_token,
  };
  if (draft.contextToken !== ctx.context_token) draft.preview = null;
  draft.contextToken = ctx.context_token;
  draft.start = Math.min(ctx.legal_start.max, Math.max(lo, draft.start));
  state.windowDrafts.set(key, draft);
  body.append(
    el(
      "p",
      MindexI18n.msg("ui.cf3132a895df", {
        p0: ctx.anchor.rank || "?",
        p1: secs(ctx.anchor.start),
        p2: secs(ctx.anchor.end),
        p3: secs(lo),
        p4: secs(hi),
      }),
      "muted",
    ),
  );
  if (ctx.extension.before || ctx.extension.after)
    body.append(
      el(
        "p",
        MindexI18n.msg("ui.7f0b259b81e5", { p0: ctx.extension.before, p1: ctx.extension.after }),
        "muted",
      ),
    );
  if (!ctx.ready) body.append(el("p", shortIds(MindexI18n.system(ctx.reason)), "warn-text"));
  const video = media("video", ctx.video_url);
  video.classList.add("window-video");
  video.muted = true;
  const readout = el("p", "", "window-readout"),
    viewport = el("div", "", "window-viewport"),
    track = el("div", "", "window-track"),
    strip = el("img", null, "window-thumbnails"),
    windowBox = el("div", "", "fixed-window"),
    result = el("div");
  strip.src = ctx.thumbnails_url;
  MindexI18n.setAttr(strip, "alt", MindexI18n.msg("ui.718ffac629d9"));
  strip.draggable = false;
  track.append(strip);
  for (const row of ctx.shots) {
    const boundary = el("div", "", "window-boundary");
    boundary.style.left = `${((row.start - lo) / (hi - lo)) * 100}%`;
    MindexI18n.setAttr(
      boundary,
      "title",
      `${shortId(row.id)} · ${secs(row.start)}–${secs(row.end)}`,
    );
    boundary.append(el("span", secs(row.start)));
    track.append(boundary);
  }
  const anchorMark = el("div", "", "window-anchor");
  anchorMark.style.left = `${((ctx.anchor.start - lo) / (hi - lo)) * 100}%`;
  anchorMark.style.width = `${((ctx.anchor.end - ctx.anchor.start) / (hi - lo)) * 100}%`;
  MindexI18n.setAttr(anchorMark, "title", MindexI18n.msg("ui.f2e05c849853"));
  track.append(anchorMark, windowBox);
  viewport.append(track);
  windowBox.tabIndex = ctx.ready ? 0 : -1;
  windowBox.setAttribute("role", "slider");
  MindexI18n.setAttr(windowBox, "aria-label", MindexI18n.msg("ui.aba7cdd1af8f"));
  windowBox.setAttribute("aria-valuemin", lo);
  windowBox.setAttribute("aria-valuemax", ctx.legal_start.max);
  windowBox.setAttribute("aria-disabled", String(!ctx.ready));
  windowBox.hidden = !ctx.ready;
  let playbackFrame = null,
    drag = null,
    saving = false,
    previewing = false;
  const saveStatus = el("p", "", "muted");
  saveStatus.setAttribute("role", "status");
  const confirm = mutationButton(
    MindexI18n.msg("ui.3ee4a0fe14a0"),
    async () => {
      if (saving || previewing || !ctx.ready) return;
      const sent = request();
      saving = true;
      update(false);
      try {
        MindexI18n.setText(
          saveStatus,
          draft.preview ? MindexI18n.msg("ui.55f2030f3f50") : MindexI18n.msg("ui.fa1e98c506ad"),
        );
        const preview =
          draft.preview ||
          (await api(`${url}/range-preview`, "POST", {
            ...sent,
            render_preview: false,
          }));
        if (!isCurrent(sent)) throw new Error(MindexI18n.msg("ui.ebf7d2633b0e"));
        draft.preview = preview;
        MindexI18n.setText(saveStatus, MindexI18n.msg("ui.55f2030f3f50"));
        const response = await api(`${url}/range-confirm`, "POST", {
          ...sent,
          preview_token: preview.preview_token,
        });
        draft.preview = null;
        if (scope === scopeKey()) await accept(response);
        if (!host) closeDialog();
        notice(MindexI18n.msg("ui.7ff45d499de7"));
      } catch (error) {
        draft.preview = null;
        MindexI18n.setText(
          saveStatus,
          shortIds(MindexI18n.msg("ui.1a1cb2ffaaae", { p0: error.message })),
        );
        throw error;
      } finally {
        saving = false;
        update(false);
      }
    },
    !ctx.ready,
  );
  function request() {
    return {
      revision: ctx.revision,
      shot_id: sid,
      context_token: ctx.context_token,
      anchor_ref: ctx.anchor_ref,
      tts_version: ctx.tts_version,
      start: draft.start,
    };
  }
  function isCurrent(sent) {
    return (
      scope === scopeKey() &&
      body.isConnected &&
      draft.start === sent.start &&
      state.data.revision === ctx.revision
    );
  }
  function seek() {
    const position = draft.start - lo;
    if (video.readyState)
      video.currentTime = Math.max(0, Math.min(video.duration || hi - lo, position));
  }
  function update(seekFrame = true) {
    windowBox.style.left = `${((draft.start - lo) / (hi - lo)) * 100}%`;
    windowBox.style.width = `${(length / (hi - lo)) * 100}%`;
    windowBox.setAttribute("aria-valuenow", draft.start);
    MindexI18n.setAttr(
      windowBox,
      "aria-valuetext",
      MindexI18n.msg("ui.47f8790ecd63", {
        p0: secs(draft.start),
        p1: secs(draft.start + length),
        p2: secs(length),
      }),
    );
    MindexI18n.setText(
      readout,
      ctx.ready
        ? MindexI18n.msg("ui.03ed6969b5f6", {
            p0: secs(draft.start),
            p1: secs(draft.start + length),
            p2: secs(length),
            p3: ctx.atempo.toFixed(4),
          })
        : length
          ? MindexI18n.msg("ui.bbfcf52e0955", { p0: secs(length), p1: secs(hi - lo) })
          : MindexI18n.msg("ui.49ead685ffa8"),
    );
    confirm.dataset.unavailable = String(!ctx.ready || saving || previewing);
    confirm.disabled = !ctx.ready || saving || previewing || busy();
    MindexI18n.setText(
      confirm,
      saving ? MindexI18n.msg("ui.9ca32f988d16") : MindexI18n.msg("ui.3ee4a0fe14a0"),
    );
    serverPreview.dataset.unavailable = String(!ctx.ready || saving || previewing);
    serverPreview.disabled = !ctx.ready || saving || previewing || busy();
    if (seekFrame) seek();
  }
  function move(value) {
    if (!ctx.ready || busy() || saving) return;
    video.pause();
    draft.start = Math.min(ctx.legal_start.max, Math.max(lo, value));
    draft.preview = null;
    result.replaceChildren();
    MindexI18n.setText(saveStatus, MindexI18n.msg("ui.44c8084f4efc"));
    update();
  }
  video.addEventListener("loadedmetadata", () => seek(), { once: true });
  function enforcePlaybackBounds() {
    if (video.paused) return false;
    const start = draft.start - lo,
      end = start + length;
    if (!ctx.ready || video.currentTime >= end) {
      video.pause();
      if (ctx.ready) video.currentTime = end;
      return false;
    }
    if (video.currentTime < start) video.currentTime = start;
    return true;
  }
  function monitorPlayback() {
    playbackFrame = null;
    if (body.isConnected && enforcePlaybackBounds())
      playbackFrame = requestAnimationFrame(monitorPlayback);
  }
  video.addEventListener("play", () => {
    const start = draft.start - lo,
      end = start + length;
    if (video.currentTime < start || video.currentTime >= end) seek();
    if (playbackFrame !== null) cancelAnimationFrame(playbackFrame);
    monitorPlayback();
  });
  video.addEventListener("pause", () => {
    if (playbackFrame !== null) cancelAnimationFrame(playbackFrame);
    playbackFrame = null;
  });
  video.addEventListener("timeupdate", enforcePlaybackBounds);
  video.addEventListener("seeking", enforcePlaybackBounds);
  track.addEventListener("pointerdown", (e) => {
    if (!ctx.ready || busy() || e.button !== 0) return;
    e.preventDefault();
    const rect = track.getBoundingClientRect(),
      time = lo + ((e.clientX - rect.left) / rect.width) * (hi - lo);
    drag = {
      pointer: e.pointerId,
      offset: e.target === windowBox ? time - draft.start : length / 2,
    };
    track.setPointerCapture(e.pointerId);
    windowBox.focus({ preventScroll: true });
    move(time - drag.offset);
  });
  track.addEventListener("pointermove", (e) => {
    if (!drag || drag.pointer !== e.pointerId) return;
    const rect = track.getBoundingClientRect();
    move(lo + ((e.clientX - rect.left) / rect.width) * (hi - lo) - drag.offset);
  });
  const stop = () => {
    drag = null;
  };
  track.addEventListener("pointerup", stop);
  track.addEventListener("pointercancel", stop);
  windowBox.addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      move(
        e.key === "Home"
          ? lo
          : e.key === "End"
            ? ctx.legal_start.max
            : draft.start +
              (e.key === "ArrowLeft" ? -1 : 1) * ctx.frame_step * (e.shiftKey ? 10 : 1),
      );
    }
  });
  const zoom = field(MindexI18n.msg("ui.33158b575eaf"), draft.zoom, "range");
  zoom.input.min = 1;
  zoom.input.max = 8;
  zoom.input.step = 0.25;
  function setZoom() {
    draft.zoom = Number(zoom.input.value);
    track.style.width = `${draft.zoom * 100}%`;
  }
  zoom.input.addEventListener("input", setZoom);
  setZoom();
  const serverPreview = mutationButton(
    MindexI18n.msg("ui.724ea6be9a18"),
    async () => {
      if (saving || previewing || !ctx.ready) return;
      const sent = request();
      previewing = true;
      update(false);
      MindexI18n.setText(saveStatus, MindexI18n.msg("ui.b31cb820fd4f"));
      try {
        const preview = await api(`${url}/range-preview`, "POST", sent);
        if (!isCurrent(sent)) return;
        draft.preview = preview;
        result.replaceChildren(media("video", preview.preview_url));
        MindexI18n.setText(saveStatus, MindexI18n.msg("ui.25bc411a8e4c"));
      } catch (error) {
        MindexI18n.setText(
          saveStatus,
          shortIds(MindexI18n.msg("ui.3bb02d7c43b1", { p0: error.message })),
        );
        throw error;
      } finally {
        previewing = false;
        update(false);
      }
    },
    !ctx.ready,
  );
  serverPreview.dataset.unavailable = String(!ctx.ready);
  body.append(
    video,
    readout,
    zoom.wrap,
    viewport,
    el("p", MindexI18n.msg("ui.ba185e8b854c"), "muted"),
    confirm,
    serverPreview,
    el("p", MindexI18n.msg("ui.128f77bcdc14"), "muted"),
    saveStatus,
    result,
  );
  if (draft.preview) result.append(media("video", draft.preview.preview_url));
  update(false);
  updateLocks();
}
