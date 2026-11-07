"use strict";
// Mindex 桌面外壳：启动并守护 mindex serve（sidecar），窗口加载它提供的同一套 Web 前端。
// 这里只有进程生命周期、认证注入、窗口行为与菜单；业务能力一律在后端，经 REST 访问。
const childProcess = require("node:child_process");
const path = require("node:path");

const {
  app,
  BrowserWindow,
  Menu,
  Notification,
  Tray,
  dialog,
  nativeImage,
  safeStorage,
  session,
  shell,
} = require("electron");

const { COOKIE, newToken, sessionValue } = require("./lib/auth");
const { dataDirProblem, ensureConfig, ensureFonts } = require("./lib/firstrun");
const { layout } = require("./lib/paths");
const { remoteConfig } = require("./lib/remote");
const { choosePort, HOST } = require("./lib/ports");
const security = require("./lib/security");
const sidecarLib = require("./lib/sidecar");
const { loadState, saveState } = require("./lib/state");
const { format, strings } = require("./lib/strings");
const trayLib = require("./lib/tray");

// 测试与调试：MINDEX_DESKTOP_HOME 把用户数据与日志放到隔离目录，不碰真实的 ~/Library。
const HOME = process.env.MINDEX_DESKTOP_HOME;
if (HOME) {
  app.setPath("userData", path.join(HOME, "user-data"));
  app.setAppLogsPath(path.join(HOME, "logs"));
}

const VERSION = app.getVersion();
let paths = null;
let text = strings("zh");
let mainWindow = null;
let quitting = false;
let shuttingDown = false;
let tray = null;
let trayUnavailable = false;

// ------------------------------------------------------------------ sidecar

const backend = {
  child: null,
  origin: null,
  token: null,
  stopping: false,
  log: null,

  mode() {
    if (paths.packaged) {
      return "packaged";
    }
    // 开发时验证冻结后端：MINDEX_DESKTOP_BACKEND=frozen 且 desktop/build/backend/mindex 已构建。
    if (process.env.MINDEX_DESKTOP_BACKEND === "frozen" && paths.backendExecutable) {
      return "packaged";
    }
    return "dev";
  },

  async start() {
    // 远程 API 客户端模式（plans §9 阶段 6）：不启动 sidecar，直接连接服务端。
    // 连接信息可在菜单「Mindex → 连接远程服务…」中维护（remote.json，0600）。
    // 远程数据处理与存储留在服务端；终端只持有用户凭据。
    const remote = remoteConfig({ userData: paths.userData, storage: safeStorage });
    if (remote) {
      this.child = null;
      this.token = remote.token;
      this.origin = remote.url;
      this.log = this.log || new sidecarLib.RotatingLog(paths.backendLog);
      this.log.write(
        `\n===== ${new Date().toISOString()} 远程 API 模式：${remote.url}（凭据来源 ${remote.source}）\n`,
      );
      this.remote = true;
      const health = await sidecarLib.waitForHealth({
        url: `${this.origin}/api/health`,
        token: this.token,
        version: null,
        timeoutMs: 30000,
        pidMatches: () => true, // 远程进程不在本机：以令牌接受为准（启动即校验）
      });
      await this.authorize();
      return { health, changed: false };
    }
    this.remote = false;
    const state = loadState(paths.stateFile);
    if (state.sidecar?.pid) {
      await sidecarLib.reapStale(state.sidecar.pid);
    }
    const { port, changed } = await choosePort(state.port);
    const config = ensureConfig(paths.userData, paths.template);
    ensureFonts(paths.fonts, paths.userData);
    const dataDir = state.data_dir && !dataDirProblem(state.data_dir) ? state.data_dir : null;
    const mode = this.mode();
    const command = sidecarLib.buildCommand({
      mode,
      repoRoot: paths.repoRoot,
      backendExecutable: paths.backendExecutable,
      uv: process.env.MINDEX_DESKTOP_UV || "uv",
      configPath: config.path,
      dataDir,
      port,
      cwd: paths.userData,
    });
    this.token = newToken();
    this.origin = `http://${HOST}:${port}`;
    this.log = this.log || new sidecarLib.RotatingLog(paths.backendLog);
    this.log.write(
      `\n===== ${new Date().toISOString()} 启动后端（${mode}）：${command.command} ${command.args.join(" ")}\n`,
    );
    const env = sidecarLib.buildEnv(process.env, {
      token: this.token,
      ffmpeg: paths.ffmpeg,
      ffprobe: paths.ffprobe,
      browsersPath: paths.browsers,
      extra: { MINDEX_DESKTOP: "1" },
    });
    this.stopping = false;
    const child = childProcess.spawn(command.command, command.args, {
      cwd: command.cwd,
      env,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true, // Windows：冻结后端为控制台程序，避免闪黑框（stdio 管道不受影响）
    });
    this.child = child;
    const exited = { value: false };
    child.stdout.on("data", (chunk) => this.log.write(chunk));
    child.stderr.on("data", (chunk) => this.log.write(chunk));
    child.on("error", (error) => {
      exited.value = true;
      this.log.write(`后端无法启动：${error.message}\n`);
    });
    child.on("exit", (code, signal) => {
      exited.value = true;
      this.log.write(`===== 后端退出：code=${code} signal=${signal}\n`);
      if (this.child === child) {
        this.child = null;
      }
      if (!this.stopping && !quitting) {
        showError(text.backendExited, this.log.tail());
      }
    });
    saveState(paths.stateFile, { ...state, port, sidecar: { pid: child.pid } });
    const health = await sidecarLib.waitForHealth({
      url: `${this.origin}/api/health`,
      token: this.token,
      version: VERSION,
      timeoutMs: 120000,
      isRunning: () => !exited.value,
      // packaged 模式 exe 即服务进程，pid 严格相等；dev 模式 uv 间接拉起 python，按父链上溯。
      // Windows dev 模式父链经 PowerShell 查询、不可靠，放宽为只靠令牌与版本校验（两者都是强校验）。
      pidMatches:
        mode !== "packaged" && process.platform === "win32"
          ? () => true
          : (pid) => pid === child.pid || sidecarLib.isDescendant(pid, child.pid),
    });
    await this.authorize();
    return { health, changed };
  },

  // 只对本 origin 附带令牌：请求头覆盖 fetch/XHR/EventSource，会话 Cookie 覆盖不经 webRequest 的请求。
  async authorize() {
    const origin = this.origin;
    const token = this.token;
    session.defaultSession.webRequest.onBeforeSendHeaders(
      { urls: [`${origin}/*`] },
      (details, callback) => {
        callback({
          requestHeaders: { ...details.requestHeaders, Authorization: `Bearer ${token}` },
        });
      },
    );
    await session.defaultSession.cookies.set({
      url: origin,
      name: COOKIE,
      value: sessionValue(token),
      httpOnly: true,
      sameSite: "strict",
      path: "/",
    });
  },

  async request(pathname, { method = "GET", timeoutMs = 3000 } = {}) {
    if (!this.origin || (!this.child && !this.remote)) {
      return null;
    }
    try {
      const response = await fetch(`${this.origin}${pathname}`, {
        method,
        headers: { Authorization: `Bearer ${this.token}` },
        signal: AbortSignal.timeout(timeoutMs),
      });
      return response.ok ? await response.json().catch(() => ({})) : null;
    } catch {
      return null;
    }
  },

  async stop() {
    if (this.remote) {
      // 远程模式：没有本机副作用需要清理
      this.stopping = true;
      return;
    }
    this.stopping = true;
    const child = this.child;
    if (child?.pid) {
      const forced = await sidecarLib.stopGroup(child.pid, { graceMs: 10000 });
      if (forced) {
        this.log?.write("===== 后端未在 10 秒内退出，已强制结束\n");
      }
    }
    this.child = null;
    const state = loadState(paths.stateFile);
    saveState(paths.stateFile, { ...state, sidecar: null });
  },
};

// ------------------------------------------------------------------ 窗口

function webPreferences() {
  return {
    preload: path.join(__dirname, "preload.js"),
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    webSecurity: true,
    spellcheck: false,
    additionalArguments: [
      `--mindex-version=${VERSION}`,
      `--mindex-background=${keepInBackground() ? "1" : "0"}`,
    ],
  };
}

const shellPage = () => path.join(paths.pages, "shell.html");

function showPage(query) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  mainWindow.loadFile(shellPage(), { query: { lang: app.getLocale(), ...query } });
}

function showLoading() {
  showPage({ mode: "loading", title: text.app, message: text.loading });
}

function showError(message, detail = "") {
  showPage({
    mode: "error",
    title: text.errorTitle,
    message,
    detail: detail.slice(-6000),
    retry: text.retry,
    logs: text.logs,
    quit: text.quit,
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: text.app,
    show: false,
    webPreferences: webPreferences(),
  });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  // 关闭窗口：偏好开启时只隐藏，sidecar 继续运行（每日选题定时照常执行）；从托盘菜单“退出”才真正退出。
  // 托盘不可用时直接关闭（随后退出应用）——隐藏后没有图标可以唤回窗口。
  mainWindow.on("close", (event) => {
    if (
      !trayLib.shouldHide({
        quitting: quitting || shuttingDown,
        keepInBackground: keepInBackground(),
        trayAvailable: !trayUnavailable,
      })
    ) {
      return;
    }
    event.preventDefault();
    mainWindow.hide();
    backgroundHint();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  showLoading();
}

function showWindow() {
  if (!mainWindow) {
    createWindow();
  } else {
    if (mainWindow.isMinimized()) {
      mainWindow.restore();
    }
    mainWindow.show();
    mainWindow.focus();
  }
}

// ------------------------------------------------------------------ 托盘 / 菜单栏

function keepInBackground() {
  return paths ? loadState(paths.stateFile).keep_in_background !== false : true;
}

function backgroundHint() {
  const state = loadState(paths.stateFile);
  if (state.background_hint_shown) {
    return;
  }
  saveState(paths.stateFile, { ...state, background_hint_shown: true });
  // 隔离测试（MINDEX_DESKTOP_HOME）不弹系统通知。
  if (HOME || !Notification.isSupported()) {
    return;
  }
  new Notification({ title: text.backgroundTitle, body: text.backgroundBody, silent: true }).show();
}

function trayAction(action) {
  if (action === "open") {
    showWindow();
  } else if (action === "topics") {
    showWindow();
    const url = trayLib.topicsUrl(backend.origin);
    if (url && backend.child) {
      mainWindow?.loadURL(url);
    }
  } else if (action === "toggle-background") {
    const state = loadState(paths.stateFile);
    saveState(paths.stateFile, { ...state, keep_in_background: !keepInBackground() });
    refreshTray();
  } else if (action === "quit") {
    app.quit();
  }
}

function refreshTray() {
  if (!tray) {
    return;
  }
  const items = trayLib
    .menuItems(text, { keepInBackground: keepInBackground() })
    .map((item) =>
      item.type === "separator" ? item : { ...item, click: () => trayAction(item.action) },
    );
  tray.setContextMenu(Menu.buildFromTemplate(items));
}

function createTray() {
  // Linux 无托盘守护进程（如未装托盘扩展的 GNOME）时 Tray 可能抛错：降级为无托盘运行，
  // 关闭窗口改为真正退出，并在本次启动提示一次（见 strings 的 trayUnavailable 文案）。
  try {
    const image = nativeImage.createFromPath(trayLib.iconPath(process.platform, paths.desktopDir));
    if (process.platform === "darwin") {
      image.setTemplateImage(true);
    }
    tray = new Tray(image);
    tray.setToolTip(text.trayTooltip);
    // Windows：单击托盘图标打开窗口；macOS 单击即弹出菜单。
    if (process.platform !== "darwin") {
      tray.on("click", showWindow);
    }
    refreshTray();
  } catch (error) {
    trayUnavailable = true;
    tray = null;
    backend.log?.write(`托盘不可用，已降级为关闭窗口即退出：${error?.message ?? error}\n`);
    if (!HOME) {
      dialog
        .showMessageBox(mainWindow ?? undefined, {
          type: "info",
          title: text.trayUnavailableTitle,
          message: text.trayUnavailable,
          buttons: [text.ok],
        })
        .catch(() => {});
    }
  }
}

// 所有 webContents（主窗口与应用内新窗口）统一的导航、新窗口与外壳命令处理。
function guard(contents) {
  contents.on("will-navigate", (event, url) => {
    const action = security.navigationAction(url, backend.origin, [shellPage()]);
    if (action === "allow") {
      return;
    }
    event.preventDefault();
    if (action === "external") {
      shell.openExternal(url);
    }
  });
  contents.setWindowOpenHandler(({ url }) => {
    const action = security.windowOpenAction(url, backend.origin);
    if (action === "internal") {
      return {
        action: "allow",
        overrideBrowserWindowOptions: { webPreferences: webPreferences() },
      };
    }
    if (action === "external") {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
  contents.on("did-navigate-in-page", (_, url) => {
    if (!security.isLocalPage(url, [shellPage()])) {
      return;
    }
    const command = security.shellCommand(url);
    if (command === "retry") {
      restart().catch((error) => showError(error.message, backend.log?.tail()));
    } else if (command === "logs") {
      shell.openPath(paths.logs);
    } else if (command === "quit") {
      app.quit();
    }
  });
}

async function launch() {
  showLoading();
  try {
    const { changed } = await backend.start();
    if (!mainWindow) {
      return;
    }
    await mainWindow.loadURL(`${backend.origin}/`);
    if (changed) {
      dialog.showMessageBox(mainWindow, {
        type: "info",
        title: text.portChangedTitle,
        message: text.portChanged,
        buttons: [text.ok],
      });
    }
  } catch (error) {
    await backend.stop();
    showError(error.message, backend.log?.tail());
  }
}

async function restart() {
  await backend.stop();
  await launch();
}

// ------------------------------------------------------------------ 菜单

async function chooseDataDir() {
  const result = await dialog.showOpenDialog(mainWindow, {
    title: text.chooseDataTitle,
    message: text.chooseDataPrompt,
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || !result.filePaths.length) {
    return;
  }
  const dir = result.filePaths[0];
  const problem = dataDirProblem(dir);
  if (problem) {
    await dialog.showMessageBox(mainWindow, {
      type: "warning",
      title: text.chooseDataInvalid,
      message: problem,
      buttons: [text.ok],
    });
    return;
  }
  const confirm = await dialog.showMessageBox(mainWindow, {
    type: "question",
    message: text.chooseDataConfirm,
    detail: dir,
    buttons: [text.restart, text.cancel],
    defaultId: 0,
    cancelId: 1,
  });
  if (confirm.response !== 0) {
    return;
  }
  saveState(paths.stateFile, { ...loadState(paths.stateFile), data_dir: dir });
  await restart();
}

function dataDirectory() {
  return loadState(paths.stateFile).data_dir || path.join(paths.userData, "data");
}

// ---------------------------------------------------------------- 远程连接设置

let remoteWindow = null;

async function openRemoteSettings() {
  const { remoteConfig } = require("./lib/remote");
  if (remoteWindow) {
    remoteWindow.focus();
    return;
  }
  remoteWindow = new BrowserWindow({
    width: 520,
    height: 360,
    title: text.remoteSettings,
    parent: mainWindow || undefined,
    modal: Boolean(mainWindow),
    show: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    webPreferences: {
      preload: path.join(__dirname, "remote-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  let current;
  let connectionError;
  let connectionErrorCode;
  try {
    current = remoteConfig({ userData: paths.userData, storage: safeStorage });
  } catch (error) {
    connectionError = error.message;
    connectionErrorCode = error.code;
  }
  const { ipcMain } = require("electron");
  const handlers = {
    "remote:get": () =>
      connectionError
        ? { error: connectionError, code: connectionErrorCode }
        : current
          ? { url: current.url, source: current.source }
          : null,
    "remote:save": (_event, value) => {
      try {
        const { saveRemote } = require("./lib/remote");
        saveRemote(value, { userData: paths.userData, storage: safeStorage });
        setImmediate(() => restartAfterRemoteChange());
        return { ok: true };
      } catch (error) {
        return { ok: false, error: error.message, code: error.code };
      }
    },
    "remote:clear": () => {
      try {
        const { clearRemote } = require("./lib/remote");
        clearRemote({ userData: paths.userData });
        setImmediate(() => restartAfterRemoteChange());
        return { ok: true };
      } catch (error) {
        return { ok: false, error: error.message, code: error.code };
      }
    },
    "remote:close": () => remoteWindow?.close(),
  };
  for (const [channel, handler] of Object.entries(handlers)) {
    ipcMain.handle(channel, handler);
  }
  remoteWindow.on("closed", () => {
    for (const channel of Object.keys(handlers)) {
      ipcMain.removeHandler(channel);
    }
    remoteWindow = null;
  });
  await remoteWindow.loadFile(path.join(paths.pages, "remote.html"));
  remoteWindow.show();
}

function restartAfterRemoteChange() {
  restart().catch((error) => showError(error.message, backend.log?.tail()));
}

function buildMenu() {
  const template = [
    { role: "appMenu" },
    {
      label: text.edit,
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "pasteAndMatchStyle" },
        { role: "delete" },
        { role: "selectAll" },
      ],
    },
    {
      label: text.view,
      submenu: [
        {
          label: text.reload,
          accelerator: "CmdOrCtrl+R",
          click: () => mainWindow?.webContents.reload(),
        },
        ...(paths.packaged ? [] : [{ role: "toggleDevTools" }]),
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: text.app,
      submenu: [
        { label: text.openData, click: () => shell.openPath(dataDirectory()) },
        {
          label: text.chooseData,
          click: () => chooseDataDir().catch((error) => showError(error.message)),
        },
        { type: "separator" },
        {
          label: text.openConfig,
          click: () => shell.openPath(path.join(paths.userData, "config.yaml")),
        },
        {
          label: text.remoteSettings,
          click: () => openRemoteSettings().catch((error) => showError(error.message)),
        },
        {
          label: text.restartBackend,
          click: () => restart().catch((error) => showError(error.message, backend.log?.tail())),
        },
        { type: "separator" },
        { label: text.openLogs, click: () => shell.openPath(paths.logs) },
      ],
    },
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ------------------------------------------------------------------ 退出

async function shutdown() {
  const health = await backend.request("/api/health");
  if (health?.busy && process.env.MINDEX_DESKTOP_FORCE_QUIT !== "1") {
    const answer = await dialog.showMessageBox(mainWindow ?? undefined, {
      type: "warning",
      title: text.quitBusyTitle,
      message: text.quitBusyTitle,
      detail: format(text.quitBusy, { tasks: health.tasks.join("、") }),
      buttons: [text.quitAnyway, text.cancel],
      defaultId: 1,
      cancelId: 1,
    });
    if (answer.response !== 0) {
      return false;
    }
  }
  // 先停“退出时停止”的本机模型服务（未开启本机服务管理时接口返回 403/404，忽略）。
  // 远程模式不触碰服务端的本机服务（那是服务端主机的资源，不属于本终端）。
  if (!backend.remote) {
    await backend.request("/api/settings/local-services/stop-on-exit", {
      method: "POST",
      timeoutMs: 20000,
    });
  }
  for (const window of BrowserWindow.getAllWindows()) {
    window.destroy();
  }
  await backend.stop();
  return true;
}

// ------------------------------------------------------------------ 启动

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (paths) {
      showWindow();
    }
  });
  // macOS：点 Dock 图标重新显示隐藏的窗口。
  app.on("activate", () => {
    if (paths) {
      showWindow();
    }
  });

  app.on("web-contents-created", (_, contents) => guard(contents));

  app.on("before-quit", (event) => {
    if (quitting) {
      return;
    }
    event.preventDefault();
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    shutdown()
      .then((done) => {
        shuttingDown = false;
        if (done) {
          quitting = true;
          app.quit();
        }
      })
      .catch(() => {
        quitting = true;
        app.quit();
      });
  });

  // 只有在不保留后台（偏好关闭）时窗口才会真正关闭，此时按原行为退出。
  app.on("window-all-closed", () => app.quit());
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => app.quit());
  }

  app.whenReady().then(() => {
    paths = layout({
      packaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      userData: app.getPath("userData"),
      logs: app.getPath("logs"),
    });
    // Windows：系统通知按 AppUserModelID 归属应用。
    if (process.platform === "win32") {
      app.setAppUserModelId(process.execPath);
    }
    text = strings(app.getLocale());
    session.defaultSession.setPermissionRequestHandler((_, permission, callback, details) =>
      callback(security.permissionAllowed(permission, details.requestingUrl, backend.origin)),
    );
    session.defaultSession.setPermissionCheckHandler((_, permission, requestingOrigin) =>
      security.permissionAllowed(permission, requestingOrigin, backend.origin),
    );
    // 下载：缺省由 Electron 弹出保存对话框；测试时以 MINDEX_DESKTOP_DOWNLOAD_DIR 直接保存。
    session.defaultSession.on("will-download", (_, item) => {
      const directory = process.env.MINDEX_DESKTOP_DOWNLOAD_DIR;
      if (directory) {
        item.setSavePath(path.join(directory, item.getFilename()));
      }
    });
    buildMenu();
    createWindow();
    createTray();
    launch();
  });
}
