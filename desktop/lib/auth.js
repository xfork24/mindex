"use strict";
// 会话令牌与会话 Cookie。Cookie 值必须与后端 shared/web_auth.py 的 session_value() 一致：
// HMAC-SHA256(key=令牌, msg="mindex-web-session-v1") 的十六进制。
const crypto = require("node:crypto");

const COOKIE = "mindex_session";

function newToken() {
  return crypto.randomBytes(32).toString("base64url");
}

function sessionValue(token) {
  return crypto.createHmac("sha256", token).update("mindex-web-session-v1").digest("hex");
}

module.exports = { COOKIE, newToken, sessionValue };
