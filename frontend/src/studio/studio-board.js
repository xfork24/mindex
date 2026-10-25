/* 创作页 · 分镜表：草稿与锁定、单元格摘要、检索组合、矩阵渲染。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
function scopeKey() {
  return `${state.id}/${plan().id}`;
}
function draftKey(sid) {
  return `${scopeKey()}/${sid}`;
}
function expandedRows() {
  if (!state.expanded.has(scopeKey()))
  state.expanded.set(scopeKey(), new Map());
  return state.expanded.get(scopeKey());
}
function hasDirtyDraft() {
  const prefix = `${scopeKey()}/`;
  // 文案框里未保存的修改同样阻止构建：否则构建用的是已保存版本，与屏幕上看到的不一致。
  return scriptDirty() || [...state.drafts].some(
    ([key, draft]) => key.startsWith(prefix) && draft.dirty
  );
}
function mutationButton(text, action, unavailable = false, allowDirty = false) {
  const n = button(text, () => {
    if (busy() || !allowDirty && hasDirtyDraft()) return;
    return action();
  });
  n.dataset.mutating = "true";
  n.dataset.unavailable = String(unavailable);
  n.dataset.allowDirty = String(allowDirty);
  return n;
}
function updateLocks() {
  if (!state.data) return;
  const locked = busy(),
    dirty = hasDirtyDraft();
  syncScriptControls();
  const collapse = $("collapse-all-rows");
  collapse.disabled = !order().length;
  MindexI18n.setText(collapse, MindexI18n.msg(expandedRows().size ? "ui.57f2d43c7d09" : "ui.expand_all_rows"));
  for (const id of [
  "plan-select",
  "new-plan",
  "copy-plan",
  "delete-job",
  "rename-plan",
  "delete-plan",
  "plan-settings",
  "run-all"])
  {
    $(id).disabled = locked || dirty && !["plan-select", "delete-job"].includes(id);
    MindexI18n.setAttr($(id), "title", dirty && !["plan-select", "delete-job"].includes(id) ? MindexI18n.msg("ui.9f185b0cc351") : "");
  }
  // 唯一的方案不能删除：按钮保持可见但禁用，并说明原因。
  if (Object.keys(state.data.plans).length < 2) {
    $("delete-plan").disabled = true;
    MindexI18n.setAttr($("delete-plan"), "title", MindexI18n.msg("ui.delete_plan_last"));
  }
  renderCancel();
  document.querySelectorAll(".regenerate-column, .execute-column").forEach((n) => {
    n.disabled =
    locked || dirty || n.dataset.stage !== "split" && !order().length;
    MindexI18n.setAttr(n, "title", dirty ? MindexI18n.msg("ui.9f185b0cc351") :

    n.dataset.stage === "split" ? MindexI18n.msg("ui.cddf7b77b6a7") : MindexI18n.msg("ui.319e0a3a0458"));


  });
  $("matrix-body").
  querySelectorAll("[data-mutating]").
  forEach((n) => {
    n.disabled =
    locked ||
    n.dataset.unavailable === "true" ||
    n.dataset.requiresTts && !plan().shots[n.dataset.requiresTts]?.tts ||
    dirty && n.dataset.allowDirty !== "true";
  });
  $("matrix-body").
  querySelectorAll("[data-shot-input]").
  forEach((n) => {
    n.disabled = locked;
  });
}
function pauseMedia(node) {
  node.querySelectorAll("audio,video").forEach((n) => n.pause());
}
function toggleCell(sid, stage) {
  const expanded = expandedRows();
  expanded.get(sid) === stage ? expanded.delete(sid) : expanded.set(sid, stage);
  renderMatrix();
}
function collapseAllRows() {
  if (!state.data) return;
  const expanded = expandedRows();
  if (expanded.size) expanded.clear();
  else for (const sid of order()) expanded.set(sid, "content");
  renderMatrix();
}
function invalidatePanel(sid) {
  const row = state.matrixRows.get(sid);
  if (row) row.panelKey = null;
}
function latestNode(stage, sid) {
  const build = [...state.data.builds].reverse().find((b) =>
  b.plan_id === plan().id &&
  b.nodes.some((n) => n.stage === stage && n.shot_id === sid)
  );
  return build?.nodes.find((n) => n.stage === stage && n.shot_id === sid);
}
function selectionWithin(node) {
  const selection = window.getSelection();
  return selection && !selection.isCollapsed && (
  node.contains(selection.anchorNode) || node.contains(selection.focusNode));
}
function executionError(stage, sid) {
  const node = latestNode(stage, sid);
  if (node?.status !== "failed") return "";
  const attempted = ver(node.version),
    tts = attempted?.parameters.tts,
    detail = node.error || attempted?.error || MindexI18n.msg("ui.6d38d4b01891");
  const context = stage === "tts" && tts ? MindexI18n.msg("ui.710dfeaa9bc9", { p0:
    tts.model, p1: tts.voice }) : "";
  const advice = stage === "tts" && MindexI18n.render(detail).includes("voice id not exist") ? MindexI18n.msg("ui.bdc1a8e66ba1") :
  "";
  const changed = stage === "tts" && tts && state.data.tts_settings &&
  tts.voice !== state.data.tts_settings.voice;
  return shortIds(MindexI18n.msg("ui.853971a580b9", { p0: context, p1: MindexI18n.system(detail), p2: changed ? MindexI18n.msg("ui.249b7096f10b", { p0:
      state.data.tts_settings.voice }) :
    advice }));
}
function cellSummary(sid, stage) {
  const selected = plan().shots[sid][stage],
    item = ver(selected);
  const cell = el("td"),
    trigger = el("div", "", "cell-trigger");
  if (stage === "source") return sourceCell(sid, cell, trigger);
  if (stage !== "content" && !shotStages(sid).includes(stage)) {
    // 此画面来源没有该阶段（例如图片分镜的召回与选片）：只显示说明，不可展开。
    trigger.setAttribute("aria-disabled", "true");
    trigger.append(
      el("span", MindexI18n.msg("ui.stage_not_applicable"), "badge"),
      el("span", MindexI18n.msg("ui.stage_not_applicable_hint"), "cell-summary muted"),
      el("span", "", "cell-expand")
    );
    trigger.lastElementChild.hidden = true;
    cell.append(trigger);
    return cell;
  }
  trigger.setAttribute("role", "button");
  trigger.tabIndex = 0;
  MindexI18n.setAttr(trigger, "aria-label", MindexI18n.msg("ui.c09c857f141c", { p0: shortId(sid), p1: LABEL[stage] }));
  trigger.setAttribute(
    "aria-expanded",
    String(expandedRows().get(sid) === stage)
  );
  trigger.setAttribute("aria-controls", `shot-detail-${sid}`);
  trigger.append(
    el(
      "span",
      selected ? MindexI18n.msg("ui.ac3d0b42ecd9", { p0: selected.slice(0, 8) }) : MindexI18n.msg("ui.53e2db70167f"),
      `badge${selected ? " good" : ""}`
    )
  );
  if (item) {
    const d = item.data;
    if (stage === "content") {
      trigger.append(el("span", d.text, "cell-summary"));
      // 画面意象非空时完整显示；为空时不显示该行，也不放占位文字。
      if (typeof d.visual === "string" && d.visual.trim())
      trigger.append(
        el("span", MindexI18n.msg("ui.visual_summary", { p0: d.visual }), "cell-summary visual-summary")
      );
      trigger.append(el("span", d.query, "cell-summary muted"));
      const source = sourceSummary(d.source);
      if (source) trigger.append(el("span", source, "cell-summary"));
      // 分镜级召回覆盖（粒度/数量）在格内摘要可见，便于发现被钉住的分镜。
      const overrides = [
        d.granularity && GRANULARITY_LABEL[d.granularity],
        d.recall_limit && MindexI18n.msg("ui.adf1951c424c", { p0: d.recall_limit })
      ].filter(Boolean);
      if (overrides.length)
      trigger.append(
        el("span", MindexI18n.msg("ui.24fc33ac59f6", { p0: overrides.join(" · ") }), "cell-summary muted")
      );
      if (state.drafts.get(draftKey(sid))?.dirty)
      trigger.append(el("span", MindexI18n.msg("ui.70ac7eb1c08c"), "badge warn"));
    } else {
      const summary =
      stage === "recall" ? MindexI18n.msg("ui.3b8b0abb1f35", { p0:
        RECALL_MODE_LABEL[d.mode || "text"], p1: d.results?.length || 0 }) :
      stage === "tts" ?
      secs(d.duration) :
      stage === "draw" ?
      MindexI18n.msg("ui.draw_summary", { p0: d.width || "?", p1: d.height || "?" }) :
      stage === "generate" ?
      MindexI18n.msg("ui.generate_summary", { p0: secs(d.duration), p1: d.polls ?? "?" }) :
      stage === "animate" ?
      MindexI18n.msg(d.origin === "manual" ? "ui.animate_summary_manual" : "ui.animate_summary", { p0: d.chars ?? "?", p1: secs(d.seconds) }) :
      stage === "select" ?
      `${d.event.video.kind === "file" ? MindexI18n.msg(d.generated ? "ui.source_aigc" : "ui.source_clip") : sourceName(d.event.video.source_id, d.event.video.source_name)} ${secs(d.event.video.start)}–${secs(d.event.video.end)}` : MindexI18n.msg("ui.a8e554e0bc5a", { p0:
        d.frames || "?", p1: d.fps || "?" });
      trigger.append(el("span", summary, "cell-summary"));
    }
  }
  if (stage === "tts") {
    // 背景音乐显示在配音列：提示点、顺延或停止。
    const line = musicSummary(sid);
    if (line) trigger.append(el("span", line, "cell-summary music-summary"));
  }
  const messages = state.data.conflicts[`${stage}:${sid}`] || [];
  if (messages.length)
  trigger.append(el("span", shortIds(MindexI18n.msg("ui.e5d4ec45ff67", { p0: messages.join("；") })), "cell-summary warn-text"));
  const node = latestNode(stage, sid);
  if (node)
  trigger.append(
    el("span", MindexI18n.msg("ui.7f95d89a3924", { p0: statusText(node.status) }), "cell-status")
  );
  const error = executionError(stage, sid);
  if (error) trigger.append(el("span", error, "cell-summary warn-text"));
  trigger.append(
    el(
      "span",
      expandedRows().get(sid) === stage ? MindexI18n.msg("ui.7831f9512e6b") : MindexI18n.msg("ui.bb066c91ece7"),
      "cell-expand"
    )
  );
  cell.addEventListener("click", (event) => {
    if (event.detail > 1 || selectionWithin(cell)) return;
    toggleCell(sid, stage);
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    toggleCell(sid, stage);
  });
  cell.append(trigger);
  return cell;
}
// 召回列里上传片段/图片分镜的格子：显示上传的素材，展开后预览；没有可执行的画面阶段。
function sourceCell(sid, cell, trigger) {
  const source = ver(plan().shots[sid].content)?.data?.source || {};
  trigger.setAttribute("role", "button");
  trigger.tabIndex = 0;
  MindexI18n.setAttr(trigger, "aria-label", MindexI18n.msg("ui.c09c857f141c", { p0: shortId(sid), p1: LABEL.source }));
  trigger.setAttribute("aria-expanded", String(expandedRows().get(sid) === "source"));
  trigger.setAttribute("aria-controls", `shot-detail-${sid}`);
  trigger.append(
    el("span", MindexI18n.msg(source.media_id ? "ui.source_cell_uploaded" : "ui.source_no_file"), `badge${source.media_id ? " good" : ""}`),
    el("span", sourceSummary(source), "cell-summary"),
    el("span", expandedRows().get(sid) === "source" ? MindexI18n.msg("ui.7831f9512e6b") : MindexI18n.msg("ui.bb066c91ece7"), "cell-expand")
  );
  cell.addEventListener("click", (event) => {
    if (event.detail > 1 || selectionWithin(cell)) return;
    toggleCell(sid, "source");
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    toggleCell(sid, "source");
  });
  cell.append(trigger);
  return cell;
}
// 检索组合依赖（后端 detail().retrieval）：召回签名记录并比对内容索引与检索发布版，
// 两者不一致的召回节点会提示选用版本不兼容。
function retrievalCombo(item, full) {
  const cut = (id) => full ? id : shortId(id);
  const unbound = MindexI18n.msg("ui.index_release_unbound");
  return MindexI18n.msg("ui.retrieval_combo", {
    p0: item.release_id ? cut(item.release_id) : unbound,
    p1: cut(item.content_id),
    p2: !item.release_id ? unbound : !item.known ? MindexI18n.msg("ui.retrieval_deps_unknown") : item.person_id ? cut(item.person_id) : MindexI18n.msg("ui.5a65ae692a28")
  });
}
// 分镜表上方一行：当前生效的组合、工作台依赖的组合，以及两者是否一致。
function renderRetrieval() {
  const box = $("retrieval-deps"), info = state.data.retrieval;
  box.replaceChildren();
  box.hidden = !info;
  if (!info) return;
  const describe = (full) => {
    const active = info.active ? retrievalCombo(info.active, full) : MindexI18n.msg("ui.retrieval_line_no_active");
    const used = !info.used.length ? MindexI18n.msg("ui.retrieval_line_no_recall") :
    info.used.map((item) => info.used.length > 1 ?
    MindexI18n.msg("ui.retrieval_combo_shots", { p0: retrievalCombo(item, full), p1: item.shots }) : retrievalCombo(item, full)).join(" / ");
    const verdict = !info.used.length || !info.active ? "ui.retrieval_line_na" :
    info.used.every((item) => item.current) ? "ui.retrieval_line_same" : "ui.retrieval_line_diff";
    return { text: MindexI18n.msg("ui.retrieval_line", { p0: active, p1: used, p2: MindexI18n.msg(verdict) }), verdict };
  };
  const line = describe(false);
  const row = el("p", line.text, `retrieval-line${line.verdict === "ui.retrieval_line_diff" ? " is-mismatch" : ""}`);
  MindexI18n.setAttr(row, "title", describe(true).text);
  box.append(row);
}
function renderMatrix() {
  const body = $("matrix-body"),
    scope = scopeKey();
  if (state.matrixScope !== scope) {
    pauseMedia(body);
    body.replaceChildren();
    state.matrixRows.clear();
    state.matrixScope = scope;
  }
  const ids = order(),
    wanted = [];
  for (const [index, sid] of ids.entries()) {
    let entry = state.matrixRows.get(sid);
    if (!entry) {
      const summary = el("tr", "", "shot-summary-row"),
        detail = el("tr", "", "shot-detail-row"),
        cell = el("td");
      cell.colSpan = matrixStages().length + 2;
      const panel = el("div", "", "shot-detail-panel");
      panel.id = `shot-detail-${sid}`;
      cell.append(panel);
      detail.append(cell);
      entry = { summary, detail, panel, panelKey: null };
      state.matrixRows.set(sid, entry);
    }
    const head = el("th", `${index + 1} · ${shortId(sid)}`);
    head.scope = "row";
    const columns = matrixStages();
    const summaryKey = JSON.stringify([
    index, plan().shots[sid], columns, state.data.shot_music?.[sid], latestNode("music", sid),
    columns.map((column) => cellStage(sid, column)).map((stage) => [state.data.conflicts[`${stage}:${sid}`], latestNode(stage, sid)]),
    state.drafts.get(draftKey(sid))?.dirty]
    );
    if (entry.summaryKey !== summaryKey && !selectionWithin(entry.summary)) {
      entry.summary.replaceChildren(
        head,
        ...["content", ...columns].map((column) => cellSummary(sid, cellStage(sid, column)))
      );
      entry.summaryKey = summaryKey;
      entry.summaryPending = false;
    } else if (entry.summaryKey !== summaryKey) {
      entry.summaryPending = true;
    }
    ["content", ...columns].forEach((name, column) => {
      const stage = cellStage(sid, name);
      const trigger = entry.summary.children[column + 1].firstElementChild;
      const expanded = expandedRows().get(sid) === stage;
      trigger.setAttribute("aria-expanded", String(expanded));
      const hint = expanded ? MindexI18n.msg("ui.7831f9512e6b") : MindexI18n.msg("ui.bb066c91ece7");
      if (trigger.lastElementChild.textContent !== hint)
      MindexI18n.setText(trigger.lastElementChild, hint);
    });
    wanted.push(entry.summary);
    // 展开的阶段已不属于该分镜（例如改了画面来源）时收起；上传素材预览（source）只属于没有画面阶段的分镜。
    const open = expandedRows().get(sid);
    if (open && open !== "content" && (open === "source" ? pictureStage(sid) : !shotStages(sid).includes(open)))
    expandedRows().delete(sid);
    const stage = expandedRows().get(sid);
    if (stage) {
      const selected = plan().shots[sid][stage];
      const history = Object.values(state.data.versions).
      filter((v) => v.stage === stage && v.shot_id === sid).
      map((v) => v.id);
      const key = JSON.stringify([
      stage,
      selected,
      stage === "content" ? null : plan().options,
      stage === "content" ? ids : null,
      stage === "tts" ? [state.data.tts_settings, plan().shots[sid].content, plan().shots[sid].music,
      state.data.shot_music?.[sid], latestNode("music", sid)] : null,
      stage === "select" ? state.data.revision : null,
      stage === "source" ? plan().shots[sid].content : null,
      ["recall", "select"].includes(stage) ? [plan().anchors?.[sid], plan().shots[sid].recall, plan().shots[sid].tts, plan().ranges[sid]] : null]
      );
      if (entry.panelKey !== key) {
        pauseMedia(entry.panel);
        entry.panel.replaceChildren();
        const heading = el("div", "", "detail-heading");
        heading.append(
          el("h3", MindexI18n.msg("ui.4d2801d57965", { p0: index + 1, p1: LABEL[stage], p2: shortId(sid) })),
          button(MindexI18n.msg("ui.5d5815647c76"), () => toggleCell(sid, stage))
        );
        entry.panel.append(heading);
        stage === "content" ?
        renderShotEditor(entry.panel, sid) :
        stage === "source" ?
        renderSourceDetails(entry.panel, sid) :
        renderStageDetails(entry.panel, sid, stage);
        entry.panelKey = key;
        entry.historyKey = null;
      }
      MindexI18n.setText(entry.panel.querySelector("h3"), MindexI18n.msg("ui.4d2801d57965", { p0:
        index + 1, p1: LABEL[stage], p2: shortId(sid) }));
      const result = entry.panel.querySelector("[data-node-result]");
      if (result) {
        const message = executionError(stage, sid);
        if (result.textContent !== message) MindexI18n.setText(result, message);
        result.hidden = !message;
      }
      const historyKey = JSON.stringify(history);
      if (entry.historyKey !== historyKey) {
        // 上传素材预览（source）没有版本历史。
        const historyBox = entry.panel.querySelector("[data-row-history]");
        if (historyBox) {
          const preferred = historyBox.querySelector("select")?.value;
          historyBox.replaceChildren(historyCell(stage, sid, preferred));
        }
        entry.historyKey = historyKey;
      }
      wanted.push(entry.detail);
    } else if (entry.detail.parentNode) {
      pauseMedia(entry.detail);
      entry.detail.remove();
    }
  }
  for (const [sid, entry] of state.matrixRows)
  if (!ids.includes(sid)) {
    pauseMedia(entry.detail);
    entry.summary.remove();
    entry.detail.remove();
    state.matrixRows.delete(sid);
  }
  if (!ids.length) {
    body.replaceChildren();
    const row = el("tr"),
      cell = el("td");
    cell.colSpan = matrixStages().length + 2;
    cell.append(
      el(
        "p", MindexI18n.msg("ui.d26826ed782c"),

        "muted"
      ),
      mutationButton(MindexI18n.msg("ui.1c9d6d4917d0"), () => changeStructure(null, "insert"))
    );
    row.append(cell);
    body.append(row);
  } else {
    for (const child of [...body.children])
    if (!wanted.includes(child)) child.remove();
    wanted.forEach((node, index) => {
      if (body.children[index] !== node)
      body.insertBefore(node, body.children[index] || null);
    });
  }
  updateLocks();
}
document.addEventListener("selectionchange", () => {
  if (state.data && [...state.matrixRows.values()].some((entry) =>
  entry.summaryPending && !selectionWithin(entry.summary)
  )) renderMatrix();
});
