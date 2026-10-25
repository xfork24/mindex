/* 创作页 · 预检与构建、成片区、取消执行、删除方案、产物与播放。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
async function preflight(force = false) {
  const id = state.id,
    pid = plan().id;
  const checked = await api(`${planBase()}/preflight?force=${force}`);
  if (id !== state.id || pid !== plan().id) return;
  const body = showDialog(
    force ? MindexI18n.msg("ui.cae32e3f53cc") : MindexI18n.msg("ui.68d9d6d0c6f8")
  );
  if (!plan().board) {
    // 还没有文案时，整体构建从"生成文案"开始；已有文案则直接复用，不重写。
    const needsScript = !plan().script, explanation = el("p");
    explanation.style.whiteSpace = "pre-line";
    explanation.append(
      el("span", MindexI18n.msg(needsScript ? "ui.auto_script_build_intro" : "ui.auto_split_build_intro"), "warn-text"),
      document.createTextNode("\n\n")
    );
    const steps = [
      ...(needsScript ? [["ui.auto_script_step_label", "ui.auto_script_step_description"]] : []),
      ...[0, 1, 2, 3, 4, 5].map((i) => [`ui.auto_split_step_${i}_label`, `ui.auto_split_step_${i}_description`])
    ];
    steps.forEach(([label, description], i) => {
      if (i) explanation.append(document.createTextNode("\n"));
      explanation.append(el("strong", MindexI18n.msg(label)), el("span", MindexI18n.msg(description)));
    });
    body.append(explanation);
  }
  const actionLabel = {
    reuse: MindexI18n.msg("ui.68f7d028b705"),
    rebuild: MindexI18n.msg("ui.f05cd1f881c7"),
    blocked: MindexI18n.msg("ui.a00db105bd49"),
    skip: MindexI18n.msg("ui.af2604e64b4c")
  };
  for (const node of plan().board ? checked.nodes : [])
  body.append(
    el(
      "p",
      shortIds(`${actionLabel[node.action]} · ${LABEL[node.stage]} ${node.shot_id || ""} · ${MindexI18n.system(node.reason)}${node.version ? ` · ${node.version.slice(0, 8)}` : ""}`),
      node.action === "blocked" ? "warn-text" : ""
    )
  );
  body.append(
    el(
      "p", MindexI18n.msg("ui.2011fb6e1206"),

      "muted"
    ),
    button(MindexI18n.msg("ui.10cdc19001e3"),

    async () => {
      await api(`${planBase()}/build`, "POST", {
        token: checked.token,
        force
      });
      closeDialog();
      await refresh();
    }
    )
  );
}
// 成片实际混入的背景音乐段落（成片版本数据 music）：按分镜边界切换，列出每段的分镜范围、时间、音量与试听。
function renderFinalMusic(body, item) {
  const list = item.data?.music;
  if (!list?.length) return;
  const box = el("div", "", "final-music");
  box.append(el("h4", MindexI18n.msg("ui.music_final_heading")));
  const bounds = item.data.boundaries || [];
  const number = (sid) => bounds.findIndex((b) => b.shot_id === sid) + 1;
  for (const segment of list) {
    const row = el("div", "", "final-music-row");
    row.append(el("p", MindexI18n.msg("ui.music_final_item", { p0: number(segment.shots[0]), p1: number(segment.shots[segment.shots.length - 1]),
      p2: secs(segment.start), p3: secs(segment.end), p4: segment.volume, p5: segment.prompt || "" })));
    const music = ver(segment.version);
    if (music?.files?.music?.url) row.append(media("audio", music.files.music.url));
    box.append(row);
  }
  body.append(box);
}
function renderFinal() {
  const p = plan(),
    selected = ver(p.final);
  MindexI18n.setText($("final-hint"), selected ? MindexI18n.msg("ui.dcfddbc47c7d", { p0:
    selected.id.slice(0, 8), p1: selected.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : "" }) : MindexI18n.msg("ui.6da8b19d8be1"));

  const player = $("final-player"),
    url = selected?.files.mp4?.url;
  if (state.finalUrl !== url) {
    state.finalUrl = url;
    player.replaceChildren();
    if (url) {
      const video = media("video", url);
      video.id = "final-video";
      if (selected.files.vtt?.url && !selected.data.burn) {
        const track = document.createElement("track");
        Object.assign(track, {
          kind: "subtitles",
          srclang: "zh",
          label: "中文",
          default: true,
          src: selected.files.vtt.url
        });
        video.append(track);
      }
      player.append(video);
      versionFiles(player, selected);
      renderFinalMusic(player, selected);
    }
  }
  const history = $("final-history");
  history.replaceChildren();
  const d = el("details");
  d.append(el("summary", MindexI18n.msg("ui.cde52b54fb7d")));
  for (const item of Object.values(state.data.versions).
  filter((v) => v.stage === "compose").
  reverse()) {
    const row = el("div", "", "final-row");
    row.append(
      el("span", `${item.id.slice(0, 8)} · ${statusText(item.status)}`),
      button(MindexI18n.msg("ui.23a597a9dc81"), () => showVersion(item.id))
    );
    if (item.status === "complete") {
      row.append(
        button(MindexI18n.msg("ui.00db442ae6c8"),

        async () => {
          await accept(
            await api(`${planBase()}/select-version`, "POST", {
              revision: state.data.revision,
              version_id: item.id,
              stage: "compose"
            })
          );
        },
        busy()
        )
      );
      if (item.data.plan)
      row.append(
        button(MindexI18n.msg("ui.8a061f3f2726"), () => createPlan("restore", item.id), busy())
      );
      versionFiles(row, item);
    }
    d.append(row);
  }
  if (selected) d.append(dependencyTree(selected.id));
  history.append(d);
}
// 取消执行与删除方案：版本工作台的构建与旧任务的步骤执行都可取消；已开始的节点或步骤照常完成。
const runningBuild = () => state.data?.builds.find((b) => b.status === "running") || null;
const legacyRunning = (job = state.data?.job) => job?.execution?.status === "running";
function cancelTarget() {
  const build = runningBuild();
  if (build) return { key: `build:${build.id}`, url: `${base()}/builds/${build.id}/cancel`, message: "ui.5d662e5ba0e8" };
  if (legacyRunning()) return { key: `job:${state.id}`, url: `${base()}/cancel`, message: "ui.studio_cancel_steps_requested" };
  return null;
}
function cancelLabel(n, cancelling) {
  n.disabled = cancelling;
  MindexI18n.setText(n, MindexI18n.msg(cancelling ? "ui.6bc0d687aeaa" : "ui.studio_cancel_run"));
  MindexI18n.setAttr(n, "title", MindexI18n.msg("ui.studio_cancel_run_hint"));
}
function renderCancel() {
  const n = $("cancel-run"), target = cancelTarget();
  // 执行已结束（或换了一次执行）时清掉“正在取消”标记。
  if (state.cancelling && state.cancelling !== target?.key) state.cancelling = null;
  n.hidden = !target;
  cancelLabel(n, !target || state.cancelling === target.key);
}
async function requestCancel(target, node) {
  state.cancelling = target.key;
  cancelLabel(node, true);
  try {
    await api(target.url, "POST");
    notice(MindexI18n.msg(target.message));
  } catch (error) {
    // 409：执行刚好已结束，刷新后按钮自然消失；其余错误照常提示。
    state.cancelling = null;
    fail(error);
  }
}
async function cancelRun() {
  const target = state.data && cancelTarget(), id = state.id;
  if (!target) return;
  await requestCancel(target, $("cancel-run"));
  if (state.id === id) await refresh();
}
async function legacyRunView(id) {
  const detail = await api(`/api/studio/jobs/${encodeURIComponent(id)}`).catch(() => null);
  if (id !== state.id) return true;
  const execution = detail?.job?.execution, box = $("legacy-run");
  if (!legacyRunning(detail?.job)) {
    box.hidden = true;
    if (state.cancelling === `job:${id}`) state.cancelling = null;
    return false;
  }
  $("no-job").hidden = true;
  $("job-workspace").hidden = true;
  box.hidden = false;
  MindexI18n.setText($("legacy-run-text"), MindexI18n.msg("ui.legacy_run_active", {
    p0: LABEL[execution.current_step] || execution.current_step || "…" }));
  cancelLabel($("cancel-legacy"), state.cancelling === `job:${id}`);
  clearTimeout(state.timer);
  state.timer = setTimeout(() => reopenAfterLegacy(id), 1500);
  return true;
}
async function reopenAfterLegacy(id) {
  if (id !== state.id) return;
  try {
    await refresh();
    loadArtifacts().catch(fail);
  } catch (error) {
    if (id === state.id && !(await legacyRunView(id))) fail(error);
  }
}
async function cancelLegacy() {
  const id = state.id;
  if (!id) return;
  await requestCancel({ key: `job:${id}`, url: `${base()}/cancel`, message: "ui.studio_cancel_steps_requested" },
    $("cancel-legacy"));
  if (state.id === id && !(await legacyRunView(id))) await reopenAfterLegacy(id);
}
async function deletePlan() {
  if (!state.data || busy() || Object.keys(state.data.plans).length < 2) return;
  const id = state.id, target = plan();
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.delete_plan_confirm", { p0: target.name })))) return;
  state.submitting = true;
  updateLocks();
  try {
    const data = await api(`${base()}/plans/${encodeURIComponent(target.id)}`, "DELETE");
    state.submitting = false;
    if (state.id !== id) return;
    await accept(data);
    notice(MindexI18n.msg("ui.plan_deleted", { p0: target.name }));
  } finally {
    state.submitting = false;
    if (state.data) updateLocks();
  }
}
function renderBuilds() {
  const view = $("build-history");
  view.replaceChildren();
  for (const b of [...state.data.builds].reverse()) {
    const d = el("details");
    d.append(
      el(
        "summary", MindexI18n.msg("ui.9610eb026792", { p0:
          shortId(b.id), p1: state.data.plans[b.plan_id]?.name || shortId(b.plan_id), p2: statusText(b.status) })
      )
    );
    for (const node of b.nodes)
    d.append(
      el(
        "p",
        shortIds(`${LABEL[node.stage]} ${node.shot_id || ""} · ${statusText(node.status)}${node.error ? ` · ${MindexI18n.system(node.error)}` : ""}`)
      )
    );
    view.append(d);
  }
}
function currentVersionEntries() {
  const p = plan();
  if (!p) return [];
  const entries = [];
  if (p.board) entries.push([p.board, LABEL.board]);
  for (const sid of order()) {
    const s = p.shots[sid] || {};
    for (const stage of ["content", "recall", "draw", "generate", "animate", "invoke", "tts", "select", "render"])
    if (s[stage]) entries.push([s[stage], `${LABEL[stage]} · ${shortId(sid)}`]);
  }
  if (p.final) entries.push([p.final, LABEL.compose]);
  return entries.filter(([id]) => ver(id));
}
function selectedCandidate(sid, recallId) {
  // “当前选中”：人工固定且锚定在当前召回版本的参考锚点优先，其次当前选片实际使用的锚点。
  const results = ver(recallId)?.data.results || [];
  if (!results.length) return null;
  const pinned = plan().anchors?.[sid];
  if (pinned?.mode === "manual" && pinned.recall_version === recallId) {
    const hit = results.find((c) => (c.id || c.segment_id) === pinned.candidate_id);
    if (hit) return hit;
  }
  const anchor = ver(plan().shots[sid]?.select)?.data?.anchor;
  const used = anchor && results.find((c) => c.segment_id === anchor.segment_id);
  return used || null;
}
function stagePreviewToggle(id, stage, chosen) {
  // select 无自身媒体，经 /versions/{id}/preview 合成画面与配音；render 直接用版本文件；
  // recall 预览当前选中的候选分镜原片。
  let host = null;
  const node = button(
    stage === "recall" ? MindexI18n.msg("ui.d869c54677a8") : MindexI18n.msg("ui.f31ed5bdb27d"),
    async () => {
      if (host?.isConnected) {
        host.hidden = !host.hidden;
        return;
      }
      host = el("div", "", "inline-preview");
      if (stage === "select") {
        const r = await api(`${base()}/versions/${id}/preview`);
        host.append(media("video", r.url), link(MindexI18n.msg("ui.e04b78659234"), r.url, true));
      } else if (stage === "recall") {
        host.append(
          el(
            "p", MindexI18n.msg("ui.c2fcd4e57b8b", { p0:
              chosen.rank || "?", p1: sourceName(chosen.source_id, chosen.source_name), p2: secs(chosen.start), p3: secs(chosen.end) }),
            "muted"
          ),
          media("video", chosen.media_url)
        );
      } else {
        const files = ver(id).files;
        host.append(media("video", files.preview?.url || files.video.url));
        if (files.audio?.url) host.append(media("audio", files.audio.url));
      }
      node.after(host);
    }
  );
  return node;
}
function currentArtifactRow(id, label, stage, sid) {
  const item = ver(id);
  const row = el("div", "", "artifact-row");
  row.append(el("span", `${label} · ${shortId(id)}${item.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : ""}`));
  versionFiles(row, item);
  const files = item.files || {};
  if (stage === "select" && !item.incomplete) row.append(stagePreviewToggle(id, stage));
  if (stage === "render" && (files.preview?.url || files.video?.url))
  row.append(stagePreviewToggle(id, stage));
  if (stage === "recall" && sid) {
    const chosen = selectedCandidate(sid, id);
    if (chosen?.media_url) row.append(stagePreviewToggle(id, stage, chosen));
  }
  return row;
}
function renderCurrentArtifacts() {
  const view = $("current-artifacts");
  view.replaceChildren();
  const entries = currentVersionEntries();
  MindexI18n.setText($("current-artifacts-hint"), entries.length ? MindexI18n.msg("ui.fd2983a531cb", { p0:
    entries.length }) : MindexI18n.msg("ui.c22d7acfe02e"));

  const p = plan();
  if (p.board && ver(p.board)) {
    const board = el("details", "", "dependency");
    board.open = true;
    board.append(el("summary", `${LABEL.board} · ${shortId(p.board)}`));
    board.append(currentArtifactRow(p.board, MindexI18n.msg("ui.c8ca780ce823")));
    const shots = el("details", "", "dependency");
    shots.open = true;
    shots.append(el("summary", MindexI18n.msg("ui.ae9da1d3ed8c", { p0: order().length })));
    order().forEach((sid, index) => {
      const s = p.shots[sid] || {};
      const stages = ["content", "recall", "draw", "generate", "animate", "invoke", "tts", "select", "render"].filter((k) => s[k] && ver(s[k]));
      if (!stages.length) return;
      const shot = el("details", "", "dependency-edge");
      shot.append(el("summary", MindexI18n.msg("ui.09c83d435a6c", { p0: index + 1, p1: shortId(sid), p2: stages.length })));
      for (const stage of stages)
      shot.append(currentArtifactRow(s[stage], LABEL[stage], stage, sid));
      shots.append(shot);
    });
    board.append(shots);
    view.append(board);
  }
  if (p.final && ver(p.final)) view.append(currentArtifactRow(p.final, LABEL.compose));
}
const RESIDUAL_LABELS = {
  previews: MindexI18n.msg("ui.a6d8932e1c71"),
  attempts: MindexI18n.msg("ui.4417c6e95e4c"),
  "migration-v2": MindexI18n.msg("ui.20fdefac205f"),
  builds: MindexI18n.msg("ui.80cf66e7dd79")
};
function artifactFileRow(item) {
  const row = el("div", "", "artifact-row");
  row.append(
    link(item.path, item.url, true),
    el("small", `${(item.bytes / 1024).toFixed(1)} KiB`)
  );
  return row;
}
function historyVersionRow(item, debugFiles) {
  const row = el("div", "", "artifact-row");
  const label = el(
    "span",
    `${LABEL[item.stage] || item.stage} · ${shortId(item.id)} · ${statusText(item.status)}` +
    `${item.incomplete ? MindexI18n.msg("ui.68f4c0f5e0e0") : ""}`
  );
  if (MindexI18n.system(item.error)) MindexI18n.setAttr(label, "title", shortIds(MindexI18n.system(item.error)));
  row.append(label);
  versionFiles(row, item);
  // 失败版本会把 debug 文件记进 files；嵌套调试目录只列尚未出现的文件，避免重复。
  const covered = new Set(Object.values(item.files || {}).map((f) => f.path));
  const extra = debugFiles.filter((f) => !covered.has(f.path));
  if (extra.length) {
    const d = el("details", "", "dependency-edge");
    d.append(el("summary", MindexI18n.msg("ui.b2b855ddd8d4", { p0: extra.length })));
    for (const f of extra) d.append(artifactFileRow(f));
    row.append(d);
  }
  return row;
}
function renderHistoryArtifacts(items) {
  const view = $("artifact-view");
  view.replaceChildren();
  const current = new Set(currentVersionEntries().map(([vid]) => vid));
  const history = Object.values(state.data.versions).filter((v) => !current.has(v.id));
  const debugOf = new Map();
  const currentDebug = new Map();
  const residual = new Map();
  const residualGroup = (name) => {
    if (!residual.has(name)) residual.set(name, []);
    return residual.get(name);
  };
  for (const item of items) {
    const versionId = item.path.match(/^versions\/([0-9a-f]+)\//)?.[1];
    if (versionId) {
      if (item.path.includes("/debug/")) {
        const bucket = current.has(versionId) ? currentDebug : debugOf;
        if (!bucket.has(versionId)) bucket.set(versionId, []);
        bucket.get(versionId).push(item);
      } else if (!state.data.versions[versionId]) {
        residualGroup(MindexI18n.msg("ui.a4e74e681f51")).push(item);
      }
      continue;
    }
    const top = item.path.includes("/") ? item.path.split("/")[0] : MindexI18n.msg("ui.8f4e93dc3dd4");
    residualGroup(RESIDUAL_LABELS[top] || top).push(item);
  }
  const archive = el("details", "", "dependency");
  archive.open = true;
  archive.append(
    el("summary", MindexI18n.msg("ui.74fe1bd6e010", { p0: history.length, p1: items.length }))
  );
  const rank = { content: 0, recall: 1, tts: 2, select: 3, render: 4 };
  const byShot = new Map();
  for (const v of history)
  if (v.shot_id && v.stage in rank) {
    if (!byShot.has(v.shot_id)) byShot.set(v.shot_id, []);
    byShot.get(v.shot_id).push(v);
  }
  const boardOrder = order();
  const shotIds = [
  ...boardOrder.filter((sid) => byShot.has(sid)),
  ...[...byShot.keys()].filter((sid) => !boardOrder.includes(sid))];

  if (shotIds.length) {
    const list = el("details", "", "dependency-edge");
    list.open = true;
    list.append(el("summary", MindexI18n.msg("ui.ae9da1d3ed8c", { p0: shotIds.length })));
    shotIds.forEach((sid) => {
      const versions = byShot.get(sid);
      const shot = el("details", "", "dependency-edge");
      const index = boardOrder.indexOf(sid);
      let prefix = index >= 0 ? MindexI18n.msg("ui.640c031a4b90", { p0: index + 1 }) : "";
      if (index < 0) {
        // 已不在当前分镜表中的历史镜头，用其文本片段帮助识别。
        const content = versions.find((v) => v.stage === "content");
        const text = (content?.data?.text || "").replace(/\s+/g, "");
        prefix += text ? `「${text.slice(0, 12)}${text.length > 12 ? "…" : ""}」 · ` : "";
      }
      shot.append(
        el("summary", MindexI18n.msg("ui.f12a0cd9968e", { p0: prefix, p1: shortId(sid), p2: versions.length }))
      );
      versions.
      sort(
        (a, b) => rank[a.stage] - rank[b.stage] || b.created_at.localeCompare(a.created_at)
      ).
      forEach((v) => shot.append(historyVersionRow(v, debugOf.get(v.id) || [])));
      list.append(shot);
    });
    archive.append(list);
  }
  for (const [stage, label] of [
  ["split", MindexI18n.msg("ui.e842286c8f23")],
  ["board", MindexI18n.msg("ui.83fb89d6c204")],
  ["compose", MindexI18n.msg("ui.79c352c0be68")]])
  {
    const versions = history.
    filter((v) => v.stage === stage).
    sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (!versions.length) continue;
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", MindexI18n.msg("ui.8d0b5b4633ec", { p0: label, p1: versions.length })));
    for (const v of versions) group.append(historyVersionRow(v, debugOf.get(v.id) || []));
    archive.append(group);
  }
  if (currentDebug.size) {
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", MindexI18n.msg("ui.4ab0dfbd5e4f", { p0: currentDebug.size })));
    for (const [vid, files] of currentDebug) {
      const v = state.data.versions[vid];
      const d = el("details", "", "dependency-edge");
      d.append(
        el(
          "summary",
          `${LABEL[v.stage] || v.stage}${v.shot_id ? ` · ${shortId(v.shot_id)}` : ""} · ${shortId(vid)}`
        )
      );
      for (const f of files) d.append(artifactFileRow(f));
      group.append(d);
    }
    archive.append(group);
  }
  for (const [name, files] of residual) {
    const group = el("details", "", "dependency-edge");
    group.append(el("summary", MindexI18n.msg("ui.49a0f32d789c", { p0: name, p1: files.length })));
    for (const f of files) group.append(artifactFileRow(f));
    archive.append(group);
  }
  view.append(archive);
}
async function loadArtifacts() {
  const id = state.id;
  if (!id) return;
  const data = await api(`${base()}/artifacts`);
  if (id !== state.id) return;
  $("download-archive").href = `${base()}/archive`;
  renderHistoryArtifacts(data.items);
}
function play(url, title) {
  MindexI18n.setText($("play-title"), shortIds(title));
  const playlist = ShotPlayer.playlistURL(url);
  if(playlist) ShotPlayer.attach($("play-video"), playlist);else $("play-video").src = url;
  $("play-dialog").showModal();
}
