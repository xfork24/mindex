'use strict';
const $ = id => document.getElementById(id);
const state = {data: null, tests: {}};
let settingsRequest = 0;

const ROLE_ORDER = ['analyzer', 'generator', 'embedding', 'asr', 'tts'];
const ROLE_DESC = {
  analyzer: '视觉帧分析（动作、情绪、场景描述），需要多模态视觉模型',
  generator: '创作文案拆分镜，纯文本任务，可选高性价比文本模型',
  embedding: '文本 / 图片 / 视频统一向量空间；切换模型后需重建索引',
  asr: '语音转文字；线上模型只出纯文本，时间戳由本地 CPU 对齐补齐',
  tts: '创作配音；支持预定义音色与克隆音色',
};
const ROLE_BACKENDS = {
  analyzer: ['openai', 'llama_cpp'],
  generator: ['openai'],
  embedding: ['mindex', 'llama_cpp', 'tokenhub'],
  asr: ['mindex', 'llama_cpp', 'openai'],
  tts: ['openai', 'minimax', 'dashscope'],
};
const ROLE_EXTRA = {
  embedding: [
    {key: 'dimensions', label: '向量维度', type: 'number', min: 1},
    {key: 'quantization', label: '量化标识', type: 'text', placeholder: '线上模型填 cloud'},
  ],
  generator: [{key: 'max_tokens', label: 'max_tokens', type: 'number', min: 100}],
  tts: [{key: 'response_format', label: '音频格式', type: 'select', options: ['wav', 'mp3']}],
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
    box.append(node('div', '当前嵌入身份与已激活索引不一致，文字/图片/视频检索将返回未就绪。'));
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
  const card = node('article', undefined, 'role-card');
  const head = node('header');
  head.append(node('h3', rolePayload.label), node('p', ROLE_DESC[rolePayload.role] || '', 'role-desc'));
  card.append(head);

  const effective = rolePayload.effective;
  const keyState = effective.api_key.has_key ? `密钥 ${effective.api_key.hint}`
    : effective.env_key_set ? `密钥（环境变量 ${effective.api_key_env}）` : '未配置密钥';
  const info = node('div', undefined, 'effective');
  info.append(node('div', `当前生效：${effective.model} · ${effective.backend} · ${effective.base_url}`));
  info.append(node('div', `${keyState}${effective.dimensions ? ` · ${effective.dimensions} 维` : ''}`, 'muted'));
  card.append(info);

  const list = node('ul', undefined, 'profile-list');
  if (!rolePayload.profiles.length) {
    const empty = node('li', undefined, 'profile-item');
    empty.append(node('span', '还没有档案；当前直接使用本地配置文件的取值。', 'profile-meta'));
    list.append(empty);
  }
  rolePayload.profiles.forEach(profile => {
    const item = node('li', undefined, `profile-item ${rolePayload.active_profile_id === profile.id ? 'is-active' : ''}`);
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
    bar.append(action('编辑', () => openDialog(rolePayload.role, profile)));
    bar.append(action('删除', () => deleteProfile(profile)));
    item.append(bar);
    const line = testLine(profile.id);
    if (line) item.append(line);
    list.append(item);
  });
  card.append(list);
  card.append(action('+ 新建档案', () => openDialog(rolePayload.role, null)));
  return card;
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
  $('rrf-k').value = data.business.search.rrf_k;
  $('candidates').value = data.business.search.candidates;
}

// --- 音色库 ---
const VOICE_PROVIDER_LABELS = {self: '自建服务', openai: 'OpenAI 协议', minimax: 'MiniMax', dashscope: '阿里'};

function renderVoices(data) {
  const voices = data.voices || [];
  const select = $('default-voice');
  select.replaceChildren();
  const follow = node('option', '跟随配置文件默认');
  follow.value = '';
  select.append(follow);
  voices.forEach(voice => {
    const option = node('option', `${voice.kind === 'clone' ? '克隆' : '预定义'} · ${voice.name}`);
    option.value = String(voice.id);
    if (data.business.default_voice === voice.voice_id) option.selected = true;
    select.append(option);
  });
  const backend = data.business.tts_backend;
  $('voice-backend-hint').textContent =
    backend === 'minimax' ? '当前 MiniMax 后端：可上传样本克隆'
    : backend === 'dashscope' ? '当前阿里后端：可用样本 URL 复刻'
    : '当前本地后端：仅支持录入音色 ID';

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
    body: JSON.stringify(value ? {default_voice_id: Number(value)} : {clear_default_voice: true}),
  });
  notify(value ? '默认音色已更新。' : '已恢复配置文件默认音色。');
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
  $('clone-mode-note').textContent = {
    upload: '上传样本到厂商完成克隆并生成预览；需要当前语音合成为 MiniMax 后端。',
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
  $('clone-consent').checked = false;
  $('clone-error').textContent = '';
  $('clone-dialog').showModal();
}
for (const button of document.querySelectorAll('.clone-mode')) {
  button.onclick = () => { cloneMode = button.dataset.mode; syncCloneMode(); };
}
$('voice-clone').onclick = () => {
  const backend = state.data?.business?.tts_backend;
  openCloneDialog(backend === 'dashscope' ? 'url' : backend === 'minimax' ? 'upload' : 'register');
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
  renderVoices(data);
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
      input.type = field.type;
      if (field.min !== undefined) input.min = field.min;
      if (field.placeholder) input.placeholder = field.placeholder;
    }
    input.id = `pf-${field.key}`;
    if (values[field.key] !== undefined && values[field.key] !== null) input.value = values[field.key];
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
  });
  const apiKey = $('pf-api-key').value;
  $('pf-save').disabled = true;
  $('pf-error').textContent = '';
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
    await api('/api/settings/business', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({search: {weights, rrf_k: Number($('rrf-k').value), candidates: Number($('candidates').value)}}),
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

run(loadSettings);
