"use strict";
// 外壳用到的路径。打包后资源在 macOS 的 Contents/Resources、Windows/Linux 的 resources 目录；
// 开发模式取仓库与 desktop/build（如已构建）。
const fs = require("node:fs");
const path = require("node:path");

const DESKTOP_DIR = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(DESKTOP_DIR, "..");

// Windows 的可执行文件带 .exe 后缀；随包冻结后端与 ffmpeg 三平台共用此规则。
function binaryName(name, platform = process.platform) {
  return platform === "win32" ? `${name}.exe` : name;
}

// resourcesPath：打包后为 process.resourcesPath；开发模式为 desktop/build。
function layout({ packaged, resourcesPath, userData, logs, platform = process.platform }) {
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
    backendExecutable: packaged ? path.join(resourcesPath, "backend", binaryName("mindex", platform))
      : existing(path.join(resources, "backend", binaryName("mindex", platform))),
    ffmpeg: existing(path.join(resources, packaged ? "bin" : "ffmpeg/bin", binaryName("ffmpeg", platform))),
    ffprobe: existing(path.join(resources, packaged ? "bin" : "ffmpeg/bin", binaryName("ffprobe", platform))),
    fonts: existing(path.join(resources, "fonts")),
    browsers: path.join(userData, "browsers"),
    pages: path.join(DESKTOP_DIR, "pages"),
    backendLog: path.join(logs, "backend.log"),
  };
}

module.exports = { DESKTOP_DIR, REPO_ROOT, binaryName, layout };
