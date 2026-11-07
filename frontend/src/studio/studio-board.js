/* 创作页 · 分镜工作台：草稿与锁定、阶段摘要、检索组合、顶部分镜条（卡片、阶段浮层、卡片产物）与下方阶段详情区。
按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
function scopeKey() {
  return `${state.id}/${plan().id}`;
}
function draftKey(sid) {
  return `${scopeKey()}/${sid}`;
}
// 当前任务/方案打开的 {sid, stage}；没有时为 null。按方案分别记住，切换方案再切回后恢复。
function selection() {
  return state.selection.get(scopeKey()) || null;
}
function hasDirtyDraft() {
  const prefix = `${scopeKey()}/`;
  // 文案框里未保存的修改同样阻止构建：否则构建用的是已保存版本，与屏幕上看到的不一致。
  return (
    scriptDirty() || [...state.drafts].some(([key, draft]) => key.startsWith(prefix) && draft.dirty)
  );
}
function mutationButton(text, action, unavailable = false, allowDirty = false) {
  const n = button(text, () => {
    if (busy() || (!allowDirty && hasDirtyDraft())) {
      return;
    }
    return action();
  });
  n.dataset.mutating = "true";
  n.dataset.unavailable = String(unavailable);
  n.dataset.allowDirty = String(allowDirty);
  return n;
}
function updateLocks() {
  if (!state.data) {
    return;
  }
  const locked = busy();
  const dirty = hasDirtyDraft();
  syncScriptControls();
  for (const id of [
    "plan-select",
    "new-plan",
    "copy-plan",
    "delete-job",
    "rename-plan",
    "delete-plan",
    "plan-settings",
    "run-all",
  ]) {
    $(id).disabled = locked || (dirty && !["plan-select", "delete-job"].includes(id));
    MindexI18n.setAttr(
      $(id),
      "title",
      dirty && !["plan-select", "delete-job"].includes(id) ? MindexI18n.msg("ui.9f185b0cc351") : "",
    );
  }
  // 唯一的方案不能删除：按钮保持可见但禁用，并说明原因。
  if (Object.keys(state.data.plans).length < 2) {
    $("delete-plan").disabled = true;
    MindexI18n.setAttr($("delete-plan"), "title", MindexI18n.msg("ui.delete_plan_last"));
  }
  renderCancel();
  // 批量操作：阶段下拉决定两个按钮的 data-stage；拆分镜之外的阶段在没有分镜时不可用。
  const batch = $("batch-stage");
  batch.disabled = locked;
  document.querySelectorAll(".regenerate-column, .execute-column").forEach((n) => {
    n.dataset.stage = batch.value;
    n.disabled = locked || dirty || (n.dataset.stage !== "split" && !order().length);
    MindexI18n.setAttr(
      n,
      "title",
      dirty
        ? MindexI18n.msg("ui.9f185b0cc351")
        : n.dataset.stage === "split"
          ? MindexI18n.msg("ui.cddf7b77b6a7")
          : MindexI18n.msg("ui.board_batch_hint", { p0: LABEL[n.dataset.stage] }),
    );
  });
  const board = $("storyboard-board");
  board.querySelectorAll("[data-mutating]").forEach((n) => {
    n.disabled =
      locked ||
      n.dataset.unavailable === "true" ||
      (n.dataset.requiresTts && !plan().shots[n.dataset.requiresTts]?.tts) ||
      (dirty && n.dataset.allowDirty !== "true");
  });
  board.querySelectorAll("[data-shot-input]").forEach((n) => {
    n.disabled = locked;
  });
}
function pauseMedia(node) {
  node.querySelectorAll("audio,video").forEach((n) => n.pause());
}
// 在下方详情区打开某分镜的某阶段；同一阶段已打开时（兼容旧的 toggleCell 语义）再次调用则关闭。
function openStage(sid, stage) {
  state.selection.set(scopeKey(), { sid, stage });
  hidePopover();
  renderMatrix();
}
function closeStage() {
  state.selection.delete(scopeKey());
  renderMatrix();
}
function toggleCell(sid, stage) {
  const current = selection();
  current?.sid === sid && current.stage === stage ? closeStage() : openStage(sid, stage);
}
function invalidatePanel() {
  state.stagePanel.key = null;
}
function latestNode(stage, sid) {
  const build = [...state.data.builds]
    .reverse()
    .find(
      (b) => b.plan_id === plan().id && b.nodes.some((n) => n.stage === stage && n.shot_id === sid),
    );
  return build?.nodes.find((n) => n.stage === stage && n.shot_id === sid);
}
function selectionWithin(node) {
  const selection = window.getSelection();
  return (
    selection &&
    !selection.isCollapsed &&
    (node.contains(selection.anchorNode) || node.contains(selection.focusNode))
  );
}
function executionError(stage, sid) {
  const node = latestNode(stage, sid);
  if (node?.status !== "failed") {
    return "";
  }
  const attempted = ver(node.version);
  const tts = attempted?.parameters.tts;
  const detail = node.error || attempted?.error || MindexI18n.msg("ui.6d38d4b01891");
  const context =
    stage === "tts" && tts
      ? MindexI18n.msg("ui.710dfeaa9bc9", { p0: tts.model, p1: tts.voice })
      : "";
  const advice =
    stage === "tts" && MindexI18n.render(detail).includes("voice id not exist")
      ? MindexI18n.msg("ui.bdc1a8e66ba1")
      : "";
  const changed =
    stage === "tts" &&
    tts &&
    state.data.tts_settings &&
    tts.voice !== state.data.tts_settings.voice;
  return shortIds(
    MindexI18n.msg("ui.853971a580b9", {
      p0: context,
      p1: MindexI18n.system(detail),
      p2: changed
        ? MindexI18n.msg("ui.249b7096f10b", { p0: state.data.tts_settings.voice })
        : advice,
    }),
  );
}
// 某分镜某阶段的摘要节点（版本徽标、摘要文字、背景音乐、冲突、最近执行与失败原因），供卡片使用。
function stageSummaryNodes(sid, stage) {
  if (stage === "source") {
    return sourceSummaryNodes(sid);
  }
  const selected = plan().shots[sid][stage];
  const item = ver(selected);
  const nodes = [];
  nodes.push(
    el(
      "span",
      selected
        ? MindexI18n.msg("ui.ac3d0b42ecd9", { p0: selected.slice(0, 8) })
        : MindexI18n.msg("ui.53e2db70167f"),
      `badge${selected ? " good" : ""}`,
    ),
  );
  if (item) {
    const d = item.data;
    if (stage === "content") {
      nodes.push(el("span", d.text, "cell-summary"));
      // 画面意象非空时完整显示；为空时不显示该行，也不放占位文字。
      if (typeof d.visual === "string" && d.visual.trim()) {
        nodes.push(
          el(
            "span",
            MindexI18n.msg("ui.visual_summary", { p0: d.visual }),
            "cell-summary visual-summary",
          ),
        );
      }
      nodes.push(el("span", d.query, "cell-summary muted"));
      const source = sourceSummary(d.source);
      if (source) {
        nodes.push(el("span", source, "cell-summary"));
      }
      // 分镜级召回覆盖（粒度/数量）在摘要可见，便于发现被钉住的分镜。
      const overrides = [
        d.granularity && GRANULARITY_LABEL[d.granularity],
        d.recall_limit && MindexI18n.msg("ui.adf1951c424c", { p0: d.recall_limit }),
      ].filter(Boolean);
      if (overrides.length) {
        nodes.push(
          el(
            "span",
            MindexI18n.msg("ui.24fc33ac59f6", { p0: overrides.join(" · ") }),
            "cell-summary muted",
          ),
        );
      }
      if (state.drafts.get(draftKey(sid))?.dirty) {
        nodes.push(el("span", MindexI18n.msg("ui.70ac7eb1c08c"), "badge warn"));
      }
    } else {
      const summary =
        stage === "recall"
          ? MindexI18n.msg("ui.3b8b0abb1f35", {
              p0: RECALL_MODE_LABEL[d.mode || "text"],
              p1: d.results?.length || 0,
            })
          : stage === "tts"
            ? secs(d.duration)
            : stage === "draw"
              ? MindexI18n.msg("ui.draw_summary", { p0: d.width || "?", p1: d.height || "?" })
              : stage === "generate"
                ? MindexI18n.msg("ui.generate_summary", {
                    p0: secs(d.duration),
                    p1: d.polls ?? "?",
                  })
                : stage === "animate"
                  ? MindexI18n.msg(
                      d.origin === "manual" ? "ui.animate_summary_manual" : "ui.animate_summary",
                      { p0: d.chars ?? "?", p1: secs(d.seconds) },
                    )
                  : stage === "invoke"
                    ? MindexI18n.msg("ui.invoke_summary", {
                        p0: d.tool || "?",
                        p1:
                          d.output === "image"
                            ? `${d.width || "?"}×${d.height || "?"}`
                            : secs(d.duration),
                      })
                    : stage === "select"
                      ? `${d.event.video.kind === "file" ? MindexI18n.msg(d.generated ? "ui.source_aigc" : d.tool ? "ui.source_tool" : "ui.source_clip") : sourceName(d.event.video.source_id, d.event.video.source_name)} ${secs(d.event.video.start)}–${secs(d.event.video.end)}`
                      : MindexI18n.msg("ui.a8e554e0bc5a", {
                          p0: d.frames || "?",
                          p1: d.fps || "?",
                        });
      nodes.push(el("span", summary, "cell-summary"));
    }
  }
  if (stage === "tts" || stage === "render") {
    // 背景音乐显示在配音列（提示点、顺延或停止），分镜渲染列同样标出本镜适用的背景音乐。
    const line = musicSummary(sid);
    if (line) {
      nodes.push(el("span", line, "cell-summary music-summary"));
    }
  }
  if (stage === "render") {
    const line = transitionSummary(sid);
    if (line) {
      nodes.push(el("span", line, "cell-summary post-summary"));
    }
  }
  const messages = state.data.conflicts[`${stage}:${sid}`] || [];
  if (messages.length) {
    nodes.push(
      el(
        "span",
        shortIds(
          MindexI18n.msg("ui.e5d4ec45ff67", {
            p0: messages.map((message) => MindexI18n.system(message)).join("; "),
          }),
        ),
        "cell-summary warn-text",
      ),
    );
  }
  const node = latestNode(stage, sid);
  if (node) {
    nodes.push(
      el("span", MindexI18n.msg("ui.7f95d89a3924", { p0: statusText(node.status) }), "cell-status"),
    );
  }
  const error = executionError(stage, sid);
  if (error) {
    nodes.push(el("span", error, "cell-summary warn-text"));
  }
  return nodes;
}
// 上传片段/图片分镜的素材预览阶段：显示上传的素材；没有可执行的画面阶段。
function sourceSummaryNodes(sid) {
  const source = ver(plan().shots[sid].content)?.data?.source || {};
  return [
    el(
      "span",
      MindexI18n.msg(source.media_id ? "ui.source_cell_uploaded" : "ui.source_no_file"),
      `badge${source.media_id ? " good" : ""}`,
    ),
    el("span", sourceSummary(source), "cell-summary"),
  ];
}
// 阶段摘要的包装元素（卡片正文与单元测试使用）。
function cellSummary(sid, stage) {
  const box = el("div", "", "card-summary");
  box.append(...stageSummaryNodes(sid, stage));
  return box;
}
// 检索组合依赖（后端 detail().retrieval）：召回签名记录并比对内容索引与检索发布版，
// 两者不一致的召回节点会提示选用版本不兼容。
function retrievalCombo(item, full) {
  const cut = (id) => (full ? id : shortId(id));
  const unbound = MindexI18n.msg("ui.index_release_unbound");
  return MindexI18n.msg("ui.retrieval_combo", {
    p0: item.release_id ? cut(item.release_id) : unbound,
    p1: cut(item.content_id),
    p2: !item.release_id
      ? unbound
      : !item.known
        ? MindexI18n.msg("ui.retrieval_deps_unknown")
        : item.person_id
          ? cut(item.person_id)
          : MindexI18n.msg("ui.5a65ae692a28"),
  });
}
// 分镜工作台上方一行：当前生效的组合、工作台依赖的组合，以及两者是否一致。
function renderRetrieval() {
  const box = $("retrieval-deps");
  const info = state.data.retrieval;
  box.replaceChildren();
  box.hidden = !info;
  if (!info) {
    return;
  }
  const describe = (full) => {
    const active = info.active
      ? retrievalCombo(info.active, full)
      : MindexI18n.msg("ui.retrieval_line_no_active");
    const used = !info.used.length
      ? MindexI18n.msg("ui.retrieval_line_no_recall")
      : info.used
          .map((item) =>
            info.used.length > 1
              ? MindexI18n.msg("ui.retrieval_combo_shots", {
                  p0: retrievalCombo(item, full),
                  p1: item.shots,
                })
              : retrievalCombo(item, full),
          )
          .join(" / ");
    const verdict =
      !info.used.length || !info.active
        ? "ui.retrieval_line_na"
        : info.used.every((item) => item.current)
          ? "ui.retrieval_line_same"
          : "ui.retrieval_line_diff";
    return {
      text: MindexI18n.msg("ui.retrieval_line", {
        p0: active,
        p1: used,
        p2: MindexI18n.msg(verdict),
      }),
      verdict,
    };
  };
  const line = describe(false);
  const row = el(
    "p",
    line.text,
    `retrieval-line${line.verdict === "ui.retrieval_line_diff" ? " is-mismatch" : ""}`,
  );
  MindexI18n.setAttr(row, "title", describe(true).text);
  box.append(row);
}
// 分镜工作台：顶部分镜条（卡片）+ 阶段浮层 + 下方阶段详情区。名称沿用 renderMatrix，供各模块调用。
function renderMatrix() {
  const strip = $("shot-strip");
  const scope = scopeKey();
  if (state.boardScope !== scope) {
    resetBoard();
    state.boardScope = scope;
  }
  const ids = order();
  // 打开的分镜已删除、或阶段已不属于该分镜（例如改了画面来源）时清除。
  const open = selection();
  if (open && (!ids.includes(open.sid) || !barStages(open.sid).includes(open.stage))) {
    state.selection.delete(scope);
  }
  const wanted = [];
  for (const [index, sid] of ids.entries()) {
    let entry = state.shotCards.get(sid);
    if (!entry) {
      entry = createShotCard(sid);
      state.shotCards.set(sid, entry);
    }
    renderShotCard(sid, index, entry);
    wanted.push(entry.card);
  }
  for (const [sid, entry] of state.shotCards) {
    if (!ids.includes(sid)) {
      pauseMedia(entry.card);
      cardObserver?.unobserve(entry.card);
      entry.card.remove();
      state.shotCards.delete(sid);
    }
  }
  if (!ids.length) {
    const empty = el("div", "", "shot-strip-empty");
    empty.append(
      el("p", MindexI18n.msg("ui.board_empty"), "muted"),
      mutationButton(MindexI18n.msg("ui.1c9d6d4917d0"), () => changeStructure(null, "insert")),
    );
    strip.replaceChildren(empty);
  } else {
    for (const child of [...strip.children]) {
      if (!wanted.includes(child)) {
        child.remove();
      }
    }
    wanted.forEach((node, index) => {
      if (strip.children[index] !== node) {
        strip.insertBefore(node, strip.children[index] || null);
      }
    });
  }
  renderStagePanel();
  if (popover.sid) {
    ids.includes(popover.sid) ? renderPopover() : hidePopover();
  }
  if (state.revealShot) {
    state.shotCards
      .get(state.revealShot)
      ?.card.scrollIntoView({ block: "nearest", inline: "nearest" });
    state.revealShot = null;
  }
  updateStripNav();
  updateLocks();
}
// 切换任务/方案或删除任务：暂停并清空卡片与详情中的媒体，收起阶段浮层。
function resetBoard() {
  for (const entry of state.shotCards.values()) {
    pauseMedia(entry.card);
    cardObserver?.unobserve(entry.card);
  }
  state.shotCards.clear();
  state.boardScope = null;
  $("shot-strip").replaceChildren();
  const panel = $("stage-panel");
  pauseMedia(panel);
  panel.replaceChildren();
  panel.hidden = true;
  $("stage-placeholder").hidden = false;
  state.stagePanel = { key: null, historyKey: null };
  hidePopover();
}
// ---------------------------------------------------------------- 分镜卡片
const STATE_LABEL_KEY = {
  none: "ui.board_state_none",
  complete: "ui.board_state_complete",
  stale: "ui.board_state_stale",
  failed: "ui.board_state_failed",
  running: "ui.board_state_running",
};
const stageStateText = (value) => MindexI18n.msg(STATE_LABEL_KEY[value] || "ui.board_state_none");
function stageDot(sid, stage) {
  const value = stageState(sid, stage);
  const dot = el("span", "", "stage-dot");
  dot.dataset.state = value;
  dot.setAttribute("aria-hidden", "true");
  MindexI18n.setAttr(
    dot,
    "title",
    MindexI18n.msg("ui.board_dot_title", { p0: LABEL[stage], p1: stageStateText(value) }),
  );
  return dot;
}
function createShotCard(sid) {
  const card = el("div", "", "shot-card");
  card.dataset.shotId = sid;
  card.setAttribute("role", "listitem");
  card.tabIndex = 0;
  card.setAttribute("aria-haspopup", "menu");
  card.setAttribute("aria-expanded", "false");
  card.setAttribute("aria-controls", "stage-popover");
  const body = el("div", "", "shot-card-body");
  const menu = el(
    "button",
    MindexI18n.msg("ui.board_stage_menu_button"),
    "quiet stage-menu-button",
  );
  menu.type = "button";
  menu.setAttribute("aria-haspopup", "menu");
  menu.setAttribute("aria-expanded", "false");
  menu.setAttribute("aria-controls", "stage-popover");
  menu.addEventListener("click", (event) => {
    event.stopPropagation();
    popover.sid === sid ? hidePopover() : showPopover(sid, { focus: true });
  });
  const mediaBox = el("div", "", "card-media");
  mediaBox.dataset.cardMedia = "true";
  // 媒体区内的点击与悬停只用于播放控制，不打开详情、不展开阶段浮层。
  mediaBox.addEventListener("click", (event) => event.stopPropagation());
  card.append(menu, body, mediaBox);
  card.addEventListener("click", (event) => {
    if (event.target.closest("[data-card-media], button, a, input, select, textarea")) {
      return;
    }
    if (event.detail > 1 || selectionWithin(card)) {
      return;
    }
    openStage(sid, cardStage(sid));
  });
  card.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") {
      return;
    }
    if (event.target.closest("[data-card-media]")) {
      scheduleHidePopover();
    } else {
      schedulePopover(sid);
    }
  });
  card.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "touch") {
      scheduleHidePopover();
    }
  });
  card.addEventListener("keydown", (event) => {
    if (event.target !== card) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openStage(sid, cardStage(sid));
      $("stage-panel").focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      showPopover(sid, { focus: true });
    } else if (event.key === "Escape" && popover.sid === sid) {
      hidePopover();
    }
  });
  // 键盘 Tab 聚焦卡片时与鼠标悬停一样展开阶段浮层（鼠标点击引起的聚焦不展开）。
  card.addEventListener("focus", () => {
    if (card.matches(":focus-visible") && !popover.suppressFocus) {
      showPopover(sid);
    }
  });
  card.addEventListener("focusout", (event) => {
    const next = event.relatedTarget;
    if (popover.sid === sid && !card.contains(next) && !$("stage-popover").contains(next)) {
      hidePopover();
    }
  });
  cardObserver?.observe(card);
  return { card, body, menu, mediaBox, key: null, mediaKey: null, media: null };
}
function renderShotCard(sid, index, entry, force = false) {
  const stage = cardStage(sid);
  const stages = barStages(sid);
  const open = selection();
  const dirty = !!state.drafts.get(draftKey(sid))?.dirty;
  const key = JSON.stringify([
    index,
    stage,
    stages,
    plan().shots[sid],
    state.data.shot_music?.[sid],
    latestNode("music", sid),
    stages.map((s) => [
      stageState(sid, s),
      state.data.conflicts[`${s}:${sid}`],
      latestNode(s, sid),
    ]),
    stage === "content" || stage === "source" ? ver(plan().shots[sid].content)?.data : null,
    dirty,
  ]);
  if (force || (entry.key !== key && !selectionWithin(entry.card))) {
    const value = stageState(sid, stage);
    const head = el("div", "", "shot-card-head");
    head.append(
      el("span", MindexI18n.msg("ui.board_card_index", { p0: index + 1 }), "shot-card-index"),
      el("span", shortId(sid), "shot-card-id muted"),
    );
    const title = el("div", "", "shot-card-stage");
    title.append(el("strong", LABEL[stage]));
    if (value === "stale") {
      title.append(el("span", MindexI18n.msg("ui.board_state_stale"), "badge warn"));
    }
    // 文案与查询之外的阶段：草稿未保存时同样在卡片上提示。
    if (dirty && stage !== "content") {
      title.append(el("span", MindexI18n.msg("ui.70ac7eb1c08c"), "badge warn"));
    }
    const dots = el("div", "", "stage-dots");
    dots.append(...stages.map((s) => stageDot(sid, s)));
    MindexI18n.setAttr(
      dots,
      "aria-label",
      MindexI18n.msg("ui.board_dots_label", {
        p0: stages
          .map((s) =>
            MindexI18n.msg("ui.board_dot_title", {
              p0: LABEL[s],
              p1: stageStateText(stageState(sid, s)),
            }),
          )
          .join(" · "),
      }),
    );
    dots.setAttribute("role", "img");
    entry.body.replaceChildren(head, title, dots, cellSummary(sid, stage));
    MindexI18n.setAttr(
      entry.card,
      "aria-label",
      MindexI18n.msg("ui.board_card_label", {
        p0: index + 1,
        p1: LABEL[stage],
        p2: stageStateText(value),
      }),
    );
    MindexI18n.setAttr(
      entry.menu,
      "aria-label",
      MindexI18n.msg("ui.board_stage_menu", { p0: index + 1 }),
    );
    entry.key = key;
    entry.pending = false;
  } else if (entry.key !== key) {
    entry.pending = true;
  }
  const isOpen = open?.sid === sid;
  entry.card.classList.toggle("is-open", isOpen);
  if (isOpen) {
    entry.card.setAttribute("aria-current", "true");
  } else {
    entry.card.removeAttribute("aria-current");
  }
  const openTag = entry.card.querySelector(".shot-card-open");
  if (isOpen && !openTag) {
    entry.body.append(
      el("span", MindexI18n.msg("ui.board_card_open", { p0: LABEL[open.stage] }), "shot-card-open"),
    );
  } else if (openTag) {
    isOpen
      ? MindexI18n.setText(openTag, MindexI18n.msg("ui.board_card_open", { p0: LABEL[open.stage] }))
      : openTag.remove();
  }
  renderCardMedia(sid, stage, entry);
}
// 编辑器草稿变化（未保存标记、摘要）时只重建该分镜卡片的文字部分。
function refreshShotCard(sid) {
  const entry = state.shotCards.get(sid);
  if (entry) {
    renderShotCard(sid, order().indexOf(sid), entry, true);
  }
  updateLocks();
}
// ---------------------------------------------------------------- 阶段详情区
function renderStagePanel() {
  const panel = $("stage-panel");
  const placeholder = $("stage-placeholder");
  const open = selection();
  if (!open) {
    if (!panel.hidden) {
      pauseMedia(panel);
      panel.replaceChildren();
      panel.hidden = true;
    }
    state.stagePanel = { key: null, historyKey: null };
    placeholder.hidden = false;
    return;
  }
  const { sid, stage } = open;
  const ids = order();
  const index = ids.indexOf(sid);
  placeholder.hidden = true;
  panel.hidden = false;
  const selected = plan().shots[sid][stage];
  const history = Object.values(state.data.versions)
    .filter((v) => v.stage === stage && v.shot_id === sid)
    .map((v) => v.id);
  const key = JSON.stringify([
    scopeKey(),
    sid,
    stage,
    selected,
    stage === "content" ? null : plan().options,
    stage === "content" ? ids : null,
    stage === "tts"
      ? [
          state.data.tts_settings,
          plan().shots[sid].content,
          plan().shots[sid].music,
          state.data.shot_music?.[sid],
          latestNode("music", sid),
        ]
      : null,
    stage === "render"
      ? [
          musicMaterialKey(sid),
          plan().shots[sid].content,
          plan().shots[sid].render,
          plan().shots[sid].overlay,
          latestNode("overlay", sid),
        ]
      : null,
    stage === "select" ? state.data.revision : null,
    stage === "source" ? plan().shots[sid].content : null,
    ["recall", "select"].includes(stage)
      ? [plan().anchors?.[sid], plan().shots[sid].recall, plan().shots[sid].tts, plan().ranges[sid]]
      : null,
  ]);
  const title = MindexI18n.msg("ui.4d2801d57965", {
    p0: index + 1,
    p1: LABEL[stage],
    p2: shortId(sid),
  });
  if (state.stagePanel.key !== key) {
    pauseMedia(panel);
    panel.replaceChildren();
    const heading = el("div", "", "detail-heading");
    heading.append(el("h3", title), button(MindexI18n.msg("ui.board_close"), closeStage));
    panel.append(heading);
    stage === "content"
      ? renderShotEditor(panel, sid)
      : stage === "source"
        ? renderSourceDetails(panel, sid)
        : renderStageDetails(panel, sid, stage);
    state.stagePanel = { key, historyKey: null };
  }
  MindexI18n.setText(panel.querySelector("h3"), title);
  const result = panel.querySelector("[data-node-result]");
  if (result) {
    const message = executionError(stage, sid);
    if (result.textContent !== message) {
      MindexI18n.setText(result, message);
    }
    result.hidden = !message;
  }
  const historyKey = JSON.stringify(history);
  if (state.stagePanel.historyKey !== historyKey) {
    // 上传素材预览（source）没有版本历史。
    const historyBox = panel.querySelector("[data-row-history]");
    if (historyBox) {
      const preferred = historyBox.querySelector("select")?.value;
      historyBox.replaceChildren(historyCell(stage, sid, preferred));
    }
    state.stagePanel.historyKey = historyKey;
  }
}
// ---------------------------------------------------------------- 阶段浮层（卡片下方竖排的全部阶段）
// 浮层放在分镜条之外并以 fixed 定位：分镜条 overflow-x:auto 会把纵向溢出一并裁剪。
const POPOVER_OPEN_MS = 120;
const POPOVER_CLOSE_MS = 200;
const popover = { sid: null, timer: null };
function schedulePopover(sid) {
  clearTimeout(popover.timer);
  if (popover.sid === sid) {
    return;
  }
  popover.timer = setTimeout(() => showPopover(sid), POPOVER_OPEN_MS);
}
function scheduleHidePopover() {
  clearTimeout(popover.timer);
  if (popover.sid) {
    popover.timer = setTimeout(() => hidePopover(), POPOVER_CLOSE_MS);
  }
}
function showPopover(sid, { focus = false } = {}) {
  clearTimeout(popover.timer);
  if (!state.data || !order().includes(sid)) {
    return;
  }
  popover.sid = sid;
  renderPopover();
  const box = $("stage-popover");
  box.hidden = false;
  positionPopover();
  for (const [id, entry] of state.shotCards) {
    entry.card.setAttribute("aria-expanded", String(id === sid));
    entry.menu.setAttribute("aria-expanded", String(id === sid));
  }
  if (focus) {
    (box.querySelector("[aria-current=true]") || box.querySelector("[role=menuitem]"))?.focus();
  }
}
function hidePopover({ restoreFocus = false } = {}) {
  clearTimeout(popover.timer);
  const sid = popover.sid;
  const box = $("stage-popover");
  popover.sid = null;
  if (!box) {
    return;
  }
  box.hidden = true;
  box.replaceChildren();
  for (const entry of state.shotCards.values()) {
    entry.card.setAttribute("aria-expanded", "false");
    entry.menu.setAttribute("aria-expanded", "false");
  }
  if (restoreFocus && sid) {
    // 程序把焦点还给卡片时不应再次展开浮层。
    popover.suppressFocus = true;
    state.shotCards.get(sid)?.card.focus();
    popover.suppressFocus = false;
  }
}
function renderPopover() {
  const sid = popover.sid;
  const box = $("stage-popover");
  const open = selection();
  const index = order().indexOf(sid);
  const focused = document.activeElement?.dataset?.stageOption;
  box.replaceChildren();
  MindexI18n.setAttr(box, "aria-label", MindexI18n.msg("ui.board_stage_menu", { p0: index + 1 }));
  box.append(
    el("p", MindexI18n.msg("ui.board_stage_menu", { p0: index + 1 }), "stage-popover-title"),
  );
  for (const stage of barStages(sid)) {
    const value = stageState(sid, stage);
    const item = el("button", "", "stage-option");
    item.type = "button";
    item.setAttribute("role", "menuitem");
    item.tabIndex = -1;
    item.dataset.stageOption = stage;
    item.append(
      stageDot(sid, stage),
      el("span", LABEL[stage], "stage-option-label"),
      el("span", stageStateText(value), "stage-option-state muted"),
    );
    if (open?.sid === sid && open.stage === stage) {
      item.setAttribute("aria-current", "true");
      item.classList.add("is-current");
    }
    if (stage === cardStage(sid)) {
      item.classList.add("is-card-stage");
    }
    item.addEventListener("click", (event) => {
      openStage(sid, stage);
      // 键盘激活（detail 为 0）时把焦点移到详情区，鼠标点击则保持原位。
      if (!event.detail) {
        $("stage-panel").focus();
      }
    });
    box.append(item);
  }
  if (focused) {
    box.querySelector(`[data-stage-option="${focused}"]`)?.focus();
  }
}
function positionPopover() {
  const box = $("stage-popover");
  if (!popover.sid || box.hidden) {
    return;
  }
  const card = state.shotCards.get(popover.sid)?.card;
  if (!card?.isConnected) {
    return hidePopover();
  }
  const rect = card.getBoundingClientRect();
  const strip = $("shot-strip").getBoundingClientRect();
  // 卡片滚出分镜条可视区域时收起。
  if (
    rect.right <= strip.left + 8 ||
    rect.left >= strip.right - 8 ||
    rect.bottom < 0 ||
    rect.top > innerHeight
  ) {
    return hidePopover();
  }
  const width = box.offsetWidth;
  const left = Math.max(8, Math.min(Math.max(rect.left, strip.left), innerWidth - width - 8));
  box.style.left = `${Math.round(left)}px`;
  box.style.top = `${Math.round(rect.bottom + 4)}px`;
}
function popoverKeydown(event) {
  const items = [...$("stage-popover").querySelectorAll("[role=menuitem]")];
  const current = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    items[(current + step + items.length) % items.length]?.focus();
  } else if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    items[event.key === "Home" ? 0 : items.length - 1]?.focus();
  } else if (event.key === "Escape" || event.key === "Tab") {
    if (event.key === "Escape") {
      event.preventDefault();
    }
    hidePopover({ restoreFocus: event.key === "Escape" });
  }
}
// ---------------------------------------------------------------- 分镜条滚动
function updateStripNav() {
  const strip = $("shot-strip");
  const max = strip.scrollWidth - strip.clientWidth;
  // 留 2px 容差：分镜条有内边距，吸附位置与 0 / 末端可能差 1–2px。
  $("strip-prev").disabled = strip.scrollLeft <= 2;
  $("strip-next").disabled = strip.scrollLeft >= max - 2;
  $("strip-prev").hidden = $("strip-next").hidden = max <= 1;
}
function scrollStrip(direction) {
  const strip = $("shot-strip");
  strip.scrollBy({ left: direction * Math.max(220, strip.clientWidth * 0.8), behavior: "smooth" });
}
// 鼠标滚轮的纵向滚动转为横向；已滚到尽头时交还给页面。
function stripWheel(event) {
  const strip = $("shot-strip");
  if (event.ctrlKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) {
    return;
  }
  const max = strip.scrollWidth - strip.clientWidth;
  if (max <= 1) {
    return;
  }
  if (
    (event.deltaY < 0 && strip.scrollLeft <= 0) ||
    (event.deltaY > 0 && strip.scrollLeft >= max - 1)
  ) {
    return;
  }
  event.preventDefault();
  strip.scrollLeft += event.deltaY * (event.deltaMode === 1 ? 16 : 1);
}
// ---------------------------------------------------------------- 卡片产物
// 卡片进入分镜条可视区域才创建媒体；离开时暂停播放。
const cardObserver =
  typeof IntersectionObserver === "function"
    ? new IntersectionObserver(
        (entries) => {
          for (const item of entries) {
            const entry = [...state.shotCards.values()].find((e) => e.card === item.target);
            if (!entry) {
              continue;
            }
            entry.visible = item.isIntersecting;
            if (item.isIntersecting) {
              buildCardMedia(entry);
            } else {
              pauseMedia(entry.mediaBox);
            }
          }
        },
        { root: document.getElementById("shot-strip"), rootMargin: "0px 200px" },
      )
    : null;
function quietVideo(node) {
  for (const video of node.tagName === "VIDEO" ? [node] : node.querySelectorAll?.("video") || []) {
    video.muted = true;
    video.defaultMuted = true;
    video.autoplay = false;
    video.preload = "metadata";
  }
  return node;
}
function cardImage(url, alt) {
  const img = el("img");
  img.src = url;
  img.loading = "lazy";
  MindexI18n.setAttr(img, "alt", alt);
  img.addEventListener(
    "error",
    () => {
      img.hidden = true;
    },
    { once: true },
  );
  return img;
}
// 某分镜某阶段在卡片上嵌入的产物：{key, build(box)}；没有产物时返回 null。
function cardMediaSpec(sid, stage) {
  const shot = plan().shots[sid] || {};
  if (stage === "source") {
    const source = ver(shot.content)?.data?.source;
    if (!source?.media_id) {
      return null;
    }
    const kind = source.kind === "image" ? "image" : "video";
    return {
      key: ["source", source.media_id, kind, source.start || 0],
      build: (box) =>
        inputUrl(source.media_id).then((url) => {
          if (url && box.isConnected) {
            box.replaceChildren(quietVideo(sourcePreviewNode(url, kind, source.start)));
          }
        }),
    };
  }
  const item = ver(shot[stage]);
  if (!item || item.status !== "complete" || item.incomplete) {
    return null;
  }
  const files = item.files || {};
  const video = (url) =>
    url && {
      key: [stage, item.id, url],
      build: (box) => box.replaceChildren(quietVideo(media("video", url))),
    };
  if (stage === "tts") {
    return files.audio?.url
      ? {
          key: [stage, item.id],
          build: (box) => box.replaceChildren(media("audio", files.audio.url)),
        }
      : null;
  }
  // 分镜渲染：带配音与字幕的视频（不静音，只在点击播放时发声），不单独显示音频。
  if (stage === "render") {
    return files.preview?.url || files.video?.url
      ? { key: [stage, item.id], build: (box) => box.replaceChildren(renderPreviewVideo(item)) }
      : null;
  }
  if (stage === "generate") {
    return video(files.generated?.url) || null;
  }
  if (stage === "invoke") {
    return item.data.output === "image"
      ? files.artifact?.url
        ? {
            key: [stage, item.id],
            build: (box) => box.replaceChildren(cardImage(files.artifact.url, LABEL.invoke)),
          }
        : null
      : video(files.artifact?.url) || null;
  }
  if (stage === "draw") {
    return files.image?.url
      ? {
          key: [stage, item.id],
          build: (box) => box.replaceChildren(cardImage(files.image.url, LABEL.draw)),
        }
      : null;
  }
  if (stage === "recall") {
    const thumbs = (item.data.results || []).filter((c) => c.thumbnail_url).slice(0, 3);
    if (!thumbs.length) {
      return null;
    }
    return {
      key: [stage, item.id],
      build: (box) => {
        const row = el("div", "", "card-thumbs");
        row.append(
          ...thumbs.map((c) => cardImage(c.thumbnail_url, sourceName(c.source_id, c.source_name))),
        );
        box.replaceChildren(row);
      },
    };
  }
  if (stage === "select") {
    return selectMediaSpec(sid, item);
  }
  return null;
}
// 选片：素材库来源点击后才加载原片区间播放列表（静音，不调用 ffmpeg 预览）；上传片段/生成画面直接播放文件区间。
function selectMediaSpec(sid, item) {
  const shot = plan().shots[sid];
  const range = item.data.event.video;
  if (range.kind === "file") {
    const generated = shotStages(sid).includes("generate")
      ? ver(shot.generate)?.files?.generated?.url
      : shotStages(sid).includes("invoke")
        ? ver(shot.invoke)?.files?.artifact?.url
        : null;
    const mediaId = ver(shot.content)?.data?.source?.media_id;
    return {
      key: ["select-file", item.id, generated, mediaId],
      build: (box) =>
        (generated ? Promise.resolve(generated) : inputUrl(mediaId)).then((url) => {
          if (!url || !box.isConnected) {
            return;
          }
          const node = quietVideo(
            media(
              "video",
              `${url}#t=${Number(range.start || 0).toFixed(3)},${Number(range.end || 0).toFixed(3)}`,
            ),
          );
          box.replaceChildren(node);
        }),
    };
  }
  const segment = range.segment_ids?.[0];
  return {
    key: ["select", item.id],
    build: (box) => {
      const poster = el("div", "", "card-select-poster");
      if (segment) {
        poster.append(
          cardImage(
            `/api/admin/segments/${encodeURIComponent(segment)}/thumbnail`,
            sourceName(range.source_id, range.source_name),
          ),
        );
      }
      const play = el("button", MindexI18n.msg("ui.board_play_range"), "quiet card-play");
      play.type = "button";
      play.addEventListener("click", () => {
        const player = quietVideo(el("video"));
        player.controls = true;
        box.replaceChildren(player);
        const attached = ShotPlayer.attach(
          player,
          `${base()}/versions/${encodeURIComponent(item.id)}/source-playlist`,
        );
        attached.ready
          .then(() => attached.togglePlayback())
          .catch((error) => {
            if (box.isConnected) {
              box.replaceChildren(
                el(
                  "p",
                  MindexI18n.msg("ui.board_media_failed", { p0: shortIds(error.message) }),
                  "warn-text",
                ),
              );
            }
          });
      });
      poster.append(play);
      box.replaceChildren(poster);
    },
  };
}
function renderCardMedia(sid, stage, entry) {
  const spec = cardMediaSpec(sid, stage);
  const key = spec ? JSON.stringify(spec.key) : null;
  if (entry.mediaKey === key) {
    return;
  }
  pauseMedia(entry.mediaBox);
  entry.mediaBox.replaceChildren();
  entry.mediaBox.hidden = !spec;
  entry.mediaKey = key;
  entry.media = spec;
  entry.built = false;
  // 没有 IntersectionObserver（或已在可视区域）时立即创建。
  if (!cardObserver || entry.visible) {
    buildCardMedia(entry);
  }
}
function buildCardMedia(entry) {
  if (entry.built || !entry.media) {
    return;
  }
  entry.built = true;
  Promise.resolve(entry.media.build(entry.mediaBox)).catch((error) => {
    if (entry.mediaBox.isConnected) {
      entry.mediaBox.replaceChildren(
        el(
          "p",
          MindexI18n.msg("ui.board_media_failed", { p0: shortIds(error.message) }),
          "warn-text",
        ),
      );
    }
  });
}
document.addEventListener("selectionchange", () => {
  if (
    state.data &&
    [...state.shotCards.values()].some((entry) => entry.pending && !selectionWithin(entry.card))
  ) {
    renderMatrix();
  }
});
