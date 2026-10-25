"use strict";
// 只暴露只读的桌面标识；业务能力一律走 REST，前端检测不到 window.MindexDesktop 时保持 Web 行为。
const { contextBridge } = require("electron");

const version = (process.argv.find((arg) => arg.startsWith("--mindex-version=")) || "").split("=")[1] || "";

contextBridge.exposeInMainWorld("MindexDesktop", Object.freeze({ version, platform: process.platform }));
