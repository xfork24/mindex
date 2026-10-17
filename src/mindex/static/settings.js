'use strict';
const $ = id => document.getElementById(id);
const EXPANDED_KEY = 'mindex.settings.roles-expanded';
const state = {data: null, tests: {}, expanded: loadExpanded()};
function loadExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]')); }
  catch { return new Set(); }
}
function toggleRole(role) {
  if (state.expanded.has(role)) state.expanded.delete(role); else state.expanded.add(role);
  try { localStorage.setItem(EXPANDED_KEY, JSON.stringify([...state.expanded])); } catch {}
  if (state.data) renderRoles(state.data);
  syncSectionRows();
}
function syncSectionRows() {
  for (const head of document.querySelectorAll('.role-row-head[data-section]')) {
    const key = head.dataset.section;
    const open = state.expanded.has(key);
    head.closest('.role-row').classList.toggle('is-open', open);
    head.setAttribute('aria-expanded', String(open));
    $(`${key}-body`).hidden = !open;
  }
}
let settingsRequest = 0;

const ROLE_ORDER = ['analyzer', 'generator', 'embedding', 'asr', 'tts', 'person'];
const ROLE_DESC = {
  person: '独立人脸和人体特征服务；按已有分镜处理，已发布人物检索不依赖服务在线',
  analyzer: '视觉帧分析（动作、情绪、场景描述），需要多模态视觉模型',
  generator: '创作文案拆分镜，纯文本任务，可选高性价比文本模型',
  embedding: '文本 / 图片 / 视频统一向量空间；切换模型后需重建索引',
  asr: '语音转文字；MiniMax 直接使用服务词级时间戳，旧纯文本流程可手动对齐',
  tts: '创作配音；支持预定义音色与克隆音色',
};
const ROLE_BACKENDS = {
  person: ['mindex'],
  analyzer: ['openai', 'llama_cpp'],
  generator: ['openai'],
  embedding: ['mindex', 'llama_cpp', 'tokenhub'],
  asr: ['mindex', 'llama_cpp', 'openai', 'minimax'],
  tts: ['openai', 'minimax', 'dashscope'],
};
const ROLE_EXTRA = {
  person: [
    {key: 'expected_space', label: '固定模型身份（粘贴 health.space JSON）', type: 'json'},
    {key: 'fps', label: '每秒最多分析帧数（≤5）', type: 'number', min: 0.1, max: 5},
    {key: 'face_threshold', label: '人脸阈值', type: 'number', min: 0.01},
    {key: 'face_margin', label: '不同人物分数差', type: 'number', min: 0.01},
    {key: 'min_face_quality', label: '帧人脸质量门槛（低于此值不自动确认）', type: 'number', min: 0, max: 1},
    {key: 'min_reference_quality', label: '参考图质量门槛（0=全部参与匹配）', type: 'number', min: 0, max: 1},
    {key: 'body_threshold', label: '人体外观相似阈值', type: 'number', min: 0.01},
  ],
  embedding: [
    {key: 'dimensions', label: '向量维度', type: 'number', min: 1},
    {key: 'quantization', label: '量化标识', type: 'text', placeholder: '线上模型填 cloud'},
  ],
  generator: [{key: 'max_tokens', label: 'max_tokens', type: 'number', min: 100}],
  tts: [
    {key: 'response_format', label: '音频格式', type: 'select', options: ['wav', 'mp3']},
    {key: 'voice', label: '默认音色 ID', type: 'text', placeholder: '如 male-qn-qingse 或克隆音色 ID'},
  ],
  analyzer: [],
  asr: [],
};
const WEIGHT_LABELS = {
  video: '画面向量', description: '动作场景', style: '情绪镜头', dialogue: '对白', lexical: '词法 BM25',
};

function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}
function voiceLabelOf(catalog, voiceId) {
  for (const group of catalog.groups || []) {
    const hit = (group.voices || []).find(voice => voice.voice_id === voiceId);
    if (hit) return hit.label;
  }
  const mine = (catalog.library || []).find(voice => voice.voice_id === voiceId);
  return mine ? `${mine.kind === 'clone' ? '克隆' : '已录入'} · ${mine.name}` : voiceId;
}

function notify(text = '', error = false) {
  $('notice').textContent = text;
  $('notice').className = `notice ${error ? 'error' : 'info'}`;
}
function errorText(detail) {
  return Array.isArray(detail) ? detail.map(e => e.msg).join('；')
    : typeof detail === 'string' ? detail : JSON.stringify(detail);
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(data.detail ?? response.status));
  return data;
}
async function run(task) {
  try { await task(); } catch (e) { notify(e.message, true); }
}

function renderIndexBanner(status) {
  const banner = $('index-banner');
  banner.replaceChildren();
  if (status.active_index_id === null) {
    banner.append(node('div', '尚未发布向量索引；检索功能暂不可用。可先在命令行执行 mindex index --activate。', 'index-banner-muted'));
    return;
  }
  if (status.matches_index) {
    banner.append(node('div', `当前嵌入身份与已激活索引一致（${status.active_index_id.slice(0, 10)}…），检索可用。`, 'index-banner-ok'));
  } else {
    const box = node('div', undefined, 'index-banner-error');
    box.append(node('div', '当前嵌入服务未就绪或与活动索引不匹配；新版文字检索可使用已有词法索引降级，图片/视频检索需恢复对应模型。'));
    box.append(node('div', `已激活索引：${status.active_index_id}。请运行 mindex index --activate 用当前嵌入重建并发布新索引，或在切换前回到原模型配置。${status.error ? `（身份探测失败：${status.error}）` : ''}`));
    banner.append(box);
  }
}

function testLine(profileId) {
  if (!(profileId in state.tests)) return null;
  const result = state.tests[profileId];
  const line = node('div', undefined, `test-result ${result.ok ? 'ok' : 'error'}`);
  if (result.ok) {
    const bits = [`✓ 连接正常 · ${result.latency_ms}ms`];
    if (result.dims) bits.push(`${result.dims} 维`);
    if (result.reply) bits.push(`「${result.reply}」`);
    if (result.bytes) bits.push(`${result.bytes} 字节音频`);
    line.textContent = bits.join(' · ');
  } else {
    line.textContent = `✗ ${result.detail}`;
  }
  return line;
}

function action(text, onClick) {
  const button = node('button', text, 'quiet');
  button.type = 'button';
  button.onclick = () => run(async () => { button.disabled = true; try { await onClick(); } finally { button.disabled = false; } });
  return button;
}

function renderRole(rolePayload) {
  const role = rolePayload.role;
  const open = state.expanded.has(role);

  const row = node('article', undefined, `role-row${open ? ' is-open' : ''}`);
  const head = node('button', undefined, 'role-row-head');
  head.type = 'button';
  head.setAttribute('aria-expanded', String(open));
  head.setAttribute('aria-controls', `role-body-${role}`);
  head.onclick = () => toggleRole(role);

  const effective = rolePayload.effective;
  const keyState = effective.api_key.has_key ? `密钥 ${effective.api_key.hint}`
    : effective.env_key_set ? `环境变量密钥` : '未配置密钥';
  head.append(node('span', '▸', 'chevron'));
  head.append(node('strong', rolePayload.label, 'role-row-title'));
  const summary = node('span', undefined, 'role-row-summary');
  summary.append(node('span', `当前生效：${effective.model} · ${effective.backend} · ${effective.base_url}`, 'role-row-effective'));
  summary.append(node('span', `${keyState}${effective.dimensions ? ` · ${effective.dimensions} 维` : ''}`, 'role-row-key'));
  head.append(summary);
  row.append(head);

  const body = node('div', undefined, 'role-row-body');
  body.id = `role-body-${role}`;
  body.hidden = !open;
  body.append(node('p', `${ROLE_DESC[role] || ''}（共 ${rolePayload.profiles.length} 个档案）`, 'role-desc'));

  const scroller = node('div', undefined, 'profile-scroller');
  if (!rolePayload.profiles.length) {
    scroller.append(node('div', '还没有档案；当前直接使用本地配置文件的取值。', 'profile-item empty'));
  }
  rolePayload.profiles.forEach(profile => {
    const item = node('div', undefined, `profile-item ${rolePayload.active_profile_id === profile.id ? 'is-active' : ''}`);
    const head2 = node('div', undefined, 'profile-head');
    head2.append(node('strong', profile.name));
    if (rolePayload.active_profile_id === profile.id) head2.append(node('span', '使用中', 'active-badge'));
    head2.append(node('span', profile.provider, 'provider-chip'));
    item.append(head2);
    item.append(node('div', `${profile.config.model || ''} · ${profile.config.backend || ''} · ${profile.config.base_url || ''}`, 'profile-meta'));
    const keyHint = profile.api_key.has_key ? `密钥 ${profile.api_key.hint}` : '无密钥';
    item.append(node('div', keyHint, 'profile-meta'));
    const bar = node('div', undefined, 'profile-actions');
    if (rolePayload.active_profile_id !== profile.id) bar.append(action('激活', () => activateProfile(profile.id)));
    bar.append(action('测试连接', () => testProfile(profile.id)));
    bar.append(action('编辑', () => openDialog(role, profile)));
    bar.append(action('删除', () => deleteProfile(profile)));
    item.append(bar);
    const line = testLine(profile.id);
    if (line) item.append(line);
    scroller.append(item);
  });
  const create = node('button', '+ 新建档案', 'new-profile-card');
  create.type = 'button';
  create.onclick = () => openDialog(role, null);
  scroller.append(create);

  body.append(scroller);
  row.append(body);
  return row;
}

function renderRoles(data) {
  const byRole = Object.fromEntries(data.roles.map(r => [r.role, r]));
  $('roles').replaceChildren(...ROLE_ORDER.map(role => renderRole(byRole[role])));
}

function renderTuning(data) {
  const weights = data.business.search.weights;
  const grid = $('weight-grid');
  grid.replaceChildren();
  for (const [key, label] of Object.entries(WEIGHT_LABELS)) {
    const wrap = node('label');
    wrap.append(node('span', label));
    const input = node('input');
    input.id = `weight-${key}`;
    input.type = 'number';
    input.min = '0';
    input.step = '0.1';
    input.value = weights[key];
    wrap.append(input);
    grid.append(wrap);
  }
  for (const [key, label] of Object.entries({visual:'视频画面', dialogue:'对白文本', analysis:'LLM 分析'})) {
    const wrap = node('label'); wrap.append(node('span', label));
    const input = node('input'); input.id = `channel-weight-${key}`;
    input.type = 'number'; input.min = '0'; input.step = '0.1';
    input.value = data.business.search.channel_weights[key];
    wrap.append(input); grid.append(wrap);
  }
  $('rrf-k').value = data.business.search.rrf_k;
  $('candidates').value = data.business.search.candidates;
  const summary = $('tuning-summary');
  summary.textContent = `三渠道 ${['visual', 'dialogue', 'analysis'].map(k => data.business.search.channel_weights[k]).join('/')} · 旧版 ${['video', 'description', 'style', 'dialogue', 'lexical'].map(k => weights[k]).join('/')}`
    + ` · RRF k=${data.business.search.rrf_k} · 候选 ${data.business.search.candidates}`;
  summary.title = '新版：画面 / 对白 / 分析；旧版：画面 / 动作场景 / 情绪镜头 / 对白 / 词法 BM25';
}

// --- 音色库 ---
const VOICE_PROVIDER_LABELS = {self: '自建服务', openai: 'OpenAI 协议', minimax: 'MiniMax', dashscope: '阿里'};

function renderVoices(data, catalog = null) {
  const voices = data.voices || [];
  const active = voices.find(voice => voice.voice_id === data.business.default_voice);
  const defaultLabel = catalog
    ? (catalog.default_voice && voiceLabelOf(catalog, catalog.default_voice)) || active?.name || '跟随配置文件默认'
    : active ? active.name : '跟随配置文件默认';
  $('voices-summary').textContent = `${voices.length} 个音色 · 默认：${defaultLabel === '跟随配置文件默认' ? defaultLabel : defaultLabel}`;
  const select = $('default-voice');
  select.replaceChildren();
  const follow = node('option', '跟随配置文件默认');
  follow.value = '';
  select.append(follow);
  if (catalog) {
    for (const group of catalog.groups || []) {
      if (!group.voices.length) continue;
      const optgroup = document.createElement('optgroup');
      optgroup.label = group.label;
      group.voices.forEach(voice => {
        const option = node('option', voice.label);
        option.value = voice.voice_id;
        if (catalog.default_voice === voice.voice_id) option.selected = true;
        optgroup.append(option);
      });
      select.append(optgroup);
    }
    if ((catalog.library || []).length) {
      const mine = document.createElement('optgroup');
      mine.label = '我的音色';
      catalog.library.forEach(voice => {
        const option = node('option', `${voice.kind === 'clone' ? '克隆' : '已录入'} · ${voice.name}`);
        option.value = voice.voice_id;
        if (catalog.default_voice === voice.voice_id) option.selected = true;
        mine.append(option);
      });
      select.append(mine);
    }
    if (catalog.default_voice && ![...select.options].some(option => option.selected)) {
      const fallback = node('option', `${catalog.default_voice}（不在当前目录）`);
      fallback.value = catalog.default_voice;
      fallback.selected = true;
      select.append(fallback);
    }
  } else {
    voices.forEach(voice => {
      const option = node('option', `${voice.kind === 'clone' ? '克隆' : '预定义'} · ${voice.name}`);
      option.value = voice.voice_id;
      if (data.business.default_voice === voice.voice_id) option.selected = true;
      select.append(option);
    });
  }
  const clone = catalog?.clone;
  $('voice-backend-hint').textContent = catalog?.error
    ? `${catalog.error}（下拉为静态目录兜底）`
    : clone?.supported
      ? clone.mode === 'url' ? '当前阿里后端：可用样本 URL 复刻'
        : clone.ref_text ? '当前本地 Qwen3-TTS：可上传样本克隆；服务重启后自动恢复'
        : '当前 MiniMax 后端：可上传样本克隆'
      : '当前后端仅支持录入音色 ID';

  const list = $('voice-list');
  list.replaceChildren();
  if (!voices.length) list.append(node('li', '还没有音色；录入已有音色 ID，或激活 MiniMax / 阿里档案后克隆。', 'muted'));
  voices.forEach(voice => {
    const item = node('li', undefined, 'voice-item');
    const head = node('div', undefined, 'profile-head');
    head.append(node('strong', voice.name));
    if (data.business.default_voice === voice.voice_id) head.append(node('span', '默认', 'active-badge'));
    head.append(node('span', `${voice.kind === 'clone' ? '克隆' : '预定义'} · ${VOICE_PROVIDER_LABELS[voice.provider] || voice.provider}`, 'provider-chip'));
    if (voice.status !== 'ready') head.append(node('span', voice.status === 'failed' ? '失败' : '进行中', 'status-badge'));
    item.append(head);
    item.append(node('div', `${voice.voice_id}${voice.sample_present ? ' · 已存样本' : ''}`, 'profile-meta'));
    if (voice.status === 'failed' && voice.notes) item.append(node('div', voice.notes, 'test-result error'));
    const bar = node('div', undefined, 'profile-actions');
    if (data.business.default_voice !== voice.voice_id) bar.append(action('设为默认', () => setDefaultVoice(voice.id)));
    bar.append(action('删除', () => deleteVoice(voice)));
    item.append(bar);
    list.append(item);
  });
}

async function setDefaultVoice(voiceId) {
  await api('/api/settings/business', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({default_voice_id: voiceId}),
  });
  notify('默认音色已更新，新创作任务即生效。');
  await loadSettings();
}
async function deleteVoice(voice) {
  if (!confirm(`删除音色「${voice.name}」？`)) return;
  await api(`/api/settings/voices/${voice.id}`, {method: 'DELETE'});
  notify('音色已删除。');
  await loadSettings();
}
$('default-voice').onchange = () => run(async () => {
  const value = $('default-voice').value;
  await api('/api/settings/business', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(value ? {default_voice: value} : {clear_default_voice: true}),
  });
  notify(value ? '默认音色已更新，新创作任务即生效。' : '已恢复配置文件默认音色。');
  await loadSettings();
});

// --- 添加音色对话框（三模式）---
let cloneMode = 'upload';
function syncCloneMode() {
  for (const button of document.querySelectorAll('.clone-mode')) {
    const active = button.dataset.mode === cloneMode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  }
  $('clone-upload-field').hidden = cloneMode !== 'upload';
  $('clone-url-field').hidden = cloneMode !== 'url';
  $('clone-register-fields').hidden = cloneMode !== 'register';
  $('clone-consent-wrap').hidden = cloneMode === 'register';
  $('clone-reftext-field').hidden = !(cloneMode === 'upload' && state.voiceCatalog?.clone?.ref_text);
  $('clone-mode-note').textContent = {
    upload: state.voiceCatalog?.clone?.ref_text
      ? '样本注册到本地 Qwen3-TTS（3 秒以上清晰人声）；填参考文本可提升克隆效果。服务重启后打开音色库会自动恢复。'
      : '上传样本到厂商完成克隆并生成预览；需要当前语音合成为 MiniMax 后端。',
    url: '阿里声音复刻要求样本是公网可访问 URL（wav/mp3/m4a，10~20 秒）。',
    register: '直接录入平台已生成的音色 ID，含本地服务（Qwen3-TTS 等）的预定义音色。',
  }[cloneMode];
}
function openCloneDialog(mode) {
  cloneMode = mode;
  syncCloneMode();
  $('clone-name').value = '';
  $('clone-file').value = '';
  $('clone-url').value = '';
  $('clone-voice-id').value = '';
  $('clone-reftext').value = '';
  $('clone-consent').checked = false;
  $('clone-error').textContent = '';
  $('clone-dialog').showModal();
}
for (const button of document.querySelectorAll('.clone-mode')) {
  button.onclick = () => { cloneMode = button.dataset.mode; syncCloneMode(); };
}
$('voice-clone').onclick = () => {
  const backend = state.data?.business?.tts_backend;
  const clone = state.voiceCatalog?.clone;
  openCloneDialog(
    backend === 'dashscope' ? 'url'
    : clone?.supported ? 'upload'
    : 'register',
  );
};
$('voice-register').onclick = () => openCloneDialog('register');
$('clone-close').onclick = () => $('clone-dialog').close();
$('clone-form').onsubmit = async event => {
  event.preventDefault();
  const name = $('clone-name').value.trim();
  $('clone-error').textContent = '';
  $('clone-save').disabled = true;
  try {
    if (cloneMode === 'register') {
      await api('/api/settings/voices', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          name, provider: $('clone-provider').value,
          voice_id: $('clone-voice-id').value.trim(), kind: 'preset',
        }),
      });
    } else {
      const form = new FormData();
      form.append('name', name);
      form.append('consent', $('clone-consent').checked ? 'true' : 'false');
      if (cloneMode === 'upload') {
        if (!$('clone-file').files.length) throw new Error('请选择声音样本文件');
        form.append('ref_text', $('clone-reftext').value.trim());
        form.append('file', $('clone-file').files[0]);
      } else {
        const url = $('clone-url').value.trim();
        if (!url) throw new Error('请填写样本公网 URL');
        form.append('audio_url', url);
      }
      await api('/api/settings/voices/clone', {method: 'POST', body: form});
    }
    $('clone-dialog').close();
    notify('音色已保存。');
    await loadSettings();
  } catch (e) {
    $('clone-error').textContent = e.message;
  } finally {
    $('clone-save').disabled = false;
  }
};

async function loadSettings() {
  const request = ++settingsRequest;
  const data = await api('/api/settings');
  if (request !== settingsRequest) return;
  state.data = data;
  renderIndexBanner(data.embedding_status);
  renderRoles(data);
  renderTuning(data);
  renderVoices(data, state.voiceCatalog);
  // An unavailable TTS catalog must not block model settings or recall tuning.
  api('/api/settings/voices/catalog').then(catalog => {
    if (request !== settingsRequest) return;
    state.voiceCatalog = catalog;
    renderVoices(data, catalog);
  }).catch(() => {});
}

async function activateProfile(profileId) {
  const result = await api(`/api/settings/profiles/${profileId}/activate`, {method: 'POST'});
  if (result.index_warning) notify(result.index_warning.message, true);
  else notify(`已激活「${result.name}」，立即生效。`);
  await loadSettings();
}

async function testProfile(profileId) {
  state.tests[profileId] = {ok: true, latency_ms: '…'};
  const result = await api(`/api/settings/profiles/${profileId}/test`, {method: 'POST'});
  state.tests[profileId] = result;
  await loadSettings();
}

async function deleteProfile(profile) {
  if (!confirm(`删除档案「${profile.name}」？此操作不可恢复。`)) return;
  await api(`/api/settings/profiles/${profile.id}`, {method: 'DELETE'});
  notify('档案已删除。');
  await loadSettings();
}

function fillBackendOptions(role, selected) {
  const select = $('pf-backend');
  select.replaceChildren(...ROLE_BACKENDS[role].map(value => {
    const option = node('option', value);
    option.value = value;
    return option;
  }));
  if (selected) select.value = selected;
}
function fillExtraFields(role, values = {}) {
  const wrap = $('pf-extra');
  wrap.replaceChildren();
  ROLE_EXTRA[role].forEach(field => {
    const label = node('label');
    label.append(node('span', field.label));
    let input;
    if (field.type === 'select') {
      input = node('select');
      field.options.forEach(value => {
        const option = node('option', value);
        option.value = value;
        input.append(option);
      });
    } else {
      input = node('input');
      input.type = field.type === 'json' ? 'text' : field.type;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      if (field.type === 'number') input.step = 'any';
      if (field.placeholder) input.placeholder = field.placeholder;
    }
    input.id = `pf-${field.key}`;
    if (values[field.key] !== undefined && values[field.key] !== null) input.value = field.type === 'json' ? JSON.stringify(values[field.key]) : values[field.key];
    label.append(input);
    wrap.append(label);
  });
}
function applyPreset(role, provider) {
  const preset = state.data?.presets?.[provider]?.[role];
  if (!preset) return;
  const config = preset.config;
  if (config.backend) fillBackendOptions(role, config.backend);
  $('pf-base-url').value = config.base_url || '';
  $('pf-model').value = config.model || '';
  $('pf-revision').value = config.revision || '';
  fillExtraFields(role, config);
  const templateWrap = $('pf-template-wrap');
  if (!templateWrap.hidden) $('pf-llama-template').checked = Boolean(config.llama_template);
  refreshVoiceCandidates();
}
for (const id of ['pf-backend', 'pf-base-url', 'pf-model', 'pf-name', 'pf-api-key']) {
  $(id).addEventListener('change', () => refreshVoiceCandidates());
}

function ensureVoiceField(tag) {
  // 返回 {element, previous}：空 select 直接赋 value 会因无匹配选项而丢失，必须先取原值。
  const current = $('pf-voice');
  if (!current) return {element: null, previous: ''};
  const previous = current.value;
  if (current.tagName.toLowerCase() === tag) return {element: current, previous};
  const replacement = document.createElement(tag);
  replacement.id = 'pf-voice';
  current.replaceWith(replacement);
  return {element: replacement, previous};
}

async function refreshVoiceCandidates() {
  if ($('pf-role').value !== 'tts') return;
  const body = {};
  if ($('pf-id').value) body.profile_id = Number($('pf-id').value);
  else {
    body.backend = $('pf-backend').value;
    const base = $('pf-base-url').value.trim();
    const model = $('pf-model').value.trim();
    if (base) body.base_url = base;
    if (model) body.model = model;
    const name = $('pf-name').value.trim();
    if (name) body.name = name;
    if ($('pf-api-key').value) body.api_key = $('pf-api-key').value;
  }
  let catalog = null;
  try {
    catalog = await api('/api/settings/voices/catalog/preview', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
  } catch {
    catalog = null; // 查询失败时保留自由输入
  }
  const kind = catalog?.voice_kind === 'clone' ? 'clone' : 'system';
  if (kind === 'clone') {
    const {element: select, previous: previousClone} = ensureVoiceField('select');
    if (!select) return;
    select.dataset.voiceKind = 'clone';
    select.removeAttribute('list');
    select.removeAttribute('placeholder');
    select.replaceChildren();
    if ((catalog?.library || []).length) {
      for (const item of catalog.library) {
        const option = node('option', `${item.name}（${item.voice_id}）`);
        option.value = item.voice_id;
        select.append(option);
      }
      if (![...select.options].some(option => option.value === previousClone)) select.value = '';
    } else {
      const empty = node('option', catalog?.hint || '音色库中还没有克隆音色；请先在「音色库」上传样本克隆。');
      empty.value = '';
      select.append(empty);
      select.value = '';
    }
    return;
  }
  const groups = catalog?.groups || [];
  if (!groups.length) {
    // 目录拉取失败且无静态兜底：退回自由输入，保证档案仍可保存
    const {element: fallback, previous: fallbackValue} = ensureVoiceField('input');
    if (fallback) {
      fallback.removeAttribute('list');
      fallback.value = fallbackValue;
      fallback.placeholder = '目录暂不可用；可手动输入音色 ID';
    }
    return;
  }
  const {element: select, previous} = ensureVoiceField('select');
  if (!select) return;
  select.removeAttribute('list');
  select.dataset.voiceKind = 'system';
  select.replaceChildren();
  const unselected = node('option', '（跟随配置文件默认）');
  unselected.value = '';
  select.append(unselected);
  for (const group of groups) {
    if (!group.voices.length) continue;
    const optgroup = document.createElement('optgroup');
    optgroup.label = group.label;
    for (const voice of group.voices) {
      const option = node('option', voice.label);
      option.value = voice.voice_id;
      optgroup.append(option);
    }
    select.append(optgroup);
  }
  if ((catalog?.library || []).length) {
    const mine = document.createElement('optgroup');
    mine.label = '我的音色';
    for (const item of catalog.library) {
      const option = node('option', `${item.kind === 'clone' ? '克隆' : '已录入'} · ${item.name}`);
      option.value = item.voice_id;
      mine.append(option);
    }
    select.append(mine);
  }
  select.value = [...select.options].some(option => option.value === previous)
    ? previous
    : ''; // 当前值不在目录中时回落到「跟随默认」，避免浏览器静默选中第一项造成误改
}

function openDialog(role, profile) {
  const data = state.data;
  $('pf-role').value = role;
  $('pf-id').value = profile ? profile.id : '';
  $('profile-title').textContent = `${(data.roles.find(r => r.role === role) || {}).label || role} · ${profile ? '编辑档案' : '新建档案'}`;
  $('pf-error').textContent = '';
  $('pf-api-key').value = '';
  const providerSelect = $('pf-provider');
  providerSelect.replaceChildren(...Object.entries(data.providers).map(([value, label]) => {
    const option = node('option', label);
    option.value = value;
    return option;
  }));
  const config = profile ? profile.config : {};
  providerSelect.value = profile ? profile.provider : 'custom';
  providerSelect.onchange = () => applyPreset(role, providerSelect.value);
  $('pf-name').value = profile ? profile.name : '';
  fillBackendOptions(role, config.backend || ROLE_BACKENDS[role][0]);
  $('pf-base-url').value = config.base_url || '';
  $('pf-model').value = config.model || '';
  $('pf-revision').value = config.revision || '';
  fillExtraFields(role, config);
  const useTemplate = role === 'analyzer' || role === 'generator';
  $('pf-template-wrap').hidden = !useTemplate;
  if (useTemplate) $('pf-llama-template').checked = profile ? config.llama_template !== false : true;
  $('pf-key-hint').textContent = profile && profile.api_key.has_key
    ? `已保存密钥 ${profile.api_key.hint}；留空保留，勾选下方框可清除。` : '保存后密钥只写入本地数据库，不会出现在页面或日志。';
  $('pf-clear-key-wrap').hidden = !(profile && profile.api_key.has_key);
  $('pf-clear-key').checked = false;
  refreshVoiceCandidates();
  return $('profile-dialog').showModal();
}

$('profile-form').onsubmit = async event => {
  event.preventDefault();
  const role = $('pf-role').value;
  const profileId = $('pf-id').value;
  const config = {
    backend: $('pf-backend').value,
    base_url: $('pf-base-url').value.trim(),
    model: $('pf-model').value.trim(),
  };
  const revision = $('pf-revision').value.trim();
  if (revision && revision !== 'main') config.revision = revision;
  if (role === 'analyzer' || role === 'generator') config.llama_template = $('pf-llama-template').checked;
  ROLE_EXTRA[role].forEach(field => {
    const input = $(`pf-${field.key}`);
    if (!input) return;
    const value = input.value.trim();
    if (value !== '') config[field.key] = field.type === 'number' ? Number(value) : value;
    if (field.type === 'json') {
      if (value === '') config[field.key] = null;
      else { try { config[field.key] = JSON.parse(value); } catch { config[field.key] = value; } }
    }
  });
  const apiKey = $('pf-api-key').value;
  $('pf-save').disabled = true;
  $('pf-error').textContent = '';
  if (role === 'tts' && $('pf-voice')?.dataset?.voiceKind === 'clone' && !$('pf-voice').value) {
    $('pf-save').disabled = false;
    $('pf-error').textContent = 'VoiceClone 型模型必须选择音色库中的克隆音色；请先在「音色库」上传样本克隆。';
    return;
  }
  try {
    const body = {role, name: $('pf-name').value.trim(), provider: $('pf-provider').value, config};
    if (apiKey !== '') body.api_key = apiKey;
    if (profileId && $('pf-clear-key')?.checked) body.api_key = '__clear__';
    await api(profileId ? `/api/settings/profiles/${profileId}` : '/api/settings/profiles', {
      method: profileId ? 'PUT' : 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    $('profile-dialog').close();
    notify('档案已保存。');
    await loadSettings();
  } catch (e) {
    $('pf-error').textContent = e.message;
  } finally {
    $('pf-save').disabled = false;
  }
};
$('profile-close').onclick = () => $('profile-dialog').close();

$('search-tuning').onsubmit = async event => {
  event.preventDefault();
  try {
    const weights = {};
    for (const key of Object.keys(WEIGHT_LABELS)) {
      const value = Number($(`weight-${key}`).value);
      if (!Number.isFinite(value) || value < 0) throw new Error(`${WEIGHT_LABELS[key]} 权重须为非负数字`);
      weights[key] = value;
    }
    const channel_weights = {};
    for (const key of ['visual', 'dialogue', 'analysis']) {
      const value = Number($(`channel-weight-${key}`).value);
      if (!Number.isFinite(value) || value < 0) throw new Error('渠道权重须为非负数字');
      channel_weights[key] = value;
    }
    await api('/api/settings/business', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({search: {weights, channel_weights, rrf_k: Number($('rrf-k').value), candidates: Number($('candidates').value)}}),
    });
    notify('检索调参已保存，立即生效。');
    await loadSettings();
  } catch (e) {
    notify(e.message, true);
  }
};
$('tuning-reset').onclick = () => run(async () => {
  await api('/api/settings/business', {method: 'DELETE'});
  notify('已恢复配置文件默认值。');
  await loadSettings();
});
$('reload').onclick = () => run(async () => {
  $('reload').disabled = true;
  try { await loadSettings(); notify('设置状态已刷新。'); } finally { $('reload').disabled = false; }
});

for (const head of document.querySelectorAll('.role-row-head[data-section]')) {
  head.onclick = () => toggleRole(head.dataset.section);
}
syncSectionRows();
run(loadSettings);
