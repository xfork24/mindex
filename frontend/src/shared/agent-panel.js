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
  const FOLLOW_SCROLL_KEY = 'mindex.agent.followScroll';
  const ALWAYS_ALLOW_KEY = 'mindex.agent.alwaysAllow';
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
    feedSeq: null, feed: null, feedRetry: null, pendingNav: null, follow: true, followScroll: false, alwaysAllow: false,
    attachments: [], creating: null, retryDelay: null, feedRetryDelay: null,
    inputHistory: [], historyIndex: null, historyDraft: null,
  };
  // SSE 断线重连：1.5 秒起指数退避，上限 30 秒；收到 ready / session.status 后复位。
  // 服务关闭或返回 401 时，每个标签页不再每 1.5 秒请求一次。
  const RECONNECT_BASE_MS = 1500, RECONNECT_MAX_MS = 30000;
  const nextReconnectDelay = (delay) => (delay ? Math.min(delay * 2, RECONNECT_MAX_MS) : RECONNECT_BASE_MS);
  const MAX_ATTACHMENTS = 12; // 与后端 MessageBody.attachments 上限一致
  const ATTACHMENT_REF = /^attachment:\/\/([0-9a-f]{32})$/;
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
  try { state.followScroll = localStorage.getItem(FOLLOW_SCROLL_KEY) === '1'; } catch {}
  try { state.alwaysAllow = localStorage.getItem(ALWAYS_ALLOW_KEY) === '1'; } catch {}
  function currentPage() {
    const path = location.pathname.replace(/^\/+|\/+$/g, '').replace(/\.html$/, '');
    if (path === 'studio/topics') return 'topics'; // 选题挖掘是创作页子页面，但有独立的工具领域与定位
    if (path === 'admin/music') return 'music'; // 音乐库是素材管理页子页面，曲库工具在 studio_post 领域
    const segment = path.split('/')[0];
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
    try {
      return await window.MindexApi.json(url, method, body);
    } catch (error) {
      // 面板把错误写进纯文本区域：服务端错误在此渲染为当前语言的字符串。
      if (error.status !== undefined && i18n()) error.message = i18n().render(error.message);
      throw error;
    }
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
  const followScrollLabel = node('label', 'agent-follow-scroll');
  const followScrollBox = node('input');
  followScrollBox.type = 'checkbox';
  followScrollBox.checked = state.followScroll;
  followScrollBox.addEventListener('change', () => {
    state.followScroll = followScrollBox.checked;
    try { localStorage.setItem(FOLLOW_SCROLL_KEY, state.followScroll ? '1' : '0'); } catch {}
    scroll();
  });
  followScrollLabel.append(followScrollBox, node('span', '', t('ui.agent_follow_scroll')));
  if (i18n()) i18n().setAttr(followScrollLabel, 'title', t('ui.agent_follow_scroll_hint'));
  // 总是允许：开启后收到确认请求即自动回复 allowed-once，不再逐次询问（选择记在本机）。
  const alwaysLabel = node('label', 'agent-always-allow');
  const alwaysBox = node('input');
  alwaysBox.type = 'checkbox';
  alwaysBox.checked = state.alwaysAllow;
  alwaysBox.addEventListener('change', () => {
    state.alwaysAllow = alwaysBox.checked;
    try { localStorage.setItem(ALWAYS_ALLOW_KEY, state.alwaysAllow ? '1' : '0'); } catch {}
    if (state.alwaysAllow) autoAllowPending();
  });
  alwaysLabel.append(alwaysBox, node('span', '', t('ui.agent_always_allow')));
  if (i18n()) i18n().setAttr(alwaysLabel, 'title', t('ui.agent_always_allow_hint'));
  const toggles = node('div', 'agent-toggles');
  toggles.append(followLabel, followScrollLabel, alwaysLabel);
  head.append(toggles);
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
  // 待发送附件：chip 列表 + 上传/发送受阻提示（与疑似密钥提示分开，互不覆盖）。
  const attachList = node('div', 'agent-attachments');
  attachList.setAttribute('role', 'list');
  label(attachList, t('ui.agent_attach_list'));
  attachList.hidden = true;
  const attachStatus = node('p', 'agent-attach-status');
  attachStatus.setAttribute('role', 'alert');
  attachStatus.hidden = true;
  const actions = node('div', 'agent-actions');
  const fileInput = node('input', 'agent-attach-input');
  fileInput.type = 'file';
  fileInput.multiple = true;
  fileInput.hidden = true;
  fileInput.tabIndex = -1;
  const attachButton = button(t('ui.agent_attach'), 'quiet agent-attach', () => fileInput.click());
  if (i18n()) i18n().setAttr(attachButton, 'title', t('ui.agent_attach_hint'));
  const send = button(t('ui.agent_send'), 'submit agent-send');
  send.type = 'submit';
  const cancel = button(t('ui.agent_cancel'), 'quiet', () => cancelTurn().catch(fail));
  cancel.hidden = true;
  actions.append(attachButton, fileInput, cancel, send);
  form.append(warning, attachList, attachStatus, input, actions);
  const dropzone = node('div', 'agent-dropzone', t('ui.agent_attach_drop'));
  dropzone.setAttribute('aria-hidden', 'true');
  drawer.append(head, statusLine, log, form, dropzone);

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
    if (focus.page === 'music') return '/admin/music';
    if (focus.page === 'topics') {
      const params = new URLSearchParams();
      if (focus.profile_id) params.set('profile', focus.profile_id);
      if (focus.card_id) params.set('card', focus.card_id);
      return '/studio/topics' + (params.toString() ? `?${params}` : '');
    }
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
      state.feedRetryDelay = null;
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
      state.feedRetryDelay = nextReconnectDelay(state.feedRetryDelay);
      state.feedRetry = setTimeout(subscribeFeed, state.feedRetryDelay);
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
  function scroll() { if (state.followScroll) log.scrollTop = log.scrollHeight; }

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
  // 附件上传与发送共用：没有会话时只创建一次（并发拖入多个文件时复用同一个请求）。
  function ensureSession() {
    if (state.session) return Promise.resolve(state.session);
    if (!state.creating) state.creating = startSession().finally(() => { state.creating = null; });
    return state.creating;
  }
  async function removeSession() {
    if (!state.session) return;
    if (!window.confirm(i18n() ? i18n().render(t('ui.agent_delete_confirm')) : '删除此会话？')) return;
    await api(`/api/agent/sessions/${encodeURIComponent(state.session)}`, 'DELETE');
    localStorage.removeItem(STORAGE_KEY);
    reset(null);
    await loadSessions('');
  }
  function reset(id) {
    pauseAll();
    if (state.source) state.source.close();
    clearTimeout(state.retry);
    for (const timer of state.builds.values()) clearTimeout(timer.timer);
    Object.assign(state, { session: id, seq: -1, source: null, status: 'idle', live: false, retryDelay: null });
    state.inputHistory = [];
    endHistory();
    state.cards.clear(); state.streams.clear(); state.approvals.clear(); state.questions.clear(); state.builds.clear();
    clearAttachments({ revoke: true }); // 附件归属原会话，切换或删除会话后不能再随新消息发送
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
    const source = new EventSource(`/api/agent/sessions/${encodeURIComponent(id)}/events?after=${state.seq}`);
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
      state.retryDelay = null;
      state.live = true; // 状态帧在补发的历史之后到达：此后才是实时事件
      setStatus(JSON.parse(message.data).status);
      autoAllowPending();
    });
    source.onerror = () => {
      source.close();
      if (state.session !== id) return;
      clearTimeout(state.retry);
      state.retryDelay = nextReconnectDelay(state.retryDelay);
      state.retry = setTimeout(subscribe, state.retryDelay); // 按最后 seq 续传
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
        let text = (data.content || []).map((b) => b.text || '').join('');
        if (data.source?.kind === 'user') {
          const files = Array.isArray(data.attachments) ? data.attachments : [];
          if (files.length) {
            // 运行时在正文末尾逐行附上「附件：attachment://… 名称（类型，大小）」供模型引用；界面改用 chip 展示。
            const cut = text.lastIndexOf('\n\n\u9644\u4ef6\uff1a' + files[0].ref);
            if (cut >= 0) text = text.slice(0, cut);
          }
          state.inputHistory.push(text); // 与渲染共用 SSE 序号去重；相同文字的不同消息分别保留。
          const item = bubble('user', text);
          if (files.length) item.append(messageAttachments(files));
        }
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
    scroll();
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
    if (data.preview?.summary) card.append(node('p', 'agent-preview', i18n() ? i18n().system(data.preview.summary) : data.preview.summary));
    if (data.preview?.changes?.length) {
      const list = node('ul', 'agent-changes');
      for (const change of data.preview.changes) {
        const item = node('li');
        item.append(node('strong', '', i18n() ? i18n().system(change.label || '') : change.label || ''));
        if (change.before !== undefined) item.append(node('span', 'agent-before', String(change.before ?? '—')));
        if (change.after !== undefined) item.append(node('span', 'agent-after', String(change.after ?? '—')));
        list.append(item);
      }
      card.append(list);
    }
    for (const text of data.preview?.warnings || []) card.append(node('p', 'agent-warning', i18n() ? i18n().system(text) : text));
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
    async function decide(value, { silent = false } = {}) {
      allow.disabled = reject.disabled = true;
      try {
        await api(`/api/agent/sessions/${encodeURIComponent(state.session)}/approvals/${encodeURIComponent(data.requestId)}`, 'POST',
          { outcome: value, grant: Boolean(grant?.checked) });
      } catch (error) {
        const entry = state.approvals.get(data.requestId);
        if (entry?.decided) return;
        allow.disabled = reject.disabled = false;
        if (entry) entry.auto = false;
        // 自动允许失败（例如另一标签页已回复）时不弹错误，保留按钮供手动处理。
        if (!silent) fail(error);
      }
    }
    state.approvals.set(data.requestId, { card, allow, reject, grant, outcome, decide, decided: false, auto: false });
    log.append(card);
    scroll();
    // 历史补发阶段（state.live=false）不自动回复：已决的请求随后会收到 approval-decided，
    // 仍待确认的请求在状态帧到达后由 autoAllowPending 统一处理。
    if (state.alwaysAllow && state.live) autoAllowPending();
    else allow.focus({ preventScroll: true });
  }
  function autoAllowPending() {
    if (!state.alwaysAllow || !state.live) return;
    for (const entry of state.approvals.values()) {
      if (entry.decided || entry.auto) continue;
      entry.auto = true;
      entry.decide('allowed-once', { silent: true });
    }
  }
  function approvalDecided(data) {
    const entry = state.approvals.get(data.requestId);
    if (!entry) return;
    entry.decided = true;
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
        await api(`/api/agent/sessions/${encodeURIComponent(state.session)}/questions/${encodeURIComponent(data.requestId)}`, 'POST', { answers });
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
        const text = i18n() ? i18n().system(item) : item.message;
        entry.logMessages = [...(entry.logMessages || []), text].slice(-6);
        if (i18n()) i18n().setText(entry.lines, entry.logMessages.join('\n'));
        else entry.lines.textContent = entry.logMessages.join('\n');
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
  const PLAYER_SRC = '/assets/shared/shot-player.js?v=0d7ae3ceb4';
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

  // ------------------------------------------------------------ 附件：选择、拖放、粘贴，XHR 上传（带进度），发送前等待完成
  // 上传到 POST /api/agent/attachments（multipart：file + session_id），返回 {id: "attachment://<token>", …}；
  // 发送消息时把 id 列表放进 attachments。移除已上传的附件会 DELETE 撤销暂存。
  const PREVIEW_TYPES = /^image\/(?:png|jpeg|gif|webp|bmp)$/; // 后端按原类型内联返回的图片类型
  function formatSize(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    const units = ['KB', 'MB', 'GB'];
    let size = value / 1024, unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
  }
  function attachmentUrl(ref) {
    const match = ATTACHMENT_REF.exec(String(ref || ''));
    return match ? `/api/agent/attachments/${match[1]}/content` : null;
  }
  function revokeRemote(ref) {
    const match = ATTACHMENT_REF.exec(String(ref || ''));
    if (match) fetch(`/api/agent/attachments/${match[1]}`, { method: 'DELETE' }).catch(() => {});
  }
  function showAttachStatus(message, kind) {
    attachStatus.hidden = false;
    attachStatus.dataset.kind = kind;
    attachStatus.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    if (i18n()) i18n().setText(attachStatus, message); else attachStatus.textContent = String(message);
  }
  function hideAttachStatus() {
    attachStatus.hidden = true;
    delete attachStatus.dataset.kind;
  }
  function refreshAttachments() {
    attachList.hidden = !state.attachments.length;
    if (attachStatus.dataset.kind === 'error' && !state.attachments.some((entry) => entry.status === 'error')) hideAttachStatus();
  }
  function setEntryState(entry, status) {
    entry.status = status;
    entry.chip.dataset.state = status;
    entry.progress.hidden = status !== 'uploading';
    entry.error.hidden = status !== 'error';
  }
  function attachmentChip(entry) {
    const chip = node('div', 'agent-attachment');
    chip.setAttribute('role', 'listitem');
    if (PREVIEW_TYPES.test(entry.file.type || '')) {
      entry.preview = URL.createObjectURL(entry.file);
      const image = node('img', 'agent-attachment-preview');
      image.alt = '';
      image.src = entry.preview;
      chip.append(image);
    }
    const name = node('span', 'agent-attachment-name', entry.file.name || 'file');
    name.title = entry.file.name || '';
    const size = node('span', 'agent-attachment-size', formatSize(entry.file.size));
    entry.progress = node('progress', 'agent-attachment-progress');
    entry.progress.max = 100;
    label(entry.progress, t('ui.agent_attach_uploading', { p0: entry.file.name || '' }));
    entry.error = node('span', 'agent-attachment-error');
    const remove = button('✕', 'quiet agent-attachment-remove', () => removeAttachment(entry));
    label(remove, t('ui.agent_attach_remove', { p0: entry.file.name || '' }));
    chip.append(name, size, entry.progress, entry.error, remove);
    entry.chip = chip;
    entry.remove = remove;
    return chip;
  }
  function uploadAttachment(entry) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      entry.xhr = xhr;
      const failed = (reason) => {
        if (entry.removed) { resolve(); return; }
        entry.reason = i18n() ? i18n().system(reason) : String(reason?.detail || reason);
        if (i18n()) i18n().setText(entry.error, entry.reason); else entry.error.textContent = entry.reason;
        setEntryState(entry, 'error');
        refreshAttachments();
        resolve();
      };
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) entry.progress.value = Math.round((event.loaded / event.total) * 100);
      });
      xhr.addEventListener('load', () => {
        entry.xhr = null;
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch {}
        if (xhr.status >= 200 && xhr.status < 300 && typeof data.id === 'string') {
          if (entry.removed) { revokeRemote(data.id); resolve(); return; } // 移除与上传完成交错
          entry.ref = data.id;
          entry.meta = data;
          entry.progress.value = 100;
          setEntryState(entry, 'done');
          refreshAttachments();
          resolve();
        } else failed(data && (data.detail || data.message) ? data : `HTTP ${xhr.status}`);
      });
      xhr.addEventListener('error', () => { entry.xhr = null; failed('Failed to fetch'); });
      xhr.addEventListener('abort', () => { entry.xhr = null; resolve(); });
      const body = new FormData();
      body.append('session_id', entry.session);
      body.append('file', entry.file, entry.file.name || 'file');
      xhr.open('POST', '/api/agent/attachments');
      xhr.send(body);
    });
  }
  async function addFiles(list) {
    const files = [...(list || [])].filter((file) => file instanceof Blob);
    if (!files.length) return;
    let sid;
    try { sid = await ensureSession(); } catch (error) { fail(error); return; }
    if (state.session !== sid) return;
    const room = Math.max(0, MAX_ATTACHMENTS - state.attachments.length);
    if (files.length > room) showAttachStatus(t('ui.agent_attach_limit', { p0: MAX_ATTACHMENTS }), 'error');
    for (const file of files.slice(0, room)) {
      const entry = { file, session: sid, status: 'uploading', ref: null, xhr: null, removed: false };
      attachList.append(attachmentChip(entry));
      setEntryState(entry, 'uploading');
      state.attachments.push(entry);
      entry.promise = uploadAttachment(entry);
    }
    refreshAttachments();
  }
  function discardAttachment(entry, revoke) {
    entry.removed = true;
    if (entry.xhr) entry.xhr.abort();
    else if (revoke && entry.ref) revokeRemote(entry.ref);
    if (entry.preview) URL.revokeObjectURL(entry.preview);
    entry.chip.remove();
  }
  function removeAttachment(entry) {
    const index = state.attachments.indexOf(entry);
    if (index < 0) return;
    // 键盘移除后把焦点交给相邻 chip 的移除按钮，没有则回到输入框。
    const next = state.attachments[index + 1] || state.attachments[index - 1];
    state.attachments.splice(index, 1);
    discardAttachment(entry, true);
    refreshAttachments();
    (next ? next.remove : input).focus({ preventScroll: true });
  }
  function clearAttachments({ revoke }) {
    for (const entry of state.attachments) discardAttachment(entry, revoke);
    state.attachments = [];
    hideAttachStatus();
    refreshAttachments();
  }
  // 发送前等待：返回附件引用列表；有失败项或会话已切换时返回 null（阻止发送）。
  async function settleAttachments(sid) {
    if (state.attachments.some((entry) => entry.status === 'uploading')) showAttachStatus(t('ui.agent_attach_wait'), 'info');
    while (state.attachments.some((entry) => entry.status === 'uploading')) {
      await Promise.all(state.attachments.map((entry) => entry.promise));
    }
    if (state.session !== sid) return null;
    const failed = state.attachments.filter((entry) => entry.status === 'error');
    if (failed.length) {
      const first = failed[0];
      showAttachStatus(t('ui.agent_attach_failed', { p0: first.file.name || '', p1: first.reason || '' }), 'error');
      return null;
    }
    hideAttachStatus();
    return state.attachments.map((entry) => entry.ref);
  }
  // 消息中的附件：图片显示缩略图并可在大图浏览中打开；其他文件显示可打开/下载的 chip。
  function messageAttachments(files) {
    const block = node('div', 'agent-message-attachments');
    label(block, t('ui.agent_attach_message'));
    const images = files
      .filter((file) => PREVIEW_TYPES.test(file.content_type || '') && attachmentUrl(file.ref))
      .map((file) => ({ kind: 'image', url: attachmentUrl(file.ref), title: file.filename || '', file }));
    images.forEach((entry, index) => {
      const tile = button('', 'agent-attachment-thumb');
      label(tile, t('ui.agent_media_open', { p0: entry.title }));
      const image = node('img');
      image.loading = 'lazy';
      image.alt = '';
      image.src = entry.url;
      tile.append(image);
      tile.addEventListener('click', () => openViewer(images, index));
      block.append(tile);
    });
    for (const file of files) {
      if (images.some((entry) => entry.file === file)) continue;
      const url = attachmentUrl(file.ref);
      const chip = node(url ? 'a' : 'span', 'agent-attachment-file');
      chip.append(node('span', 'agent-attachment-name', file.filename || ''),
        node('span', 'agent-attachment-size', formatSize(file.bytes)));
      if (url) {
        chip.href = url;
        chip.target = '_blank';
        chip.rel = 'noopener';
        label(chip, t('ui.agent_attach_open', { p0: file.filename || '' }));
      }
      block.append(chip);
    }
    return block;
  }
  fileInput.addEventListener('change', () => {
    const files = [...fileInput.files];
    fileInput.value = ''; // 允许再次选择同一文件
    addFiles(files);
  });
  const draggingFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');
  drawer.addEventListener('dragover', (event) => {
    if (!draggingFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    drawer.classList.add('is-dragover');
  });
  drawer.addEventListener('dragleave', (event) => {
    if (!drawer.contains(event.relatedTarget)) drawer.classList.remove('is-dragover');
  });
  drawer.addEventListener('drop', (event) => {
    drawer.classList.remove('is-dragover');
    if (!draggingFiles(event)) return;
    event.preventDefault();
    addFiles(event.dataTransfer.files);
  });
  // 粘贴文件（截图、复制的文件）作为附件；粘贴文本仍走输入框的疑似密钥检测。
  drawer.addEventListener('paste', (event) => {
    const files = event.clipboardData?.files;
    if (!files?.length) return;
    event.preventDefault();
    addFiles(files);
  });

  // ------------------------------------------------------------ 发送
  function looksLikeSecret(text) { return KEY_PATTERN.test(text); }
  function warn(show) {
    warning.hidden = !show;
    if (show && i18n()) i18n().setText(warning, t('ui.agent_secret_paste'));
  }
  // 历史只回填文字；待发送附件与会话切换时的草稿处理保持原有行为。
  function endHistory() {
    state.historyIndex = null;
    state.historyDraft = null;
  }
  function historySelectionChanged() {
    if (state.historyIndex !== null && (input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length)) endHistory();
  }
  function browseHistory(step) {
    historySelectionChanged();
    if (state.historyIndex === null) {
      if (step > 0 || !state.inputHistory.length || input.selectionStart !== 0 || input.selectionEnd !== 0) return false;
      state.historyDraft = {
        value: input.value, start: input.selectionStart, end: input.selectionEnd, direction: input.selectionDirection,
      };
      state.historyIndex = state.inputHistory.length;
    }
    const next = Math.max(0, state.historyIndex + step);
    if (next >= state.inputHistory.length) {
      const draft = state.historyDraft;
      endHistory();
      input.value = draft.value;
      input.setSelectionRange(draft.start, draft.end, draft.direction);
    } else {
      state.historyIndex = next;
      input.value = state.inputHistory[next];
      input.setSelectionRange(input.value.length, input.value.length);
    }
    warn(looksLikeSecret(input.value));
    return true;
  }
  let composing = false;
  input.addEventListener('compositionstart', () => { composing = true; endHistory(); });
  input.addEventListener('compositionend', () => { composing = false; });
  input.addEventListener('pointerdown', endHistory);
  input.addEventListener('click', endHistory);
  input.addEventListener('select', historySelectionChanged);
  input.addEventListener('paste', (event) => {
    if (event.clipboardData?.files?.length) return; // 文件由抽屉上的 paste 监听处理
    const text = event.clipboardData?.getData('text') || '';
    if (looksLikeSecret(text)) { event.preventDefault(); warn(true); }
  });
  input.addEventListener('input', () => { endHistory(); if (!looksLikeSecret(input.value)) warn(false); });
  input.addEventListener('keydown', (event) => {
    if (composing || event.isComposing || event.keyCode === 229) return;
    if ((event.key === 'ArrowUp' || event.key === 'ArrowDown') && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey) {
      if (browseHistory(event.key === 'ArrowUp' ? -1 : 1)) event.preventDefault();
      return;
    }
    if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) endHistory();
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || state.status !== 'idle' || send.dataset.sending) return;
    if (looksLikeSecret(text)) { warn(true); return; }
    send.disabled = true;
    send.dataset.sending = '1';
    try {
      const sid = await ensureSession();
      const refs = await settleAttachments(sid);
      if (refs === null) { send.disabled = state.status !== 'idle'; return; }
      const seq = state.seq;
      await api(`/api/agent/sessions/${encodeURIComponent(sid)}/messages`, 'POST', { content: text, context: pageContext(), attachments: refs });
      endHistory();
      input.value = '';
      clearAttachments({ revoke: false });
      // 快速回复的 SSE 可能早于 HTTP 响应；保留已收到的完成/待确认状态。
      if (state.seq === seq) setStatus('running');
    } catch (error) {
      fail(error);
      send.disabled = state.status !== 'idle';
    } finally {
      delete send.dataset.sending;
    }
  });
  async function cancelTurn() {
    if (state.session) await api(`/api/agent/sessions/${encodeURIComponent(state.session)}/cancel`, 'POST', {});
  }

  window.MindexAgent = { toggle, open: () => toggle(true), looksLikeSecret, state, highlight, clearHighlights,
    tab: TAB, currentPage, setWidth, get width() { return width; }, openViewer, addFiles };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
