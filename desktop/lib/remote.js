"use strict";
// 远程 API 客户端模式（plans §9 阶段 6）：桌面直接连接远程服务，不启动 sidecar。
// 远程数据处理/存储留在服务端，终端只持有用户凭据；不向终端分发 PG/S3 管理凭据。
//
// 配置来源（按优先级）：
//   1. 环境变量 MINDEX_DESKTOP_REMOTE_URL + MINDEX_DESKTOP_REMOTE_TOKEN（自动化/受管终端）
//   2. <userData>/remote.json：{ "url": "https://mindex.internal", "token": "..." }
// 缺失或非法时返回 null（回落到本地 sidecar 模式）。
const fs = require("node:fs");
const path = require("node:path");

const ENV_URL = "MINDEX_DESKTOP_REMOTE_URL";
const ENV_TOKEN = "MINDEX_DESKTOP_REMOTE_TOKEN";

function normalizeUrl(raw) {
  const value = String(raw || "").trim().replace(/\/+$/, "");
  if (!value) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (!["http:", "https:"].includes(parsed.protocol)) return null;
  return `${parsed.protocol}//${parsed.host}`;
}

// 远程凭据：只接受非空字符串；不做任何持久化写回（用户自行管理来源）。
function remoteConfig({ env = process.env, userData, readFile = (file) => fs.readFileSync(file, "utf8") } = {}) {
  const envUrl = normalizeUrl(env[ENV_URL]);
  const envToken = String(env[ENV_TOKEN] || "").trim();
  if (envUrl && envToken) return { url: envUrl, token: envToken, source: "env" };
  if (!userData) return null;
  const file = path.join(userData, "remote.json");
  let body;
  try {
    body = JSON.parse(readFile(file));
  } catch {
    return null;
  }
  const url = normalizeUrl(body?.url);
  const token = String(body?.token || "").trim();
  if (!url || !token) return null;
  return { url, token, source: file };
}

module.exports = { ENV_URL, ENV_TOKEN, normalizeUrl, remoteConfig };
