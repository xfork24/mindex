"use strict";
// 外壳用到的路径。打包后资源在 Contents/Resources；开发模式取仓库与 desktop/build（如已构建）。
const fs = require("node:fs");
const path = require("node:path");

const DESKTOP_DIR = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(DESKTOP_DIR, "..");

// resourcesPath：打包后为 process.resourcesPath；开发模式为 desktop/build。
function layout({ packaged, resourcesPath, userData, logs }) {
  const resources = packaged ? resourcesPath : path.join(DESKTOP_DIR, "build");
  const existing = (file) => (fs.existsSync(file) ? file : null);
  return {
    packaged,
    repoRoot: REPO_ROOT,
    desktopDir: DESKTOP_DIR,
    userData,
    logs,
    stateFile: path.join(userData, "desktop-state.json"),
    template: packaged ? path.join(resourcesPath, "config.template.yaml") : path.join(DESKTOP_DIR, "config.template.yaml"),
    backendExecutable: packaged ? path.join(resourcesPath, "backend", "mindex") : existing(path.join(resources, "backend", "mindex")),
    ffmpeg: existing(path.join(resources, packaged ? "bin" : "ffmpeg/bin", "ffmpeg")),
    ffprobe: existing(path.join(resources, packaged ? "bin" : "ffmpeg/bin", "ffprobe")),
    fonts: existing(path.join(resources, "fonts")),
    browsers: path.join(userData, "browsers"),
    pages: path.join(DESKTOP_DIR, "pages"),
    backendLog: path.join(logs, "backend.log"),
  };
}

module.exports = { DESKTOP_DIR, REPO_ROOT, layout };
