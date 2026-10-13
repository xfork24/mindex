'use strict';

// 基础层：与 app.js/admin.js 相同的封装与防竞态模式。

const STEPS = [
  { key: 'split', label: '拆分镜', hint: 'LLM 拆分文案' },
  { key: 'recall', label: '召回', hint: '语义召回候选画面' },
  { key: 'tts', label: '配音', hint: '逐镜生成语音' },
  { key: 'select', label: '选片', hint: '时长对齐选片' },
  { key: 'render', label: '渲染', hint: '三轨合成成片' },
];
const STATUS_LABEL = { pending: '待执行', running: '执行中', complete: '已完成', failed: '失败' };

const state = { jobs: [], currentId: null, detail: null, pollTimer: null, jobToken: 0, editing: false, draft: [] };

function $(id) { return document.getElementById(id); }

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function errorText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => [item.loc ? item.loc.join('.') : '', item.msg].join('：')).join('；');
  }
  return JSON.stringify(detail);
}

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(data.detail ?? response.status));
  return data;
}

function notice(text, type) {
  const node = $('notice');
  node.textContent = text || '';
  node.dataset.type = type || 'info';
}

function formatSeconds(value) {
  return `${Number(value || 0).toFixed(1)}s`;
}

// 任务列表层。

async function loadJobs() {
  try {
    const data = await api('/api/studio/jobs');
    state.jobs = data.items;
    renderJobList();
  } catch (error) {
    notice(`读取任务失败：${error.message}`, 'error');
  }
}

function miniSteps(steps) {
  const wrap = element('span', '', 'mini-steps');
  for (const step of STEPS) {
    wrap.appendChild(element('span', '', `mini-dot ${steps[step.key] || 'pending'}`));
  }
  return wrap;
}

function renderJobList() {
  const list = $('job-list');
  list.replaceChildren();
  if (!state.jobs.length) {
    list.appendChild(element('p', '还没有创作任务。', 'muted'));
    return;
  }
  for (const job of state.jobs) {
    const item = element('button', '', `job-item${job.id === state.currentId ? ' active' : ''}`);
    item.type = 'button';
    item.appendChild(element('div', job.title, 'job-title'));
    const sub = element('div', '', 'job-sub');
    sub.appendChild(element('span', new Date(job.created_at).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' })));
    sub.appendChild(miniSteps(job.steps));
    item.appendChild(sub);
    item.addEventListener('click', () => selectJob(job.id));
    list.appendChild(item);
  }
}

async function selectJob(id) {
  state.currentId = id;
  state.editing = false;
  history.replaceState(null, '', `/studio?job=${id}`);
  renderJobList();
  await refreshJob();
}

// 轮询层：任一步骤 running 时每 1.5s 拉一次详情，空闲即停。

function schedulePoll() {
  const steps = state.detail ? state.detail.job.steps : {};
  const running = Object.values(steps).some((info) => info.status === 'running');
  if (running && !state.pollTimer) state.pollTimer = setInterval(refreshJob, 1500);
  if (!running && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    loadJobs();
  }
}

async function refreshJob() {
  if (!state.currentId) return;
  const token = ++state.jobToken;
  try {
    const detail = await api(`/api/studio/jobs/${state.currentId}`);
    if (token !== state.jobToken) return;
    state.detail = detail;
    renderJob(detail);
  } catch (error) {
    if (token !== state.jobToken) return;
    notice(`读取任务失败：${error.message}`, 'error');
  } finally {
    if (token === state.jobToken) schedulePoll();
  }
}

// 渲染层。

function renderJob(detail) {
  const { job } = detail;
  $('no-job').hidden = true;
  $('job-workspace').hidden = false;
  const header = $('job-header');
  header.replaceChildren();
  header.appendChild(element('h2', job.title));
  header.appendChild(element('p', `任务 ${job.id} · 创建于 ${new Date(job.created_at).toLocaleString('zh-CN')}`, 'muted'));
  renderSteps(job);
  $('run-all').disabled = Object.values(job.steps).some((info) => info.status === 'running');
  $('job-meta').textContent = job.config_snapshot
    ? `生成模型 ${job.config_snapshot.generator_model} · 语音 ${job.config_snapshot.tts_model}`
    : '';
  renderStoryboard(detail);
  renderCandidates(detail);
  renderTimeline(detail);
  renderResult(detail);
}

function renderSteps(job) {
  const bar = $('steps-bar');
  bar.replaceChildren();
  const busy = Object.values(job.steps).some((info) => info.status === 'running');
  for (const step of STEPS) {
    const info = job.steps[step.key] || { status: 'pending' };
    const card = element('div', '', `step-node ${info.status}`);
    card.setAttribute('role', 'listitem');
    const title = element('div', '', 'step-title');
    title.appendChild(element('span', '', `mini-dot ${info.status}`));
    title.appendChild(element('span', step.label));
    card.appendChild(title);
    card.appendChild(element('span', `${STATUS_LABEL[info.status]}${info.elapsed_ms ? ` · ${(info.elapsed_ms / 1000).toFixed(1)}s` : ''}`, 'step-status'));
    if (info.status === 'failed' && info.error) {
      card.appendChild(element('div', info.error, 'step-error'));
    }
    const action = element('button', info.status === 'complete' ? '重跑' : '执行', 'quiet');
    action.type = 'button';
    action.disabled = busy || info.status === 'running' || !readyFor(step.key, job);
    action.title = action.disabled && !busy ? '上游步骤未完成' : step.hint;
    action.addEventListener('click', () => runStep(step.key));
    card.appendChild(action);
    bar.appendChild(card);
  }
}

function readyFor(step, job) {
  const deps = { split: [], recall: ['split'], tts: ['split'], select: ['recall', 'tts'], render: ['select'] }[step] || [];
  return deps.every((dep) => job.steps[dep] && job.steps[dep].status === 'complete');
}

async function runStep(step) {
  notice(`正在提交步骤 ${step}…`);
  try {
    await api(`/api/studio/jobs/${state.currentId}/steps/${step}`, { method: 'POST' });
    notice(`步骤 ${step} 已开始执行。`);
    await refreshJob();
  } catch (error) {
    notice(`无法执行：${error.message}`, 'error');
  }
}

// 分镜层：查看与编辑两种模式，编辑时保存前提示级联作废。

function renderStoryboard(detail) {
  const section = $('storyboard-section');
  const storyboard = detail.storyboard;
  section.hidden = !storyboard;
  if (!storyboard) return;
  $('storyboard-warning').textContent = storyboard.coverage_warning || '';
  $('toggle-edit').hidden = state.editing || detail.job.steps.split.status !== 'complete';
  $('save-storyboard').hidden = !state.editing;
  const view = $('storyboard-view');
  view.replaceChildren();
  const shots = state.editing ? state.draft : storyboard.shots;
  shots.forEach((shot, index) => {
    view.appendChild(shotCard(shot, index, detail));
  });
  if (!shots.length) view.appendChild(element('p', '没有分镜。', 'muted'));
}

function shotCard(shot, index, detail) {
  const card = element('div', '', 'shot-card');
  const head = element('div', '', 'shot-head');
  head.appendChild(element('strong', `分镜 ${index + 1} · ${shot.id}`));
  if (detail.shots[index] && detail.shots[index].audio) {
    head.appendChild(element('span', `配音 ${formatSeconds(detail.shots[index].audio.duration)}`));
  }
  head.appendChild(element('span', `预估 ${formatSeconds(shot.estimated_seconds)}`));
  card.appendChild(head);
  const grid = element('div', '', 'shot-grid');
  const textLabel = element('label', '分镜文本（配音与字幕）');
  const textInput = document.createElement('textarea');
  textInput.rows = 2;
  textInput.maxLength = 200;
  textInput.value = shot.text;
  textInput.readOnly = !state.editing;
  if (state.editing) textInput.addEventListener('input', () => { state.draft[index].text = textInput.value; });
  textLabel.appendChild(textInput);
  const queryLabel = element('label', '检索查询（用于召回画面）');
  const queryInput = document.createElement('textarea');
  queryInput.rows = 2;
  queryInput.maxLength = 400;
  queryInput.value = shot.query;
  queryInput.readOnly = !state.editing;
  if (state.editing) queryInput.addEventListener('input', () => { state.draft[index].query = queryInput.value; });
  queryLabel.appendChild(queryInput);
  grid.appendChild(textLabel);
  grid.appendChild(queryLabel);
  card.appendChild(grid);
  if (state.editing) card.appendChild(shotActions(index, detail));
  return card;
}

function shotActions(index, detail) {
  const meta = element('div', '', 'shot-meta');
  const actions = element('div', '', 'shot-actions');
  const buttons = [
    ['↑ 上移', () => moveShot(index, -1), index > 0],
    ['↓ 下移', () => moveShot(index, 1), index < state.draft.length - 1],
    ['⇤ 合并到上一镜', () => mergeShot(index), index > 0],
    ['＋ 其后插入', () => insertShot(index), true],
    ['✕ 删除', () => state.draft.splice(index, 1), state.draft.length > 1],
  ];
  for (const [label, action, enabled] of buttons) {
    const button = element('button', label, 'quiet');
    button.type = 'button';
    button.disabled = !enabled;
    button.addEventListener('click', () => {
      action();
      renderStoryboard(state.detail);
    });
    actions.appendChild(button);
  }
  meta.appendChild(actions);
  return meta;
}

function moveShot(index, delta) {
  const [shot] = state.draft.splice(index, 1);
  state.draft.splice(index + delta, 0, shot);
}

function mergeShot(index) {
  const previous = state.draft[index - 1];
  const current = state.draft[index];
  previous.text += current.text;
  previous.query = `${previous.query}；${current.query}`;
  previous.estimated_seconds += current.estimated_seconds;
  state.draft.splice(index, 1);
}

function insertShot(index) {
  state.draft.splice(index + 1, 0, {
    id: `shot-new-${Math.random().toString(36).slice(2, 6)}`,
    text: '新分镜文本',
    query: '新分镜的画面查询',
    estimated_seconds: 5,
  });
}

async function saveStoryboard() {
  if (!confirm('保存分镜将作废召回及之后的所有产物，需要重新执行。继续？')) return;
  try {
    const shots = state.draft.map((shot, index) => ({
      id: shot.id,
      text: (shot.text || '').trim(),
      query: (shot.query || '').trim(),
      estimated_seconds: Number(shot.estimated_seconds) || (shot.text.length / 4).toFixed(1),
    }));
    if (shots.some((shot) => !shot.text || !shot.query)) throw new Error('分镜文本与查询不能为空');
    await api(`/api/studio/jobs/${state.currentId}/storyboard`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shots }),
    });
    state.editing = false;
    notice('分镜已保存，召回及之后的步骤已重置。');
    await refreshJob();
  } catch (error) {
    notice(`保存失败：${error.message}`, 'error');
  }
}

// 候选层：缩略图条 + 对话框播放（复用媒体端点）。

function renderCandidates(detail) {
  const section = $('candidates-section');
  const recallDone = ['complete', 'failed'].includes(detail.job.steps.recall.status);
  section.hidden = !recallDone || !detail.shots.length;
  if (section.hidden) return;
  const view = $('candidates-view');
  view.replaceChildren();
  for (const shot of detail.shots) {
    const row = element('div', '', 'candidate-row');
    const head = element('div', '', 'shot-head');
    head.appendChild(element('strong', `分镜 ${shot.id}`));
    const audioText = shot.audio ? `配音 ${formatSeconds(shot.audio.duration)}` : '配音未生成';
    head.appendChild(element('span', audioText));
    const badge = element('span', shot.candidates ? shot.candidates.status : '未召回', 'badge');
    head.appendChild(badge);
    row.appendChild(head);
    const strip = element('div', '', 'candidate-strip');
    const timelineEvent = detail.timeline && detail.timeline.events.find((event) => event.shot_id === shot.id);
    const selectedId = timelineEvent ? timelineEvent.video.segment_id : null;
    if (shot.candidates && shot.candidates.count) {
      const pool = shot.candidates.pool || [];
      const items = pool.length ? pool : [shot.candidates.top];
      for (const item of items) {
        strip.appendChild(candidateThumb(item, selectedId));
      }
      if (shot.candidates.count > items.length) {
        strip.appendChild(element('span', `还有 ${shot.candidates.count - items.length} 个候选`, 'muted'));
      }
    } else {
      strip.appendChild(element('span', '该分镜没有召回结果，请修改查询后重跑召回。', 'muted'));
    }
    row.appendChild(strip);
    view.appendChild(row);
  }
  // 拉全量候选池补充缩略图条（详情接口只带 top）。
  if (!state.candidatePools) state.candidatePools = {};
  for (const shot of detail.shots) {
    if (!(shot.id in state.candidatePools)) {
      state.candidatePools[shot.id] = null;
      api(`/api/studio/jobs/${state.currentId}/artifacts/candidates/${shot.id}.json`)
        .then((pool) => {
          state.candidatePools[shot.id] = pool;
          const index = state.detail.shots.findIndex((item) => item.id === shot.id);
          if (index >= 0) state.detail.shots[index].candidates.pool = pool.results || [];
          if (state.detail && !state.pollTimer) renderCandidates(state.detail);
        })
        .catch(() => {});
    } else if (shot.candidates && state.candidatePools[shot.id]) {
      shot.candidates.pool = state.candidatePools[shot.id].results || [];
    }
  }
}

function candidateThumb(item, selectedId) {
  const button = element('button', '', `candidate-thumb${item.segment_id === selectedId ? ' selected' : ''}`);
  button.type = 'button';
  button.title = `${item.source_name || item.source_id} ${item.start.toFixed(1)}–${item.end.toFixed(1)}s · 召回第 ${item.rank} 名`;
  const image = document.createElement('img');
  image.src = item.thumbnail_url;
  image.alt = item.source_name || item.segment_id;
  image.loading = 'lazy';
  button.appendChild(image);
  const meta = element('div', '', 'thumb-meta');
  meta.appendChild(element('div', `${formatSeconds(item.duration)} · 第${item.rank}名`));
  meta.appendChild(element('div', item.source_name || '', ''));
  button.appendChild(meta);
  button.addEventListener('click', () => playMedia(item.media_url, `${item.source_name || ''} ${item.start.toFixed(1)}–${item.end.toFixed(1)}s`));
  return button;
}

function playMedia(url, title) {
  $('play-title').textContent = title || '';
  $('play-meta').textContent = '';
  $('play-video').src = url;
  $('play-dialog').showModal();
}

// 选片层：展示事件 + 换选（限候选池）后整体保存。

function renderTimeline(detail) {
  const section = $('timeline-section');
  const show = detail.timeline && detail.timeline.events.length;
  section.hidden = !show;
  if (!show) return;
  const view = $('timeline-view');
  view.replaceChildren();
  const alignLabel = {
    nearest: '直接匹配', extended: '相邻延长', atempo: '微变速', 'extended+atempo': '延长+变速',
    padded: '冻结帧补齐', 'extended+padded': '延长+冻结帧', trimmed: '裁剪视频', 'extended+trimmed': '延长+裁剪', manual: '人工指定',
  };
  for (const event of detail.timeline.events) {
    const card = element('div', '', 'timeline-event');
    const head = element('div', '', 'shot-head');
    head.appendChild(element('strong', `分镜 ${event.shot_id}`));
    const badgeClass = event.warning ? 'badge warn' : 'badge good';
    head.appendChild(element('span', alignLabel[event.video.alignment] || event.video.alignment, badgeClass));
    if (Math.abs(event.audio.atempo - 1) > 0.001) {
      head.appendChild(element('span', `变速 ${(event.audio.atempo * 100).toFixed(0)}%`, 'badge'));
    }
    card.appendChild(head);
    card.appendChild(element('div', `画面：${event.video.source_name || event.video.source_id} · 原片 ${event.video.start.toFixed(1)}–${event.video.end.toFixed(1)}s（跨 ${event.video.segment_ids.length} 个镜头）`));
    card.appendChild(element('div', `配音 ${formatSeconds(event.audio.duration)} → 成片时长 ${formatSeconds(event.audio.render_duration)}`));
    if (event.warning) card.appendChild(element('div', event.warning, 'warn-text'));
    const pick = element('div', '', 'line');
    const label = element('label', '换选画面 ');
    const select = document.createElement('select');
    select.dataset.shotId = event.shot_id;
    const pool = (state.candidatePools && state.candidatePools[event.shot_id] && state.candidatePools[event.shot_id].results) || [];
    const options = pool.length ? pool : [{ segment_id: event.video.segment_id, source_name: event.video.source_name, rank: event.video.rank, start: event.video.start, end: event.video.end, duration: event.video.end - event.video.start }];
    for (const item of options) {
      const option = document.createElement('option');
      option.value = item.segment_id;
      option.textContent = `第${item.rank}名 · ${item.source_name || item.source_id} ${formatSeconds(item.duration)}`;
      option.selected = item.segment_id === event.video.segment_id;
      select.appendChild(option);
    }
    label.appendChild(select);
    pick.appendChild(label);
    card.appendChild(pick);
    view.appendChild(card);
  }
  $('save-timeline').hidden = false;
}

async function saveTimeline() {
  if (!confirm('保存换选将作废渲染产物，需要重新渲染。继续？')) return;
  const selects = $('timeline-view').querySelectorAll('select[data-shot-id]');
  const events = [...selects].map((select) => ({ shot_id: select.dataset.shotId, video: { segment_id: select.value } }));
  try {
    await api(`/api/studio/jobs/${state.currentId}/timeline`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events }),
    });
    notice('换选已保存，渲染步骤已重置。');
    await refreshJob();
  } catch (error) {
    notice(`保存失败：${error.message}`, 'error');
  }
}

// 成片区。

function renderResult(detail) {
  const section = $('result-section');
  section.hidden = !detail.render || !detail.render.ready;
  if (section.hidden) return;
  $('final-video').src = detail.render.final_url;
  $('download-mp4').href = `${detail.render.final_url}?download=true`;
  $('download-srt').href = `${detail.render.srt_url}?download=true`;
}

// 事件绑定与启动。

function bindEvents() {
  $('reload').addEventListener('click', loadJobs);
  $('create-form').addEventListener('submit', createJob);
  $('run-all').addEventListener('click', runAll);
  $('toggle-edit').addEventListener('click', () => {
    state.editing = true;
    state.draft = JSON.parse(JSON.stringify(state.detail.storyboard.shots));
    renderStoryboard(state.detail);
  });
  $('save-storyboard').addEventListener('click', saveStoryboard);
  $('save-timeline').addEventListener('click', saveTimeline);
  $('play-close').addEventListener('click', () => {
    $('play-video').pause();
    $('play-dialog').close();
  });
}

async function createJob(event) {
  event.preventDefault();
  const text = $('create-text').value.trim();
  if (!text) {
    notice('请输入创作文案。', 'error');
    return;
  }
  $('create-button').disabled = true;
  try {
    const job = await api('/api/studio/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        options: {
          audio_mode: $('create-audio-mode').value,
          burn: $('create-burn').value === 'true',
          recall_limit: Number($('create-recall-limit').value),
        },
      }),
    });
    notice('任务已创建，先执行"拆分镜"或点"全自动连跑"。');
    $('create-text').value = '';
    state.candidatePools = {};
    await loadJobs();
    await selectJob(job.id);
  } catch (error) {
    notice(`创建失败：${error.message}`, 'error');
  } finally {
    $('create-button').disabled = false;
  }
}

async function runAll() {
  try {
    await api(`/api/studio/jobs/${state.currentId}/run`, { method: 'POST' });
    notice('全自动连跑已启动，遇失败会停下。');
    await refreshJob();
  } catch (error) {
    notice(`无法连跑：${error.message}`, 'error');
  }
}

async function boot() {
  bindEvents();
  await loadJobs();
  const params = new URLSearchParams(location.search);
  const job = params.get('job');
  if (job && state.jobs.some((item) => item.id === job)) await selectJob(job);
}

boot();
