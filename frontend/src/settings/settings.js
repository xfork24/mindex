'use strict';
const $ = (id) => document.getElementById(id);
const EXPANDED_KEY = 'mindex.settings.roles-expanded';
const state = { data: null, tests: {}, expanded: loadExpanded() };
function loadExpanded() {
  try {return new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY) || '[]'));}
  catch {return new Set();}
}
function toggleRole(role) {
  if (state.expanded.has(role)) state.expanded.delete(role);else state.expanded.add(role);
  try {localStorage.setItem(EXPANDED_KEY, JSON.stringify([...state.expanded]));} catch {}
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

const ROLE_ORDER = ['analyzer', 'writer', 'generator', 'embedding', 'asr', 'tts', 'image_generator', 'video_generator', 'music_generator', 'animator', 'person', 'agent'];
// 支持本地 ComfyUI 工作流导入的角色及其可映射字段（与后端 shared/comfyui.py 一致）。
const COMFY_ROLES = ['video_generator', 'image_generator', 'music_generator'];
const ROLE_DESC = {
  person: MindexI18n.msg("ui.907c176fe85a"),
  analyzer: MindexI18n.msg("ui.85feed61e44f"),
  writer: MindexI18n.msg("ui.writer_role_description"),
  generator: MindexI18n.msg("ui.09b768c5ad0a"),
  embedding: MindexI18n.msg("ui.bd997040b2e2"),
  asr: MindexI18n.msg("ui.b74f293dd13e"),
  tts: MindexI18n.msg("ui.35cda4046011"),
  image_generator: MindexI18n.msg("ui.image_generator_role_description"),
  video_generator: MindexI18n.msg("ui.video_generator_role_description"),
  music_generator: MindexI18n.msg("ui.music_generator_role_description"),
  animator: MindexI18n.msg("ui.animator_role_description"),
  agent: MindexI18n.msg("ui.agent_role_description")
};
const ROLE_BACKENDS = {
  person: ['mindex'],
  analyzer: ['openai', 'llama_cpp'],
  writer: ['openai'],
  generator: ['openai'],
  embedding: ['mindex', 'llama_cpp', 'tokenhub'],
  asr: ['mindex', 'llama_cpp', 'openai', 'minimax'],
  tts: ['openai', 'minimax', 'dashscope'],
  image_generator: ['minimax', 'comfyui'],
  video_generator: ['minimax_h3', 'comfyui', 'minimax', 'dashscope'],
  music_generator: ['minimax', 'comfyui', 'audiocpp'],
  animator: ['openai', 'anthropic'],
  agent: ['openai', 'anthropic']
};
// 每个模型档案都可配置单次请求超时（秒），所有调用该模型的地方都用它；留空用缺省值：
// 图片、视频、音乐与动画生成 10 分钟，其他模型 3 分钟（与后端 shared/config.py 一致）。
const GENERATION_ROLES = ['video_generator', 'image_generator', 'music_generator', 'animator'];
function timeoutField(role) {
  const fallback = GENERATION_ROLES.includes(role) ? 600 : 180;
  return { key: 'timeout', label: MindexI18n.msg("ui.model_timeout"), type: 'number', min: 1, step: 1,
    placeholder: MindexI18n.msg("ui.model_timeout_placeholder", { p0: fallback }) };
}
const ROLE_EXTRA = {
  person: [
  { key: 'expected_space', label: MindexI18n.msg("ui.7f5c519b5795"), type: 'json' },
  { key: 'frame_interval', label: '间隔帧数', type: 'number', min: 1, step: 1 },
  { key: 'face_threshold', label: MindexI18n.msg("ui.c7501d1b57d8"), type: 'number', min: 0.01 },
  { key: 'face_margin', label: MindexI18n.msg("ui.400a124b6162"), type: 'number', min: 0.01 },
  { key: 'min_face_quality', label: MindexI18n.msg("ui.9ced26663827"), type: 'number', min: 0, max: 1 },
  { key: 'min_reference_quality', label: MindexI18n.msg("ui.8ea4592494d3"), type: 'number', min: 0, max: 1 },
  { key: 'body_threshold', label: MindexI18n.msg("ui.83b62368c610"), type: 'number', min: 0.01 }],

  embedding: [
  { key: 'dimensions', label: MindexI18n.msg("ui.9733d2a5ddb4"), type: 'number', min: 1 },
  { key: 'quantization', label: MindexI18n.msg("ui.eb2b7bc048c6"), type: 'text', placeholder: MindexI18n.msg("ui.8814a2ac40dc") }],

  writer: [{ key: 'max_tokens', label: 'max_tokens', type: 'number', min: 100 }],
  generator: [{ key: 'max_tokens', label: 'max_tokens', type: 'number', min: 100 }],
  tts: [
  { key: 'response_format', label: MindexI18n.msg("ui.df5363469d15"), type: 'select', options: ['wav', 'mp3'] },
  { key: 'voice', label: MindexI18n.msg("ui.9bbd54bf4bf3"), type: 'text', placeholder: MindexI18n.msg("ui.c5186b8d1b79") }],

  analyzer: [
  { key: 'thinking_mode', label: MindexI18n.msg("ui.69805dd4c1c2"), type: 'select', options: ['auto', 'low', 'provider'] },
  { key: 'response_retries', label: MindexI18n.msg("ui.2b4e456f4b30"), type: 'number', min: 0, max: 20 },
  { key: 'initial_output_tokens', label: MindexI18n.msg("ui.377c6dff15af"), type: 'number', min: 64, max: 131072 },
  { key: 'max_output_tokens', label: MindexI18n.msg("ui.ee7fe23f341f"), type: 'number', min: 64, max: 131072 }],

  asr: [{ key: 'max_tokens', label: 'max_tokens', type: 'number', min: 256 }],
  video_generator: [
  { key: 'durations', label: MindexI18n.msg("ui.video_durations"), type: 'list', placeholder: MindexI18n.msg("ui.video_durations_placeholder") },
  { key: 'fps', label: MindexI18n.msg("ui.video_fps"), type: 'number', min: 1, max: 60, step: 1 },
  { key: 'poll_interval', label: MindexI18n.msg("ui.video_poll_interval"), type: 'number', min: 0.1 },
  { key: 'max_wait', label: MindexI18n.msg("ui.video_max_wait"), type: 'number', min: 1 }],

  image_generator: [
  { key: 'poll_interval', label: MindexI18n.msg("ui.video_poll_interval"), type: 'number', min: 0.1 },
  { key: 'max_wait', label: MindexI18n.msg("ui.video_max_wait"), type: 'number', min: 1 }],

  music_generator: [
  { key: 'cover_model', label: MindexI18n.msg("ui.music_cover_model"), type: 'text', placeholder: 'music-cover' },
  { key: 'num_inference_steps', label: 'num_inference_steps', type: 'number', min: 1, max: 200, step: 1 },
  { key: 'max_seconds', label: 'max_seconds', type: 'number', min: 1, max: 300, step: 1 },
  { key: 'poll_interval', label: MindexI18n.msg("ui.video_poll_interval"), type: 'number', min: 0.1 },
  { key: 'max_wait', label: MindexI18n.msg("ui.video_max_wait"), type: 'number', min: 1 }],

  animator: [
  { key: 'max_tokens', label: 'max_tokens', type: 'number', min: 1000 },
  { key: 'retries', label: MindexI18n.msg("ui.animator_retries"), type: 'number', min: 0, max: 10, step: 1 },
  { key: 'max_html_kb', label: MindexI18n.msg("ui.animator_max_html_kb"), type: 'number', min: 1, max: 5000, step: 1 },
  { key: 'max_capture_seconds', label: MindexI18n.msg("ui.animator_max_capture_seconds"), type: 'number', min: 1, max: 3600 }],

  agent: [
  { key: 'max_tokens', label: 'max_tokens', type: 'number', min: 256 },
  { key: 'vision', label: MindexI18n.msg("ui.agent_vision"), hint: MindexI18n.msg("ui.agent_vision_hint"), type: 'checkbox' }]
};
for (const role of Object.keys(ROLE_EXTRA)) ROLE_EXTRA[role].unshift(timeoutField(role));
const WEIGHT_LABELS = {
  video: MindexI18n.msg("ui.1a6981c253a3"), description: MindexI18n.msg("ui.522d79717392"), style: MindexI18n.msg("ui.5e46e0ea4cd0"), dialogue: MindexI18n.msg("ui.56073ee07b2d"), lexical: MindexI18n.msg("ui.7fe742805071")
};

function node(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) MindexI18n.setText(n, text);
  if (className) n.className = className;
  return n;
}
function voiceLabelOf(catalog, voiceId) {
  for (const group of catalog.groups || []) {
    const hit = (group.voices || []).find((voice) => voice.voice_id === voiceId);
    if (hit) return MindexI18n.voice(hit);
  }
  const mine = (catalog.library || []).find((voice) => voice.voice_id === voiceId);
  return mine ? `${mine.kind === 'clone' ? MindexI18n.msg("ui.6e60458a4727") : MindexI18n.msg("ui.4512b3b0e457")} · ${mine.name}` : voiceId;
}

function notify(text = '', error = false) {
  if (globalThis.MindexFeedback) return globalThis.MindexFeedback.show('notice', error ? MindexI18n.system(text) : text, error ? 'error' : 'info');

  MindexI18n.setText($('notice'), error ? MindexI18n.system(text) : text);
  $('notice').className = `notice ${error ? 'error' : 'info'}`;
}
function errorText(detail) {
  return Array.isArray(detail) ? detail.map((e) => e.msg).join('；') :
  typeof detail === 'string' ? detail : JSON.stringify(detail);
}
async function api(url, options = {}) {
  return MindexApi.request(url, options);
}
async function run(task) {
  try {await task();} catch (e) {notify(e.message, true);}
}

function renderIndexBanner(status) {
  const banner = $('index-banner');
  banner.replaceChildren();
  if (status.active_index_id === null) {
    banner.append(node('div', MindexI18n.msg("ui.3b88b478e401"), 'index-banner-muted'));
    return;
  }
  if (status.matches_index) {
    banner.append(node('div', MindexI18n.msg("ui.a6ed61231fea", { p0: status.active_index_id.slice(0, 10) }), 'index-banner-ok'));
  } else {
    const box = node('div', undefined, 'index-banner-error');
    box.append(node('div', MindexI18n.msg("ui.5f0c8163f905")));
    box.append(node('div', MindexI18n.msg("ui.e07d9aa4781f", { p0: status.active_index_id, p1: status.error ? MindexI18n.msg("ui.a2387d1945f7", { p0: MindexI18n.system(status.error) }) : '' })));
    banner.append(box);
  }
}

function testLine(profileId) {
  if (!(profileId in state.tests)) return null;
  const result = state.tests[profileId];
  const line = node('div', undefined, `test-result ${result.ok ? 'ok' : 'error'}`);
  if (result.ok) {
    const bits = [MindexI18n.msg("ui.79ccfbf3ca48", { p0: result.latency_ms })];
    if (result.dims) bits.push(MindexI18n.msg("ui.b38853581295", { p0: result.dims }));
    if (result.reply) bits.push(`「${result.reply}」`);
    if (result.voice) bits.push(result.voice);
    if (result.bytes) bits.push(MindexI18n.msg("ui.4dd220045314", { p0: result.bytes }));
    MindexI18n.setText(line, bits.join(' · '));
  } else {
    MindexI18n.setText(line, `✗ ${MindexI18n.system(result.detail)}`);
  }
  return line;
}

function action(text, onClick) {
  const button = node('button', text, 'quiet');
  button.type = 'button';
  button.onclick = () => run(async () => {button.disabled = true;try {await onClick();} finally {button.disabled = false;}});
  return button;
}

function renderRole(rolePayload) {
  const role = rolePayload.role;
  const open = state.expanded.has(role);

  const row = node('article', undefined, `role-row${open ? ' is-open' : ''}`);
  row.dataset.role = role;
  const head = node('button', undefined, 'role-row-head');
  head.type = 'button';
  head.setAttribute('aria-expanded', String(open));
  head.setAttribute('aria-controls', `role-body-${role}`);
  head.onclick = () => toggleRole(role);

  const effective = rolePayload.effective;
  const keyState = effective.key_shared_from === 'analyzer' ? MindexI18n.msg("ui.animator_key_shared", { p0: effective.api_key.hint }) :
  effective.api_key.has_key ? MindexI18n.msg("ui.bc24ebb490a2", { p0: effective.api_key.hint }) :
  effective.env_key_set ? MindexI18n.msg("ui.aa7487d83f4a", {}) : MindexI18n.msg("ui.a3e7368ec053");
  head.append(node('span', '▸', 'chevron'));
  head.append(node('strong', MindexI18n.builtin(rolePayload.label), 'role-row-title'));
  const summary = node('span', undefined, 'role-row-summary');
  summary.append(node('span', MindexI18n.msg("ui.61236d70e259", { p0: effective.model, p1: effective.backend, p2: effective.base_url }), 'role-row-effective'));
  summary.append(node('span', `${keyState}${effective.dimensions ? MindexI18n.msg("ui.d5b0619c7e95", { p0: effective.dimensions }) : ''}${MindexI18n.msg("ui.model_timeout_summary", { p0: effective.timeout })}`, 'role-row-key'));
  head.append(summary);
  row.append(head);

  const body = node('div', undefined, 'role-row-body');
  body.id = `role-body-${role}`;
  body.hidden = !open;
  body.append(node('p', MindexI18n.msg("ui.e20aa0fa9eb2", { p0: ROLE_DESC[role] || '', p1: rolePayload.profiles.length }), 'role-desc'));

  const scroller = node('div', undefined, 'profile-scroller');
  if (!rolePayload.profiles.length) {
    scroller.append(node('div', MindexI18n.msg("ui.cdbe79925ca7"), 'profile-item empty'));
  }
  rolePayload.profiles.forEach((profile) => {
    const item = node('div', undefined, `profile-item ${rolePayload.active_profile_id === profile.id ? 'is-active' : ''}`);
    item.dataset.profileId = String(profile.id);
    const head2 = node('div', undefined, 'profile-head');
    head2.append(node('strong', profile.name));
    if (rolePayload.active_profile_id === profile.id) head2.append(node('span', MindexI18n.msg("ui.e6aa2cbd7b42"), 'active-badge'));
    head2.append(node('span', profile.provider, 'provider-chip'));
    item.append(head2);
    item.append(node('div', `${profile.config.model || ''} · ${profile.config.backend || ''} · ${profile.config.base_url || ''}`, 'profile-meta'));
    const keyHint = profile.api_key.has_key ? MindexI18n.msg("ui.bc24ebb490a2", { p0: profile.api_key.hint }) :
    role === 'animator' && rolePayload.active_profile_id === profile.id && effective.key_shared_from === 'analyzer' ?
    MindexI18n.msg("ui.animator_key_shared", { p0: effective.api_key.hint }) : MindexI18n.msg("ui.e80f2d522dd3");
    item.append(node('div', keyHint, 'profile-meta'));
    const bar = node('div', undefined, 'profile-actions');
    if (rolePayload.active_profile_id !== profile.id) bar.append(action(MindexI18n.msg("ui.4c25820818d6"), () => activateProfile(profile.id)));
    bar.append(action(MindexI18n.msg("ui.10b7d8ecccef"), () => testProfile(profile.id)));
    bar.append(action(MindexI18n.msg("ui.a7f814c0a40d"), () => openDialog(role, profile)));
    bar.append(action(MindexI18n.msg("ui.3755f56f2f83"), () => deleteProfile(profile)));
    item.append(bar);
    const line = testLine(profile.id);
    if (line) item.append(line);
    scroller.append(item);
  });
  const create = node('button', MindexI18n.msg("ui.b60dbc5381cd"), 'new-profile-card');
  create.type = 'button';
  create.onclick = () => openDialog(role, null);
  scroller.append(create);

  body.append(scroller);
  row.append(body);
  return row;
}

function renderRoles(data) {
  const byRole = Object.fromEntries(data.roles.map((r) => [r.role, r]));
  $('roles').replaceChildren(...ROLE_ORDER.map((role) => renderRole(byRole[role])));
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
  for (const [key, label] of Object.entries({ visual: MindexI18n.msg("ui.fe84e2eb06a1"), dialogue: MindexI18n.msg("ui.19a061a8abf3"), analysis: MindexI18n.msg("ui.5484a583c2c9") })) {
    const wrap = node('label');wrap.append(node('span', label));
    const input = node('input');input.id = `channel-weight-${key}`;
    input.type = 'number';input.min = '0';input.step = '0.1';
    input.value = data.business.search.channel_weights[key];
    wrap.append(input);grid.append(wrap);
  }
  $('rrf-k').value = data.business.search.rrf_k;
  $('candidates').value = data.business.search.candidates;
  const summary = $('tuning-summary');
  MindexI18n.setText(summary, MindexI18n.msg("ui.3329e060c3ae", { p0: ['visual', 'dialogue', 'analysis'].map((k) => data.business.search.channel_weights[k]).join('/'), p1: ['video', 'description', 'style', 'dialogue', 'lexical'].map((k) => weights[k]).join('/') }) + MindexI18n.msg("ui.5035217a37ce", { p0:
    data.business.search.rrf_k, p1: data.business.search.candidates }));
  MindexI18n.setAttr(summary, "title", MindexI18n.msg("ui.278a6e38a880"));
}

// --- 音色库 ---
const VOICE_PROVIDER_LABELS = { self: MindexI18n.msg("ui.d4d31613eca7"), openai: MindexI18n.msg("ui.54928be09e5b"), minimax: 'MiniMax', dashscope: MindexI18n.msg("ui.6590ec95970e") };

function renderVoices(data, catalog = null) {
  const voices = data.voices || [];
  const active = voices.find((voice) => voice.voice_id === data.business.default_voice);
  const defaultLabel = catalog ?
  catalog.default_voice && voiceLabelOf(catalog, catalog.default_voice) || active?.name || MindexI18n.msg("ui.6f5ec3201394") :
  active ? active.name : MindexI18n.msg("ui.6f5ec3201394");
  MindexI18n.setText($('voices-summary'), MindexI18n.msg("ui.d23f60530e93", { p0: voices.length, p1: defaultLabel === MindexI18n.msg("ui.6f5ec3201394") ? defaultLabel : defaultLabel }));
  const select = $('default-voice');
  select.replaceChildren();
  const follow = node('option', MindexI18n.msg("ui.6f5ec3201394"));
  follow.value = '';
  select.append(follow);
  if (catalog) {
    for (const group of catalog.groups || []) {
      if (!group.voices.length) continue;
      const optgroup = document.createElement('optgroup');
      MindexI18n.setAttr(optgroup, "label", MindexI18n.builtin(group.label));
      group.voices.forEach((voice) => {
        const option = node('option', MindexI18n.voice(voice));
        option.value = voice.voice_id;
        if (catalog.default_voice === voice.voice_id) option.selected = true;
        optgroup.append(option);
      });
      select.append(optgroup);
    }
    if ((catalog.library || []).length) {
      const mine = document.createElement('optgroup');
      MindexI18n.setAttr(mine, "label", MindexI18n.msg("ui.6ec5494286de"));
      catalog.library.forEach((voice) => {
        const option = node('option', `${voice.kind === 'clone' ? MindexI18n.msg("ui.6e60458a4727") : MindexI18n.msg("ui.4512b3b0e457")} · ${voice.name}`);
        option.value = voice.voice_id;
        if (catalog.default_voice === voice.voice_id) option.selected = true;
        mine.append(option);
      });
      select.append(mine);
    }
    if (catalog.default_voice && ![...select.options].some((option) => option.selected)) {
      const fallback = node('option', MindexI18n.msg("ui.b5acf285f054", { p0: catalog.default_voice }));
      fallback.value = catalog.default_voice;
      fallback.selected = true;
      select.append(fallback);
    }
  } else {
    voices.forEach((voice) => {
      const option = node('option', `${voice.kind === 'clone' ? MindexI18n.msg("ui.6e60458a4727") : MindexI18n.msg("ui.d2712c1576ab")} · ${voice.name}`);
      option.value = voice.voice_id;
      if (data.business.default_voice === voice.voice_id) option.selected = true;
      select.append(option);
    });
  }
  const clone = catalog?.clone;
  MindexI18n.setText($('voice-backend-hint'), catalog?.error ? MindexI18n.msg("ui.63a91778c733", { p0: MindexI18n.system(
      catalog.error) }) :
  clone?.supported ?
  clone.mode === 'url' ? MindexI18n.msg("ui.5683b124d6bc") :
  clone.ref_text ? MindexI18n.msg("ui.b80eab631353") : MindexI18n.msg("ui.ddaaa717a93d") : MindexI18n.msg("ui.681eeab7e0e2"));



  const list = $('voice-list');
  list.replaceChildren();
  if (!voices.length) list.append(node('li', MindexI18n.msg("ui.f15b2e0489b0"), 'muted'));
  voices.forEach((voice) => {
    const item = node('li', undefined, 'voice-item');
    const head = node('div', undefined, 'profile-head');
    head.append(node('strong', voice.name));
    if (data.business.default_voice === voice.voice_id) head.append(node('span', MindexI18n.msg("ui.c8d09cf955f5"), 'active-badge'));
    head.append(node('span', `${voice.kind === 'clone' ? MindexI18n.msg("ui.6e60458a4727") : MindexI18n.msg("ui.d2712c1576ab")} · ${VOICE_PROVIDER_LABELS[voice.provider] || voice.provider}`, 'provider-chip'));
    if (voice.status !== 'ready') head.append(node('span', voice.status === 'failed' ? MindexI18n.msg("ui.3e3c8068bb0e") : MindexI18n.msg("ui.6f1972e48ec8"), 'status-badge'));
    item.append(head);
    item.append(node('div', `${voice.voice_id}${voice.sample_present ? MindexI18n.msg("ui.efd2726aaa62") : ''}`, 'profile-meta'));
    if (voice.status === 'failed' && voice.notes) item.append(node('div', voice.notes, 'test-result error'));
    const bar = node('div', undefined, 'profile-actions');
    if (data.business.default_voice !== voice.voice_id) bar.append(action(MindexI18n.msg("ui.536a009a9b59"), () => setDefaultVoice(voice.id)));
    bar.append(action(MindexI18n.msg("ui.3755f56f2f83"), () => deleteVoice(voice)));
    item.append(bar);
    list.append(item);
  });
}

async function setDefaultVoice(voiceId) {
  await api('/api/settings/business', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ default_voice_id: voiceId })
  });
  notify(MindexI18n.msg("ui.4d29ec2245b6"));
  await loadSettings();
}
async function deleteVoice(voice) {
  if (!confirm(MindexI18n.render(MindexI18n.msg("ui.7d16a0d1b523", { p0: voice.name })))) return;
  await api(`/api/settings/voices/${voice.id}`, { method: 'DELETE' });
  notify(MindexI18n.msg("ui.11141858d215"));
  await loadSettings();
}
$('default-voice').onchange = () => run(async () => {
  const value = $('default-voice').value;
  await api('/api/settings/business', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(value ? { default_voice: value } : { clear_default_voice: true })
  });
  notify(value ? MindexI18n.msg("ui.4d29ec2245b6") : MindexI18n.msg("ui.9f1bb76e83c6"));
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
  MindexI18n.setText($('clone-mode-note'), {
    upload: state.voiceCatalog?.clone?.ref_text ? MindexI18n.msg("ui.3291a06d1151") : MindexI18n.msg("ui.c61d27fab089"),


    url: MindexI18n.msg("ui.65b47afd67f5"),
    register: MindexI18n.msg("ui.3011807ab73f")
  }[cloneMode]);
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
  MindexI18n.setText($('clone-error'), '');
  $('clone-dialog').showModal();
}
for (const button of document.querySelectorAll('.clone-mode')) {
  button.onclick = () => {cloneMode = button.dataset.mode;syncCloneMode();};
}
$('voice-clone').onclick = () => {
  const backend = state.data?.business?.tts_backend;
  const clone = state.voiceCatalog?.clone;
  openCloneDialog(
    backend === 'dashscope' ? 'url' :
    clone?.supported ? 'upload' :
    'register'
  );
};
$('voice-register').onclick = () => openCloneDialog('register');
$('clone-close').onclick = () => $('clone-dialog').close();
$('clone-form').onsubmit = async (event) => {
  event.preventDefault();
  const name = $('clone-name').value.trim();
  MindexI18n.setText($('clone-error'), '');
  $('clone-save').disabled = true;
  try {
    if (cloneMode === 'register') {
      await api('/api/settings/voices', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name, provider: $('clone-provider').value,
          voice_id: $('clone-voice-id').value.trim(), kind: 'preset'
        })
      });
    } else {
      const form = new FormData();
      form.append('name', name);
      form.append('consent', $('clone-consent').checked ? 'true' : 'false');
      if (cloneMode === 'upload') {
        if (!$('clone-file').files.length) throw new Error(MindexI18n.msg("ui.78ad35fdb232"));
        form.append('ref_text', $('clone-reftext').value.trim());
        form.append('file', $('clone-file').files[0]);
      } else {
        const url = $('clone-url').value.trim();
        if (!url) throw new Error(MindexI18n.msg("ui.6bbfc9aa3613"));
        form.append('audio_url', url);
      }
      await api('/api/settings/voices/clone', { method: 'POST', body: form });
    }
    $('clone-dialog').close();
    notify(MindexI18n.msg("ui.ef6533b58426"));
    await loadSettings();
  } catch (e) {
    MindexI18n.setText($('clone-error'), e.message);
  } finally {
    $('clone-save').disabled = false;
  }
};

async function loadSettings() {
  const request = ++settingsRequest;
  const data = await api('/api/settings?probe_embedding=false');
  if (request !== settingsRequest) return;
  state.data = data;
  $('enforce-gpu-lock').checked = data.business.enforce_gpu_lock === true;
  $('index-banner').replaceChildren();
  api('/api/settings/embedding-status').then((status) => {
    if (request !== settingsRequest) return;
    state.data.embedding_status = status;
    renderIndexBanner(status);
  }).catch((error) => {
    if (request !== settingsRequest) return;
    $('index-banner').append(node('div', error.message, 'index-banner-error'));
  });
  renderRoles(data);
  renderTuning(data);
  renderVoices(data, state.voiceCatalog);
  loadReleases().catch((error) => showRelease(MindexI18n.system(error.message), true));
  loadLocalServices().catch((error) => showServices(MindexI18n.system(error.message), true));
  loadAnimationBrowser().catch((error) => showBrowser(MindexI18n.system(error.message), true));
  // An unavailable TTS catalog must not block model settings or recall tuning.
  api('/api/settings/voices/catalog').then((catalog) => {
    if (request !== settingsRequest) return;
    state.voiceCatalog = catalog;
    renderVoices(data, catalog);
  }).catch((error) => {
    // 音色目录不可用不阻塞其它设置，但要告诉用户，而不是静默吞掉。
    if (request === settingsRequest) notify(error.message, true);
  });
}

// ---- 索引版本：检索组合 = 内容索引 + 人物快照 ----
// 创作召回版本的签名记录并比对「内容索引 ID」与「检索发布版 ID」；
// 这里的「当前生效」与它们读取同一来源（GET /api/persons/releases 的 active）。
let releasesRequest = 0;
const shortId = (id, empty) => (id ? id.slice(0, 8) : empty);
function showRelease(text, error = false) {
  const status = $('release-notice');
  MindexI18n.setText(status, text);
  status.className = `notice ${error ? 'error' : 'info'}`;
}
async function releaseAction(control, task, message) {
  control.disabled = true;
  showRelease(MindexI18n.msg('ui.person_release_busy'));
  try {await task();showRelease(message);}
  catch (error) {showRelease(MindexI18n.system(error.message), true);}
  finally {control.disabled = false;}
}
// 发布、恢复、删除后：刷新列表，并同步顶部「嵌入与索引是否匹配」横幅。
async function refreshReleases() {
  await loadReleases();
  api('/api/settings/embedding-status').then(renderIndexBanner).catch((error) => {
    $('index-banner').replaceChildren(node('div', error.message, 'index-banner-error'));
  });
}
function releaseButton(text, task, message, className = 'quiet') {
  const b = node('button', text, className);
  b.type = 'button';
  b.onclick = () => releaseAction(b, task, message);
  return b;
}
async function loadReleases() {
  const request = ++releasesRequest;
  const data = await api('/api/persons/releases');
  if (request !== releasesRequest) return;
  renderReleases(data);
}
function renderReleases(data) {
  const active = data.active;
  const unbound = MindexI18n.msg('ui.index_release_unbound');
  const unpublished = MindexI18n.msg('ui.5a65ae692a28');
  const current = $('release-current');
  current.classList.toggle('is-empty', !active);
  if (active) {
    const params = (full) => ({
      p0: full ? active.release_id || unbound : shortId(active.release_id, unbound),
      p1: full ? active.content_id : shortId(active.content_id),
      p2: full ? active.person_id || unpublished : shortId(active.person_id, unpublished),
    });
    MindexI18n.setText(current, MindexI18n.msg('ui.index_release_current', params(false)));
    MindexI18n.setAttr(current, 'title', MindexI18n.msg('ui.index_release_current', params(true)));
    MindexI18n.setText($('releases-summary'), MindexI18n.msg('ui.index_release_summary', { p0: shortId(active.release_id, unbound) }));
  } else {
    MindexI18n.setText(current, MindexI18n.msg('ui.index_release_none'));
    current.removeAttribute('title');
    MindexI18n.setText($('releases-summary'), MindexI18n.msg('ui.index_release_none'));
  }
  const list = $('releases');
  list.replaceChildren();
  if (!data.releases.length) list.append(node('li', MindexI18n.msg('ui.index_release_empty'), 'muted'));
  for (const release of data.releases) {
    const isActive = release.id === active?.release_id;
    const row = node('li', undefined, `release-item${isActive ? ' is-active' : ''}`);
    const text = node('span', MindexI18n.msg('ui.index_release_item', {
      p0: release.created_at,
      p1: shortId(release.id),
      p2: shortId(release.content_id),
      p3: shortId(release.person_id, unpublished),
    }), 'release-text');
    MindexI18n.setAttr(text, 'title', MindexI18n.msg('ui.index_release_item', {
      p0: release.created_at, p1: release.id, p2: release.content_id, p3: release.person_id || unpublished,
    }));
    row.append(text);
    if (isActive) row.append(node('span', MindexI18n.msg('ui.index_release_active'), 'release-badge'));
    else {
      row.append(releaseButton(MindexI18n.msg('ui.9abe6a920d15'), async () => {
        await api(`/api/persons/releases/${release.id}/activate`, { method: 'POST' });
        await refreshReleases();
      }, MindexI18n.msg('ui.ce106faa390a')));
    }
    row.append(releaseButton(MindexI18n.msg('ui.3755f56f2f83'), async () => {
      await api(`/api/persons/releases/${release.id}`, { method: 'DELETE' });
      await refreshReleases();
    }, MindexI18n.msg('ui.69d484db9977'), 'quiet danger'));
    list.append(row);
  }
}
$('publish').onclick = () => releaseAction($('publish'), async () => {
  await api('/api/persons/publish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  await refreshReleases();
}, MindexI18n.msg('ui.person_release_complete'));

// ---- 本机服务：启停配置文件 local_services 中定义的本机模型服务（后端 settings/local_services.py） ----
// 只有启停、日志与两个开关；命令、端口、工作目录只能在配置文件里改。
let servicesRequest = 0;
let servicesTimer = null;
const servicesLogs = new Set();
function showServices(text, error = false) {
  const status = $('services-notice');
  MindexI18n.setText(status, text);
  status.className = `notice ${error ? 'error' : 'info'}`;
}
async function serviceAction(control, task, message) {
  control.disabled = true;
  try {await task();if (message) showServices(message);}
  catch (error) {showServices(MindexI18n.system(error.message), true);}
  finally {control.disabled = false;}
}
async function loadLocalServices() {
  const request = ++servicesRequest;
  const data = await api('/api/settings/local-services');
  if (request !== servicesRequest) return;
  renderLocalServices(data || {});
  // 有服务正在启动（进程在、健康检查未通过）时每 3 秒刷新，直到健康或退出。
  if (servicesTimer) clearTimeout(servicesTimer);
  servicesTimer = null;
  if ((data?.items || []).some((item) => item.running && !item.healthy) && typeof setTimeout === 'function')
    servicesTimer = setTimeout(() => loadLocalServices().catch(() => {}), 3000);
}
function serviceBadge(text, tone) {
  return node('span', text, `service-badge${tone ? ` ${tone}` : ''}`);
}
function serviceFlag(item, key, label) {
  const wrap = node('label');
  const box = node('input');
  box.type = 'checkbox';
  box.checked = Boolean(item[key]);
  box.onchange = () => serviceAction(box, async () => {
    await api(`/api/settings/local-services/${encodeURIComponent(item.name)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [key]: box.checked }),
    });
    await loadLocalServices();
  });
  wrap.append(box, node('span', label));
  return wrap;
}
function renderLocalServices(data) {
  const list = $('services');
  list.replaceChildren();
  const items = data.items || [];
  if (!data.enabled) {
    MindexI18n.setText($('services-summary'), MindexI18n.msg('ui.local_services_summary_off'));
    list.append(node('li', MindexI18n.msg('ui.local_services_disabled'), 'muted'));
    return;
  }
  MindexI18n.setText($('services-summary'), MindexI18n.msg('ui.local_services_summary', {
    p0: items.length, p1: items.filter((item) => item.running || item.external).length,
  }));
  if (!items.length) list.append(node('li', MindexI18n.msg('ui.local_services_empty'), 'muted'));
  for (const item of items) {
    const row = node('li', undefined, `release-item service-item${item.running && item.healthy ? ' is-active' : ''}`);
    row.dataset.service = item.name;
    const head = node('div', undefined, 'service-head');
    head.append(node('strong', item.label || item.name));
    head.append(node('span', MindexI18n.msg('ui.local_service_meta', {
      p0: item.port, p1: item.role || item.name, p2: item.pid ?? '—',
    }), 'muted'));
    const badges = node('div', undefined, 'service-badges');
    badges.append(
      item.running ? serviceBadge(MindexI18n.msg('ui.local_service_running'), 'ok') :
      item.external ? serviceBadge(MindexI18n.msg('ui.local_service_external'), 'warn') :
      serviceBadge(MindexI18n.msg('ui.local_service_stopped')),
      item.healthy ? serviceBadge(MindexI18n.msg('ui.local_service_healthy'), 'ok') :
      serviceBadge(MindexI18n.msg('ui.local_service_unhealthy'), item.running ? 'warn' : ''),
    );
    head.append(badges);
    row.append(head);
    const name = encodeURIComponent(item.name);
    if (item.running) {
      const stop = node('button', MindexI18n.msg('ui.local_service_stop'), 'quiet danger');
      stop.type = 'button';
      stop.onclick = () => serviceAction(stop, async () => {
        await api(`/api/settings/local-services/${name}/stop`, { method: 'POST' });
        await loadLocalServices();
      }, MindexI18n.msg('ui.local_service_stopped_notice', { p0: item.label || item.name }));
      row.append(stop);
    } else if (!item.external) {
      const start = node('button', MindexI18n.msg('ui.local_service_start'), 'quiet');
      start.type = 'button';
      start.onclick = () => serviceAction(start, async () => {
        showServices(MindexI18n.msg('ui.local_service_starting', { p0: item.label || item.name }));
        await api(`/api/settings/local-services/${name}/start`, { method: 'POST' });
        await loadLocalServices();
      }, MindexI18n.msg('ui.local_service_started', { p0: item.label || item.name }));
      row.append(start);
    }
    const open = servicesLogs.has(item.name);
    const logs = node('button', MindexI18n.msg(open ? 'ui.local_service_hide_logs' : 'ui.local_service_logs'), 'quiet');
    logs.type = 'button';
    logs.setAttribute('aria-expanded', String(open));
    logs.onclick = () => serviceAction(logs, async () => {
      if (servicesLogs.has(item.name)) servicesLogs.delete(item.name);else servicesLogs.add(item.name);
      await loadLocalServices();
    });
    row.append(logs);
    if (item.detail) row.append(node('div', MindexI18n.system(item.detail), 'service-detail'));
    const flags = node('div', undefined, 'service-flags');
    flags.append(serviceFlag(item, 'autostart', MindexI18n.msg('ui.local_service_autostart')),
      serviceFlag(item, 'stop_on_exit', MindexI18n.msg('ui.local_service_stop_on_exit')));
    row.append(flags);
    if (open) {
      const pre = node('pre', MindexI18n.msg('ui.local_service_no_logs'), 'service-log');
      row.append(pre);
      api(`/api/settings/local-services/${name}/logs?tail=200`).then((result) => {
        MindexI18n.setText(pre, result?.text || MindexI18n.msg('ui.local_service_no_logs'));
      }).catch((error) => MindexI18n.setText(pre, MindexI18n.system(error.message)));
    }
    list.append(row);
  }
}
$('services-refresh').onclick = () => serviceAction($('services-refresh'), loadLocalServices);

// ---- AI 动画录制组件：Playwright Chromium headless shell 的检测与一键安装（后端 settings/browser_install.py） ----
let browserRequest = 0;
let browserTimer = null;
function showBrowser(text, error = false) {
  const status = $('browser-notice');
  MindexI18n.setText(status, text);
  status.className = `notice ${error ? 'error' : 'info'}`;
}
async function loadAnimationBrowser() {
  const request = ++browserRequest;
  const data = await api('/api/settings/animation-browser');
  if (request !== browserRequest) return;
  renderAnimationBrowser(data || {});
  if (browserTimer) clearTimeout(browserTimer);
  browserTimer = null;
  if (data?.task?.status === 'running' && typeof setTimeout === 'function')
    browserTimer = setTimeout(() => loadAnimationBrowser().catch(() => {}), 2000);
}
function renderAnimationBrowser(data) {
  const task = data.task || {};
  const running = task.status === 'running';
  const stateKey = !data.available ? 'ui.animation_browser_unavailable' : running ? 'ui.animation_browser_installing' :
    data.installed ? 'ui.animation_browser_installed' : 'ui.animation_browser_missing';
  MindexI18n.setText($('browser-state'), MindexI18n.msg(stateKey, { p0: data.revision || '' }));
  MindexI18n.setText($('browser-summary'), MindexI18n.msg(stateKey, { p0: data.revision || '' }));
  MindexI18n.setText($('browser-path'), data.path ? MindexI18n.msg('ui.animation_browser_path', { p0: data.path }) : '');
  const install = $('browser-install');
  install.hidden = !data.available || Boolean(data.installed && !running);
  install.disabled = running;
  const log = $('browser-log');
  log.hidden = !task.log;
  MindexI18n.setText(log, task.log || '');
  if (task.status === 'failed') showBrowser(MindexI18n.system(task.error || ''), true);
  else if (task.status === 'complete' && data.installed) showBrowser(MindexI18n.msg('ui.animation_browser_done'));
}
$('browser-install').onclick = () => run(async () => {
  $('browser-install').disabled = true;
  try {
    await api('/api/settings/animation-browser/install', { method: 'POST' });
    showBrowser(MindexI18n.msg('ui.animation_browser_started'));
    await loadAnimationBrowser();
  } catch (error) {
    showBrowser(MindexI18n.system(error.message), true);
    $('browser-install').disabled = false;
  }
});

async function activateProfile(profileId) {
  const result = await api(`/api/settings/profiles/${profileId}/activate`, { method: 'POST' });
  if (result.index_warning) notify(MindexI18n.system(result.index_warning), true);else
  notify(MindexI18n.msg("ui.3222aa90a764", { p0: result.name }));
  await loadSettings();
}

async function testProfile(profileId) {
  state.tests[profileId] = { ok: true, latency_ms: '…' };
  const result = await api(`/api/settings/profiles/${profileId}/test`, { method: 'POST' });
  state.tests[profileId] = result;
  await loadSettings();
}

async function deleteProfile(profile) {
  if (!confirm(MindexI18n.render(MindexI18n.msg("ui.c07e9d3fe6f2", { p0: profile.name })))) return;
  await api(`/api/settings/profiles/${profile.id}`, { method: 'DELETE' });
  notify(MindexI18n.msg("ui.71b63fe4dc89"));
  await loadSettings();
}

function fillBackendOptions(role, selected) {
  const select = $('pf-backend');
  select.replaceChildren(...ROLE_BACKENDS[role].map((value) => {
    const option = node('option', value);
    option.value = value;
    return option;
  }));
  if (selected) select.value = selected;
}
function fillExtraFields(role, values = {}) {
  const wrap = $('pf-extra');
  wrap.replaceChildren();
  ROLE_EXTRA[role].forEach((field) => {
    const label = node('label');
    if (field.type === 'checkbox') {
      // 复选框沿用 label.check 样式：框在前、标题与说明在后。
      label.className = 'check';
      const input = node('input');
      input.type = 'checkbox';
      input.id = `pf-${field.key}`;
      input.checked = Boolean(values[field.key]);
      label.append(input, node('span', field.label));
      if (field.hint) label.append(node('span', field.hint, 'muted'));
      wrap.append(label);
      return;
    }
    label.append(node('span', field.label));
    let input;
    if (field.type === 'select') {
      input = node('select');
      field.options.forEach((value) => {
        const option = node('option', value);
        option.value = value;
        input.append(option);
      });
    } else {
      input = node('input');
      input.type = field.type === 'json' || field.type === 'list' ? 'text' : field.type;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      if (field.type === 'number') input.step = field.step || 'any';
      if (field.placeholder) MindexI18n.setAttr(input, "placeholder", field.placeholder);
    }
    input.id = `pf-${field.key}`;
    if (values[field.key] !== undefined && values[field.key] !== null) input.value = field.type === 'json' ? JSON.stringify(values[field.key]) :
    field.type === 'list' ? values[field.key].join(', ') : values[field.key];
    label.append(input);
    wrap.append(label);
  });
  fillComfyEditor(role, values);
}

// ComfyUI 工作流导入与节点映射（只在视频生成角色的 comfyui 后端显示）。映射字段与后端 shared/comfyui.py 一致。
const COMFY_FIELDS = {
  video_generator: [
  ['prompt', "ui.comfyui_field_prompt"], ['negative', "ui.comfyui_field_negative"], ['seed', "ui.comfyui_field_seed"],
  ['frames', "ui.comfyui_field_frames"], ['width', "ui.comfyui_field_width"], ['height', "ui.comfyui_field_height"],
  ['image', "ui.comfyui_field_image"]],
  image_generator: [
  ['prompt', "ui.comfyui_field_prompt"], ['negative', "ui.comfyui_field_negative"], ['seed', "ui.comfyui_field_seed"],
  ['width', "ui.comfyui_field_width"], ['height', "ui.comfyui_field_height"], ['image', "ui.comfyui_field_subject"]],
  music_generator: [
  ['prompt', "ui.comfyui_field_music_prompt"], ['lyrics', "ui.comfyui_field_lyrics"], ['seconds', "ui.comfyui_field_seconds"],
  ['seed', "ui.comfyui_field_seed"], ['audio', "ui.comfyui_field_audio"]]
};

function fillComfyEditor(role, values = {}) {
  const host = $('pf-comfyui');
  host.replaceChildren();
  host.hidden = !COMFY_ROLES.includes(role);
  if (host.hidden) {
    $('pf-model').required = true;
    return;
  }
  host.append(node('h3', MindexI18n.msg("ui.comfyui_workflow_title")));
  host.append(node('p', MindexI18n.msg("ui.comfyui_workflow_hint"), 'muted'));
  const area = node('textarea');
  area.id = 'pf-comfyui-workflow';
  area.rows = 6;
  area.spellcheck = false;
  MindexI18n.setAttr(area, 'aria-label', MindexI18n.msg("ui.comfyui_workflow_title"));
  MindexI18n.setAttr(area, 'placeholder', MindexI18n.msg("ui.comfyui_workflow_placeholder"));
  if (values.comfyui_workflow) area.value = JSON.stringify(values.comfyui_workflow, null, 2);
  const fileLabel = node('label', undefined);
  fileLabel.append(node('span', MindexI18n.msg("ui.comfyui_workflow_file")));
  const file = node('input');
  file.type = 'file';
  file.accept = '.json,application/json';
  file.id = 'pf-comfyui-file';
  file.onchange = async () => {
    const chosen = file.files[0];
    if (!chosen) return;
    area.value = await chosen.text();
    const status = $('pf-comfyui-status');
    try {
      const workflow = parseWorkflow(area.value);
      MindexI18n.setText(status, MindexI18n.msg("ui.comfyui_workflow_loaded", { p0: Object.keys(workflow).length }));
      status.className = 'muted';
    } catch (error) {
      MindexI18n.setText(status, error.message);
      status.className = 'warn-text';
    }
  };
  fileLabel.append(file);
  const status = node('p', '', 'muted');
  status.id = 'pf-comfyui-status';
  status.setAttribute('role', 'status');
  host.append(area, fileLabel, status);
  host.append(node('h3', MindexI18n.msg("ui.comfyui_mapping_title")));
  const grid = node('div', undefined, 'form-grid');
  const mapping = values.comfyui_mapping || {};
  for (const [key, label] of COMFY_FIELDS[role]) {
    const row = node('label');
    row.append(node('span', `${MindexI18n.msg(label)} · ${key}`));
    const input = node('input');
    input.id = `pf-comfyui-map-${key}`;
    input.dataset.mappingField = key;
    MindexI18n.setAttr(input, 'placeholder', MindexI18n.msg("ui.comfyui_mapping_placeholder"));
    input.value = mapping[key] || '';
    row.append(input);
    grid.append(row);
  }
  host.append(grid);
  syncComfyEditor();
}
function syncComfyEditor() {
  const host = $('pf-comfyui');
  if (!COMFY_ROLES.includes($('pf-role').value)) return;
  const comfy = $('pf-backend').value === 'comfyui';
  host.hidden = !comfy;
  // 模型名对 ComfyUI 与 MiniMax H3 可选（以工作流/分镜设置为准），其余线上厂商必填。
  $('pf-model').required = !comfy && $('pf-backend').value !== 'minimax_h3';
}
$('pf-backend').addEventListener('change', () => syncComfyEditor());

// 客户端预校验：与后端校验同口径，失败时给出中文/英文提示，服务端仍会再次校验。
function parseWorkflow(text) {
  let workflow;
  try {workflow = JSON.parse(text);} catch (error) {
    throw new Error(MindexI18n.msg("ui.comfyui_invalid_json", { p0: error.message }));
  }
  if (!workflow || typeof workflow !== 'object' || Array.isArray(workflow) || !Object.keys(workflow).length)
  throw new Error(MindexI18n.msg("ui.comfyui_invalid_json", { p0: '{}' }));
  if ('nodes' in workflow && 'links' in workflow) throw new Error(MindexI18n.msg("ui.comfyui_ui_format"));
  for (const [id, item] of Object.entries(workflow)) {
    if (!item || typeof item.class_type !== 'string' || !item.inputs || typeof item.inputs !== 'object')
    throw new Error(MindexI18n.msg("ui.comfyui_not_api", { p0: id }));
  }
  return workflow;
}
function comfyConfig() {
  const text = $('pf-comfyui-workflow').value.trim();
  const mapping = {};
  for (const input of document.querySelectorAll('#pf-comfyui [data-mapping-field]')) {
    const value = input.value.trim();
    if (value) mapping[input.dataset.mappingField] = value;
  }
  if (!text) throw new Error(MindexI18n.msg("ui.comfyui_workflow_required"));
  const workflow = parseWorkflow(text);
  if (!mapping.prompt) throw new Error(MindexI18n.msg("ui.comfyui_mapping_prompt_required"));
  for (const [key, target] of Object.entries(mapping)) {
    const parts = target.split('.');
    if (parts.length !== 3 || parts[1] !== 'inputs' || !parts[0] || !parts[2])
    throw new Error(MindexI18n.msg("ui.comfyui_mapping_format", { p0: key }));
    if (!workflow[parts[0]]) throw new Error(MindexI18n.msg("ui.comfyui_mapping_node", { p0: key, p1: parts[0] }));
    if (!(parts[2] in workflow[parts[0]].inputs))
    throw new Error(MindexI18n.msg("ui.comfyui_mapping_input", { p0: key, p1: parts[0], p2: parts[2] }));
  }
  return { comfyui_workflow: workflow, comfyui_mapping: mapping };
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
  if (!current) return { element: null, previous: '' };
  const previous = current.value;
  if (current.tagName.toLowerCase() === tag) return { element: current, previous };
  const replacement = document.createElement(tag);
  replacement.id = 'pf-voice';
  current.replaceWith(replacement);
  return { element: replacement, previous };
}

async function refreshVoiceCandidates() {
  if ($('pf-role').value !== 'tts') return;
  const body = {};
  if ($('pf-id').value) body.profile_id = Number($('pf-id').value);else
  {
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
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
  } catch {
    catalog = null; // 查询失败时保留自由输入
  }
  const kind = catalog?.voice_kind === 'clone' ? 'clone' : 'system';
  if (kind === 'clone') {
    const { element: select, previous: previousClone } = ensureVoiceField('select');
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
      if (![...select.options].some((option) => option.value === previousClone)) select.value = '';
    } else {
      const empty = node('option', catalog?.hint || MindexI18n.msg("ui.ecd04f151041"));
      empty.value = '';
      select.append(empty);
      select.value = '';
    }
    return;
  }
  const groups = catalog?.groups || [];
  if (!groups.length) {
    // 目录拉取失败且无静态兜底：退回自由输入，保证档案仍可保存
    const { element: fallback, previous: fallbackValue } = ensureVoiceField('input');
    if (fallback) {
      fallback.removeAttribute('list');
      fallback.value = fallbackValue;
      MindexI18n.setAttr(fallback, "placeholder", MindexI18n.msg("ui.5de84896cf73"));
    }
    return;
  }
  const { element: select, previous } = ensureVoiceField('select');
  if (!select) return;
  select.removeAttribute('list');
  select.dataset.voiceKind = 'system';
  select.replaceChildren();
  const unselected = node('option', MindexI18n.msg("ui.60399713b37d"));
  unselected.value = '';
  select.append(unselected);
  for (const group of groups) {
    if (!group.voices.length) continue;
    const optgroup = document.createElement('optgroup');
    MindexI18n.setAttr(optgroup, "label", MindexI18n.builtin(group.label));
    for (const voice of group.voices) {
      const option = node('option', MindexI18n.voice(voice));
      option.value = voice.voice_id;
      optgroup.append(option);
    }
    select.append(optgroup);
  }
  if ((catalog?.library || []).length) {
    const mine = document.createElement('optgroup');
    MindexI18n.setAttr(mine, "label", MindexI18n.msg("ui.6ec5494286de"));
    for (const item of catalog.library) {
      const option = node('option', `${item.kind === 'clone' ? MindexI18n.msg("ui.6e60458a4727") : MindexI18n.msg("ui.4512b3b0e457")} · ${item.name}`);
      option.value = item.voice_id;
      mine.append(option);
    }
    select.append(mine);
  }
  select.value = [...select.options].some((option) => option.value === previous) ?
  previous :
  ''; // 当前值不在目录中时回落到「跟随默认」，避免浏览器静默选中第一项造成误改
}

function openDialog(role, profile) {
  const data = state.data;
  $('pf-role').value = role;
  $('pf-id').value = profile ? profile.id : '';
  MindexI18n.setText($('profile-title'), `${MindexI18n.builtin((data.roles.find((r) => r.role === role) || {}).label) || role} · ${profile ? MindexI18n.msg("ui.8c59a69b6752") : MindexI18n.msg("ui.88b10e4a74d9")}`);
  MindexI18n.setText($('pf-error'), '');
  $('pf-api-key').value = '';
  const providerSelect = $('pf-provider');
  providerSelect.replaceChildren(...Object.entries(data.providers).map(([value, label]) => {
    const option = node('option', MindexI18n.builtin(label));
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
  const useTemplate = role === 'analyzer' || role === 'generator' || role === 'writer';
  $('pf-template-wrap').hidden = !useTemplate;
  if (useTemplate) $('pf-llama-template').checked = profile ? config.llama_template !== false : true;
  MindexI18n.setText($('pf-key-hint'), profile && profile.api_key.has_key ? MindexI18n.msg("ui.d1b7dd066ef7", { p0:
    profile.api_key.hint }) : MindexI18n.msg("ui.4f0df10ff5f1"));
  $('pf-clear-key-wrap').hidden = !(profile && profile.api_key.has_key);
  $('pf-clear-key').checked = false;
  refreshVoiceCandidates();
  return $('profile-dialog').showModal();
}

$('profile-form').onsubmit = async (event) => {
  event.preventDefault();
  const role = $('pf-role').value;
  const profileId = $('pf-id').value;
  const config = {
    backend: $('pf-backend').value,
    base_url: $('pf-base-url').value.trim(),
    model: $('pf-model').value.trim()
  };
  const revision = $('pf-revision').value.trim();
  if (revision && revision !== 'main') config.revision = revision;
  if (role === 'analyzer' || role === 'generator' || role === 'writer') config.llama_template = $('pf-llama-template').checked;
  ROLE_EXTRA[role].forEach((field) => {
    const input = $(`pf-${field.key}`);
    if (!input) return;
    if (field.type === 'checkbox') {
      config[field.key] = input.checked;
      return;
    }
    const value = input.value.trim();
    if (field.type === 'list') {
      if (value !== '') config[field.key] = value.split(/[,，\s]+/).filter(Boolean).map(Number);
      return;
    }
    if (value !== '') config[field.key] = field.type === 'number' ? Number(value) : value;
    if (field.type === 'json') {
      if (value === '') config[field.key] = null;else
      {try {config[field.key] = JSON.parse(value);} catch {config[field.key] = value;}}
    }
  });
  if (COMFY_ROLES.includes(role)) {
    if ((config.durations || []).some((value) => !Number.isFinite(value) || value <= 0)) {
      MindexI18n.setText($('pf-error'), MindexI18n.msg("ui.video_durations_invalid"));
      return;
    }
    try {
      Object.assign(config, config.backend === 'comfyui' ? comfyConfig() : { comfyui_workflow: null, comfyui_mapping: null });
    } catch (error) {
      MindexI18n.setText($('pf-error'), error.message);
      return;
    }
  }
  const apiKey = $('pf-api-key').value;
  $('pf-save').disabled = true;
  MindexI18n.setText($('pf-error'), '');
  if (role === 'tts' && $('pf-voice')?.dataset?.voiceKind === 'clone' && !$('pf-voice').value) {
    $('pf-save').disabled = false;
    MindexI18n.setText($('pf-error'), MindexI18n.msg("ui.9cef851d2fdf"));
    return;
  }
  try {
    const body = { role, name: $('pf-name').value.trim(), provider: $('pf-provider').value, config };
    if (apiKey !== '') body.api_key = apiKey;
    if (profileId && $('pf-clear-key')?.checked) body.api_key = '__clear__';
    await api(profileId ? `/api/settings/profiles/${profileId}` : '/api/settings/profiles', {
      method: profileId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    $('profile-dialog').close();
    notify(MindexI18n.msg("ui.4e5bda7d504f"));
    await loadSettings();
  } catch (e) {
    MindexI18n.setText($('pf-error'), e.message);
  } finally {
    $('pf-save').disabled = false;
  }
};
$('profile-close').onclick = () => $('profile-dialog').close();

$('search-tuning').onsubmit = async (event) => {
  event.preventDefault();
  try {
    const weights = {};
    for (const key of Object.keys(WEIGHT_LABELS)) {
      const value = Number($(`weight-${key}`).value);
      if (!Number.isFinite(value) || value < 0) throw new Error(MindexI18n.msg("ui.e834ef69f5b6", { p0: WEIGHT_LABELS[key] }));
      weights[key] = value;
    }
    const channel_weights = {};
    for (const key of ['visual', 'dialogue', 'analysis']) {
      const value = Number($(`channel-weight-${key}`).value);
      if (!Number.isFinite(value) || value < 0) throw new Error(MindexI18n.msg("ui.5dffe4819a2e"));
      channel_weights[key] = value;
    }
    await api('/api/settings/business', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search: { weights, channel_weights, rrf_k: Number($('rrf-k').value), candidates: Number($('candidates').value) } })
    });
    notify(MindexI18n.msg("ui.df0f016c336e"));
    await loadSettings();
  } catch (e) {
    notify(e.message, true);
  }
};
$('enforce-gpu-lock').onchange = async (event) => {
  const box = event.currentTarget;
  box.disabled = true;
  try {
    await api('/api/settings/business', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enforce_gpu_lock: box.checked })
    });
    notify(MindexI18n.msg("ui.7a13ca5bd7fc"));
    await loadSettings();
  } catch (e) {notify(e.message, true);} finally
  {box.disabled = false;}
};
$('tuning-reset').onclick = () => run(async () => {
  await api('/api/settings/business', { method: 'DELETE' });
  notify(MindexI18n.msg("ui.3cefe3ad3a1c"));
  await loadSettings();
});
$('reload').onclick = () => run(async () => {
  $('reload').disabled = true;
  try {await loadSettings();notify(MindexI18n.msg("ui.7109661933dc"));} finally {$('reload').disabled = false;}
});

for (const head of document.querySelectorAll('.role-row-head[data-section]')) {
  head.onclick = () => toggleRole(head.dataset.section);
}
if (typeof location !== 'undefined' && /^#(services|browser)$/.test(location.hash || '')) {
  state.expanded.add(location.hash.slice(1));
}
syncSectionRows();
run(loadSettings);
// 智能助手修改设置（档案、业务调参）后刷新本页，并定位到相关角色、档案或区块。
// 控制器单测在没有 window 的沙箱里运行本文件，因此需要守卫。
if (typeof window !== 'undefined' && window.addEventListener) {
  let agentReload = Promise.resolve();
  window.MindexAgentContext = () => ({ page: 'settings', expanded_roles: [...state.expanded] });
  window.addEventListener('mindex:agent-mutation', (event) => {
    if ((event.detail || {}).domain === 'settings') agentReload = run(loadSettings);
  });
  const persist = () => {
    try {localStorage.setItem(EXPANDED_KEY, JSON.stringify([...state.expanded]));} catch {}
  };
  window.MindexAgentFocus = async (focus, { passive = false } = {}) => {
    if (!state.data) return false;
    await agentReload;
    const mark = (element, options) => window.MindexAgent?.highlight(element, options);
    if (focus.role) {
      if (!passive && !state.expanded.has(focus.role)) {
        state.expanded.add(focus.role);
        persist();
        renderRoles(state.data);
      }
      const row = document.querySelector(`.role-row[data-role="${CSS.escape(focus.role)}"]`);
      const card = focus.profile_id != null ?
        document.querySelector(`.profile-item[data-profile-id="${CSS.escape(String(focus.profile_id))}"]`) : null;
      mark(row, { scroll: !card });
      if (card) mark(card);
      return true;
    }
    const section = focus.section;
    if (['tuning', 'voices', 'releases', 'services', 'browser'].includes(section)) {
      if (!passive) {
        state.expanded.add(section);
        persist();
        syncSectionRows();
      }
      mark($(`${section}-row`));
    } else if (section === 'processing') mark($('processing-settings'));
    else mark($('roles'));
    return true;
  };
}
