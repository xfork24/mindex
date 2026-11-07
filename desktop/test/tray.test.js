"use strict";
// 菜单栏/托盘常驻的纯逻辑：关闭判定、菜单项、图标路径与“今日选题”地址。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { STRINGS } = require("../lib/strings");
const tray = require("../lib/tray");

test("关闭窗口：偏好开启且未在退出时只隐藏；退出中或偏好关闭时真正关闭", () => {
  assert.equal(tray.shouldHide({ quitting: false, keepInBackground: true }), true);
  assert.equal(tray.shouldHide({ quitting: false, keepInBackground: undefined }), true); // 缺省开启
  assert.equal(tray.shouldHide({ quitting: true, keepInBackground: true }), false);
  assert.equal(tray.shouldHide({ quitting: false, keepInBackground: false }), false);
});

test("关闭窗口：托盘不可用（如无托盘守护进程的 Linux）时不隐藏，避免窗口无法唤回", () => {
  assert.equal(
    tray.shouldHide({ quitting: false, keepInBackground: true, trayAvailable: false }),
    false,
  );
  assert.equal(
    tray.shouldHide({ quitting: false, keepInBackground: undefined, trayAvailable: false }),
    false,
  );
  assert.equal(
    tray.shouldHide({ quitting: false, keepInBackground: true, trayAvailable: true }),
    true,
  );
  assert.equal(
    tray.shouldHide({ quitting: false, keepInBackground: true, trayAvailable: undefined }),
    true,
  ); // 缺省可用
});

test("托盘菜单：打开、今日选题、保留后台（勾选）、退出", () => {
  for (const text of [STRINGS.zh, STRINGS.en]) {
    const items = tray.menuItems(text, { keepInBackground: true });
    assert.deepEqual(
      items.filter((i) => i.action).map((i) => i.action),
      ["open", "topics", "toggle-background", "quit"],
    );
    assert.ok(items.every((i) => i.type === "separator" || i.label));
    const toggle = items.find((i) => i.action === "toggle-background");
    assert.equal(toggle.type, "checkbox");
    assert.equal(toggle.checked, true);
    assert.equal(
      tray
        .menuItems(text, { keepInBackground: false })
        .find((i) => i.action === "toggle-background").checked,
      false,
    );
  }
  assert.equal(tray.menuItems(STRINGS.zh, { keepInBackground: true }).at(-1).label, "退出 Mindex");
});

test("图标：macOS 用模板图，其他平台用彩色图，文件随包分发", () => {
  const desktop = path.join(__dirname, "..");
  const mac = tray.iconPath("darwin", desktop);
  const win = tray.iconPath("win32", desktop);
  assert.match(mac, /trayTemplate\.png$/);
  assert.match(win, /tray\.png$/);
  for (const file of [mac, win, path.join(desktop, "assets", "trayTemplate@2x.png")]) {
    assert.ok(fs.statSync(file).size > 0, file);
  }
  const pkg = require("../package.json");
  assert.ok(pkg.build.files.includes("assets/**"));
});

test("今日选题：同源的选题挖掘页", () => {
  assert.equal(tray.topicsUrl("http://127.0.0.1:51234"), "http://127.0.0.1:51234/studio/topics");
  assert.equal(tray.topicsUrl(null), null);
});
