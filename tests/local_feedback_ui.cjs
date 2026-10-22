const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag, cls = '') {this.tag = tag;this.className = cls;this.children = [];this.dataset = {};this.open = false;this.hidden = false;}
  get isConnected() {return this.tag === 'main' || !!this.parentElement?.isConnected;}
  matches(selector) {return selector.split(',').some(s => {s = s.trim();return s === this.tag || s.startsWith('.') && this.className.split(' ').includes(s.slice(1)) || s === '[hidden]' && this.hidden;});}
  closest(selector) {return this.matches(selector) ? this : this.parentElement?.closest(selector);}
  append(child) {child.remove();this.children.push(child);child.parentElement = this;}
  remove() {if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this);this.parentElement = null;}
}
const main = new Element('main'), intro = new Element('section', 'intro'), section = new Element('section'),
  panel = new Element('div', 'panel'), control = new Element('button'), notice = new Element('div', 'notice'),
  dialog = new Element('dialog'), dialogButton = new Element('button');
main.append(intro);main.append(notice);main.append(section);section.append(panel);panel.append(control);main.append(dialog);dialog.append(dialogButton);
const events = {};
const document = {
  addEventListener(type, fn) {events[type] = fn;},
  getElementById(id) {return id === 'notice' ? notice : null;},
  querySelectorAll() {return [intro, section];},
  querySelector() {return main;},
};
const sandbox = {window: {}, document, MindexI18n: {setText(element, text) {element.textContent = text;}}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('frontend/src/shared/feedback.js', 'utf8'), sandbox);
const feedback = sandbox.window.MindexFeedback;
assert.equal(notice.parentElement, section, 'initial status belongs inside a functional section');
assert.equal(notice.hidden, true);
events.click({target: control});
feedback.show('notice', '保存成功', 'info');
assert.equal(notice.parentElement, panel);
assert.equal(notice.textContent, '保存成功');
assert.equal(notice.hidden, false);
feedback.show('notice', '失败原因', 'error');
assert.equal(panel.children.filter(c => c === notice).length, 1, 'updates replace the single line');
assert.equal(notice.title, '失败原因');
assert.equal(notice.dataset.type, 'error');
// Removing/rebuilding a card must not discard or strand its status element.
panel.remove();
feedback.show('notice', '已刷新');
assert.equal(notice.parentElement, section);
events.submit({target: section});
dialog.open = true;
events.click({target: dialogButton});
feedback.show('notice', '弹窗内错误', 'error');
assert.equal(notice.parentElement, dialog);
dialog.open = false;
feedback.show('notice', '保存完成');
assert.equal(notice.parentElement, section, 'closed dialogs return feedback to the initiating section');
section.hidden = true;
const other = new Element('section');main.append(other);
document.querySelectorAll = () => [intro, section, other];
feedback.show('notice', '当前区域');
assert.equal(notice.parentElement, other, 'feedback must not land inside a hidden pane');
feedback.show('notice', '');
assert.equal(notice.hidden, true);
assert.match(fs.readFileSync('frontend/src/shared/style.css', 'utf8'), /\.notice\.local-feedback\{[^}]*white-space:nowrap/);
// Studio's existing dialog error area must be the only recipient of an error.
const studio = fs.readFileSync('frontend/src/studio/studio.js', 'utf8');
let globalErrors = 0, dialogErrors = 0;
const workDialog = {open: true};
const errorContext = {
  $(id) {return id === 'work-dialog' ? workDialog : id === 'dialog-content' ? {prepend() {dialogErrors++;}} : null;},
  el() {return {setAttribute() {}};}, shortIds: text => text,
  notice() {globalErrors++;},
};
vm.createContext(errorContext);
vm.runInContext(studio.slice(studio.indexOf('function fail(error)'), studio.indexOf('async function api(')), errorContext);
errorContext.fail(Error('弹窗操作失败'));
assert.equal(dialogErrors, 1);
assert.equal(globalErrors, 0);
workDialog.open = false;
errorContext.fail(Error('区域操作失败'));
assert.equal(globalErrors, 1);
console.log('Local feedback checks passed');
