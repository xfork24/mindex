"use strict";
// 外壳自身（菜单、对话框、本地页面）的文案：中文为源，系统语言非中文时用英文。页面文案仍由 Web 端 i18n 负责。
const STRINGS = {
  zh: {
    app: "Mindex",
    edit: "编辑", view: "显示", window: "窗口", help: "帮助",
    openLogs: "打开日志目录", openData: "打开数据目录", chooseData: "选择数据目录…", openConfig: "打开配置文件", reload: "重新加载",
    configChanged: "修改配置文件后需要重启后端才会生效。", restartBackend: "重启后端",
    chooseDataTitle: "选择 Mindex 数据目录",
    chooseDataPrompt: "选择空目录（新建素材库）或已有的 Mindex 数据目录（含 metadata.sqlite3）",
    chooseDataConfirm: "切换数据目录将重启后端。不要让 Web 版与桌面版同时使用同一个数据目录。",
    chooseDataInvalid: "无法使用该目录",
    restart: "重启", cancel: "取消", ok: "好",
    quitBusyTitle: "仍有任务在运行",
    quitBusy: "以下任务正在运行：{tasks}。退出会中断它们，下次启动时自动恢复为可重试状态。",
    quitAnyway: "仍要退出",
    portChangedTitle: "端口已变更",
    portChanged: "上次使用的端口被占用，已改用新端口。界面语言等偏好需要重新设置。",
    loading: "正在启动 Mindex…",
    errorTitle: "Mindex 启动失败",
    retry: "重试", logs: "打开日志", quit: "退出",
    backendExited: "后端进程意外退出",
  },
  en: {
    app: "Mindex",
    edit: "Edit", view: "View", window: "Window", help: "Help",
    openLogs: "Open Logs Folder", openData: "Open Data Folder", chooseData: "Choose Data Folder…", openConfig: "Open Config File", reload: "Reload",
    configChanged: "Restart the backend after editing the config file for changes to take effect.", restartBackend: "Restart Backend",
    chooseDataTitle: "Choose the Mindex data folder",
    chooseDataPrompt: "Choose an empty folder (new library) or an existing Mindex data folder (with metadata.sqlite3)",
    chooseDataConfirm: "Switching the data folder restarts the backend. Do not use the same data folder from the web and desktop versions at the same time.",
    chooseDataInvalid: "This folder cannot be used",
    restart: "Restart", cancel: "Cancel", ok: "OK",
    quitBusyTitle: "Tasks are still running",
    quitBusy: "These tasks are running: {tasks}. Quitting interrupts them; they become retryable on next launch.",
    quitAnyway: "Quit Anyway",
    portChangedTitle: "Port changed",
    portChanged: "The previous port was in use, so a new port is used. Interface preferences such as language need to be set again.",
    loading: "Starting Mindex…",
    errorTitle: "Mindex failed to start",
    retry: "Retry", logs: "Open Logs", quit: "Quit",
    backendExited: "The backend process exited unexpectedly",
  },
};

function strings(locale) {
  return String(locale || "").toLowerCase().startsWith("zh") ? STRINGS.zh : STRINGS.en;
}

function format(template, values) {
  return template.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
}

module.exports = { STRINGS, strings, format };
