/* Read the language before the parser reaches any visible page content. */
(function () {
  'use strict';
  let english = false;
  try { english = localStorage.getItem('mindex.language') === 'en'; } catch {}
  document.documentElement.lang = english ? 'en' : 'zh-CN';
  if (!english) return;
  document.documentElement.setAttribute('data-i18n-pending', '');
  const style = document.createElement('style');
  style.textContent = 'html[data-i18n-pending] body{visibility:hidden}';
  document.head.append(style);
})();
