"use strict";
// 外壳纯逻辑测试（node --test desktop/test）：不启动 Electron，不访问网络之外的资源。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { sessionValue } = require("../lib/auth");
const { dataDirProblem, ensureConfig, ensureFonts } = require("../lib/firstrun");
const { choosePort, isPortFree, freePort } = require("../lib/ports");
const security = require("../lib/security");
const sidecar = require("../lib/sidecar");
const { loadState, saveState } = require("../lib/state");
const { STRINGS, strings, format } = require("../lib/strings");

const temp = () => fs.mkdtempSync(path.join(os.tmpdir(), "mindex-desktop-"));

test("会话 Cookie 与后端 web_auth.session_value 一致", () => {
  // Python：hmac.new(b"test-token", b"mindex-web-session-v1", hashlib.sha256).hexdigest()
  assert.equal(sessionValue("test-token"), "ccd691643ee30372b2424135395e6882aa100a53a9f1af0fa8a4525afee2f75e");
});

test("状态文件：缺失、损坏与非法值回落到默认；原子写入", () => {
  const dir = temp();
  const file = path.join(dir, "nested", "desktop-state.json");
  assert.deepEqual(loadState(file), { port: null, data_dir: null, sidecar: null });
  saveState(file, { port: 51234, data_dir: "/Volumes/素材/data" });
  assert.deepEqual(loadState(file), { port: 51234, data_dir: "/Volumes/素材/data", sidecar: null });
  fs.writeFileSync(file, "{broken");
  assert.equal(loadState(file).port, null);
  fs.writeFileSync(file, JSON.stringify({ port: 70000, data_dir: "relative/dir", sidecar: { pid: "x" } }));
  assert.deepEqual(loadState(file), { port: null, data_dir: null, sidecar: null });
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ["desktop-state.json"]);
});

test("端口：保存的端口空闲时复用，被占用时换新端口并标记 changed", async () => {
  const saved = await freePort();
  assert.deepEqual(await choosePort(saved), { port: saved, changed: false });
  const blocker = net.createServer();
  await new Promise((resolve) => blocker.listen(saved, "127.0.0.1", resolve));
  try {
    assert.equal(await isPortFree(saved), false);
    const result = await choosePort(saved);
    assert.equal(result.changed, true);
    assert.notEqual(result.port, saved);
  } finally {
    blocker.close();
  }
  const first = await choosePort(null);
  assert.equal(first.changed, false);
  assert.ok(first.port > 0);
});

test("启动参数：全局选项在 serve 之前；dev 与 packaged 两种模式", () => {
  const dev = sidecar.buildCommand({ mode: "dev", repoRoot: "/repo", uv: "/opt/uv", configPath: "/u/config.yaml", dataDir: null, port: 5000 });
  assert.deepEqual(dev, { command: "/opt/uv", cwd: "/repo",
    args: ["run", "--project", "/repo", "mindex", "--config", "/u/config.yaml", "serve", "--host", "127.0.0.1", "--port", "5000"] });
  const packaged = sidecar.buildCommand({ mode: "packaged", backendExecutable: "/App/backend/mindex", configPath: "/u/config.yaml",
    dataDir: "/data dir", port: 6000, cwd: "/u" });
  assert.deepEqual(packaged.args, ["--config", "/u/config.yaml", "--data-dir", "/data dir", "serve", "--host", "127.0.0.1", "--port", "6000"]);
  assert.equal(packaged.command, "/App/backend/mindex");
  assert.ok(packaged.args.indexOf("--data-dir") < packaged.args.indexOf("serve"));
  assert.throws(() => sidecar.buildCommand({ mode: "other", configPath: "/c", port: 1 }), /未知/);
});

test("子进程环境：注入令牌与随包二进制，不继承 MINDEX_CONFIG", () => {
  const env = sidecar.buildEnv({ PATH: "/usr/bin", MINDEX_CONFIG: "/x.yaml", ELECTRON_RUN_AS_NODE: "1" },
    { token: "t", ffmpeg: "/R/bin/ffmpeg", ffprobe: "/R/bin/ffprobe", browsersPath: "/U/browsers", extra: { MINDEX_DESKTOP: "1" } });
  assert.equal(env.MINDEX_WEB_TOKEN, "t");
  assert.equal(env.MINDEX_FFMPEG, "/R/bin/ffmpeg");
  assert.equal(env.MINDEX_FFPROBE, "/R/bin/ffprobe");
  assert.equal(env.PLAYWRIGHT_BROWSERS_PATH, "/U/browsers");
  assert.equal(env.MINDEX_DESKTOP, "1");
  assert.equal(env.PATH, "/usr/bin");
  assert.ok(!("MINDEX_CONFIG" in env) && !("ELECTRON_RUN_AS_NODE" in env));
  const bare = sidecar.buildEnv({}, { token: "t" });
  assert.ok(!("MINDEX_FFMPEG" in bare) && !("PLAYWRIGHT_BROWSERS_PATH" in bare));
});

test("导航与新窗口：只认本 origin；外链仅 http/https；拖入的文件不能导航", () => {
  const origin = "http://127.0.0.1:5000";
  const page = "/App/pages/shell.html";
  assert.equal(security.navigationAction("http://127.0.0.1:5000/studio?job=1", origin, [page]), "allow");
  assert.equal(security.navigationAction("http://127.0.0.1:5001/", origin, [page]), "external");
  assert.equal(security.navigationAction("http://localhost:5000/", origin, [page]), "external");
  assert.equal(security.navigationAction("file:///Users/me/movie.mp4", origin, [page]), "deny");
  assert.equal(security.navigationAction("file:///App/pages/shell.html?mode=error", origin, [page]), "allow");
  assert.equal(security.navigationAction("javascript:alert(1)", origin, [page]), "deny");
  assert.equal(security.windowOpenAction("http://127.0.0.1:5000/admin", origin), "internal");
  assert.equal(security.windowOpenAction("https://example.com/", origin), "external");
  assert.equal(security.windowOpenAction("file:///etc/passwd", origin), "deny");
  assert.equal(security.windowOpenAction("smb://server/share", origin), "deny");
  assert.equal(security.windowOpenAction("http://127.0.0.1:5000/", null), "external");
});

test("外壳命令只认本地页面上的白名单", () => {
  assert.equal(security.shellCommand("file:///App/pages/shell.html?mode=error#command=retry"), "retry");
  assert.equal(security.shellCommand("file:///App/pages/shell.html#command=logs"), "logs");
  assert.equal(security.shellCommand("file:///App/pages/shell.html#command=rm"), null);
  assert.equal(security.shellCommand("http://127.0.0.1:5000/#command=quit"), null);
  assert.equal(security.isLocalPage("file:///App/pages/shell.html?x=1", ["/App/pages/shell.html"]), true);
  assert.equal(security.isLocalPage("file:///App/pages/other.html", ["/App/pages/shell.html"]), false);
});

test("权限：只允许本 origin 的剪贴板写入与全屏", () => {
  const origin = "http://127.0.0.1:5000";
  assert.equal(security.permissionAllowed("clipboard-sanitized-write", "http://127.0.0.1:5000/studio", origin), true);
  assert.equal(security.permissionAllowed("fullscreen", "http://127.0.0.1:5000", origin), true);
  assert.equal(security.permissionAllowed("geolocation", "http://127.0.0.1:5000/", origin), false);
  assert.equal(security.permissionAllowed("fullscreen", "https://example.com/", origin), false);
});

test("健康等待：成功、pid 不符、版本不符、进程退出、超时", async () => {
  const respond = (status, body) => async () => ({ status, json: async () => body });
  const ok = await sidecar.waitForHealth({ url: "u", token: "t", version: "0.1.0",
    fetchImpl: respond(200, { pid: 7, version: "0.1.0" }), pidMatches: (pid) => pid === 7 });
  assert.equal(ok.pid, 7);
  await assert.rejects(sidecar.waitForHealth({ url: "u", token: "t", fetchImpl: respond(200, { pid: 8 }), pidMatches: () => false }),
    (error) => error.code === "pid-mismatch");
  await assert.rejects(sidecar.waitForHealth({ url: "u", token: "t", version: "0.2.0", fetchImpl: respond(200, { pid: 7, version: "0.1.0" }) }),
    (error) => error.code === "version-mismatch");
  await assert.rejects(sidecar.waitForHealth({ url: "u", token: "t", fetchImpl: respond(401, {}) }),
    (error) => error.code === "pid-mismatch");
  await assert.rejects(sidecar.waitForHealth({ url: "u", token: "t", isRunning: () => false, fetchImpl: respond(200, {}) }),
    (error) => error.code === "exited");
  const refused = async () => { throw new Error("ECONNREFUSED"); };
  await assert.rejects(sidecar.waitForHealth({ url: "u", token: "t", timeoutMs: 300, intervalMs: 50, fetchImpl: refused }),
    (error) => error.code === "timeout" && /ECONNREFUSED/.test(error.message));
  let calls = 0;
  const later = async () => (++calls < 3 ? refused() : { status: 200, json: async () => ({ pid: 1, version: "v" }) });
  assert.equal((await sidecar.waitForHealth({ url: "u", token: "t", version: "v", intervalMs: 10, fetchImpl: later })).pid, 1);
});

test("后代进程判定与进程组停止", async () => {
  const parents = { 30: 20, 20: 10, 10: 1 };
  assert.equal(sidecar.isDescendant(30, 10, (pid) => parents[pid] || 0), true);
  assert.equal(sidecar.isDescendant(30, 99, (pid) => parents[pid] || 0), false);
  assert.equal(sidecar.isDescendant(10, 10, () => 0), true);

  let living = true;
  const signals = [];
  const graceful = await sidecar.stopGroup(42, { isAlive: () => living, kill: (pid, signal) => { signals.push([pid, signal]); living = false; } });
  assert.equal(graceful, false);
  assert.deepEqual(signals, [[-42, "SIGTERM"]]);
  const stubborn = [];
  const forced = await sidecar.stopGroup(43, { graceMs: 50, pollMs: 10, isAlive: () => true, kill: (pid, signal) => stubborn.push(signal) });
  assert.equal(forced, true);
  assert.deepEqual(stubborn, ["SIGTERM", "SIGKILL"]);
  assert.equal(await sidecar.stopGroup(44, { isAlive: () => false }), false);
});

test("遗留后端：只结束命令行像 mindex serve 的进程", async () => {
  const stopped = [];
  const stop = async (pid) => stopped.push(pid);
  assert.equal(await sidecar.reapStale(5, { isAlive: () => true, command: () => "/App/backend/mindex --config c serve --port 1", stop }), true);
  assert.equal(await sidecar.reapStale(6, { isAlive: () => true, command: () => "/usr/bin/vim notes.txt", stop }), false);
  assert.equal(await sidecar.reapStale(7, { isAlive: () => false, command: () => "mindex serve", stop }), false);
  assert.deepEqual(stopped, [5]);
});

test("真实子进程：detached 启动后整组停止，不残留孙进程", async () => {
  const { spawn } = require("node:child_process");
  const child = spawn("/bin/sh", ["-c", "sleep 30 & echo $!; wait"], { detached: true, stdio: ["ignore", "pipe", "ignore"] });
  const grandchild = Number(await new Promise((resolve) => child.stdout.once("data", (data) => resolve(String(data).trim()))));
  assert.ok(sidecar.alive(grandchild));
  await sidecar.stopGroup(child.pid, { graceMs: 3000 });
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(sidecar.alive(child.pid), false);
  assert.equal(sidecar.alive(grandchild), false);
});

test("日志轮转与尾部读取", () => {
  const dir = temp();
  const log = new sidecar.RotatingLog(path.join(dir, "logs", "backend.log"), { maxBytes: 100, keep: 3 });
  for (let index = 0; index < 12; index += 1) log.write(`line ${index} ${"x".repeat(20)}\n`);
  const files = fs.readdirSync(path.join(dir, "logs")).sort();
  assert.deepEqual(files, ["backend.log", "backend.log.1", "backend.log.2"]);
  assert.match(log.tail(), /line 11/);
  assert.ok(fs.statSync(path.join(dir, "logs", "backend.log")).size <= 100);
});

test("首次运行：配置只生成一次，字体按需复制", () => {
  const dir = temp();
  const template = path.join(dir, "template.yaml");
  fs.writeFileSync(template, "data_dir: data\n");
  const user = path.join(dir, "user");
  assert.equal(ensureConfig(user, template).created, true);
  fs.writeFileSync(path.join(user, "config.yaml"), "data_dir: mine\n");
  assert.equal(ensureConfig(user, template).created, false);
  assert.equal(fs.readFileSync(path.join(user, "config.yaml"), "utf8"), "data_dir: mine\n");
  const fonts = path.join(dir, "fonts");
  fs.mkdirSync(fonts);
  fs.writeFileSync(path.join(fonts, "A.otf"), "font-a");
  assert.deepEqual(ensureFonts(fonts, user), ["A.otf"]);
  assert.deepEqual(ensureFonts(fonts, user), []);
  fs.writeFileSync(path.join(fonts, "A.otf"), "font-a-v2");
  assert.deepEqual(ensureFonts(fonts, user), ["A.otf"]);
  assert.deepEqual(ensureFonts(path.join(dir, "missing"), user), []);
});

test("数据目录：空目录或含 metadata.sqlite3 的目录可用", () => {
  const dir = temp();
  assert.equal(dataDirProblem(dir), null);
  fs.writeFileSync(path.join(dir, ".DS_Store"), "");
  assert.equal(dataDirProblem(dir), null);
  fs.writeFileSync(path.join(dir, "notes.txt"), "x");
  assert.match(dataDirProblem(dir), /metadata\.sqlite3/);
  fs.writeFileSync(path.join(dir, "metadata.sqlite3"), "");
  assert.equal(dataDirProblem(dir), null);
  assert.match(dataDirProblem("relative"), /绝对路径/);
  assert.match(dataDirProblem(path.join(dir, "missing")), /无法读取/);
});

test("文案：中英文键一致；按系统语言选择", () => {
  assert.deepEqual(Object.keys(STRINGS.zh).sort(), Object.keys(STRINGS.en).sort());
  assert.equal(strings("zh-CN").edit, "编辑");
  assert.equal(strings("en-US").edit, "Edit");
  assert.equal(format("任务：{tasks}", { tasks: "studio" }), "任务：studio");
});

test("桌面版本号与后端 pyproject 一致", () => {
  const pkg = require("../package.json");
  const pyproject = fs.readFileSync(path.join(__dirname, "..", "..", "pyproject.toml"), "utf8");
  assert.equal(pkg.version, /^version\s*=\s*"([^"]+)"/m.exec(pyproject)[1]);
});
