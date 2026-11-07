"use strict";
// 远程连接设置页：仅编辑 URL 与用户凭据，经 IPC 保存到 remote.json（0600）。
// 页面不接触后端管理凭据；保存后需重启应用以按远程模式连接。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld(
  "MindexRemote",
  Object.freeze({
    current: () => ipcRenderer.invoke("remote:get"),
    save: (value) => ipcRenderer.invoke("remote:save", value),
    clear: () => ipcRenderer.invoke("remote:clear"),
    close: () => ipcRenderer.invoke("remote:close"),
  }),
);
