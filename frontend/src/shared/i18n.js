/* UI messages are explicitly bound. User/model content is never dictionary-matched. */
(function () {
  'use strict';
  const catalog = window.MindexMessages;
  const storageKey = 'mindex.language';
  let language = 'zh';
  try { language = localStorage.getItem(storageKey) === 'en' ? 'en' : 'zh'; } catch {}
  const messages = new Map(), tokens = new Map(), bindings = new WeakMap();
  let serial = 0;
  const tokenPattern = /\uE000z(\d+)\uE001/g;
  const legacy = Object.entries(catalog).filter(([key, entry]) => !key.startsWith('system.') && /[\u4e00-\u9fff]/.test(entry.zh)).map(([key, entry]) => {
    const names = [];
    const pattern = entry.zh.split(/(\{\w+\})/g).map(part => {
      if (/^\{\w+\}$/.test(part)) { names.push(part.slice(1, -1)); return '([\\s\\S]*?)'; }
      return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('');
    return {key, names, regex: new RegExp('^' + pattern + '$'), size: entry.zh.length};
  }).sort((a, b) => Number(!b.names.length) - Number(!a.names.length) || b.size - a.size);
  function msg(key, params = {}) {
    const identity = JSON.stringify([key, params]);
    if (tokens.has(identity)) return tokens.get(identity);
    const id = String(++serial), token = '\uE000z' + id + '\uE001';
    messages.set(id, {key, params}); tokens.set(identity, token);
    return token;
  }
  function render(value, depth = 0) {
    if (depth > 20) return String(value ?? '');
    return String(value ?? '').replace(tokenPattern, (_, id) => {
      const item = messages.get(id);
      if (!item) return '';
      if (item.key === 'system.date' || item.key === 'system.time') {
        const date = new Date(item.params.value);
        return item.key === 'system.time' ? date.toLocaleTimeString(language === 'zh' ? 'zh-CN' : 'en-US') : date.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US');
      }
      const entry = catalog[item.key];
      const template = entry?.[language] ?? entry?.zh ?? item.key;
      return template.replace(/\{(\w+)\}/g, (all, name) => render(item.params[name] ?? all, depth + 1));
    });
  }
  function system(value) {
    if (!value) return '';
    if (Array.isArray(value)) return msg('system.validation', {diagnostic: value.map(item => [...(item.loc || []), item.msg || JSON.stringify(item)].join(': ')).join('; ')});
    if (typeof value === 'object') {
      if (value.message_key && catalog[value.message_key]) return msg(value.message_key, value.message_params || {});
      return system(value.message ?? value.detail ?? value.error ?? JSON.stringify(value));
    }
    const text = String(value);
    if (/^(Failed to fetch|Load failed|NetworkError.*|fetch failed)$/.test(text)) return msg('system.network');
    if (/\uE000z\d+\uE001/.test(text)) return text;
    for (const entry of legacy) {
      const found = text.match(entry.regex);
      if (found) return msg(entry.key, Object.fromEntries(entry.names.map((n, i) => [n, found[i + 1]])));
    }
    return msg('system.diagnostic', {diagnostic: text});
  }
  function remember(node, attribute, value) {
    let map = bindings.get(node);
    if (!map) { map = new Map(); bindings.set(node, map); }
    if (/\uE000z\d+\uE001/.test(String(value))) map.set(attribute, value);
    else map.delete(attribute);
  }
  function setText(node, value) {
    if (!node) return;
    const text = render(value);
    if (node.nodeType === 3) {
      remember(node, '#text', value);
      if (node.data !== text) node.data = text;
      return;
    }
    // A label may gain inputs, select options or media after its initial text.
    // Remember its text node, so a language change never replaces those children.
    if (node.textContent !== text) node.textContent = text;
    const target = node.firstChild;
    let map = bindings.get(node);
    if (!map) { map = new Map(); bindings.set(node, map); }
    if (target?.nodeType === 3 && /\uE000z\d+\uE001/.test(String(value))) map.set('#text', {value, target});
    else map.delete('#text');
  }
  function setAttr(node, attribute, value) {
    if (!node) return;
    remember(node, attribute, value);
    node.setAttribute(attribute, render(value));
  }
  function textNode(value) {
    const node = document.createTextNode(''); setText(node, value); return node;
  }
  function bind(node, key, params = {}, attribute = '#text') {
    if (attribute === '#text') setText(node, msg(key, params)); else setAttr(node, attribute, msg(key, params));
  }
  function update() {
    const scroll = [];
    document.querySelectorAll('*').forEach(node => {if (node.scrollTop || node.scrollLeft) scroll.push([node, node.scrollTop, node.scrollLeft]);});
    const position = [window.scrollX, window.scrollY];
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    const visit = node => {
      for (const [attribute, value] of bindings.get(node) || []) {
        if (attribute === '#text') {
          if (Array.isArray(value)) return msg('system.validation', {diagnostic: value.map(item => [...(item.loc || []), item.msg || JSON.stringify(item)].join(': ')).join('; ')});
    if (typeof value === 'object') {
            if (value.target.parentNode === node) value.target.data = render(value.value);
          } else if (node.nodeType === 3) node.data = render(value);
        } else setAttr(node, attribute, value);
      }
      for (const child of [...node.childNodes]) visit(child);
    };
    visit(document.documentElement);
    document.querySelectorAll('.language-switch').forEach(node => bind(node, 'language.switch', {}, 'aria-label'));
    document.querySelectorAll('[data-language]').forEach(button => {
      button.setAttribute('aria-current', String(button.dataset.language === language));
    });
    for (const [node, top, left] of scroll) {node.scrollTop = top;node.scrollLeft = left;}
    if (window.scrollX !== position[0] || window.scrollY !== position[1]) window.scrollTo(...position);
  }
  function setLanguage(next, persist = true) {
    next = next === 'en' ? 'en' : 'zh';
    if (language === next) return;
    language = next;
    if (persist) { try { localStorage.setItem(storageKey, language); } catch {} }
    update();
    window.dispatchEvent(new CustomEvent('mindex:languagechange', {detail: {language}}));
  }
  function init() {
    document.querySelectorAll('[data-i18n]').forEach(node => {
      const parts = JSON.parse(node.dataset.i18n);
      for (const [index, key] of parts) bind(node.childNodes[index], key);
    });
    for (const attribute of ['placeholder', 'aria-label', 'alt', 'title', 'data-query']) {
      document.querySelectorAll(`[data-i18n-${attribute}]`).forEach(node => bind(node, node.getAttribute(`data-i18n-${attribute}`), {}, attribute));
    }
    document.querySelectorAll('[data-language-toggle]').forEach(button => button.addEventListener('click', () => setLanguage(language === 'zh' ? 'en' : 'zh')));
    update();
  }
  window.addEventListener('storage', event => {
    if (event.key === storageKey || event.key === null) setLanguage(event.newValue, false);
  });
  function builtin(value) {
    const entry = Object.entries(catalog).find(([, item]) => item.zh === value);
    return entry ? msg(entry[0]) : value;
  }
  function voice(item) {
    return catalog['voice.' + item.voice_id] ? msg('voice.' + item.voice_id) : item.label;
  }
  function status(value) {
    const labels = {running: 'ui.594249700590', queued: 'ui.4dcbbcfa6154', failed: 'ui.3e3c8068bb0e', complete: 'ui.33246f6a5e5b', interrupted: 'ui.8dd5ad764e4b', cancelled: 'ui.a5ffdc95eeb0', pending: 'ui.59a9eb4e6574', blocked: 'ui.a00db105bd49', reused: 'ui.68f7d028b705', skipped: 'ui.31a98593f16d'};
    return labels[value] ? msg(labels[value]) : value;
  }
  window.MindexI18n = {msg, render, system, builtin, voice, status, setText, setAttr, textNode, bind, setLanguage, date: (value, time = false) => {
    // Dates participate in bindings without rebuilding lists.
    return msg(time ? 'system.time' : 'system.date', {value: new Date(value).toISOString()});
  }, get language() { return language; }, catalog};
  // Date formatting is localized when resolving the message, including existing log lines.
  catalog['system.date'] = {zh: '{value}', en: '{value}'};
  catalog['system.time'] = {zh: '{value}', en: '{value}'};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
