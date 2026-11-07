"use strict";
// 首次运行：生成配置、复制随包字体；校验用户选择的数据目录。
const fs = require("node:fs");
const path = require("node:path");

// 配置只在不存在时生成，之后不覆盖用户修改。返回 {path, created}。
function ensureConfig(userData, template) {
  const target = path.join(userData, "config.yaml");
  if (fs.existsSync(target)) {
    return { path: target, created: false };
  }
  fs.mkdirSync(userData, { recursive: true });
  fs.copyFileSync(template, target);
  return { path: target, created: true };
}

// 随包字体复制到用户数据目录 fonts/：渲染签名记录相对配置目录的字体路径，应用移动或升级后保持不变。
// 内容（大小）不同才覆盖。返回复制的文件名列表。
function ensureFonts(sourceDir, userData) {
  if (!sourceDir || !fs.existsSync(sourceDir)) {
    return [];
  }
  const target = path.join(userData, "fonts");
  fs.mkdirSync(target, { recursive: true });
  const copied = [];
  for (const name of fs.readdirSync(sourceDir)) {
    const from = path.join(sourceDir, name);
    if (!fs.statSync(from).isFile()) {
      continue;
    }
    const to = path.join(target, name);
    if (fs.existsSync(to) && fs.statSync(to).size === fs.statSync(from).size) {
      continue;
    }
    fs.copyFileSync(from, to);
    copied.push(name);
  }
  return copied;
}

// 数据目录：空目录（新建库）或已有 Mindex 数据目录（含 metadata.sqlite3）。返回 null 表示可用，否则为原因。
function dataDirProblem(dir) {
  if (!dir || !path.isAbsolute(dir)) {
    return "请选择一个绝对路径的目录";
  }
  let entries;
  try {
    entries = fs.readdirSync(dir).filter((name) => name !== ".DS_Store");
  } catch {
    return "无法读取该目录";
  }
  if (entries.length === 0 || entries.includes("metadata.sqlite3")) {
    return null;
  }
  return "该目录既不是空目录，也不是 Mindex 数据目录（缺少 metadata.sqlite3）";
}

module.exports = { ensureConfig, ensureFonts, dataDirProblem };
