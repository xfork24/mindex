"use strict";
// 只暴露只读的桌面标识；业务能力一律走 REST，前端检测不到 window.MindexDesktop 时保持 Web 行为。
const { contextBridge } = require("electron");

const argument = (name) => (process.argv.find((arg) => arg.startsWith(`--${name}=`)) || "").split("=")[1] || "";
const version = argument("mindex-version");
// background：关闭窗口时是否留在菜单栏/托盘继续运行（窗口加载时的偏好，只读）。选题页据此提示定时照常执行。
const background = argument("mindex-background") === "1";

contextBridge.exposeInMainWorld("MindexDesktop", Object.freeze({ version, platform: process.platform, background }));
