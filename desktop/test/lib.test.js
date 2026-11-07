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
  assert.equal(
    sessionValue("test-token"),
    "ccd691643ee30372b2424135395e6882aa100a53a9f1af0fa8a4525afee2f75e",
  );
});

test("状态文件：缺失、损坏与非法值回落到默认；原子写入", () => {
  const dir = temp();
  const file = path.join(dir, "nested", "desktop-state.json");
  const defaults = {
    port: null,
    data_dir: null,
    sidecar: null,
    keep_in_background: true,
    background_hint_shown: false,
  };
  assert.deepEqual(loadState(file), defaults);
  saveState(file, { port: 51234, data_dir: "/Volumes/素材/data" });
  assert.deepEqual(loadState(file), { ...defaults, port: 51234, data_dir: "/Volumes/素材/data" });
  fs.writeFileSync(file, "{broken");
  assert.equal(loadState(file).port, null);
  fs.writeFileSync(
    file,
    JSON.stringify({
      port: 70000,
      data_dir: "relative/dir",
      sidecar: { pid: "x" },
      keep_in_background: "no",
      background_hint_shown: 1,
    }),
  );
  assert.deepEqual(loadState(file), defaults);
  saveState(file, { keep_in_background: false, background_hint_shown: true });
  assert.deepEqual(loadState(file), {
    ...defaults,
    keep_in_background: false,
    background_hint_shown: true,
  });
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
  const dev = sidecar.buildCommand({
    mode: "dev",
    repoRoot: "/repo",
    uv: "/opt/uv",
    configPath: "/u/config.yaml",
    dataDir: null,
    port: 5000,
  });
  assert.deepEqual(dev, {
    command: "/opt/uv",
    cwd: "/repo",
    args: [
      "run",
      "--project",
      "/repo",
      "mindex",
      "--config",
      "/u/config.yaml",
      "serve",
      "--host",
      "127.0.0.1",
      "--port",
      "5000",
    ],
  });
  const packaged = sidecar.buildCommand({
    mode: "packaged",
    backendExecutable: "/App/backend/mindex",
    configPath: "/u/config.yaml",
    dataDir: "/data dir",
    port: 6000,
    cwd: "/u",
  });
  assert.deepEqual(packaged.args, [
    "--config",
    "/u/config.yaml",
    "--data-dir",
    "/data dir",
    "serve",
    "--host",
    "127.0.0.1",
    "--port",
    "6000",
  ]);
  assert.equal(packaged.command, "/App/backend/mindex");
  assert.ok(packaged.args.indexOf("--data-dir") < packaged.args.indexOf("serve"));
  assert.throws(() => sidecar.buildCommand({ mode: "other", configPath: "/c", port: 1 }), /未知/);
});

test("子进程环境：注入令牌与随包二进制，不继承 MINDEX_CONFIG", () => {
  const env = sidecar.buildEnv(
    { PATH: "/usr/bin", MINDEX_CONFIG: "/x.yaml", ELECTRON_RUN_AS_NODE: "1" },
    {
      token: "t",
      ffmpeg: "/R/bin/ffmpeg",
      ffprobe: "/R/bin/ffprobe",
      browsersPath: "/U/browsers",
      extra: { MINDEX_DESKTOP: "1" },
    },
  );
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
  assert.equal(
    security.navigationAction("http://127.0.0.1:5000/studio?job=1", origin, [page]),
    "allow",
  );
  assert.equal(security.navigationAction("http://127.0.0.1:5001/", origin, [page]), "external");
  assert.equal(security.navigationAction("http://localhost:5000/", origin, [page]), "external");
  assert.equal(security.navigationAction("file:///Users/me/movie.mp4", origin, [page]), "deny");
  assert.equal(
    security.navigationAction("file:///App/pages/shell.html?mode=error", origin, [page]),
    "allow",
  );
  assert.equal(security.navigationAction("javascript:alert(1)", origin, [page]), "deny");
  assert.equal(security.windowOpenAction("http://127.0.0.1:5000/admin", origin), "internal");
  assert.equal(security.windowOpenAction("https://example.com/", origin), "external");
  assert.equal(security.windowOpenAction("file:///etc/passwd", origin), "deny");
  assert.equal(security.windowOpenAction("smb://server/share", origin), "deny");
  assert.equal(security.windowOpenAction("http://127.0.0.1:5000/", null), "external");
});

test("外壳命令只认本地页面上的白名单", () => {
  assert.equal(
    security.shellCommand("file:///App/pages/shell.html?mode=error#command=retry"),
    "retry",
  );
  assert.equal(security.shellCommand("file:///App/pages/shell.html#command=logs"), "logs");
  assert.equal(security.shellCommand("file:///App/pages/shell.html#command=rm"), null);
  assert.equal(security.shellCommand("http://127.0.0.1:5000/#command=quit"), null);
  assert.equal(
    security.isLocalPage("file:///App/pages/shell.html?x=1", ["/App/pages/shell.html"]),
    true,
  );
  assert.equal(
    security.isLocalPage("file:///App/pages/other.html", ["/App/pages/shell.html"]),
    false,
  );
});

test("权限：只允许本 origin 的剪贴板写入与全屏", () => {
  const origin = "http://127.0.0.1:5000";
  assert.equal(
    security.permissionAllowed("clipboard-sanitized-write", "http://127.0.0.1:5000/studio", origin),
    true,
  );
  assert.equal(security.permissionAllowed("fullscreen", "http://127.0.0.1:5000", origin), true);
  assert.equal(security.permissionAllowed("geolocation", "http://127.0.0.1:5000/", origin), false);
  assert.equal(security.permissionAllowed("fullscreen", "https://example.com/", origin), false);
});

test("健康等待：成功、pid 不符、版本不符、进程退出、超时", async () => {
  const respond = (status, body) => async () => ({ status, json: async () => body });
  const ok = await sidecar.waitForHealth({
    url: "u",
    token: "t",
    version: "0.1.0",
    fetchImpl: respond(200, { pid: 7, version: "0.1.0" }),
    pidMatches: (pid) => pid === 7,
  });
  assert.equal(ok.pid, 7);
  await assert.rejects(
    sidecar.waitForHealth({
      url: "u",
      token: "t",
      fetchImpl: respond(200, { pid: 8 }),
      pidMatches: () => false,
    }),
    (error) => error.code === "pid-mismatch",
  );
  await assert.rejects(
    sidecar.waitForHealth({
      url: "u",
      token: "t",
      version: "0.2.0",
      fetchImpl: respond(200, { pid: 7, version: "0.1.0" }),
    }),
    (error) => error.code === "version-mismatch",
  );
  await assert.rejects(
    sidecar.waitForHealth({ url: "u", token: "t", fetchImpl: respond(401, {}) }),
    (error) => error.code === "pid-mismatch",
  );
  await assert.rejects(
    sidecar.waitForHealth({
      url: "u",
      token: "t",
      isRunning: () => false,
      fetchImpl: respond(200, {}),
    }),
    (error) => error.code === "exited",
  );
  const refused = async () => {
    throw new Error("ECONNREFUSED");
  };
  await assert.rejects(
    sidecar.waitForHealth({
      url: "u",
      token: "t",
      timeoutMs: 300,
      intervalMs: 50,
      fetchImpl: refused,
    }),
    (error) => error.code === "timeout" && /ECONNREFUSED/.test(error.message),
  );
  let calls = 0;
  const later = async () =>
    ++calls < 3 ? refused() : { status: 200, json: async () => ({ pid: 1, version: "v" }) };
  assert.equal(
    (
      await sidecar.waitForHealth({
        url: "u",
        token: "t",
        version: "v",
        intervalMs: 10,
        fetchImpl: later,
      })
    ).pid,
    1,
  );
});

test("后代进程判定与进程组停止", async () => {
  const parents = { 30: 20, 20: 10, 10: 1 };
  assert.equal(
    sidecar.isDescendant(30, 10, (pid) => parents[pid] || 0),
    true,
  );
  assert.equal(
    sidecar.isDescendant(30, 99, (pid) => parents[pid] || 0),
    false,
  );
  assert.equal(
    sidecar.isDescendant(10, 10, () => 0),
    true,
  );

  // POSIX 信号语义（kill 注入）只在非 Windows 成立；Windows 的停止语义由
  // “进程组停止（Windows）”用例按 taskkill 注入验证。
  if (process.platform === "win32") {
    return;
  }

  let living = true;
  const signals = [];
  const graceful = await sidecar.stopGroup(42, {
    isAlive: () => living,
    kill: (pid, signal) => {
      signals.push([pid, signal]);
      living = false;
    },
  });
  assert.equal(graceful, false);
  assert.deepEqual(signals, [[-42, "SIGTERM"]]);
  const stubborn = [];
  const forced = await sidecar.stopGroup(43, {
    graceMs: 50,
    pollMs: 10,
    isAlive: () => true,
    kill: (pid, signal) => stubborn.push(signal),
  });
  assert.equal(forced, true);
  assert.deepEqual(stubborn, ["SIGTERM", "SIGKILL"]);
  assert.equal(await sidecar.stopGroup(44, { isAlive: () => false }), false);
});

test("遗留后端：只结束命令行像 mindex serve 的进程", async () => {
  const stopped = [];
  const stop = async (pid) => stopped.push(pid);
  assert.equal(
    await sidecar.reapStale(5, {
      isAlive: () => true,
      command: () => "/App/backend/mindex --config c serve --port 1",
      stop,
    }),
    true,
  );
  assert.equal(
    await sidecar.reapStale(6, {
      isAlive: () => true,
      command: () => "/usr/bin/vim notes.txt",
      stop,
    }),
    false,
  );
  assert.equal(
    await sidecar.reapStale(7, { isAlive: () => false, command: () => "mindex serve", stop }),
    false,
  );
  assert.deepEqual(stopped, [5]);
});

test("真实子进程：detached 启动后整组停止，不残留孙进程", async () => {
  // 用 node 自身代替 /bin/sh（Windows 没有 sh）；孙进程写 pid 到 stdout。
  const { spawn } = require("node:child_process");
  const script = `
    const { spawn } = require("node:child_process");
    const grandchild = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
    process.stdout.write(String(grandchild.pid));
    setInterval(() => {}, 1000);
  `;
  const child = spawn(process.execPath, ["-e", script], {
    detached: true,
    stdio: ["ignore", "pipe", "ignore"],
  });
  const grandchild = Number(
    await new Promise((resolve, reject) => {
      child.stdout.once("data", (data) => resolve(String(data).trim()));
      child.once("exit", () => reject(new Error("父进程在写出孙进程 pid 前退出")));
      setTimeout(() => reject(new Error("等待孙进程 pid 超时")), 10000).unref();
    }),
  );
  assert.ok(sidecar.alive(grandchild));
  await sidecar.stopGroup(child.pid, { graceMs: 5000 });
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal(sidecar.alive(child.pid), false);
  assert.equal(sidecar.alive(grandchild), false);
});

test("日志轮转与尾部读取", () => {
  const dir = temp();
  const log = new sidecar.RotatingLog(path.join(dir, "logs", "backend.log"), {
    maxBytes: 100,
    keep: 3,
  });
  for (let index = 0; index < 12; index += 1) {
    log.write(`line ${index} ${"x".repeat(20)}\n`);
  }
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

// ---------------------------------------------------------------- 跨平台分支

const { binaryName, layout } = require("../lib/paths");

test("可执行名：Windows 加 .exe，其他平台原样", () => {
  assert.equal(binaryName("mindex", "win32"), "mindex.exe");
  assert.equal(binaryName("ffmpeg", "win32"), "ffmpeg.exe");
  assert.equal(binaryName("mindex", "darwin"), "mindex");
  assert.equal(binaryName("mindex", "linux"), "mindex");
});

test("打包布局：三平台的随包二进制路径（Windows 带 .exe）", () => {
  // ffmpeg/ffprobe 按存在性解析（打包后在 Resources/bin 下），需要真实的临时资源目录；
  // backendExecutable 在打包模式下直接拼接。
  const dir = temp();
  fs.mkdirSync(path.join(dir, "bin"));
  for (const name of ["ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"]) {
    fs.writeFileSync(path.join(dir, "bin", name), "");
  }
  const base = {
    packaged: true,
    resourcesPath: dir,
    userData: path.join(dir, "user"),
    logs: path.join(dir, "logs"),
  };
  const mac = layout({ ...base, platform: "darwin" });
  assert.equal(mac.backendExecutable, path.join(dir, "backend", "mindex"));
  assert.equal(mac.ffmpeg, path.join(dir, "bin", "ffmpeg"));
  const win = layout({ ...base, platform: "win32" });
  assert.equal(win.backendExecutable, path.join(dir, "backend", "mindex.exe"));
  assert.equal(win.ffmpeg, path.join(dir, "bin", "ffmpeg.exe"));
  assert.equal(win.ffprobe, path.join(dir, "bin", "ffprobe.exe"));
});

test("进程组停止（Windows）：先 taskkill 软停，宽限后 /T /F 强杀", async () => {
  const calls = [];
  const exec = (command, args) => calls.push([command, ...args]);
  // 宽限内退出（第 3 次探测起进程消失）：只有软停一次，未强杀。
  let probes = 0;
  assert.equal(
    await sidecar.stopGroup(42, {
      platform: "win32",
      graceMs: 300,
      pollMs: 10,
      exec,
      isAlive: () => ++probes < 3,
      kill: (pid, sig) => calls.push(["kill", pid, sig]),
    }),
    false,
  );
  // 顽固进程：软停 + 宽限超时 + /T /F。
  assert.equal(
    await sidecar.stopGroup(43, {
      platform: "win32",
      graceMs: 60,
      pollMs: 10,
      exec,
      isAlive: () => true,
      kill: (pid, sig) => calls.push(["kill", pid, sig]),
    }),
    true,
  );
  assert.deepEqual(calls, [
    ["taskkill", "/PID", "42", "/T"],
    ["taskkill", "/PID", "43", "/T"],
    ["taskkill", "/PID", "43", "/T", "/F"],
  ]);
});

test("进程信息（Windows）：PowerShell 单次查询父 pid 与命令行；失败时容错", () => {
  const json = JSON.stringify({
    ProcessId: 7,
    ParentProcessId: 5,
    CommandLine: "C:\\app\\backend\\mindex.exe --config c serve",
  });
  const exec = (command, args, options) => {
    assert.equal(command, "powershell");
    assert.match(args.join(" "), /Get-CimInstance/);
    assert.match(args.join(" "), /ProcessId=7/);
    return json;
  };
  const info = sidecar.processInfo(7, { platform: "win32", exec });
  assert.deepEqual(info, { parent: 5, command: "C:\\app\\backend\\mindex.exe --config c serve" });
  const broken = sidecar.processInfo(8, {
    platform: "win32",
    exec: () => {
      throw new Error("powershell 缺失");
    },
  });
  assert.deepEqual(broken, { parent: 0, command: "" });
  // PowerShell 查不到进程（返回空）同样容错。
  assert.deepEqual(sidecar.processInfo(9, { platform: "win32", exec: () => "" }), {
    parent: 0,
    command: "",
  });
  // pid 来自状态文件（可能是字符串数字），查询前归一。
  assert.deepEqual(sidecar.processInfo("7", { platform: "win32", exec }), {
    parent: 5,
    command: "C:\\app\\backend\\mindex.exe --config c serve",
  });
});

test("父 pid 与命令行查询（Windows 缺省走 PowerShell）——reapStale/isDescendant 在 Windows 可用的前提", () => {
  const json = JSON.stringify({
    ProcessId: 7,
    ParentProcessId: 5,
    CommandLine: "C:\\app\\backend\\mindex.exe --config c serve",
  });
  const exec = () => json;
  // parentPid/processCommand 的 win32 分支必须委托 processInfo（直接用 ps 在 Windows 上恒失败，
  // 会让 reapStale 永远清理不了崩溃遗留的后端进程）。
  assert.equal(sidecar.parentPid(7, { platform: "win32", exec }), 5);
  assert.equal(
    sidecar.processCommand(7, { platform: "win32", exec }),
    "C:\\app\\backend\\mindex.exe --config c serve",
  );
  // 查询失败时的容错语义与 POSIX 一致。
  assert.equal(
    sidecar.parentPid(7, {
      platform: "win32",
      exec: () => {
        throw new Error("x");
      },
    }),
    0,
  );
  assert.equal(
    sidecar.processCommand(7, {
      platform: "win32",
      exec: () => {
        throw new Error("x");
      },
    }),
    "",
  );
});

// ------------------------------------------------------------------ 远程 API 客户端模式

// 系统密钥环替身，仅核对调用与文件格式；三平台真实安全存储需安装包验收。
const remoteStorage = {
  isEncryptionAvailable: () => true,
  getSelectedStorageBackend: () => "gnome_libsecret",
  encryptString: (value) => Buffer.from(`test-only:${value}`),
  decryptString: (value) => {
    const text = value.toString();
    if (!text.startsWith("test-only:")) {
      throw new Error("bad ciphertext");
    }
    return text.slice(10);
  },
};

test("远程配置：环境变量优先、URL 归一化、非法配置明确报错", () => {
  const { remoteConfig, normalizeUrl } = require("../lib/remote");
  assert.equal(normalizeUrl("https://mindex.internal/"), "https://mindex.internal");
  assert.equal(normalizeUrl("https://mindex.internal:8443//"), "https://mindex.internal:8443");
  assert.equal(normalizeUrl("ftp://x"), null);
  assert.equal(normalizeUrl("not a url"), null);
  const env = {
    MINDEX_DESKTOP_REMOTE_URL: "https://api.example/",
    MINDEX_DESKTOP_REMOTE_TOKEN: " tok ",
  };
  assert.deepEqual(remoteConfig({ env }), {
    url: "https://api.example",
    token: "tok",
    source: "env",
  });
  assert.throws(() => remoteConfig({ env: { MINDEX_DESKTOP_REMOTE_URL: "https://api.example" } }), {
    code: "invalid-token",
  });
  assert.throws(
    () =>
      remoteConfig({
        env: { MINDEX_DESKTOP_REMOTE_URL: "ftp://x", MINDEX_DESKTOP_REMOTE_TOKEN: "t" },
      }),
    { code: "invalid-url" },
  );
  for (const url of [
    "https://user:password@host",
    "https://host/api",
    "https://host?token=t",
    "https://host/#t",
  ]) {
    assert.equal(normalizeUrl(url), null);
  }
});

test("远程配置：缺失配置、本地凭据迁移与损坏拒绝", () => {
  const { remoteConfig } = require("../lib/remote");
  const dir = temp();
  const file = path.join(dir, "remote.json");
  const read = () => fs.readFileSync(file, "utf8");
  assert.equal(remoteConfig({ env: {}, userData: dir, readFile: read }), null); // 文件缺失
  fs.writeFileSync(file, "{ not json");
  assert.throws(() => remoteConfig({ env: {}, userData: dir, readFile: read }), {
    code: "invalid-config",
  });
  fs.writeFileSync(file, JSON.stringify({ url: "https://remote.example/", token: "abc" }));
  assert.deepEqual(
    remoteConfig({ env: {}, userData: dir, storage: remoteStorage, readFile: read }),
    { url: "https://remote.example", token: "abc", source: file },
  );
  assert.equal(JSON.parse(read()).token, undefined);
  assert.equal(JSON.parse(read()).credential.version, 1);
  fs.writeFileSync(file, JSON.stringify({ url: "https://remote.example/" })); // 缺令牌
  assert.throws(() => remoteConfig({ env: {}, userData: dir, readFile: read }), {
    code: "invalid-config",
  });
});

// ------------------------------------------------------------------ 远程凭据保存

test("远程配置保存：0600 原子写入、校验、清除", () => {
  const { saveRemote, clearRemote, remoteConfig } = require("../lib/remote");
  const dir = temp();
  const saved = saveRemote(
    { url: "https://remote.example/", token: " secret " },
    { userData: dir, storage: remoteStorage },
  );
  assert.equal(saved.url, "https://remote.example");
  assert.equal(saved.token, "secret");
  const file = path.join(dir, "remote.json");
  assert.ok(fs.existsSync(file));
  if (process.platform !== "win32") {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  }
  assert.equal(fs.readFileSync(file, "utf8").includes("secret"), false);
  assert.deepEqual(remoteConfig({ env: {}, userData: dir, storage: remoteStorage }), {
    url: "https://remote.example",
    token: "secret",
    source: file,
  });
  assert.throws(() => saveRemote({ url: "ftp://x", token: "t" }, { userData: dir }), {
    code: "invalid-url",
  });
  assert.throws(() => saveRemote({ url: "https://x", token: "  " }, { userData: dir }), {
    code: "invalid-token",
  });
  assert.equal(clearRemote({ userData: dir }), true);
  assert.equal(fs.existsSync(file), false);
  assert.equal(clearRemote({ userData: dir }), true); // 幂等
});

test("远程配置保存：写入失败不破坏既有文件（临时文件 + rename）", () => {
  const { saveRemote } = require("../lib/remote");
  const dir = temp();
  saveRemote(
    { url: "https://ok.example", token: "keep" },
    { userData: dir, storage: remoteStorage },
  );
  const before = fs.readFileSync(path.join(dir, "remote.json"), "utf8");
  const failing = () => {
    throw new Error("disk full");
  };
  assert.throws(
    () =>
      saveRemote(
        { url: "https://new.example", token: "x" },
        { userData: dir, storage: remoteStorage, writeFile: failing },
      ),
    /disk full/,
  );
  assert.equal(fs.readFileSync(path.join(dir, "remote.json"), "utf8"), before);
});

test("远程凭据：安全存储不可用或解密失败不回落本地，不覆盖既有配置", () => {
  const { remoteConfig, saveRemote } = require("../lib/remote");
  const dir = temp();
  const file = path.join(dir, "remote.json");
  const legacy = JSON.stringify({ url: "https://ok.example", token: "keep" });
  fs.writeFileSync(file, legacy);
  assert.throws(() => remoteConfig({ env: {}, userData: dir }), { code: "storage-unavailable" });
  assert.equal(fs.readFileSync(file, "utf8"), legacy);
  saveRemote(
    { url: "https://ok.example", token: "keep" },
    { userData: dir, storage: remoteStorage },
  );
  const broken = {
    ...remoteStorage,
    decryptString: () => {
      throw new Error("secret error");
    },
  };
  assert.throws(() => remoteConfig({ env: {}, userData: dir, storage: broken }), {
    code: "decrypt-failed",
  });
  const unavailable = { ...remoteStorage, isEncryptionAvailable: () => false };
  assert.throws(
    () =>
      saveRemote(
        { url: "https://ok.example", token: "x" },
        { userData: dir, storage: unavailable },
      ),
    { code: "storage-unavailable" },
  );
  if (process.platform === "linux") {
    assert.throws(
      () =>
        saveRemote(
          { url: "https://ok.example", token: "x" },
          {
            userData: dir,
            storage: { ...remoteStorage, getSelectedStorageBackend: () => "basic_text" },
          },
        ),
      { code: "storage-unavailable" },
    );
  }
});
