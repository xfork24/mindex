"use strict";
// 远程连接设置页逻辑：读取当前配置、保存/清除、错误提示（中文为源，英文随系统语言）。
const TEXT = {
  zh: {
    title: "连接远程服务",
    hint: "填写服务地址与用户访问令牌；远程数据处理与存储留在服务端，本机只保存连接信息。保存后需重启应用。",
    url: "服务地址",
    token: "用户访问令牌",
    save: "保存并重启",
    clear: "清除远程配置",
    cancel: "取消",
    saved: "已保存，正在重启…",
    cleared: "已清除远程配置，正在重启…",
    invalidUrl: "服务地址必须是 http(s) 地址，不能包含路径、凭据或查询参数",
    invalidToken: "请填写用户访问令牌",
    invalidConfig: "远程连接配置损坏；请重新保存或清除",
    storageUnavailable: "系统安全凭据存储不可用；请解锁系统密钥环或通过环境变量提供凭据",
    decryptFailed: "无法解密远程凭据；请重新保存",
    failed: "保存失败：",
    envNotice: "当前通过环境变量连接（MINDEX_DESKTOP_REMOTE_URL），保存文件后仍以环境变量为准。",
  },
  en: {
    title: "Connect to a remote server",
    hint: "Enter the server URL and your access token. Processing and storage stay on the server; only connection details are kept locally. Restart is required.",
    url: "Server URL",
    token: "Access token",
    save: "Save and restart",
    clear: "Clear remote settings",
    cancel: "Cancel",
    saved: "Saved. Restarting…",
    cleared: "Remote settings cleared. Restarting…",
    invalidUrl: "Use an http(s) server address without paths, credentials, or query parameters",
    invalidToken: "Access token is required",
    invalidConfig: "Remote settings are damaged. Save them again or clear them.",
    storageUnavailable:
      "Secure credential storage is unavailable. Unlock the system keyring or use environment variables.",
    decryptFailed: "The remote credential could not be decrypted. Save it again.",
    failed: "Save failed: ",
    envNotice:
      "Connected via environment variables (MINDEX_DESKTOP_REMOTE_URL); they take precedence over the saved file.",
  },
};

const lang = (navigator.language || "zh").toLowerCase().startsWith("zh") ? "zh" : "en";
const t = TEXT[lang];
const errorText = (value) =>
  t[
    {
      "invalid-url": "invalidUrl",
      "invalid-token": "invalidToken",
      "invalid-config": "invalidConfig",
      "storage-unavailable": "storageUnavailable",
      "decrypt-failed": "decryptFailed",
    }[value?.code]
  ] ||
  value?.error ||
  "";

const $ = (id) => document.getElementById(id);
const status = (message, kind) => {
  const node = $("status");
  node.textContent = message;
  node.className = kind || "";
};

for (const node of document.querySelectorAll("[data-i18n]")) {
  const key = node.dataset.i18n;
  if (t[key]) node.textContent = t[key];
}

window.MindexRemote.current().then((value) => {
  if (value?.error) status(errorText(value), "error");
  if (value?.url) $("url").value = value.url;
  if (value?.source === "env") status(t.envNotice);
});

$("save").addEventListener("click", async () => {
  const url = $("url").value.trim();
  const token = $("token").value.trim();
  if (!/^https?:\/\//i.test(url)) return status(t.invalidUrl, "error");
  if (!token) return status(t.invalidToken, "error");
  const result = await window.MindexRemote.save({ url, token });
  if (!result?.ok) return status(t.failed + errorText(result), "error");
  status(t.saved, "ok");
  setTimeout(() => window.MindexRemote.close(), 600);
});

$("clear").addEventListener("click", async () => {
  const result = await window.MindexRemote.clear();
  if (!result?.ok) return status(t.failed + errorText(result), "error");
  status(t.cleared, "ok");
  setTimeout(() => window.MindexRemote.close(), 600);
});

$("cancel").addEventListener("click", () => window.MindexRemote.close());
