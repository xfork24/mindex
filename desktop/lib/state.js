"use strict";
// desktop-state.json：外壳自身的持久状态（端口、所选数据目录、上次后端进程）。损坏或缺失时回落到默认值。
const fs = require("node:fs");
const path = require("node:path");

const DEFAULTS = Object.freeze({ port: null, data_dir: null, sidecar: null });

function loadState(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) return { ...DEFAULTS };
    return {
      port: Number.isInteger(value.port) && value.port > 0 && value.port < 65536 ? value.port : null,
      data_dir: typeof value.data_dir === "string" && path.isAbsolute(value.data_dir) ? value.data_dir : null,
      sidecar: value.sidecar && Number.isInteger(value.sidecar.pid) ? { pid: value.sidecar.pid } : null,
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
