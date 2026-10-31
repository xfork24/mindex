// 桌面外壳冒烟：真实 Electron + 真实 mindex serve（开发模式 uv run，或 MINDEX_SMOKE_APP 指定已安装的 .app 可执行文件）。
// 用户数据与日志放在临时目录（MINDEX_DESKTOP_HOME），不碰 ~/Library。
const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const desktop = path.join(__dirname, "..", "desktop");
const { _electron } = require(path.join(desktop, "node_modules", "playwright-core"));
const packaged = process.env.MINDEX_SMOKE_APP || "";
const isWin = process.platform === "win32";
const appImage = packaged.endsWith(".AppImage");
const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "mindex-smoke-")));
const downloads = path.join(home, "downloads");
fs.mkdirSync(downloads);
const APP = /^http:\/\/127\.0\.0\.1:\d+\//;
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
// Windows 没有对其他进程的 SIGKILL；dev 模式杀的还是 uv.exe 树，用 taskkill /T /F 连子进程一起结束。
const killTree = (pid) => {
  if (isWin) childProcess.execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
  else process.kill(pid, "SIGKILL");
};
// Linux CI 容器里 Electron 需要 --no-sandbox（chrome-sandbox 未设 SUID）；AppImage 还需解包运行。
const electronArgs = (appDir) => [
  ...(appDir ? [appDir] : []),
  ...(process.platform === "linux" ? ["--no-sandbox"] : []),
];
const launchEnv = () => ({
  ...process.env,
  ...(appImage ? { APPIMAGE_EXTRACT_AND_RUN: "1" } : {}),
});

async function waitFor(predicate, timeout, message) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(message);
}

(async () => {
  const app = await _electron.launch({
    executablePath: packaged || require(path.join(desktop, "node_modules", "electron")),
    args: electronArgs(packaged ? null : desktop),
    env: { ...launchEnv(), MINDEX_DESKTOP_HOME: home, MINDEX_DESKTOP_DOWNLOAD_DIR: downloads, MINDEX_DESKTOP_FORCE_QUIT: "1" },
    timeout: 180000,
  });
  let sidecarPid = null;
  try {
    const win = await app.firstWindow();
    await win.waitForURL(APP, { timeout: 180000 });
    const origin = new URL(win.url()).origin;

    // 1. 渲染进程隔离：没有 Node 能力，只有只读的 MindexDesktop。
    const isolated = await win.evaluate(() => ({
      require: typeof require, process: typeof process, desktop: window.MindexDesktop,
      frozen: Object.isFrozen(window.MindexDesktop),
    }));
    assert.equal(isolated.require, "undefined");
    assert.equal(isolated.process, "undefined");
    assert.equal(isolated.desktop.version, require(path.join(desktop, "package.json")).version);
    assert.equal(isolated.desktop.platform, process.platform);
    assert.equal(isolated.frozen, true);

    // 2. 五个主页面与文案、风格、交付模板及选题子页都直接可用（认证由外壳注入，不出现登录页）。
    for (const page of ["/", "/admin", "/persons", "/studio", "/settings", "/studio/templates", "/studio/brands", "/studio/delivery-templates", "/studio/topics"]) {
      const response = await win.goto(`${origin}${page}`);
      assert.equal(response.status(), 200, page);
      assert.ok(!win.url().includes("/login"), `${page} 被重定向到登录页`);
    }
    // 选题挖掘页与 Web 端同一套前端：标签顺序一致，档案接口经外壳认证可用。
    const tabs = await win.$$eval(".studio-tabs a", (links) => links.map((a) => a.getAttribute("href")));
    assert.deepEqual(tabs, ["/studio", "/studio/topics", "/studio/templates", "/studio/brands", "/studio/delivery-templates"]);
    await win.locator("#topics-empty").waitFor({ timeout: 15000 });
    const profiles = await win.evaluate(async () => (await fetch("/api/topics/profiles")).status);
    assert.equal(profiles, 200);

    // 3. 页面脚本自身的请求（不带任何凭证设置）与 SSE 都通过认证。
    const health = await win.evaluate(async () => {
      const response = await fetch("/api/health", { credentials: "omit" });
      return { status: response.status, body: await response.json() };
    });
    assert.equal(health.status, 200);
    assert.equal(health.body.busy, false);
    sidecarPid = health.body.pid;
    assert.ok(alive(sidecarPid));
    assert.ok(health.body.data_dir.startsWith(home), health.body.data_dir);
    const sse = await win.evaluate(() => new Promise((resolve) => {
      const source = new EventSource("/api/agent/changes?after=-1");
      const timer = setTimeout(() => { source.close(); resolve("timeout"); }, 15000);
      source.addEventListener("ready", () => { clearTimeout(timer); source.close(); resolve("ready"); });
      source.onerror = () => { clearTimeout(timer); source.close(); resolve("error"); };
    }));
    assert.equal(sse, "ready");

    // 3b. 托盘常驻：渲染进程能读到后台偏好；选题页提示定时照常执行（sidecar 运行着每日调度）。
    assert.equal(isolated.desktop.background, true);
    const created = await win.evaluate(async () => (await fetch("/api/topics/profiles", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "冒烟档案", domain: "测试", schedule: { enabled: true, time: "23:59", count: 1 } }),
    })).status);
    assert.equal(created, 201);
    await win.goto(`${origin}/studio/topics`);
    await win.locator("#schedule-status", { hasText: "后台运行中" }).waitFor({ timeout: 15000 });
    // 关闭窗口只隐藏，后端继续运行；点 Dock（activate）重新显示。
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
    await waitFor(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map((w) => w.isVisible()))
      .then((visible) => visible.length === 1 && visible[0] === false), 10000, "关闭窗口后窗口应隐藏而不是销毁");
    assert.ok(alive(sidecarPid));
    const hiddenHealth = await win.evaluate(async () => (await fetch("/api/health")).status);
    assert.equal(hiddenHealth, 200);
    assert.equal(JSON.parse(fs.readFileSync(path.join(home, "user-data", "desktop-state.json"), "utf8")).background_hint_shown, true);
    await app.evaluate(({ app: electronApp }) => electronApp.emit("activate"));
    await waitFor(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()), 10000,
      "activate 后窗口应重新显示");

    // 4. 下载经外壳保存（测试时直接写入临时目录，正式使用弹出保存对话框）。
    await win.evaluate(() => {
      const link = Object.assign(document.createElement("a"), { href: "/api/health", download: "health.json" });
      document.body.append(link);
      link.click();
    });
    await waitFor(() => fs.existsSync(path.join(downloads, "health.json")) && fs.statSync(path.join(downloads, "health.json")).size > 0,
      15000, "下载没有完成");
    assert.equal(JSON.parse(fs.readFileSync(path.join(downloads, "health.json"), "utf8")).pid, sidecarPid);

    // 5. 同源新窗口在应用内打开，并同样通过认证。
    const [child] = await Promise.all([app.waitForEvent("window"), win.evaluate(() => window.open("/admin", "_blank"))]);
    await child.waitForLoadState();
    assert.match(child.url(), new RegExp(`^${origin}/admin`));
    assert.ok(!child.url().includes("/login"));
    await child.close();

    // 6. 不允许导航到本地文件（例如误把文件拖进窗口）。
    const before = win.url();
    const localFileUrl = isWin ? "file:///c:/windows/win.ini" : "file:///etc/hosts";
    await win.evaluate((url) => { location.href = url; }, localFileUrl);
    await new Promise((resolve) => setTimeout(resolve, 800));
    assert.equal(win.url(), before);

    // 7. 后端意外退出：显示错误页；点“重试”后恢复。
    killTree(sidecarPid);
    await win.waitForURL(/shell\.html\?.*mode=error/, { timeout: 30000 });
    assert.match(await win.textContent("#message"), /意外退出|exited/);
    await win.click("#retry");
    await win.waitForURL(APP, { timeout: 180000 });
    sidecarPid = (await win.evaluate(async () => (await (await fetch("/api/health")).json()).pid));
    assert.ok(alive(sidecarPid));
    const state = JSON.parse(fs.readFileSync(path.join(home, "user-data", "desktop-state.json"), "utf8"));
    assert.equal(new URL(win.url()).port, String(state.port));  // 端口持久化
    assert.ok(state.sidecar && state.sidecar.pid);
  } finally {
    await app.close();
  }
  // 8. 退出应用后后端进程不残留，状态里不再记录后端进程。
  await waitFor(() => !alive(sidecarPid), 15000, "退出后后端进程仍在运行");
  const state = JSON.parse(fs.readFileSync(path.join(home, "user-data", "desktop-state.json"), "utf8"));
  assert.equal(state.sidecar, null);
  assert.ok(fs.existsSync(path.join(home, "user-data", "config.yaml")));
  const backendLog = fs.readFileSync(path.join(home, "logs", "backend.log"), "utf8");
  const expectedMode = packaged || process.env.MINDEX_DESKTOP_BACKEND === "frozen" ? "packaged" : "dev";
  assert.ok(backendLog.includes(`启动后端（${expectedMode}）`), `后端模式应为 ${expectedMode}`);

  // 9. 选定的数据目录（菜单“选择数据目录…”写入状态）以 --data-dir 传给后端；端口沿用上次。
  const custom = path.join(home, "custom-data");
  fs.mkdirSync(custom);
  fs.writeFileSync(path.join(home, "user-data", "desktop-state.json"), JSON.stringify({ ...state, data_dir: custom }));
  const again = await _electron.launch({
    executablePath: packaged || require(path.join(desktop, "node_modules", "electron")),
    args: electronArgs(packaged ? null : desktop),
    env: { ...launchEnv(), MINDEX_DESKTOP_HOME: home, MINDEX_DESKTOP_FORCE_QUIT: "1" },
    timeout: 180000,
  });
  try {
    const win = await again.firstWindow();
    await win.waitForURL(APP, { timeout: 180000 });
    assert.equal(new URL(win.url()).port, String(state.port));
    const body = await win.evaluate(async () => (await fetch("/api/health")).json());
    assert.equal(body.data_dir, custom);
    assert.ok(fs.existsSync(path.join(custom, "metadata.sqlite3")));
    sidecarPid = body.pid;
  } finally {
    await again.close();
  }
  await waitFor(() => !alive(sidecarPid), 15000, "第二次退出后后端进程仍在运行");
  fs.rmSync(home, { recursive: true, force: true });
  console.log("desktop smoke ok");
})().catch((error) => {
  console.error(error);
  console.error(`日志：${path.join(home, "logs", "backend.log")}`);
  process.exit(1);
});
