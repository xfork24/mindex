"use strict";
// 窗口导航与新窗口的判定：只允许本后端 origin；外链只交给系统浏览器且仅限 http/https。

function parse(url) {
  try {
    return new URL(url);
  } catch {
    return null;
  }
}

function isAppUrl(url, origin) {
  const parsed = parse(url);
  return Boolean(parsed && origin && parsed.origin === origin);
}

function isExternalAllowed(url) {
  const parsed = parse(url);
  return Boolean(parsed && (parsed.protocol === "http:" || parsed.protocol === "https:"));
}

// 新窗口（target=_blank、window.open）：internal 在应用内打开，external 交给系统浏览器，deny 拒绝。
function windowOpenAction(url, origin) {
  if (isAppUrl(url, origin)) {
    return "internal";
  }
  if (isExternalAllowed(url)) {
    return "external";
  }
  return "deny";
}

// 外壳本地页面（启动页、错误页）上的按钮把地址的 hash 设为 #command=<命令>（页内导航，不离开页面），
// 主进程在 did-navigate-in-page 中读取并执行。只认 file: 页面上的白名单命令。
function shellCommand(url) {
  const parsed = parse(url);
  if (!parsed || parsed.protocol !== "file:") {
    return null;
  }
  const match = /^#command=([a-z]+)$/.exec(parsed.hash);
  return match && ["retry", "logs", "quit"].includes(match[1]) ? match[1] : null;
}

function isLocalPage(url, localPages = []) {
  const parsed = parse(url);
  return Boolean(
    parsed &&
    parsed.protocol === "file:" &&
    localPages.some((page) => parsed.pathname === new URL(`file://${page}`).pathname),
  );
}

// 页面内导航（will-navigate）：本 origin 与外壳本地页面放行；其余一律阻止（http/https 改由系统浏览器打开）。
// 拖入窗口的文件会导航到 file://，不在白名单内，因此被阻止。
function navigationAction(url, origin, localPages = []) {
  if (isAppUrl(url, origin) || isLocalPage(url, localPages)) {
    return "allow";
  }
  if (isExternalAllowed(url)) {
    return "external";
  }
  return "deny";
}

const ALLOWED_PERMISSIONS = new Set(["clipboard-sanitized-write", "fullscreen"]);

function permissionAllowed(permission, requestingUrl, origin) {
  return ALLOWED_PERMISSIONS.has(permission) && isAppUrl(requestingUrl, origin);
}

module.exports = {
  isAppUrl,
  isExternalAllowed,
  isLocalPage,
  windowOpenAction,
  navigationAction,
  shellCommand,
  permissionAllowed,
};
