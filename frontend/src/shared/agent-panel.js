'use strict';

// 全局智能助手面板：右下角启动按钮 + 右侧抽屉。
// 会话事件经 SSE（/api/agent/sessions/{id}/events）推送，断线按最后 seq 续传。
// 页面同步：订阅全局通道 /api/agent/changes——mutation 让所有页面刷新；focus 让发起会话的标签页定位
// （同页立即定位，跨页在会话空闲或等待确认时跳转），其余来源只做页内被动高亮。
// 工具输出与用户消息用 textContent 渲染；助手回复经 MindexMarkdown（marked + DOMPurify 净化）渲染。
(() => {
  const STORAGE_KEY = 'mindex.agent.session';
  const OPEN_KEY = 'mindex.agent.open';
  const TAB_KEY = 'mindex.agent.tab';
  const FOCUS_KEY = 'mindex.agent.focus';
  const FOLLOW_KEY = 'mindex.agent.follow';
  const WIDTH_KEY = 'mindex.agent.width';
  const DEFAULT_WIDTH = 440, MIN_WIDTH = 320, WIDTH_STEP = 24;
  const PAGES = { '': 'search', 'index.html': 'search', studio: 'studio', settings: 'settings', admin: 'admin', persons: 'persons' };
  // 疑似密钥：常见前缀，或 32 位以上、含数字且含非十六进制字母的串（排除版本 ID 这类纯十六进制）。
  const KEY_PATTERN = /\b(?:sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}|(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[g-zG-Z])[A-Za-z0-9_-]{32,})\b/;
  const i18n = () => window.MindexI18n;
  const t = (key, params) => i18n() ? i18n().msg(key, params) : key;
  const state = {
    session: null, seq: -1, source: null, retry: null, status: 'idle', open: false, live: false,
    cards: new Map(), streams: new Map(), approvals: new Map(), questions: new Map(), builds: new Map(),
    feedSeq: null, feed: null, feedRetry: null, pendingNav: null, follow: true,
  };
  const TAB = (() => {
    try {
      let id = sessionStorage.getItem(TAB_KEY);
      if (!id) {
        id = 'tab-' + (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now());
        sessionStorage.setItem(TAB_KEY, id);
      }
      return id;
    } catch { return 'tab-' + Math.random().toString(36).slice(2); }
  })();
  try { state.follow = localStorage.getItem(FOLLOW_KEY) !== '0'; } catch {}
  function currentPage() {
    const segment = location.pathname.replace(/^\/+|\/+$/g, '').replace(/\.html$/, '').split('/')[0];
    return PAGES[segment] || PAGES[segment + '.html'] || segment || 'search';
  }

  function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) {
      if (i18n() && /\uE000z\d+\uE001/.test(String(text))) i18n().setText(n, text);
      else n.textContent = String(text);
    }
    return n;
  }
  function label(n, value) {
    if (i18n()) i18n().setAttr(n, 'aria-label', value); else n.setAttribute('aria-label', String(value));
  }
  function button(text, cls, onClick) {
    const b = node('button', cls || 'quiet', text);
    b.type = 'button';
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  async function api(url, method = 'GET', body) {
    const response = await fetch(url, {
      method,
      ...(body !== undefined ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    });
    if (response.status === 204) return {};
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(i18n() ? i18n().render(i18n().system(data)) : (data.detail || response.status));
    return data;
  }
  function pageContext() {
    let context = { page: currentPage() };
    try {
      if (typeof window.MindexAgentContext === 'function') context = { ...context, ...window.MindexAgentContext() };
    } catch {}
    return { ...context, tab_id: TAB };
  }

  // ------------------------------------------------------------ DOM
  const launcher = button(t('ui.agent_launcher'), 'agent-launcher');
  launcher.setAttribute('aria-controls', 'agent-drawer');
  launcher.setAttribute('aria-expanded', 'false');
  const drawer = node('aside', 'agent-drawer');
  drawer.id = 'agent-drawer';
  drawer.hidden = true;
  label(drawer, t('ui.agent_title'));
  const head = node('div', 'agent-head');
  const title = node('h2', 'agent-title', t('ui.agent_title'));
  const sessions = node('select', 'agent-sessions');
  label(sessions, t('ui.agent_sessions'));
  const newButton = button(t('ui.agent_new_session'), 'quiet', () => startSession().catch(fail));
  const deleteButton = button(t('ui.agent_delete_session'), 'quiet', () => removeSession().catch(fail));
  const close = button('✕', 'quiet agent-close', () => toggle(false));
  label(close, t('ui.agent_close'));
  head.append(title, sessions, newButton, deleteButton, close);
  const followLabel = node('label', 'agent-follow');
  const followBox = node('input');
  followBox.type = 'checkbox';
  followBox.checked = state.follow;
  followBox.addEventListener('change', () => {
    state.follow = followBox.checked;
    if (!state.follow) state.pendingNav = null;
    try { localStorage.setItem(FOLLOW_KEY, state.follow ? '1' : '0'); } catch {}
  });
  followLabel.append(followBox, node('span', '', t('ui.agent_follow')));
  if (i18n()) i18n().setAttr(followLabel, 'title', t('ui.agent_follow_hint'));
  head.append(followLabel);
  const statusLine = node('div', 'agent-status');
  statusLine.setAttribute('role', 'status');
  const log = node('div', 'agent-log');
  log.setAttribute('role', 'log');
  log.setAttribute('aria-live', 'polite');
  log.tabIndex = 0;
  const form = node('form', 'agent-composer');
  const input = node('textarea', 'agent-input');
  input.rows = 3;
  input.maxLength = 8000;
  label(input, t('ui.agent_input_label'));
  if (i18n()) i18n().setAttr(input, 'placeholder', t('ui.agent_placeholder'));
  const warning = node('p', 'agent-warning');
  warning.hidden = true;
  warning.setAttribute('role', 'alert');
  const actions = node('div', 'agent-actions');
  const send = button(t('ui.agent_send'), 'submit agent-send');
  send.type = 'submit';
  const cancel = button(t('ui.agent_cancel'), 'quiet', () => cancelTurn().catch(fail));
  cancel.hidden = true;
  actions.append(cancel, send);
  form.append(warning, input, actions);
  drawer.append(head, statusLine, log, form);

  // ------------------------------------------------------------ 可调宽度：左边缘拖拽手柄（鼠标、触屏、键盘）
  const resizer = node('div', 'agent-resize');
  resizer.tabIndex = 0;
  resizer.setAttribute('role', 'separator');
  resizer.setAttribute('aria-orientation', 'vertical');
  resizer.setAttribute('aria-controls', 'agent-drawer');
  resizer.setAttribute('aria-valuemin', String(MIN_WIDTH));
  label(resizer, t('ui.agent_resize'));
  if (i18n()) i18n().setAttr(resizer, 'title', t('ui.agent_resize_hint'));
  drawer.prepend(resizer);
  const maxWidth = () => Math.max(MIN_WIDTH, Math.floor(window.innerWidth * 0.8));
  let width = DEFAULT_WIDTH;
  function setWidth(value, persist = true) {
    width = Math.round(Math.min(maxWidth(), Math.max(MIN_WIDTH, Number(value) || DEFAULT_WIDTH)));
    document.documentElement.style.setProperty('--agent-width', `${width}px`);
    resizer.setAttribute('aria-valuenow', String(width));
    resizer.setAttribute('aria-valuemax', String(maxWidth()));
    if (persist) { try { localStorage.setItem(WIDTH_KEY, String(width)); } catch {} }
  }
  try { setWidth(localStorage.getItem(WIDTH_KEY) || DEFAULT_WIDTH, false); } catch { setWidth(DEFAULT_WIDTH, false); }
  resizer.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    drawer.classList.add('is-resizing');
    const move = (e) => setWidth(window.innerWidth - e.clientX, false);
    const stop = () => {
      resizer.removeEventListener('pointermove', move);
      drawer.classList.remove('is-resizing');
      setWidth(width);
    };
    resizer.addEventListener('pointermove', move);
    resizer.addEventListener('pointerup', stop, { once: true });
    resizer.addEventListener('pointercancel', stop, { once: true });
  });
  resizer.addEventListener('keydown', (event) => {
    const steps = { ArrowLeft: WIDTH_STEP, ArrowRight: -WIDTH_STEP };  // 手柄在左边缘：向左即加宽
    if (event.key in steps) { event.preventDefault(); setWidth(width + steps[event.key]); }
    else if (event.key === 'Home') { event.preventDefault(); setWidth(MIN_WIDTH); }
    else if (event.key === 'End') { event.preventDefault(); setWidth(maxWidth()); }
    else if (event.key === 'Enter') { event.preventDefault(); setWidth(DEFAULT_WIDTH); }
  });
  resizer.addEventListener('dblclick', () => setWidth(DEFAULT_WIDTH));
  window.addEventListener('resize', () => setWidth(width, false));

  function mount() {
    document.body.append(launcher, drawer, viewer);
    if (sessionStorage.getItem(OPEN_KEY) === '1') toggle(true);
    subscribeFeed();
    resumeFocus();
  }

  // ------------------------------------------------------------ 页面同步与定位
  function highlight(element, { scroll = true } = {}) {
    if (!element) return false;
    document.querySelectorAll('[data-agent-focus]').forEach((n) => { if (n !== element) delete n.dataset.agentFocus; });
    element.classList.remove('agent-focus');
    void element.offsetWidth; // 重新触发动画
    element.classList.add('agent-focus');
    element.dataset.agentFocus = 'true';
    clearTimeout(element.agentFocusTimer);
    element.agentFocusTimer = setTimeout(() => element.classList.remove('agent-focus'), 2400);
    if (scroll) element.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return true;
  }
  function clearHighlights() {
    document.querySelectorAll('[data-agent-focus]').forEach((n) => delete n.dataset.agentFocus);
  }
  async function applyFocus(focus, options = {}) {
    if (typeof window.MindexAgentFocus !== 'function') return true;
    try { return await window.MindexAgentFocus(focus, options); } catch (error) { console.warn(error); return true; }
  }
  function urlFor(focus) {
    if (focus.page === 'studio') return focus.job_id ? `/studio?job=${encodeURIComponent(focus.job_id)}` : '/studio';
    if (focus.page === 'search') return '/';
    return '/' + focus.page;
  }
  function navigateTo(focus) {
    state.pendingNav = null;
    try {
      sessionStorage.setItem(FOCUS_KEY, JSON.stringify(focus));
      sessionStorage.setItem(OPEN_KEY, '1');
    } catch {}
    location.href = urlFor(focus);
  }
  function resumeFocus() {
    let focus = null;
    try {
      focus = JSON.parse(sessionStorage.getItem(FOCUS_KEY) || 'null');
      sessionStorage.removeItem(FOCUS_KEY);
    } catch {}
    if (!focus || focus.page !== currentPage()) return;
    const started = Date.now();
    const attempt = async () => {
      if (await applyFocus(focus)) return;
      if (Date.now() - started < 15000) setTimeout(attempt, 200);
    };
    setTimeout(attempt, 0);
  }
  function queueFocus(change) {
    const focus = change.data;
    if (focus.page === currentPage()) {
      state.pendingNav = null;
      applyFocus(focus);
      return;
    }
    // 跨页定位：等发起会话空闲或等待确认时再跳转，避免一轮多次调用工具时页面来回跳。
    const settled = change.session_id !== state.session || state.status === 'idle' || state.status === 'waiting';
    if (settled) navigateTo(focus); else state.pendingNav = focus;
  }
  function onChange(change) {
    if (change.kind === 'mutation') {
      window.dispatchEvent(new CustomEvent('mindex:agent-mutation', { detail: { ...change.data, source: change.source } }));
      return;
    }
    if (change.kind !== 'focus' || !state.follow) return;
    if (change.tab === TAB) queueFocus(change);
    else if (change.data.page === currentPage()) applyFocus(change.data, { passive: true });
  }
  function subscribeFeed() {
    if (typeof EventSource === 'undefined') return;
    const after = state.feedSeq === null ? -1 : state.feedSeq;
    const source = new EventSource(`/api/agent/changes?after=${after}`);
    state.feed = source;
    source.addEventListener('ready', (message) => {
      if (state.feedSeq === null) state.feedSeq = JSON.parse(message.data).seq;
    });
    source.addEventListener('change', (message) => {
      const change = JSON.parse(message.data);
      if (state.feedSeq !== null && change.seq <= state.feedSeq) return;
      state.feedSeq = change.seq;
      onChange(change);
    });
    source.onerror = () => {
      source.close();
      clearTimeout(state.feedRetry);
      state.feedRetry = setTimeout(subscribeFeed, 1500);
    };
  }

  function toggle(open = !state.open) {
    state.open = open;
    if (!open) pauseAll();
    drawer.hidden = !open;
    launcher.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('agent-open', open);
    try { sessionStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch {}
    if (open) {
      loadSessions().catch(fail);
      input.focus();
    }
  }
  launcher.addEventListener('click', () => toggle());
  drawer.addEventListener('keydown', (event) => { if (event.key === 'Escape') toggle(false); });

  function fail(error) {
    const item = node('p', 'agent-error', error?.message || String(error));
    item.setAttribute('role', 'alert');
    log.append(item);
    scroll();
  }
  function scroll() { log.scrollTop = log.scrollHeight; }

  // ------------------------------------------------------------ 会话
  async function loadSessions(select) {
    const data = await api('/api/agent/sessions');
    sessions.replaceChildren();
    const placeholder = node('option', '', t('ui.agent_no_session'));
    placeholder.value = '';
    sessions.append(placeholder);
    for (const item of data.items) {
      const option = node('option', '', item.title || item.id);
      option.value = item.id;
      sessions.append(option);
    }
    let target = select || state.session || localStorage.getItem(STORAGE_KEY);
    if (!data.items.some((item) => item.id === target)) target = data.items[0]?.id || '';
    sessions.value = target;
    if (target && target !== state.session) connect(target);
    if (!target) reset(null);
  }
  sessions.addEventListener('change', () => {
    if (sessions.value) connect(sessions.value); else reset(null);
  });
  async function startSession() {
    const session = await api('/api/agent/sessions', 'POST', {});
    await loadSessions(session.id);
    return session.id;
  }
  async function removeSession() {
    if (!state.session) return;
    if (!window.confirm(i18n() ? i18n().render(t('ui.agent_delete_confirm')) : '删除此会话？')) return;
    await api(`/api/agent/sessions/${state.session}`, 'DELETE');
    localStorage.removeItem(STORAGE_KEY);
    reset(null);
    await loadSessions('');
  }
  function reset(id) {
    pauseAll();
    if (state.source) state.source.close();
    clearTimeout(state.retry);
    for (const timer of state.builds.values()) clearTimeout(timer.timer);
    Object.assign(state, { session: id, seq: -1, source: null, status: 'idle', live: false });
    state.cards.clear(); state.streams.clear(); state.approvals.clear(); state.questions.clear(); state.builds.clear();
    log.replaceChildren();
    if (!id) log.append(node('p', 'agent-empty', t('ui.agent_empty')));
    setStatus('idle');
  }
  function connect(id) {
    reset(id);
    try { localStorage.setItem(STORAGE_KEY, id); } catch {}
    subscribe();
  }
  function subscribe() {
    if (!state.session) return;
    const id = state.session;
    const source = new EventSource(`/api/agent/sessions/${id}/events?after=${state.seq}`);
    state.source = source;
    source.addEventListener('session.event', (message) => {
      if (state.session !== id) return;
      const event = JSON.parse(message.data);
      if (event.seq <= state.seq) return;
      state.seq = event.seq;
      apply(event);
    });
    source.addEventListener('assistant.stream', (message) => {
      if (state.session === id) stream(JSON.parse(message.data));
    });
    source.addEventListener('session.status', (message) => {
      if (state.session !== id) return;
      state.live = true; // 状态帧在补发的历史之后到达：此后才是实时事件
      setStatus(JSON.parse(message.data).status);
    });
    source.onerror = () => {
      source.close();
      if (state.session !== id) return;
      clearTimeout(state.retry);
      state.retry = setTimeout(subscribe, 1500); // 按最后 seq 续传
    };
  }

  function setStatus(status) {
    state.status = status;
    const labels = { idle: '', running: t('ui.agent_status_running'), waiting: t('ui.agent_status_waiting') };
    if (i18n()) i18n().setText(statusLine, labels[status] || ''); else statusLine.textContent = '';
    statusLine.dataset.status = status;
    cancel.hidden = status === 'idle';
    send.disabled = status !== 'idle';
    if (state.pendingNav && state.follow && (status === 'idle' || status === 'waiting')) navigateTo(state.pendingNav);
  }

  // ------------------------------------------------------------ 渲染
  function bubble(role, text, extra = '') {
    const item = node('div', `agent-message agent-${role} ${extra}`.trim());
    const body = node('div', 'agent-text', text);
    item.append(body);
    log.append(item);
    scroll();
    return item;
  }
  function markdown(target, text) {
    if (window.MindexMarkdown) window.MindexMarkdown.render(target, text); else target.textContent = text;
  }
  function streamKey(turn, step) { return `${turn}:${step}`; }
  function stream(data) {
    const key = streamKey(data.turn, data.step);
    let entry = state.streams.get(key);
    if (!entry) {
      entry = { item: bubble('assistant', '', 'is-streaming'), text: '' };
      state.streams.set(key, entry);
    }
    const chunk = data.chunk;
    if (chunk.type === 'text-delta') {
      entry.text += chunk.text;
      // 流式时按帧节流重渲染整段（未闭合的代码块等由 marked 容错处理）。
      if (!entry.frame) {
        entry.frame = requestAnimationFrame(() => {
          entry.frame = null;
          markdown(entry.item.querySelector('.agent-text'), entry.text);
          scroll();
        });
      }
    }
  }
  function apply(event) {
    const data = event.data;
    switch (event.type) {
      case 'user/message': {
        const text = (data.content || []).map((b) => b.text || '').join('');
        if (data.source?.kind === 'user') bubble('user', text);
        else if (data.source?.kind === 'followup') bubble('system', t('ui.agent_followup_note'));
        break;
      }
      case 'assistant/message': {
        const key = streamKey(data.turn, data.step);
        const entry = state.streams.get(key);
        const text = data.message.content.filter((b) => b.type === 'text').map((b) => b.text).join('');
        const reasoning = data.message.content.filter((b) => b.type === 'reasoning').map((b) => b.text).join('');
        let item = entry?.item;
        if (!text && !reasoning) { item?.remove(); state.streams.delete(key); break; }
        if (!item) item = bubble('assistant', '');
        item.classList.remove('is-streaming');
        if (entry?.frame) cancelAnimationFrame(entry.frame);
        markdown(item.querySelector('.agent-text'), text);
        if (reasoning) {
          const details = node('details', 'agent-reasoning');
          details.append(node('summary', '', t('ui.agent_reasoning')), node('pre', '', reasoning));
          item.prepend(details);
        }
        if (data.interrupted) item.classList.add('is-interrupted');
        state.streams.delete(key);
        break;
      }
      case 'tool/call': toolCall(data); break;
      case 'tool/result': toolResult(data); break;
      case 'plugin:mindex/approval-asked': approvalCard(data); break;
      case 'plugin:mindex/approval-decided': approvalDecided(data); break;
      case 'plugin:mindex/question-asked': questionCard(data); break;
      case 'plugin:mindex/question-answered': questionAnswered(data); break;
      case 'plugin:mindex/build-watch': buildCard(data); break;
      case 'plugin:mindex/build-finished': buildFinished(data); break;
      // 改动与定位统一由全局通道（/api/agent/changes）派发，会话事件里的记录只用于追溯。
      case 'plugin:mindex/notice': bubble('system', data.text); break;
      case 'turn/end':
        if (data.reason?.kind === 'error') fail(new Error(i18n() ? i18n().render(t('ui.agent_turn_error', { p0: data.reason.error?.message || '' })) : data.reason.error?.message));
        if (data.reason?.kind === 'interrupted') bubble('system', t('ui.agent_interrupted'));
        if (data.reason?.kind === 'aborted') bubble('system', t('ui.agent_aborted'));
        for (const entry of state.streams.values()) entry.item.classList.remove('is-streaming');
        break;
      default: break;
    }
  }
  function toolCall(data) {
    const card = node('details', 'agent-tool');
    const summary = node('summary', '', t('ui.agent_tool_call', { p0: data.name }));
    const args = node('pre', 'agent-args', prettyJson(data.arguments));
    const result = node('pre', 'agent-result', t('ui.agent_tool_running'));
    card.append(summary, args, result);
    log.append(card);
    state.cards.set(data.callId, card);
    scroll();
  }
  function toolResult(data) {
    const card = state.cards.get(data.message.toolCallId);
    if (!card) return;
    const text = (data.message.content || []).map((b) => b.text || '').join('');
    card.querySelector('.agent-result').textContent = text;
    if (data.meta?.media?.length) card.after(mediaBlock(data.meta.media));
    card.classList.toggle('is-error', Boolean(data.message.isError));
    card.querySelector('summary').dataset.state = data.message.isError ? 'error' : 'done';
  }
  function prettyJson(raw) {
    try { return JSON.stringify(JSON.parse(raw || '{}'), null, 2); } catch { return raw; }
  }

  // ------------------------------------------------------------ 确认
  function approvalCard(data) {
    const card = node('section', `agent-card agent-approval risk-${data.risk}`);
    label(card, t('ui.agent_approval_title'));
    card.append(node('h3', '', t(data.risk === 'danger' ? 'ui.agent_approval_danger' : 'ui.agent_approval_write', { p0: data.name })));
    if (data.preview?.summary) card.append(node('p', 'agent-preview', data.preview.summary));
    if (data.preview?.changes?.length) {
      const list = node('ul', 'agent-changes');
      for (const change of data.preview.changes) {
        const item = node('li');
        item.append(node('strong', '', change.label || ''));
        if (change.before !== undefined) item.append(node('span', 'agent-before', String(change.before ?? '—')));
        if (change.after !== undefined) item.append(node('span', 'agent-after', String(change.after ?? '—')));
        list.append(item);
      }
      card.append(list);
    }
    for (const text of data.preview?.warnings || []) card.append(node('p', 'agent-warning', text));
    const details = node('details', 'agent-args-wrap');
    details.append(node('summary', '', t('ui.agent_arguments')), node('pre', 'agent-args', prettyJson(data.arguments)));
    card.append(details);
    const bar = node('div', 'agent-card-actions');
    let grant = null;
    if (data.allowGrant) {
      const label = node('label', 'agent-grant');
      grant = node('input');
      grant.type = 'checkbox';
      label.append(grant, node('span', '', t('ui.agent_grant')));
      bar.append(label);
    }
    const reject = button(t('ui.agent_reject'), 'quiet', () => decide('rejected'));
    const allow = button(t('ui.agent_allow'), 'submit', () => decide('allowed-once'));
    bar.append(reject, allow);
    card.append(bar);
    const outcome = node('p', 'agent-outcome');
    outcome.setAttribute('role', 'status');
    card.append(outcome);
    async function decide(value) {
      allow.disabled = reject.disabled = true;
      try {
        await api(`/api/agent/sessions/${state.session}/approvals/${data.requestId}`, 'POST',
          { outcome: value, grant: Boolean(grant?.checked) });
      } catch (error) { allow.disabled = reject.disabled = false; fail(error); }
    }
    state.approvals.set(data.requestId, { card, allow, reject, grant, outcome });
    log.append(card);
    scroll();
    allow.focus({ preventScroll: true });
  }
  function approvalDecided(data) {
    const entry = state.approvals.get(data.requestId);
    if (!entry) return;
    entry.allow.disabled = entry.reject.disabled = true;
    if (entry.grant) entry.grant.disabled = true;
    const labels = { 'allowed-once': 'ui.agent_outcome_allowed', rejected: 'ui.agent_outcome_rejected',
      cancelled: 'ui.agent_outcome_cancelled', unavailable: 'ui.agent_outcome_unavailable' };
    if (i18n()) i18n().setText(entry.outcome, t(labels[data.outcome] || 'ui.agent_outcome_cancelled'));
    entry.card.dataset.outcome = data.outcome;
  }

  // ------------------------------------------------------------ 提问
  function questionCard(data) {
    const card = node('form', 'agent-card agent-question');
    const controls = [];
    for (const question of data.questions) {
      const box = node('fieldset', 'agent-q');
      box.append(node('legend', '', question.header ? `${question.header} · ${question.question}` : question.question));
      const entry = { id: question.id, options: [], custom: null, fields: [] };
      for (const option of question.options || []) {
        const label = node('label', 'agent-option');
        const choice = node('input');
        choice.type = question.multi_select ? 'checkbox' : 'radio';
        choice.name = `${data.requestId}-${question.id}`;
        choice.value = option.label;
        label.append(choice, node('span', '', option.label));
        if (option.description) label.append(node('small', 'muted', option.description));
        box.append(label);
        entry.options.push(choice);
      }
      for (const field of question.fields || []) {
        const label = node('label', 'agent-field');
        label.append(node('span', '', field.label + (field.required ? ' *' : '')));
        let control;
        if (field.type === 'select') {
          control = node('select');
          for (const value of field.options || []) {
            const option = node('option', '', value);
            option.value = value;
            control.append(option);
          }
        } else {
          control = node('input');
          control.type = field.type === 'secret' ? 'password' : field.type === 'number' ? 'number' : 'text';
          if (field.type === 'secret') control.autocomplete = 'off';
          if (field.placeholder) control.placeholder = field.placeholder;
          if (field.default !== undefined && field.default !== null && field.type !== 'secret') control.value = field.default;
        }
        control.required = Boolean(field.required);
        control.dataset.field = field.id;
        control.dataset.kind = field.type;
        label.append(control);
        if (field.type === 'secret') label.append(node('small', 'muted', t('ui.agent_secret_hint')));
        box.append(label);
        entry.fields.push(control);
      }
      if (!(question.fields || []).length) {
        const label = node('label', 'agent-field');
        label.append(node('span', '', t('ui.agent_custom_answer')));
        entry.custom = node('input');
        entry.custom.type = 'text';
        label.append(entry.custom);
        box.append(label);
      }
      controls.push(entry);
      card.append(box);
    }
    const bar = node('div', 'agent-card-actions');
    const submit = button(t('ui.agent_answer'), 'submit');
    submit.type = 'submit';
    bar.append(submit);
    card.append(bar);
    const outcome = node('p', 'agent-outcome');
    card.append(outcome);
    card.addEventListener('submit', async (event) => {
      event.preventDefault();
      const answers = controls.map((entry) => {
        const answer = { id: entry.id, selected: entry.options.filter((o) => o.checked).map((o) => o.value) };
        if (entry.custom?.value.trim()) answer.custom = entry.custom.value.trim();
        if (entry.fields.length) {
          answer.values = {};
          for (const control of entry.fields) {
            const value = control.value.trim();
            answer.values[control.dataset.field] = control.dataset.kind === 'number' && value !== '' ? Number(value) : value;
          }
        }
        return answer;
      });
      submit.disabled = true;
      try {
        await api(`/api/agent/sessions/${state.session}/questions/${data.requestId}`, 'POST', { answers });
        // 密钥明文只发给保险库一次，提交后立即从页面清除。
        for (const entry of controls) for (const control of entry.fields) if (control.dataset.kind === 'secret') control.value = '';
      } catch (error) { submit.disabled = false; fail(error); }
    });
    state.questions.set(data.requestId, { card, submit, outcome });
    log.append(card);
    scroll();
  }
  function questionAnswered(data) {
    const entry = state.questions.get(data.requestId);
    if (!entry) return;
    entry.submit.disabled = true;
    entry.card.querySelectorAll('input,select').forEach((n) => { n.disabled = true; if (n.type === 'password') n.value = ''; });
    if (i18n()) i18n().setText(entry.outcome, t(data.cancelled ? 'ui.agent_outcome_cancelled' : 'ui.agent_answered'));
  }

  // ------------------------------------------------------------ 构建进度
  function buildCard(data) {
    const card = node('section', 'agent-card agent-build');
    const heading = node('h3', '', t('ui.agent_build_title', { p0: data.build_id }));
    const status = node('p', 'agent-build-status', t('ui.agent_build_running'));
    status.setAttribute('role', 'status');
    const lines = node('pre', 'agent-build-log');
    const link = node('a', 'quiet nav-link', t('ui.agent_open_studio'));
    link.href = `/studio?job=${encodeURIComponent(data.job_id)}`;
    card.append(heading, status, lines, link);
    log.append(card);
    scroll();
    const entry = { card, status, lines, seq: 0, done: false, timer: null, data };
    state.builds.set(data.build_id, entry);
    poll(entry);
  }
  async function poll(entry) {
    if (entry.done || !state.builds.has(entry.data.build_id)) return;
    try {
      const data = await api(`/api/studio/jobs/${entry.data.job_id}/builds/${entry.data.build_id}/logs?after=${entry.seq}`);
      for (const item of data.items) {
        const text = i18n() ? i18n().render(i18n().system(item)) : item.message;
        entry.lines.textContent = (entry.lines.textContent + '\n' + text).trim().split('\n').slice(-6).join('\n');
      }
      entry.seq = data.next_seq ?? entry.seq;
      if (data.status !== 'running') return finishBuild(entry, data.status);
    } catch {}
    entry.timer = setTimeout(() => poll(entry), 1500);
  }
  function finishBuild(entry, status) {
    entry.done = true;
    clearTimeout(entry.timer);
    if (i18n()) i18n().setText(entry.status, t('ui.agent_build_done', { p0: i18n().status(status) }));
    entry.card.dataset.status = status;
  }
  function buildFinished(data) {
    let entry = state.builds.get(data.build_id);
    if (!entry) return;
    finishBuild(entry, data.status);
    if (data.media?.length && !entry.card.querySelector('.agent-media')) entry.card.append(mediaBlock(data.media));
  }

  // ------------------------------------------------------------ 媒体：缩略图网格、大图浏览、音视频播放
  // 只加载本站 /api/ 地址；与后端 tools/media.py 的校验一致。
  const SAFE_MEDIA = /^\/api\/[^\s]*$/;
  const safeMedia = (url) => typeof url === 'string' && SAFE_MEDIA.test(url) && !url.includes('//') && !url.includes('..');
  const PLAYER_SRC = '/assets/shared/shot-player.js?v=20261019-shot-media-6';
  let playerLoading = null;
  function ensureShotPlayer() {
    if (window.ShotPlayer) return Promise.resolve();
    if (!playerLoading) {
      playerLoading = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = PLAYER_SRC;
        script.onload = resolve;
        script.onerror = () => reject(new Error('shot-player'));
        document.head.append(script);
      });
    }
    return playerLoading;
  }
  function mediaElement(entry, { autoplay = false } = {}) {
    const tag = entry.kind === 'audio' ? 'audio' : 'video';
    const element = node(tag, 'agent-media-element');
    element.controls = true;
    element.preload = 'metadata';
    if (entry.poster && safeMedia(entry.poster) && tag === 'video') element.poster = entry.poster;
    label(element, entry.title || '');
    if (entry.playlist) {
      ensureShotPlayer().then(() => {
        const player = window.ShotPlayer.attach(element, entry.url);
        if (autoplay) player.ready.then(() => player.togglePlayback()).catch(() => {});
      }).catch(() => { element.dataset.error = 'player'; });
    } else {
      element.src = entry.url;
      if (autoplay) element.play().catch(() => {});
    }
    return element;
  }
  function pauseAll(root = document) {
    root.querySelectorAll('.agent-drawer audio, .agent-drawer video, .agent-viewer audio, .agent-viewer video')
      .forEach((n) => { try { n.pause(); } catch {} });
  }
  function mediaBlock(items) {
    const list = items.filter((entry) => entry && safeMedia(entry.url) && ['image', 'audio', 'video'].includes(entry.kind));
    const block = node('div', 'agent-media');
    const tiles = list.filter((entry) => entry.kind === 'image' || (entry.kind === 'video' && entry.poster && safeMedia(entry.poster)));
    if (tiles.length) {
      const grid = node('div', 'agent-media-grid');
      tiles.forEach((entry, index) => {
        const tile = button('', 'agent-media-tile');
        label(tile, t('ui.agent_media_open', { p0: entry.title || '' }));
        const image = node('img');
        image.loading = 'lazy';
        image.alt = '';
        image.src = entry.kind === 'image' ? entry.url : entry.poster;
        tile.append(image);
        if (entry.kind === 'video') tile.append(node('span', 'agent-media-play', '▶'));
        tile.append(node('span', 'agent-media-caption', entry.title || ''));
        tile.dataset.kind = entry.kind;
        tile.addEventListener('click', () => openViewer(tiles, index));
        grid.append(tile);
      });
      block.append(grid);
    }
    for (const entry of list.filter((value) => !tiles.includes(value))) {
      const details = node('details', `agent-player agent-player-${entry.kind}`);
      details.append(node('summary', '', `${entry.kind === 'audio' ? '♪' : '▶'} ${entry.title || ''}`));
      // 折叠的播放器展开时才创建媒体元素，避免一次加载大量音视频。
      const mount = () => {
        if (details.querySelector('.agent-media-element')) return;
        details.append(mediaElement(entry));
      };
      details.addEventListener('toggle', () => { if (details.open) mount(); else pauseAll(details); });
      if (!entry.collapsed) { details.open = true; mount(); }
      block.append(details);
    }
    return block;
  }
  const viewer = node('dialog', 'agent-viewer');
  label(viewer, t('ui.agent_media_viewer'));
  const viewerStage = node('div', 'agent-viewer-stage');
  const viewerCaption = node('p', 'agent-viewer-caption');
  viewerCaption.setAttribute('aria-live', 'polite');
  const viewerPrev = button('‹', 'quiet agent-viewer-prev', () => stepViewer(-1));
  const viewerNext = button('›', 'quiet agent-viewer-next', () => stepViewer(1));
  const viewerClose = button('✕', 'quiet agent-viewer-close', () => viewer.close());
  label(viewerPrev, t('ui.agent_media_prev'));
  label(viewerNext, t('ui.agent_media_next'));
  label(viewerClose, t('ui.agent_media_close'));
  viewer.append(viewerClose, viewerPrev, viewerStage, viewerNext, viewerCaption);
  const viewerState = { items: [], index: 0 };
  function showViewer() {
    pauseAll(viewer);
    const entry = viewerState.items[viewerState.index];
    viewerStage.replaceChildren();
    if (entry.kind === 'image') {
      const image = node('img', 'agent-viewer-image');
      image.src = entry.url;
      image.alt = entry.title || '';
      viewerStage.append(image);
    } else viewerStage.append(mediaElement(entry, { autoplay: true }));
    viewerCaption.textContent = `${entry.title || ''}  ·  ${viewerState.index + 1} / ${viewerState.items.length}`;
    viewerPrev.disabled = viewerNext.disabled = viewerState.items.length < 2;
  }
  function openViewer(items, index) {
    viewerState.items = items;
    viewerState.index = index;
    showViewer();
    if (!viewer.open) viewer.showModal();
    viewerNext.focus({ preventScroll: true });
  }
  function stepViewer(delta) {
    if (viewerState.items.length < 2) return;
    viewerState.index = (viewerState.index + delta + viewerState.items.length) % viewerState.items.length;
    showViewer();
  }
  viewer.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowLeft') { event.preventDefault(); stepViewer(-1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); stepViewer(1); }
  });
  viewer.addEventListener('close', () => { pauseAll(viewer); viewerStage.replaceChildren(); });
  viewer.addEventListener('click', (event) => { if (event.target === viewer) viewer.close(); });
  // Markdown 回复里的本站图片：点击在大图浏览中打开。
  log.addEventListener('click', (event) => {
    const image = event.target.closest?.('.agent-markdown img');
    if (!image || !safeMedia(image.getAttribute('src'))) return;
    const images = [...image.closest('.agent-markdown').querySelectorAll('img')]
      .filter((n) => safeMedia(n.getAttribute('src')))
      .map((n) => ({ kind: 'image', url: n.getAttribute('src'), title: n.alt || '' }));
    openViewer(images, images.findIndex((entry) => entry.url === image.getAttribute('src')));
  });

  // ------------------------------------------------------------ 发送
  function looksLikeSecret(text) { return KEY_PATTERN.test(text); }
  function warn(show) {
    warning.hidden = !show;
    if (show && i18n()) i18n().setText(warning, t('ui.agent_secret_paste'));
  }
  input.addEventListener('paste', (event) => {
    const text = event.clipboardData?.getData('text') || '';
    if (looksLikeSecret(text)) { event.preventDefault(); warn(true); }
  });
  input.addEventListener('input', () => { if (!looksLikeSecret(input.value)) warn(false); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || state.status !== 'idle') return;
    if (looksLikeSecret(text)) { warn(true); return; }
    send.disabled = true;
    try {
      if (!state.session) await startSession();
      await api(`/api/agent/sessions/${state.session}/messages`, 'POST', { content: text, context: pageContext() });
      input.value = '';
      setStatus('running');
    } catch (error) {
      fail(error);
      send.disabled = false;
    }
  });
  async function cancelTurn() {
    if (state.session) await api(`/api/agent/sessions/${state.session}/cancel`, 'POST', {});
  }

  window.MindexAgent = { toggle, open: () => toggle(true), looksLikeSecret, state, highlight, clearHighlights,
    tab: TAB, currentPage, setWidth, get width() { return width; }, openViewer };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
