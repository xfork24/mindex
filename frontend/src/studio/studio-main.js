/* 创作页 · 页面事件绑定、新建任务、智能助手上下文与启动（必须最后加载）。按 studio.html 中的顺序以经典脚本加载，顶层声明共享全局作用域。 */
"use strict";
$("play-close").addEventListener("click", () => {
  $("play-video").pause();
  $("play-dialog").close();
});
$("dialog-close").addEventListener("click", closeDialog);
$("work-dialog").addEventListener("close", () =>
$("work-dialog").
querySelectorAll("video,audio").
forEach((n) => n.pause())
);
$("play-dialog").addEventListener("close", () => $("play-video").pause());
$("reload").addEventListener("click", () => loadJobs().catch(fail));
$("reload-artifacts").addEventListener("click", () =>
loadArtifacts().catch(fail)
);
$("plan-select").addEventListener("change", () =>
api(`${base()}/plans/${$("plan-select").value}/activate`, "POST").
then(accept).
catch(fail)
);
$("new-plan").addEventListener("click", () => createPlan("new"));
$("copy-plan").addEventListener("click", () => createPlan("copy"));
$("delete-job").addEventListener("click", async () => {
  if (!state.data || busy()) return;
  const id = state.id;
  if (!window.confirm(MindexI18n.render(MindexI18n.msg("ui.delete_job_confirm", { p0: state.data.job.title })))) return;
  state.submitting = true;
  updateLocks();
  try {
    await api(`/api/studio/jobs/${encodeURIComponent(id)}`, "DELETE");
    if (state.id === id) {
      state.token++;
      clearTimeout(state.timer);
      state.id = state.data = state.finalUrl = state.matrixScope = null;
      state.drafts.clear();
      state.windowDrafts.clear();
      state.matrixRows.clear();
      $("matrix-body").replaceChildren();
      $("final-player").replaceChildren();
      closeDialog();
      $("play-video").pause();
      $("play-video").removeAttribute("src");
      $("play-video").load();
      $("play-dialog").close();
      $("job-workspace").hidden = true;
      $("no-job").hidden = false;
      history.replaceState(null, "", "/studio");
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
$("collapse-all-rows").addEventListener("click", collapseAllRows);
document.
querySelectorAll(".regenerate-column, .execute-column").
forEach((n) =>
n.addEventListener("click", () =>
regenerateColumn(n.dataset.stage, !n.classList.contains("execute-column")).catch(fail)
)
);
const matrixResize = new ResizeObserver((entries) => {
  for (const entry of entries)
  entry.target.style.setProperty(
    "--matrix-viewport-width",
    `${entry.contentRect.width}px`
  );
});
matrixResize.observe(document.querySelector(".matrix-scroll"));
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
    dotCount = dotCount % 3 + 1;
    $("create-dots").textContent = ".".repeat(dotCount);
  }, 500);
  try {
    const scope = createSourceScope();
    if (scope && !scope.length)
      throw new Error(MindexI18n.msg("ui.0fb24191cd2c"));
    const job = await api("/api/studio/jobs", "POST", {
      text: $("create-text").value,
      options: {
        // 音轨/字幕/召回数量不再在创建时设置：默认值生效，之后在方案参数里调整。
        // 全选时 scope 为 null：不限素材，后续新增素材自动可用。
        ...(scope ? { source_ids: scope } : {})
      }
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
$("script-template").addEventListener("change", (event) => chooseTemplate(event.target.value).catch(fail));
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
    plan_id: plan().id,
    shot_count: order().length,
    expanded_shots: [...expandedRows().keys()].map(shortId)
  };
};
// 改动来自任何来源（本标签、其他标签、MCP）都刷新；切换任务只由本标签的定位决定。
let agentRefresh = Promise.resolve();
window.addEventListener("mindex:agent-mutation", (event) => {
  const detail = event.detail || {};
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
  if (state.id && (!detail.job_id || detail.job_id === state.id)) agentRefresh = refresh().catch(fail);
});
const AGENT_SECTIONS = {
  jobs: "#job-list", script: "#script-section", plan: ".plan-bar", storyboard: "#storyboard-section",
  log: ".execution-log-section", final: "#result-section", artifacts: "#artifacts-section"
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
    notice(MindexI18n.msg("ui.agent_focus_other_plan", { p0: state.data.plans[focus.plan_id]?.name || focus.plan_id }));
  let shots = focus.shots || [];
  if (shots.some((sid) => !order().includes(sid))) {
    await refresh();
    shots = shots.filter((sid) => order().includes(sid));
  }
  if (shots.length) {
    $("storyboard-section").open = true;
    if (focus.stage && !passive) {
      // 召回列统一显示画面阶段：定位到召回/生成图片/生成画面/生成动画时展开该分镜实际的画面阶段。
      for (const sid of shots.slice(0, 5)) expandedRows().set(sid, PICTURE_STAGES.includes(focus.stage) ? cellStage(sid, "recall") : focus.stage);
      render();
    }
    shots.forEach((sid, index) => mark(state.matrixRows.get(sid)?.summary, { scroll: index === 0 }));
    return true;
  }
  const target = document.querySelector(AGENT_SECTIONS[focus.section] || AGENT_SECTIONS.storyboard);
  if (target?.tagName === "DETAILS") target.open = true;
  mark(target);
  return true;
};
async function boot() {
  const id = new URLSearchParams(location.search).get("job");
  // 创建表单在选任务之前就要列出素材；和任务列表一样不阻塞工作台首屏。
  loadSources().catch(() => {});
  await Promise.all([
    loadJobs(),
    id ? selectJob(id, { reloadJobs: false }) : Promise.resolve()
  ]);
}
boot().catch(fail);
