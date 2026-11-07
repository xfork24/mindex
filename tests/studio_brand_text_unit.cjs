// 风格模板显示文字：真实 studio-template-text.js + messages.json 目录，不依赖浏览器。
// 内置模板的名称与说明必须走文案目录（中英双语），用户模板原样显示用户内容。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const catalog = require("./i18n_adapter.cjs");
const messages = JSON.parse(
  fs.readFileSync(path.join(__dirname, "../frontend/src/shared/messages.json"), "utf8"),
);
const sandbox = { MindexI18n: catalog };
vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "../frontend/src/studio/studio-template-text.js"), "utf8"),
  sandbox,
);
const run = (code) => vm.runInContext(code, sandbox);

const PLAIN = { id: "plain", name: "简洁", description: "白字细描边字幕。", builtin: true };
const NEWS = { id: "news", name: "新闻资讯", description: "半透明底条字幕。", builtin: true };
const TECH = { id: "tech", name: "科技评测", description: "粗描边字幕。", builtin: true };
const USER = { id: "user-abc", name: "我的风格", description: "自己写的说明", builtin: false };

// 1. 内置模板的名称与说明走文案目录；目录里每个键都有中英两种文案。
for (const [item, nameKey, descKey] of [
  [PLAIN, "ui.brands_name_plain", "ui.brands_desc_plain"],
  [NEWS, "ui.brands_name_news", "ui.brands_desc_news"],
  [TECH, "ui.brands_name_tech", "ui.brands_desc_tech"],
]) {
  assert.equal(run(`brandName(${JSON.stringify(item)})`), messages[nameKey].zh);
  assert.equal(run(`brandDescription(${JSON.stringify(item)})`), messages[descKey].zh);
  assert.ok(messages[nameKey].en && messages[descKey].en, `${nameKey}/${descKey} 缺少英文文案`);
  assert.notEqual(messages[nameKey].en, messages[nameKey].zh);
}
// 2. 用户模板是用户内容：名称与说明原样显示，不进文案目录。
assert.equal(run(`brandName(${JSON.stringify(USER)})`), "我的风格");
assert.equal(run(`brandDescription(${JSON.stringify(USER)})`), "自己写的说明");
assert.equal(run(`brandDescription(${JSON.stringify({ ...USER, description: "" })})`), "");
console.log("studio brand text ok");
