"use strict";
// electron-builder afterPack：在生成 dmg/zip 之前对整个 .app 做 ad-hoc 签名（没有 Apple Developer ID 时）。
// Apple Silicon 上没有签名的可执行文件无法运行；随包的冻结后端与 ffmpeg 里每个 Mach-O 都先单独签名，再签整个 .app。
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const MAGIC = new Set(["feedfacf", "cffaedfe", "cafebabe", "bebafeca"]);

function isMachO(file) {
  try {
    const handle = fs.openSync(file, "r");
    const buffer = Buffer.alloc(4);
    fs.readSync(handle, buffer, 0, 4, 0);
    fs.closeSync(handle);
    return MAGIC.has(buffer.toString("hex"));
  } catch {
    return false;
  }
}

function* files(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isSymbolicLink()) {
      continue;
    }
    if (entry.isDirectory()) {
      yield* files(full);
    } else if (entry.isFile()) {
      yield full;
    }
  }
}

function sign(target, extra = []) {
  childProcess.execFileSync(
    "codesign",
    ["--force", "--sign", "-", "--timestamp=none", ...extra, target],
    { stdio: "pipe" },
  );
}

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") {
    return;
  }
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const resources = path.join(app, "Contents", "Resources");
  let count = 0;
  for (const sub of ["backend", "bin"]) {
    const dir = path.join(resources, sub);
    if (!fs.existsSync(dir)) {
      throw new Error(`安装包缺少 Resources/${sub}`);
    }
    for (const file of files(dir)) {
      if (!isMachO(file)) {
        continue;
      }
      sign(file);
      count += 1;
    }
  }
  sign(app, ["--deep"]);
  childProcess.execFileSync("codesign", ["--verify", "--deep", "--strict", app], { stdio: "pipe" });
  console.log(`  • ad-hoc 签名：随包 Mach-O ${count} 个，整个 ${path.basename(app)} 已校验`);
};
