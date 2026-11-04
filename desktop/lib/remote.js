"use strict";
// 远程模式凭据经系统安全存储加密；无配置才进入本地模式，损坏配置必须报错。
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const ENV_URL = "MINDEX_DESKTOP_REMOTE_URL";
const ENV_TOKEN = "MINDEX_DESKTOP_REMOTE_TOKEN";
const ERRORS = {
  "invalid-url": "远程服务地址无效：请填写不含路径、凭据或查询参数的 http(s) 地址",
  "invalid-token": "远程访问令牌不能为空",
  "invalid-config": "远程连接配置损坏；请在连接设置中重新保存或清除",
  "storage-unavailable": "系统安全凭据存储不可用；请解锁系统密钥环，或通过环境变量提供远程凭据",
  "decrypt-failed": "无法解密远程凭据；请在连接设置中重新保存",
};

function problem(code) {
  const error = new Error(ERRORS[code]);
  error.code = code;
  return error;
}

function normalizeUrl(raw) {
  const value = String(raw || "").trim().replace(/\/+$/, "");
  if (!value) return null;
  let parsed;
  try { parsed = new URL(value); } catch { return null; }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password ||
      parsed.search || parsed.hash || !["", "/"].includes(parsed.pathname)) return null;
  return parsed.origin;
}

function requireStorage(storage) {
  if (!storage?.isEncryptionAvailable() ||
      (process.platform === "linux" && ["basic_text", "unknown"].includes(storage.getSelectedStorageBackend()))) {
    throw problem("storage-unavailable");
  }
  return storage;
}

function remoteConfig({ env = process.env, userData, storage,
  readFile = (file) => fs.readFileSync(file, "utf8") } = {}) {
  if (env[ENV_URL] !== undefined || env[ENV_TOKEN] !== undefined) {
    const url = normalizeUrl(env[ENV_URL]);
    const token = String(env[ENV_TOKEN] || "").trim();
    if (!url) throw problem("invalid-url");
    if (!token) throw problem("invalid-token");
    return { url, token, source: "env" };
  }
  if (!userData) return null;
  const file = path.join(userData, "remote.json");
  let body;
  try { body = JSON.parse(readFile(file)); } catch (error) {
    if (error.code === "ENOENT") return null;
    throw problem("invalid-config");
  }
  const url = normalizeUrl(body?.url);
  if (!url) throw problem("invalid-url");
  // 旧明文只在加密存储可用且原子替换成功后迁移；失败保留原件并明确报错。
  if (typeof body.token === "string") return saveRemote({ url, token: body.token }, { userData, storage });
  if (body.credential?.version !== 1 || typeof body.credential?.ciphertext !== "string") {
    throw problem("invalid-config");
  }
  const secure = requireStorage(storage);
  let token;
  try { token = secure.decryptString(Buffer.from(body.credential.ciphertext, "base64")); }
  catch { throw problem("decrypt-failed"); }
  if (typeof token !== "string" || !token.trim()) throw problem("decrypt-failed");
  return { url, token, source: file };
}

function saveRemote({ url, token }, { userData, storage,
  writeFile = (file, body, options) => fs.writeFileSync(file, body, options),
  rename = (from, to) => fs.renameSync(from, to) } = {}) {
  const normalized = normalizeUrl(url);
  const value = String(token || "").trim();
  if (!normalized) throw problem("invalid-url");
  if (!value) throw problem("invalid-token");
  const secure = requireStorage(storage);
  let encrypted;
  try { encrypted = secure.encryptString(value).toString("base64"); }
  catch { throw problem("storage-unavailable"); }
  const file = path.join(userData, "remote.json");
  const temporary = `${file}.${randomUUID()}.partial`;
  try {
    writeFile(temporary, JSON.stringify({ url: normalized, credential: { version: 1, ciphertext: encrypted } }, null, 2),
      { mode: 0o600, flag: "wx" });
    rename(temporary, file);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
  return { url: normalized, token: value, source: file };
}

function clearRemote({ userData, unlink = (file) => fs.rmSync(file, { force: true }) } = {}) {
  unlink(path.join(userData, "remote.json"));
  return true;
}

module.exports = { ENV_URL, ENV_TOKEN, normalizeUrl, remoteConfig, saveRemote, clearRemote };
