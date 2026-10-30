'use strict';

// Keep transient feedback beside the controls that initiated the operation.
// Retain ancestor references so a re-rendered card can fall back to its section.
window.MindexFeedback = (() => {
  const messages = new Map();
  let dialogOrigins = new WeakMap();
  const selector = '[data-feedback-region], .panel, section, aside, article, form, dialog';
  let origin = [];
  const visible = element => element?.isConnected && !element.closest('[hidden]') &&
    (!element.closest('dialog') || element.closest('dialog').open);
  function capture(event) {
    const previous = origin;
    origin = [];
    for (let element = event.target; element?.closest; element = element.parentElement) {
      if (element.matches(selector)) origin.push(element);
    }
    const dialog = event.target?.closest?.('dialog');
    if (dialog) {
      if (!dialogOrigins.has(dialog)) dialogOrigins.set(dialog, previous.filter(element => !element.closest('dialog')));
      origin.push(...dialogOrigins.get(dialog));
    } else dialogOrigins = new WeakMap();
  }
  for (const event of ['click', 'submit', 'change', 'drop']) document.addEventListener(event, capture, true);
  function region(anchor) {
    const explicit = anchor?.closest(selector);
    if (visible(explicit)) return explicit;
    return origin.find(visible) ||
      [...document.querySelectorAll('main section, main aside, main form')].find(element =>
        visible(element) && !element.matches('.intro, .admin-intro, .studio-intro')) || document.querySelector('main');
  }
  function show(id, text, type = '', anchor = null) {
    const message = messages.get(id) || document.getElementById(id);
    const host = region(anchor);
    if (!message || !host) return;
    messages.set(id, message);
    if (message.parentElement !== host) host.append(message);
    MindexI18n.setText(message, text);
    message.className = `notice local-feedback ${type}`;
    message.dataset.type = type;
    MindexI18n.setAttr(message, 'title', text || '');
    message.hidden = !text;
  }
  for (const id of ['notice', 'admin-notice']) if (document.getElementById(id)) show(id, '');
  return {show};
})();
