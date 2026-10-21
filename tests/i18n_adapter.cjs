// Chinese renderer for the existing small DOM controller adapters.
const catalog = require('../frontend/src/shared/messages.json');
function msg(key, params = {}) {
  const template = catalog[key]?.zh ?? key;
  return template.replace(/\{(\w+)\}/g, (_, name) => params[name] ?? `{${name}}`);
}
module.exports = {
  msg, render: value => String(value ?? ''),
  setText(node, value) { if (node) node.textContent = String(value ?? ''); },
  setAttr(node, attr, value) { node.setAttribute(attr, String(value ?? '')); if (['title','alt','placeholder','label'].includes(attr)) node[attr] = String(value ?? ''); },
  system(value) { return typeof value === 'object' ? value.message || value.detail || value.error || '' : value || ''; },
  builtin: value => value, voice: value => value.label, status: value => value,
  date: (value, time) => time ? new Date(value).toLocaleTimeString('zh-CN') : new Date(value).toLocaleString('zh-CN'),
};
