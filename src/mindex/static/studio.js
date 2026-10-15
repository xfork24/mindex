"use strict";
const STAGES = ["recall", "tts", "select", "render"];
const LABEL = {
  content: "文本与查询",
  board: "分镜表",
  split: "拆分镜",
  recall: "召回",
  tts: "配音",
  select: "选片与对齐",
  render: "分镜渲染",
  compose: "成片合成",
};
const STATUS = {
  running: "执行中",
  partial: "部分完成",
  failed: "失败",
  blocked: "阻塞",
  pending: "等待",
  complete: "完成",
  reused: "复用",
  skipped: "跳过",
};
const state = {
  id: null,
  data: null,
  jobs: [],
  timer: null,
  token: 0,
  submitting: false,
  openCells: new Set(),
  finalUrl: null,
};
const $ = (id) => document.getElementById(id);
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function button(text, action, disabled = false) {
  const n = el("button", text, "quiet");
  n.type = "button";
  n.disabled = disabled;
  n.addEventListener("click", () => {
    $("dialog-error")?.remove();
    Promise.resolve().then(action).catch(fail);
  });
  return n;
}
function link(text, url, download = false) {
  const n = el("a", text, "quiet nav-link");
  n.href =
    url + (download ? `${url.includes("?") ? "&" : "?"}download=true` : "");
  return n;
}
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").dataset.type = error ? "error" : "info";
}
function fail(error) {
  notice(error.message, true);
  if ($("work-dialog").open) {
    $("dialog-error")?.remove();
    const message = el("p", error.message, "warn-text");
    message.id = "dialog-error";
    message.setAttribute("role", "alert");
    $("dialog-content").prepend(message);
  }
}
async function api(url, method = "GET", body) {
  const r = await fetch(url, {
    method,
    ...(body
      ? {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail || r.status),
    );
  return data;
}
const base = () => `/api/studio/jobs/${state.id}`;
const plan = () => state.data.plans[state.data.active_plan];
const planBase = () => `${base()}/plans/${plan().id}`;
const ver = (id) => state.data.versions[id];
const order = () => (plan().board ? ver(plan().board).data.order : []);
const busy = () =>
  state.submitting || state.data?.builds.some((b) => b.status === "running");
const secs = (n) => `${Number(n || 0).toFixed(3)}s`;
function showDialog(title) {
  $("dialog-title").textContent = title;
  const body = $("dialog-content");
  body.replaceChildren();
  if (!$("work-dialog").open) $("work-dialog").showModal();
  return body;
}
function closeDialog() {
  $("work-dialog")
    .querySelectorAll("video,audio")
    .forEach((n) => n.pause());
  $("work-dialog").close();
}
function field(label, value, type = "text", choices = null) {
  const wrap = el("label", label, "form-field");
  const n = document.createElement(
    choices ? "select" : type === "textarea" ? "textarea" : "input",
  );
  if (choices) {
    for (const [v, text] of choices) {
      const o = el("option", text);
      o.value = v;
      n.append(o);
    }
  } else if (type !== "textarea") n.type = type;
  n.value = value ?? "";
  if (type === "number") n.step = "0.001";
  wrap.append(n);
  return { wrap, input: n };
}
function jsonView(value) {
  const pre = el("pre", JSON.stringify(value, null, 2), "json-view");
  return pre;
}
function media(tag, url) {
  const n = document.createElement(tag);
  n.controls = true;
  n.preload = "metadata";
  n.src = url;
  return n;
}
async function loadJobs() {
  const data = await api("/api/studio/jobs");
  state.jobs = data.items;
  const list = $("job-list");
  list.replaceChildren();
  for (const job of data.items) {
    const b = button(job.title, () => selectJob(job.id));
    b.className = `job-item${job.id === state.id ? " active" : ""}`;
    b.append(
      el("div", new Date(job.created_at).toLocaleString("zh-CN"), "job-sub"),
    );
    list.append(b);
  }
}
async function selectJob(id) {
  state.token++;
  clearTimeout(state.timer);
  state.id = id;
  state.data = null;
  state.finalUrl = null;
  state.openCells.clear();
  history.replaceState(null, "", `/studio?job=${id}`);
  closeDialog();
  await refresh();
  await loadJobs();
  await loadArtifacts();
}
async function refresh() {
  const token = ++state.token,
    id = state.id;
  if (!id) return;
  const data = await api(`${base()}/workspace`);
  if (token !== state.token || id !== state.id) return;
  const wasBusy = state.data?.builds.some((b) => b.status === "running");
  state.data = data;
  render();
  clearTimeout(state.timer);
  if (busy()) state.timer = setTimeout(() => refresh().catch(fail), 1200);
  else if (wasBusy) {
    await loadArtifacts();
    await loadJobs();
  }
}
async function accept(data) {
  if (data.job.id !== state.id) return;
  state.data = data;
  render();
}
function render() {
  const p = plan(),
    job = state.data.job,
    locked = busy();
  $("no-job").hidden = true;
  $("job-workspace").hidden = false;
  const header = $("job-header");
  header.replaceChildren();
  const title = field("任务标题", job.title);
  title.input.maxLength = 60;
  header.append(
    title.wrap,
    button(
      "保存标题",
      async () => {
        await api(`${base()}/title`, "PATCH", {
          title: title.input.value.trim(),
        });
        await refresh();
        await loadJobs();
      },
      locked,
    ),
    el("p", `任务 ${job.id}`, "muted"),
  );
  const select = $("plan-select");
  select.replaceChildren();
  for (const item of Object.values(state.data.plans)) {
    const o = el("option", item.name);
    o.value = item.id;
    o.selected = item.id === p.id;
    select.append(o);
  }
  for (const id of [
    "plan-select",
    "new-plan",
    "copy-plan",
    "rename-plan",
    "plan-settings",
    "run-all",
    "force-build",
    "split",
    "edit-board",
  ])
    $(id).disabled = locked;
  $("split").textContent = p.board ? "重新拆分镜" : "生成分镜";
  const activeBuild = [...state.data.builds]
    .reverse()
    .find((b) => b.plan_id === p.id);
  $("execution-status").textContent = activeBuild
    ? `${STATUS[activeBuild.status]} · ${activeBuild.id}`
    : "尚未构建";
  $("shot-count").textContent = `${order().length} 镜`;
  const original = job.input_text.replace(/\s/g, "");
  const texts = order()
    .map((sid) => ver(p.shots[sid].content).data.text)
    .join("")
    .replace(/\s/g, "");
  $("storyboard-warning").textContent =
    p.board && original !== texts ? "分镜未按顺序完整覆盖原文，请核对。" : "";
  renderMatrix();
  renderFinal();
  renderBuilds();
}
function dependencyTree(id, seen = new Set()) {
  const item = ver(id);
  if (!item) return el("p", `缺失记录 ${id}`, "warn-text");
  const d = el("details", "", "dependency");
  d.append(
    el(
      "summary",
      `${LABEL[item.stage]} ${item.shot_id || ""} · ${id.slice(0, 8)}${item.incomplete ? " · 记录不完整" : ""}`,
    ),
  );
  d.append(button("查看节点详情", () => showVersion(id)));
  if (seen.has(id)) return d;
  const next = new Set(seen).add(id);
  for (const [name, dep] of Object.entries(item.inputs))
    if (dep) {
      const row = el("div", "", "dependency-edge");
      row.append(el("span", name, "muted"), dependencyTree(dep, next));
      d.append(row);
    }
  return d;
}
function versionFiles(body, item) {
  for (const [name, metadata] of Object.entries(item.files)) {
    if (metadata.url) body.append(link(`下载 ${name}`, metadata.url, true));
  }
}
function showVersion(id) {
  const item = ver(id),
    body = showDialog(
      `${LABEL[item.stage]} · ${id.slice(0, 8)} · ${STATUS[item.status]}`,
    );
  if (item.incomplete)
    body.append(
      el(
        "p",
        "历史记录不完整，未知关系未推断。原文件可查看和下载。",
        "warn-text",
      ),
    );
  if (item.error) body.append(el("p", item.error, "warn-text"));
  if (item.files.audio?.url) body.append(media("audio", item.files.audio.url));
  if (item.files.preview?.url || item.files.video?.url)
    body.append(
      media("video", item.files.preview?.url || item.files.video.url),
    );
  if (item.files.mp4?.url) body.append(media("video", item.files.mp4.url));
  if (item.stage === "select" && !item.incomplete)
    body.append(
      button("预览此版本（画面与配音）", async () => {
        const r = await api(`${base()}/versions/${id}/preview`);
        body.append(media("video", r.url));
      }),
    );
  versionFiles(body, item);
  body.append(
    el("h3", "实际输入与参数"),
    jsonView({
      inputs: item.inputs,
      parameters: item.parameters,
      data:
        item.stage === "compose"
          ? { ...item.data, graph: undefined, plan: undefined }
          : item.data,
    }),
  );
  body.append(el("h3", "依赖树"), dependencyTree(id));
  if (item.stage === "compose" && item.data.plan)
    body.append(
      button("复制此成片为新方案", () => createPlan("restore", id), busy()),
    );
}
function renderMatrix() {
  const body = $("matrix-body");
  body.replaceChildren();
  const p = plan();
  for (const [index, sid] of order().entries()) {
    const row = el("tr");
    const head = el("th", `${index + 1} · ${sid}`);
    head.scope = "row";
    row.append(head);
    const shot = ver(p.shots[sid].content).data;
    const text = el("td");
    text.append(
      el("p", shot.text, "cell-summary"),
      el("p", shot.query, "muted cell-summary"),
      el("small", `内容 ${p.shots[sid].content.slice(0, 8)}`),
      button("编辑", () => editBoard(sid), busy()),
    );
    text.append(historyCell("content", sid));
    row.append(text);
    for (const stage of STAGES) {
      const cell = el("td"),
        vid = p.shots[sid][stage],
        item = ver(vid);
      cell.append(
        el(
          "span",
          vid ? `选用 ${vid.slice(0, 8)}` : "未选择",
          `badge${vid ? " good" : ""}`,
        ),
      );
      if (item) {
        const d = item.data;
        const summary =
          stage === "recall"
            ? `${d.results?.length || 0} 个候选`
            : stage === "tts"
              ? secs(d.duration)
              : stage === "select"
                ? `${d.event.video.source_name || d.event.video.source_id} ${secs(d.event.video.start)}–${secs(d.event.video.end)}`
                : `${d.frames || "?"} 帧 · ${d.fps || "?"} fps`;
        cell.append(
          el("p", summary, "cell-summary"),
          button("查看选用版本", () => showVersion(vid)),
        );
      }
      const messages = state.data.conflicts[`${stage}:${sid}`] || [];
      if (messages.length)
        cell.append(el("p", messages.join("；"), "warn-text"));
      const latestBuild = [...state.data.builds]
        .reverse()
        .find(
          (b) =>
            b.plan_id === p.id &&
            b.nodes.some((n) => n.stage === stage && n.shot_id === sid),
        );
      const node = latestBuild?.nodes.find(
        (n) => n.stage === stage && n.shot_id === sid,
      );
      if (node)
        cell.append(
          el(
            "p",
            `最近执行：${STATUS[node.status]}${node.error ? ` · ${node.error}` : ""}`,
            node.status === "failed" || node.status === "blocked"
              ? "warn-text"
              : "muted",
          ),
        );
      cell.append(
        button("执行 / 复用", () => runNode(stage, sid, false), busy()),
        button("强制重新生成", () => runNode(stage, sid, true), busy()),
      );
      if (stage === "select")
        cell.append(
          button(
            "人工选择连续镜头",
            () => manualRange(sid),
            busy() || !p.shots[sid].tts,
          ),
        );
      const details = el("details");
      const key = `${stage}:${sid}`;
      details.open = state.openCells.has(key);
      details.append(el("summary", "展开详情与历史版本"));
      details.addEventListener("toggle", () =>
        details.open ? state.openCells.add(key) : state.openCells.delete(key),
      );
      if (stage === "recall" && item) {
        const strip = el("div", "", "candidate-strip");
        for (const c of item.data.results || []) {
          const b = button(
            `${c.source_name || c.source_id} ${secs(c.start)}–${secs(c.end)}`,
            () => play(c.media_url, "召回候选"),
          );
          if (c.thumbnail_url) {
            const img = el("img");
            img.src = c.thumbnail_url;
            img.loading = "lazy";
            img.alt = c.source_name || c.source_id;
            b.prepend(img);
          }
          b.className = "candidate-thumb";
          strip.append(b);
        }
        details.append(strip);
      }
      if (stage === "tts" && item?.files.audio)
        details.append(media("audio", item.files.audio.url));
      if (stage === "select" && item)
        details.append(
          jsonView({
            anchor: item.data.anchor,
            range: item.data.range,
            actual_range: item.data.actual_range,
            extension: item.data.extension,
            trim_seconds: item.data.trim_seconds,
            freeze_seconds: item.data.freeze_seconds,
            atempo: item.data.atempo,
          }),
        );
      details.append(historyCell(stage, sid));
      cell.append(details);
      row.append(cell);
    }
    body.append(row);
  }
  if (!order().length) {
    const row = el("tr"),
      cell = el("td", "先生成分镜，或编辑分镜结构手动添加。");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
}
function historyCell(stage, sid) {
  const box = el("div", "", "version-history");
  const items = Object.values(state.data.versions)
    .filter((v) => v.stage === stage && v.shot_id === sid)
    .reverse();
  const select = document.createElement("select");
  select.setAttribute("aria-label", `${sid} ${LABEL[stage]}历史版本`);
  for (const item of items) {
    const o = el(
      "option",
      `${item.id.slice(0, 8)} · ${STATUS[item.status]} · ${new Date(item.created_at).toLocaleString("zh-CN")}`,
    );
    o.value = item.id;
    select.append(o);
  }
  if (!items.length) return el("p", "暂无历史", "muted");
  const choose = button(
    "选为构建输入",
    async () => {
      await accept(
        await api(`${planBase()}/select-version`, "POST", {
          revision: state.data.revision,
          version_id: select.value,
          stage,
          shot_id: sid,
        }),
      );
      notice("选择已保存。兼容性提示已更新；尚未执行构建。");
    },
    busy() || ver(select.value).status !== "complete",
  );
  select.addEventListener("change", () => {
    choose.disabled = busy() || ver(select.value).status !== "complete";
  });
  box.append(
    select,
    button("查看历史版本", () => showVersion(select.value)),
    choose,
  );
  return box;
}
function nodeInputs(stage, sid) {
  const s = plan().shots[sid];
  if (stage === "split") return {};
  const inputs = { content: s.content };
  if (stage === "select") {
    inputs.tts = s.tts || null;
    if (!plan().ranges[sid]) inputs.recall = s.recall || null;
  }
  if (stage === "render")
    Object.assign(inputs, { select: s.select || null, tts: s.tts || null });
  return inputs;
}
async function runNode(stage, sid, force) {
  const body = {
    stage,
    shot_id: sid || null,
    force,
    revision: state.data.revision,
    inputs: nodeInputs(stage, sid),
  };
  state.submitting = true;
  render();
  try {
    await api(`${planBase()}/nodes`, "POST", body);
    notice(`${LABEL[stage]}已提交。`);
  } finally {
    state.submitting = false;
    await refresh();
  }
}
function createPlan(mode, finalId = null) {
  const body = showDialog(mode === "restore" ? "从成片恢复方案" : "构建方案");
  const f = field(
    "方案名称",
    mode === "new" ? "新方案" : `${plan().name} 副本`,
  );
  body.append(
    f.wrap,
    button("保存方案", async () => {
      await accept(
        await api(`${base()}/plans`, "POST", {
          name: f.input.value,
          ...(mode === "copy" ? { copy_from: plan().id } : {}),
          ...(mode === "restore" ? { from_final: finalId } : {}),
        }),
      );
      closeDialog();
    }),
  );
}
function renamePlan() {
  const body = showDialog("命名方案"),
    f = field("方案名称", plan().name);
  body.append(
    f.wrap,
    button("保存", async () => {
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          name: f.input.value,
        }),
      );
      closeDialog();
    }),
  );
}
function editBoard(focusSid) {
  const body = showDialog("编辑分镜结构与内容");
  let draft = order().map((sid) => ({
    ...ver(plan().shots[sid].content).data,
  }));
  const list = el("div");
  function draw() {
    list.replaceChildren();
    for (const [i, shot] of draft.entries()) {
      const card = el("div", "", "shot-card");
      card.append(el("strong", `${i + 1} · ${shot.id || "新分镜"}`));
      for (const [key, label] of [
        ["text", "旁白与字幕"],
        ["query", "查询"],
      ]) {
        const f = field(label, shot[key], "textarea");
        f.input.maxLength = key === "text" ? 200 : 400;
        f.input.addEventListener("input", () => {
          shot[key] = f.input.value;
        });
        if (shot.id === focusSid && key === "text")
          setTimeout(() => f.input.focus(), 0);
        card.append(f.wrap);
      }
      const duration = field("预估秒数", shot.estimated_seconds, "number");
      duration.input.addEventListener("input", () => {
        shot.estimated_seconds = Number(duration.input.value);
      });
      card.append(duration.wrap);
      card.append(
        button(
          "上移",
          () => {
            [draft[i - 1], draft[i]] = [draft[i], draft[i - 1]];
            draw();
          },
          i === 0,
        ),
        button(
          "下移",
          () => {
            [draft[i + 1], draft[i]] = [draft[i], draft[i + 1]];
            draw();
          },
          i === draft.length - 1,
        ),
        button(
          "合并到上一镜",
          () => {
            draft[i - 1].text += shot.text;
            draft[i - 1].query += `；${shot.query}`;
            draft[i - 1].estimated_seconds += shot.estimated_seconds;
            draft.splice(i, 1);
            draw();
          },
          i === 0,
        ),
        button(
          "删除",
          () => {
            draft.splice(i, 1);
            draw();
          },
          draft.length === 1,
        ),
      );
      list.append(card);
    }
  }
  body.append(
    el(
      "p",
      "保存只修改此方案；已选下游版本保留，变化后显示兼容性提示。",
      "muted",
    ),
    list,
    button("添加分镜", () => {
      draft.push({
        id: null,
        text: "新旁白",
        query: "新画面",
        estimated_seconds: 3,
      });
      draw();
    }),
    button("保存分镜", async () => {
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          shots: draft,
        }),
      );
      closeDialog();
      notice("分镜已保存。");
    }),
  );
  draw();
}
function editSettings() {
  const body = showDialog("方案参数"),
    fields = {},
    o = plan().options;
  const definitions = [
    ["voice", "声音 ID（当前配音服务）", "text"],
    ["recall_limit", "召回数量 1–50", "number"],
    [
      "audio_mode",
      "原声混音",
      "text",
      [
        ["tts_only", "仅配音"],
        ["ducking", "配音与原声"],
      ],
    ],
    ["duck_gain", "原声增益 0–1", "number"],
    [
      "burn",
      "字幕模式",
      "text",
      [
        ["true", "烧录字幕"],
        ["false", "软字幕"],
      ],
    ],
    ["atempo", "配音速度 0.75–1.25（留空自动）", "number"],
    [
      "alignment",
      "补齐方式",
      "text",
      [
        ["auto", "自动裁剪 / 变速 / 冻结"],
        ["freeze", "原速配音，裁剪或冻结"],
        ["strict", "严格时长匹配"],
      ],
    ],
  ];
  for (const [key, label, type, choices] of definitions) {
    const f = field(label, o[key], type, choices);
    fields[key] = f.input;
    body.append(f.wrap);
  }
  body.append(
    button("保存参数", async () => {
      const options = { ...o };
      for (const key of Object.keys(fields)) options[key] = fields[key].value;
      for (const key of ["recall_limit", "duck_gain"])
        options[key] = Number(options[key]);
      options.atempo =
        fields.atempo.value === "" ? null : Number(fields.atempo.value);
      options.burn = options.burn === "true";
      await accept(
        await api(planBase(), "PATCH", {
          revision: state.data.revision,
          options,
        }),
      );
      closeDialog();
      notice("参数已保存；不会自动执行。");
    }),
  );
}
async function manualRange(sid) {
  const p = plan(),
    selection = ver(p.shots[sid].select)?.data,
    candidates = ver(p.shots[sid].recall)?.data.results || [];
  const body = showDialog(`连续镜头选择 · ${sid}`);
  const initial = p.ranges[sid] || selection?.range || candidates[0] || {};
  const source = field("素材 ID", initial.source_id || "");
  const start = field("开始秒", initial.start ?? 0, "number"),
    end = field("结束秒", initial.end ?? 1, "number");
  const sources = [
    ...new Set(
      candidates
        .map((c) => c.source_id)
        .concat(initial.source_id || [])
        .filter(Boolean),
    ),
  ];
  const sourceChoices = document.createElement("datalist");
  sourceChoices.id = "source-choices";
  for (const id of sources) {
    const option = el("option");
    option.value = id;
    sourceChoices.append(option);
  }
  source.input.setAttribute("list", sourceChoices.id);
  const shotsBox = el("div", "", "source-shots"),
    result = el("div");
  let checks = [],
    preview = null;
  const confirm = button(
    "确认对齐并保存新选片版本",
    async () => {
      await accept(
        await api(`${planBase()}/range-confirm`, "POST", {
          ...request(),
          preview_token: preview.preview_token,
        }),
      );
      closeDialog();
      notice("人工范围与对齐结果已保存，分镜视频保留原版本。");
    },
    true,
  );
  function invalidate() {
    preview = null;
    confirm.disabled = true;
    result.replaceChildren();
  }
  function request() {
    return {
      revision: state.data.revision,
      shot_id: sid,
      source_id: source.input.value,
      start: Number(start.input.value),
      end: Number(end.input.value),
      ...(checks.some((c) => c.input.checked)
        ? {
            segment_ids: checks
              .filter((c) => c.input.checked)
              .map((c) => c.row.id),
          }
        : {}),
    };
  }
  for (const f of [source, start, end])
    f.input.addEventListener("input", invalidate);
  async function loadShots() {
    invalidate();
    const response = await api(
      `/api/studio/sources/${encodeURIComponent(source.input.value)}/shots`,
    );
    checks = [];
    shotsBox.replaceChildren();
    for (const row of response.items) {
      const label = el(
        "label",
        "",
        `source-shot${row.visible ? "" : " hidden-shot"}`,
      );
      const input = document.createElement("input");
      input.type = "checkbox";
      input.disabled = !row.visible;
      input.checked =
        row.visible &&
        row.start < Number(end.input.value) &&
        row.end > Number(start.input.value);
      const record = { input, row };
      checks.push(record);
      input.addEventListener("change", () => {
        invalidate();
        const chosen = checks.filter((c) => c.input.checked);
        if (chosen.length) {
          start.input.value = chosen[0].row.start;
          end.input.value = chosen.at(-1).row.end;
        }
      });
      label.append(
        input,
        el(
          "span",
          `${row.id} · ${secs(row.start)}–${secs(row.end)}${row.visible ? "" : " · 已隐藏"}`,
        ),
        button(
          "预览镜头",
          () => play(`/api/admin/segments/${row.id}/media`, row.id),
          !row.visible,
        ),
      );
      shotsBox.append(label);
    }
  }
  body.append(
    el(
      "p",
      "只允许同一素材中的连续镜头。勾选后可微调边界；预览会校验隐藏区间与时间连续性。",
      "muted",
    ),
    source.wrap,
    sourceChoices,
    button("加载素材镜头", loadShots),
    shotsBox,
    start.wrap,
    end.wrap,
    button("预览范围与对齐结果", async () => {
      preview = await api(`${planBase()}/range-preview`, "POST", request());
      result.replaceChildren(
        el(
          "p",
          `选定 ${secs(preview.range.start)}–${secs(preview.range.end)}；实际 ${secs(preview.actual_range.start)}–${secs(preview.actual_range.end)}；配音速度 ${preview.atempo.toFixed(3)}×；裁剪 ${secs(preview.trim_seconds)}；冻结 ${secs(preview.freeze_seconds)}`,
        ),
        media("video", preview.preview_url),
      );
      confirm.disabled = false;
    }),
    result,
    confirm,
  );
  if (source.input.value) await loadShots();
}
async function preflight(force = false) {
  const id = state.id,
    pid = plan().id;
  const checked = await api(`${planBase()}/preflight?force=${force}`);
  if (id !== state.id || pid !== plan().id) return;
  const body = showDialog(
    force ? "强制重新生成：确认构建" : "构建预检：确认后执行",
  );
  const actionLabel = {
    reuse: "复用",
    rebuild: "重建",
    blocked: "阻塞",
    skip: "无需执行",
  };
  for (const node of checked.nodes)
    body.append(
      el(
        "p",
        `${actionLabel[node.action]} · ${LABEL[node.stage]} ${node.shot_id || ""} · ${node.reason}${node.version ? ` · ${node.version.slice(0, 8)}` : ""}`,
        node.action === "blocked" ? "warn-text" : "",
      ),
    );
  body.append(
    el(
      "p",
      "独立节点会继续执行；必需依赖未齐时保留已有成片。重试会重新计算复用清单。",
      "muted",
    ),
    button(
      "确认构建",
      async () => {
        await api(`${planBase()}/build`, "POST", {
          token: checked.token,
          force,
        });
        closeDialog();
        await refresh();
      },
      !plan().board,
    ),
  );
}
function renderFinal() {
  const p = plan(),
    selected = ver(p.final);
  $("final-hint").textContent = selected
    ? `当前方案成片 ${selected.id.slice(0, 8)}${selected.incomplete ? " · 记录不完整" : ""}`
    : "尚未生成此方案的成片";
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
          src: selected.files.vtt.url,
        });
        video.append(track);
      }
      player.append(video);
      versionFiles(player, selected);
    }
  }
  const history = $("final-history");
  history.replaceChildren();
  const d = el("details");
  d.append(el("summary", "历史成片与完整依赖"));
  for (const item of Object.values(state.data.versions)
    .filter((v) => v.stage === "compose")
    .reverse()) {
    const row = el("div", "", "final-row");
    row.append(
      el("span", `${item.id.slice(0, 8)} · ${STATUS[item.status]}`),
      button("查看与播放", () => showVersion(item.id)),
    );
    if (item.status === "complete") {
      row.append(
        button(
          "选为方案成片",
          async () => {
            await accept(
              await api(`${planBase()}/select-version`, "POST", {
                revision: state.data.revision,
                version_id: item.id,
                stage: "compose",
              }),
            );
          },
          busy(),
        ),
      );
      if (item.data.plan)
        row.append(
          button("复制为新方案", () => createPlan("restore", item.id), busy()),
        );
      versionFiles(row, item);
    }
    d.append(row);
  }
  if (selected) d.append(dependencyTree(selected.id));
  history.append(d);
}
function renderBuilds() {
  const view = $("build-history");
  view.replaceChildren();
  for (const b of [...state.data.builds].reverse()) {
    const d = el("details");
    d.append(
      el(
        "summary",
        `构建 ${b.id} · ${state.data.plans[b.plan_id]?.name || b.plan_id} · ${STATUS[b.status]}`,
      ),
    );
    for (const node of b.nodes)
      d.append(
        el(
          "p",
          `${LABEL[node.stage]} ${node.shot_id || ""} · ${STATUS[node.status]}${node.error ? ` · ${node.error}` : ""}`,
        ),
      );
    view.append(d);
  }
}
async function loadArtifacts() {
  const id = state.id;
  if (!id) return;
  const data = await api(`${base()}/artifacts`);
  if (id !== state.id) return;
  $("download-archive").href = `${base()}/archive`;
  const view = $("artifact-view");
  view.replaceChildren();
  const groups = new Map();
  for (const item of data.items) {
    const groupName = item.path.startsWith("versions/")
      ? item.path.split("/").slice(0, 2).join("/")
      : item.attempt || "任务与旧产物";
    if (!groups.has(groupName)) {
      const d = el("details");
      d.append(el("summary", groupName));
      groups.set(groupName, d);
      view.append(d);
    }
    const row = el("div", "", "artifact-row");
    row.append(
      link(item.path, item.url, true),
      el("small", `${(item.bytes / 1024).toFixed(1)} KiB`),
    );
    groups.get(groupName).append(row);
  }
}
function play(url, title) {
  $("play-title").textContent = title;
  $("play-video").src = url;
  $("play-dialog").showModal();
}
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
$("reload-artifacts").addEventListener("click", () =>
  loadArtifacts().catch(fail),
);
$("plan-select").addEventListener("change", () =>
  api(`${base()}/plans/${$("plan-select").value}/activate`, "POST")
    .then(accept)
    .catch(fail),
);
$("new-plan").addEventListener("click", () => createPlan("new"));
$("copy-plan").addEventListener("click", () => createPlan("copy"));
$("rename-plan").addEventListener("click", renamePlan);
$("plan-settings").addEventListener("click", editSettings);
$("edit-board").addEventListener("click", () => editBoard());
$("split").addEventListener("click", () =>
  runNode("split", null, true).catch(fail),
);
$("run-all").addEventListener("click", () => preflight().catch(fail));
$("force-build").addEventListener("click", () => preflight(true).catch(fail));
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("create-button").disabled = true;
  try {
    const job = await api("/api/studio/jobs", "POST", {
      text: $("create-text").value,
      options: {
        audio_mode: $("create-audio-mode").value,
        burn: $("create-burn").value === "true",
        recall_limit: Number($("create-recall-limit").value),
      },
    });
    await selectJob(job.id);
    $("create-text").value = "";
    notice("任务已创建，请先生成分镜。");
  } catch (error) {
    fail(error);
  } finally {
    $("create-button").disabled = false;
  }
});
async function boot() {
  await loadJobs();
  const id = new URLSearchParams(location.search).get("job");
  if (id && state.jobs.some((j) => j.id === id)) await selectJob(id);
}
boot().catch(fail);
