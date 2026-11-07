const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const catalog = require("../frontend/src/shared/messages.json");
const context = {
  window: { MindexMessages: catalog, addEventListener() {}, dispatchEvent() {} },
  document: {
    readyState: "loading",
    addEventListener() {},
    documentElement: { childNodes: [] },
    querySelectorAll() {
      return [];
    },
  },
  localStorage: {
    getItem() {
      return "en";
    },
    setItem() {},
  },
  CustomEvent: class {},
};
context.window.scrollX = context.window.scrollY = 0;
vm.runInNewContext(fs.readFileSync("frontend/src/shared/i18n.js", "utf8"), context);
const i = context.window.MindexI18n;
// Every diagnostic template, including nested historical conflicts, resolves in English.
for (const [key, entry] of Object.entries(catalog)) {
  if (key.startsWith("system.")) {
    continue;
  }
  const params = Object.fromEntries(
    [...entry.zh.matchAll(/\{(\w+)\}/g)].map((m) => [m[1], "example"]),
  );
  const token = i.system({ message_key: key, message_params: params });
  assert.ok(!/[一-鿿]/.test(i.render(token)), key);
}
const raw = "上游 recall 不兼容：查询、召回方式或召回设置已变化；阶段参数与当前方案不一致";
const token = i.system(raw);
assert.ok(!/[一-鿿]/.test(i.render(token)), i.render(token));
i.setLanguage("zh");
assert.match(i.render(token), /阶段参数与当前方案不一致/);
i.setLanguage("en");
assert.ok(!/[一-鿿]/.test(i.render(token)));
// UI content parameters never get translated by dictionary matching.
assert.match(
  i.render(i.msg("ui.conflict_upstream", { p0: "用户输入", p1: "阶段参数与当前方案不一致" })),
  /阶段参数与当前方案不一致/,
);
assert.match(
  i.render(i.system("修改领域档案「阶段参数与当前方案不一致」")),
  /阶段参数与当前方案不一致/,
);
assert.match(
  i.render(i.system("参数 召回权重（视觉）：原值 3，当前 1")),
  /Recall weight \(visual\)/,
);
console.log(
  "PASS: all catalog entries, nested diagnostics, repeated language switching, content boundary",
);
