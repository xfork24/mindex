/* 主题三态切换（auto/light/dark）：head 内同步执行防闪烁；auto 跟随系统并监听变化。
   浅色是 :root 默认 token，深色由 <html data-theme="dark"> 覆盖；偏好持久化在 localStorage。 */
(function () {
  'use strict';
  const KEY = 'mindex.theme';
  const CYCLE = ['auto', 'light', 'dark'];
  const KEYS = {auto: 'ui.theme_auto', light: 'ui.theme_light', dark: 'ui.theme_dark'};
  const readPreference = () => {
    try {
      const saved = localStorage.getItem(KEY);
      return CYCLE.includes(saved) ? saved : 'auto';
    } catch { return 'auto'; }
  };
  let preference = readPreference();
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const resolved = () => preference === 'auto' ? (media && media.matches ? 'dark' : 'light') : preference;
  const label = () => {
    const i18n = window.MindexI18n;
    return i18n ? i18n.render(i18n.msg(KEYS[preference])) : {auto: '主题：跟随系统', light: '主题：浅色', dark: '主题：深色'}[preference];
  };
  const apply = () => {
    const root = document.documentElement;
    if (resolved() === 'dark') root.setAttribute('data-theme', 'dark');
    else root.removeAttribute('data-theme');
    for (const button of document.querySelectorAll('[data-theme-toggle]')) {
      button.dataset.mode = preference;
      button.setAttribute('aria-label', label());
      button.title = label();
    }
  };
  apply();
  const bind = () => {
    for (const button of document.querySelectorAll('[data-theme-toggle]')) {
      if (button.dataset.bound) continue;
      button.dataset.bound = '1';
      button.addEventListener('click', () => {
        preference = CYCLE[(CYCLE.indexOf(preference) + 1) % CYCLE.length];
        try { localStorage.setItem(KEY, preference); } catch {}
        apply();
      });
    }
    apply();
  };
  document.addEventListener('DOMContentLoaded', bind);
  window.addEventListener('mindex:languagechange', apply);
  if (media && media.addEventListener) media.addEventListener('change', () => { if (preference === 'auto') apply(); });
})();
