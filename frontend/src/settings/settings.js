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

const ROLE_ORDER = ['analyzer', 'generator', 'embedding', 'asr', 'tts', 'person'];
const ROLE_DESC = {
  person: MindexI18n.msg("ui.907c176fe85a"),
  analyzer: MindexI18n.msg("ui.85feed61e44f"),
  generator: MindexI18n.msg("ui.09b768c5ad0a"),
  embedding: MindexI18n.msg("ui.bd997040b2e2"),
  asr: MindexI18n.msg("ui.b74f293dd13e"),
  tts: MindexI18n.msg("ui.35cda4046011")
};
const ROLE_BACKENDS = {
  person: ['mindex'],
  analyzer: ['openai', 'llama_cpp'],
  generator: ['openai'],
  embedding: ['mindex', 'llama_cpp', 'tokenhub'],
  asr: ['mindex', 'llama_cpp', 'openai', 'minimax'],
  tts: ['openai', 'minimax', 'dashscope']
};
const ROLE_EXTRA = {
  person: [
  { key: 'expected_space', label: MindexI18n.msg("ui.7f5c519b5795"), type: 'json' },
  { key: 'fps', label: MindexI18n.msg("ui.958085c940ec"), type: 'number', min: 0.1, max: 5 },
  { key: 'face_threshold', label: MindexI18n.msg("ui.c7501d1b57d8"), type: 'number', min: 0.01 },
  { key: 'face_margin', label: MindexI18n.msg("ui.400a124b6162"), type: 'number', min: 0.01 },
  { key: 'min_face_quality', label: MindexI18n.msg("ui.9ced26663827"), type: 'number', min: 0, max: 1 },
  { key: 'min_reference_quality', label: MindexI18n.msg("ui.8ea4592494d3"), type: 'number', min: 0, max: 1 },
  { key: 'body_threshold', label: MindexI18n.msg("ui.83b62368c610"), type: 'number', min: 0.01 }],

  embedding: [
  { key: 'dimensions', label: MindexI18n.msg("ui.9733d2a5ddb4"), type: 'number', min: 1 },
  { key: 'quantization', label: MindexI18n.msg("ui.eb2b7bc048c6"), type: 'text', placeholder: MindexI18n.msg("ui.8814a2ac40dc") }],

  generator: [{ key: 'max_tokens', label: 'max_tokens', type: 'number', min: 100 }],
  tts: [
  { key: 'response_format', label: MindexI18n.msg("ui.df5363469d15"), type: 'select', options: ['wav', 'mp3'] },
  { key: 'voice', label: MindexI18n.msg("ui.9bbd54bf4bf3"), type: 'text', placeholder: MindexI18n.msg("ui.c5186b8d1b79") }],

  analyzer: [
  { key: 'thinking_mode', label: MindexI18n.msg("ui.69805dd4c1c2"), type: 'select', options: ['auto', 'low', 'provider'] },
  { key: 'response_retries', label: MindexI18n.msg("ui.2b4e456f4b30"), type: 'number', min: 0, max: 20 },
  { key: 'initial_output_tokens', label: MindexI18n.msg("ui.377c6dff15af"), type: 'number', min: 64, max: 131072 },
  { key: 'max_output_tokens', label: MindexI18n.msg("ui.ee7fe23f341f"), type: 'number', min: 64, max: 131072 }],

  asr: []
};
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
  MindexI18n.setText($('notice'), error ? MindexI18n.system(text) : text);
  $('notice').className = `notice ${error ? 'error' : 'info'}`;
}
function errorText(detail) {
  return Array.isArray(detail) ? detail.map((e) => e.msg).join('；') :
  typeof detail === 'string' ? detail : JSON.stringify(detail);
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(MindexI18n.system(data));
  return data;
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
  const head = node('button', undefined, 'role-row-head');
  head.type = 'button';
  head.setAttribute('aria-expanded', String(open));
  head.setAttribute('aria-controls', `role-body-${role}`);
  head.onclick = () => toggleRole(role);

  const effective = rolePayload.effective;
  const keyState = effective.api_key.has_key ? MindexI18n.msg("ui.bc24ebb490a2", { p0: effective.api_key.hint }) :
  effective.env_key_set ? MindexI18n.msg("ui.aa7487d83f4a", {}) : MindexI18n.msg("ui.a3e7368ec053");
  head.append(node('span', '▸', 'chevron'));
  head.append(node('strong', MindexI18n.builtin(rolePayload.label), 'role-row-title'));
  const summary = node('span', undefined, 'role-row-summary');
  summary.append(node('span', MindexI18n.msg("ui.61236d70e259", { p0: effective.model, p1: effective.backend, p2: effective.base_url }), 'role-row-effective'));
  summary.append(node('span', `${keyState}${effective.dimensions ? MindexI18n.msg("ui.d5b0619c7e95", { p0: effective.dimensions }) : ''}`, 'role-row-key'));
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
    const head2 = node('div', undefined, 'profile-head');
    head2.append(node('strong', profile.name));
    if (rolePayload.active_profile_id === profile.id) head2.append(node('span', MindexI18n.msg("ui.e6aa2cbd7b42"), 'active-badge'));
    head2.append(node('span', profile.provider, 'provider-chip'));
    item.append(head2);
    item.append(node('div', `${profile.config.model || ''} · ${profile.config.backend || ''} · ${profile.config.base_url || ''}`, 'profile-meta'));
    const keyHint = profile.api_key.has_key ? MindexI18n.msg("ui.bc24ebb490a2", { p0: profile.api_key.hint }) : MindexI18n.msg("ui.e80f2d522dd3");
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
  // An unavailable TTS catalog must not block model settings or recall tuning.
  api('/api/settings/voices/catalog').then((catalog) => {
    if (request !== settingsRequest) return;
    state.voiceCatalog = catalog;
    renderVoices(data, catalog);
  }).catch(() => {});
}

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
      input.type = field.type === 'json' ? 'text' : field.type;
      if (field.min !== undefined) input.min = field.min;
      if (field.max !== undefined) input.max = field.max;
      if (field.type === 'number') input.step = 'any';
      if (field.placeholder) MindexI18n.setAttr(input, "placeholder", field.placeholder);
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
  const useTemplate = role === 'analyzer' || role === 'generator';
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
  if (role === 'analyzer' || role === 'generator') config.llama_template = $('pf-llama-template').checked;
  ROLE_EXTRA[role].forEach((field) => {
    const input = $(`pf-${field.key}`);
    if (!input) return;
    const value = input.value.trim();
    if (value !== '') config[field.key] = field.type === 'number' ? Number(value) : value;
    if (field.type === 'json') {
      if (value === '') config[field.key] = null;else
      {try {config[field.key] = JSON.parse(value);} catch {config[field.key] = value;}}
    }
  });
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
$('processing-settings').onsubmit = async (event) => {
  event.preventDefault();
  $('processing-save').disabled = true;
  try {
    await api('/api/settings/business', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enforce_gpu_lock: $('enforce-gpu-lock').checked })
    });
    notify(MindexI18n.msg("ui.7a13ca5bd7fc"));
    await loadSettings();
  } catch (e) {notify(e.message, true);} finally
  {$('processing-save').disabled = false;}
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
syncSectionRows();
run(loadSettings);
