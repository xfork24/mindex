"use strict";
// 菜单栏（macOS）/ 系统托盘（Windows）常驻：关闭窗口时只隐藏，sidecar 继续运行，每日选题定时照常执行。
// 纯逻辑（关闭判定、菜单项、图标）在这里，便于 node --test；Electron 对象只在 main.js 里使用。
const path = require("node:path");

// 关闭窗口时是否只隐藏：正在退出时一律放行；偏好关闭时按原行为关闭窗口（随后退出应用）。
// 托盘不可用（如无托盘守护进程的 Linux 桌面）时必须真正关闭——隐藏后没有图标可以唤回窗口。
function shouldHide({ quitting, keepInBackground, trayAvailable = true }) {
  return !quitting && trayAvailable && keepInBackground !== false;
}

// 托盘菜单：action 由 main.js 映射为具体操作；只有 quit 走优雅退出流程。
function menuItems(text, { keepInBackground }) {
  return [
    { action: "open", label: text.trayOpen },
    { action: "topics", label: text.trayTopics },
    { type: "separator" },
    {
      action: "toggle-background",
      label: text.trayKeep,
      type: "checkbox",
      checked: keepInBackground !== false,
    },
    { type: "separator" },
    { action: "quit", label: text.trayQuit },
  ];
}

// macOS 用模板图（系统按深浅色着色），其他平台用彩色图。
function iconPath(platform, desktopDir) {
  return path.join(desktopDir, "assets", platform === "darwin" ? "trayTemplate.png" : "tray.png");
}

// “今日选题”打开的页面：同源的选题挖掘页。
function topicsUrl(origin) {
  return origin ? `${origin}/studio/topics` : null;
}

module.exports = { shouldHide, menuItems, iconPath, topicsUrl };
