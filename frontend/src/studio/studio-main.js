/* 创作页 · 页面事件绑定、新建任务、智能助手上下文与启动（必须最后加载）。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
// 任务创建的素材组筛选默认全选：全选=不限（不保存组 ID，后续新增素材组自动可用）。
const createGroups = MindexGroups.picker("source", [], () => {}, "ui.groups_filter", {
  listbox: true,
  defaultAll: true,
});
$("create-sources").closest("label").before(createGroups.wrap);
createGroupLink = MindexGroups.linkSources(createGroups, $("create-sources"), {
  entries: () => state.sources || [],
  selection: () => createSourceSelection,
  fill: (entries, chosen) => fillCreateSources(entries, chosen),
  onError: (error) => fail(error),
});
$("create-sources").addEventListener("change", () => {
  toggleAllOf($("create-sources"));
  createSourceSelection = pickedIds($("create-sources"));
});
("use strict");
$("play-close").addEventListener("click", () => {
  $("play-video").pause();
  $("play-dialog").close();
});
$("dialog-close").addEventListener("click", closeDialog);
$("work-dialog").addEventListener("close", () =>
  $("work-dialog")
    .querySelectorAll("video,audio")
    .forEach((n) => n.pause()),
);
$("play-dialog").addEventListener("close", () => $("play-video").pause());
$("reload").addEventListener("click", () => loadJobs().catch(fail));
$("reload-artifacts").addEventListener("click", () => loadArtifacts().catch(fail));
$("plan-select").addEventListener("change", () =>
  api(`${base()}/plans/${$("plan-select").value}/activate`, "POST")
    .then(accept)
    .catch(fail),
);
$("new-plan").addEventListener("click", () => createPlan("new"));
$("copy-plan").addEventListener("click", () => createPlan("copy"));
$("delete-job").addEventListener("click", async () => {
  if (!state.data || busy()) return;
  const id = state.id;
  if (
    !window.confirm(
      MindexI18n.render(MindexI18n.msg("ui.delete_job_confirm", { p0: state.data.job.title })),
    )
  )
    return;
  state.submitting = true;
  updateLocks();
  try {
    await api(`/api/studio/jobs/${encodeURIComponent(id)}`, "DELETE");
    if (state.id === id) {
      state.token++;
      clearTimeout(state.timer);
      resetBoard();
      state.id = state.data = state.finalUrl = null;
      state.drafts.clear();
      state.windowDrafts.clear();
      $("final-player").replaceChildren();
      closeDialog();
      $("play-video").pause();
      $("play-video").removeAttribute("src");
      $("play-video").load();
      $("play-dialog").close();
      $("job-workspace").hidden = true;
      $("no-job").hidden = false;
      history.replaceState(null, "", "/");
    }
    await loadJobs();
    notice(MindexI18n.msg("ui.job_deleted"));
  } catch (error) {
    fail(error);
  } finally {
    state.submitting = false;
    if (state.data) updateLocks();
  }
});
$("rename-plan").addEventListener("click", renamePlan);
$("delete-plan").addEventListener("click", () => deletePlan().catch(fail));
$("cancel-run").addEventListener("click", () => cancelRun().catch(fail));
$("cancel-legacy").addEventListener("click", () => cancelLegacy().catch(fail));
$("plan-settings").addEventListener("click", editSettings);
$("run-all").addEventListener("click", () => preflight().catch(fail));
// 批量操作：两个按钮的 data-stage 由阶段下拉同步（updateLocks）。
$("batch-stage").addEventListener("change", () => {
  if (state.data) updateLocks();
});
document
  .querySelectorAll(".regenerate-column, .execute-column")
  .forEach((n) =>
    n.addEventListener("click", () =>
      regenerateColumn($("batch-stage").value, !n.classList.contains("execute-column")).catch(fail),
    ),
  );
// 分镜条：两端按钮、滚轮转横向、滚动时浮层跟随卡片。
$("strip-prev").addEventListener("click", () => scrollStrip(-1));
$("strip-next").addEventListener("click", () => scrollStrip(1));
$("shot-strip").addEventListener("wheel", stripWheel, { passive: false });
$("shot-strip").addEventListener(
  "scroll",
  () => {
    updateStripNav();
    positionPopover();
  },
  { passive: true },
);
window.addEventListener("resize", () => {
  if (state.data) updateStripNav();
  positionPopover();
});
window.addEventListener("scroll", positionPopover, { passive: true, capture: true });
// 阶段浮层：鼠标在浮层内保持展开；键盘上下切换、Esc 收起；焦点或点击离开卡片与浮层时收起。
$("stage-popover").addEventListener("pointerenter", () => clearTimeout(popover.timer));
$("stage-popover").addEventListener("pointerleave", (event) => {
  if (event.pointerType !== "touch") scheduleHidePopover();
});
$("stage-popover").addEventListener("keydown", popoverKeydown);
$("stage-popover").addEventListener("focusout", (event) => {
  const next = event.relatedTarget,
    card = state.shotCards.get(popover.sid)?.card;
  if (next && !$("stage-popover").contains(next) && !card?.contains(next)) hidePopover();
});
document.addEventListener("pointerdown", (event) => {
  if (!popover.sid || $("stage-popover").contains(event.target)) return;
  if (event.target.closest?.(".stage-menu-button")) return;
  if (
    event.pointerType === "mouse" &&
    state.shotCards.get(popover.sid)?.card.contains(event.target)
  )
    return;
  hidePopover();
});
// Ctrl/⌘ + Enter 视为点击「创建任务」；按钮禁用（创建中）时 click() 不触发提交。
$("create-form").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.isComposing) return;
  event.preventDefault();
  $("create-button").click();
});
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("create-button").disabled = true;
  $("create-status").hidden = false;
  let dotCount = 1;
  $("create-dots").textContent = ".";
  const dotsTimer = setInterval(() => {
    dotCount = (dotCount % 3) + 1;
    $("create-dots").textContent = ".".repeat(dotCount);
  }, 500);
  try {
    const scope = createSourceScope();
    if (scope && !scope.length) throw new Error(MindexI18n.msg("ui.0fb24191cd2c"));
    const job = await api("/api/studio/jobs", "POST", {
      text: $("create-text").value,
      language: MindexI18n.language,
      options: {
        // 音轨/字幕/召回数量不再在创建时设置：默认值生效，之后在方案参数里调整。
        // 全选时 scope 为 null：不限素材，后续新增素材自动可用。
        ...(scope ? { source_ids: scope } : {}),
        source_group_ids: createGroups.values(),
      },
    });
    await selectJob(job.id);
    $("create-text").value = "";
    notice(MindexI18n.msg("ui.34fa75919260"));
  } catch (error) {
    fail(error);
  } finally {
    $("create-button").disabled = false;
    clearInterval(dotsTimer);
    $("create-status").hidden = true;
  }
});
$("script-text").addEventListener("input", () => {
  if (!state.data) return;
  scriptDrafts.set(scopeKey(), { base: plan().script || "", value: $("script-text").value });
  updateLocks();
});
// 手动编辑的文案在失焦时自动保存为新版本：不再需要“保存文案”按钮。
$("script-text").addEventListener("blur", () => {
  if (!state.data || busy() || !scriptDirty()) return;
  saveScript().catch(fail);
});
$("script-generate").addEventListener("click", () => generateScript().catch(fail));
$("script-template").addEventListener("change", (event) =>
  chooseTemplate(event.target.value).catch(fail),
);
// 「管理模板」是指向模板管理页的链接；从管理页返回（含浏览器往返缓存）或切回本标签时重新读取模板目录。
window.addEventListener("pageshow", (event) => {
  if (event.persisted && state.data) refreshTemplates().catch(() => {});
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.data) refreshTemplates().catch(() => {});
});
// 智能助手：随消息发送当前任务、方案与展开的分镜；助手写操作完成后刷新工作台。
window.MindexAgentContext = () => {
  if (!state.data) return { page: "studio", job_id: state.id || null };
  return {
    page: "studio",
    job_id: state.id,
    job_title: state.data.job?.title,
    source_group_ids: state.data.job?.source_group_ids || [],
    plan_id: plan().id,
    shot_count: order().length,
    // 当前在详情区打开的分镜（沿用 expanded_shots 键名，至多一个）与阶段。
    expanded_shots: selection() ? [shortId(selection().sid)] : [],
    open_stage: selection()?.stage || null,
  };
};
// 改动来自任何来源（本标签、其他标签、MCP）都刷新；切换任务只由本标签的定位决定。
let agentRefresh = Promise.resolve();
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
  if (detail.groups && state.id) {
    state.materialDigest = null;
    refresh().catch(fail);
  }
  if (detail.domain !== "studio") return;
  loadJobs().catch(() => {});
  // 文案模板是全局数据（复制/修改/删除模板）：重新读取模板目录。
  if (detail.templates) refreshTemplates().catch(() => {});
  if (detail.deleted && detail.job_id === state.id) {
    state.id = null;
    state.data = null;
    $("job-workspace").hidden = true;
    $("no-job").hidden = false;
    return;
  }
  if (state.id && (!detail.job_id || detail.job_id === state.id))
    agentRefresh = refresh()
      .catch(fail)
      .then(() => {
        if (agentFocusMark) markAgentShots(agentFocusMark.shots);
      });
});
// 最近一次助手定位的分镜标记：分镜板因 agent mutation 重渲染会替换卡片元素，
// 渲染完成后重放高亮，避免跨页定位被随后的刷新冲掉。
let agentFocusMark = null;
function markAgentShots(shots, scroll = false) {
  const cards = shots.map((sid) => state.shotCards.get(sid)?.card).filter(Boolean);
  if (!cards.length) return false;
  cards.forEach((card, index) => {
    if (scroll && index === 0) card.scrollIntoView({ block: "nearest", inline: "nearest" });
    window.MindexAgent?.highlight(card, { scroll: scroll && index === 0 });
  });
  return true;
}
const AGENT_SECTIONS = {
  jobs: "#job-list",
  script: "#script-section",
  plan: ".plan-bar",
  storyboard: "#storyboard-section",
  log: ".execution-log-section",
  final: "#result-section",
  artifacts: "#artifacts-section",
};
// 智能助手定位：切换任务、展开并高亮分镜行或区块。页面未就绪时返回 false，由面板稍后重试。
window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
  if (state.id && !state.data) return false;
  const mark = (element, options) => window.MindexAgent?.highlight(element, options);
  if (focus.job_id && focus.job_id !== state.id) {
    if (passive) return true;
    await selectJob(focus.job_id);
  }
  await agentRefresh;
  if (focus.section === "jobs" || !state.data) {
    mark(document.querySelector(AGENT_SECTIONS.jobs));
    return true;
  }
  if (focus.plan_id && focus.plan_id !== plan().id && !passive)
    notice(
      MindexI18n.msg("ui.agent_focus_other_plan", {
        p0: state.data.plans[focus.plan_id]?.name || focus.plan_id,
      }),
    );
  let shots = focus.shots || [];
  if (shots.some((sid) => !order().includes(sid))) {
    await refresh();
    shots = shots.filter((sid) => order().includes(sid));
  }
  if (shots.length) {
    $("storyboard-section").open = true;
    if (focus.stage && !passive) {
      // 画面阶段统一映射为该分镜实际的画面阶段（或上传素材预览），背景音乐并入配音；只打开第一个目标分镜。
      const sid = shots[0];
      const stage = PICTURE_STAGES.includes(focus.stage)
        ? cellStage(sid, "recall")
        : focus.stage === "music"
          ? "tts"
          : focus.stage;
      if (barStages(sid).includes(stage)) state.selection.set(scopeKey(), { sid, stage });
      render();
    }
    if (!markAgentShots(shots, true)) return false;
    agentFocusMark = { shots };
    return true;
  }
  agentFocusMark = null;
  const target = document.querySelector(AGENT_SECTIONS[focus.section] || AGENT_SECTIONS.storyboard);
  if (target?.tagName === "DETAILS") target.open = true;
  mark(target);
  return true;
};
async function boot() {
  const id = new URLSearchParams(location.search).get("job");
  // 创建表单在选任务之前就要列出素材；和任务列表一样不阻塞工作台首屏。
  loadSources().catch(() => {});
  await Promise.all([loadJobs(), id ? selectJob(id, { reloadJobs: false }) : Promise.resolve()]);
}
boot().catch(fail);
