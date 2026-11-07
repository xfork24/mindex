"use strict";
// desktop-state.json：外壳自身的持久状态（端口、所选数据目录、上次后端进程、关闭窗口时是否留在后台）。
// 损坏或缺失时回落到默认值。
const fs = require("node:fs");
const path = require("node:path");

// keep_in_background：关闭窗口时缩到菜单栏/托盘继续运行（每日选题定时照常执行），缺省开启。
// background_hint_shown：首次缩到后台时的系统通知只提示一次。
const DEFAULTS = Object.freeze({
  port: null,
  data_dir: null,
  sidecar: null,
  keep_in_background: true,
  background_hint_shown: false,
});

function loadState(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) return { ...DEFAULTS };
    return {
      port:
        Number.isInteger(value.port) && value.port > 0 && value.port < 65536 ? value.port : null,
      data_dir:
        typeof value.data_dir === "string" && path.isAbsolute(value.data_dir)
          ? value.data_dir
          : null,
      sidecar:
        value.sidecar && Number.isInteger(value.sidecar.pid) ? { pid: value.sidecar.pid } : null,
      keep_in_background:
        typeof value.keep_in_background === "boolean" ? value.keep_in_background : true,
      background_hint_shown: value.background_hint_shown === true,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

// 原子写入：先写临时文件再改名，避免中途退出留下半截 JSON。
function saveState(file, state) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify({ ...DEFAULTS, ...state }, null, 2) + "\n");
  fs.renameSync(temporary, file);
}

module.exports = { DEFAULTS, loadState, saveState };
