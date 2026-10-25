"use strict";
// 后端 sidecar：启动参数、环境变量、日志、健康等待与停止。不含业务逻辑。
const childProcess = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const { HOST } = require("./ports");

// 全局选项（--config、--data-dir）必须放在子命令 serve 之前（见 backend/src/cli.py 的 configure）。
function serveArguments({ configPath, dataDir, port }) {
  const args = ["--config", configPath];
  if (dataDir) args.push("--data-dir", dataDir);
  args.push("serve", "--host", HOST, "--port", String(port));
  return args;
}

// dev：仓库里 `uv run mindex`；packaged：冻结后端可执行文件。返回 {command, args, cwd}。
function buildCommand({ mode, repoRoot, backendExecutable, uv = "uv", configPath, dataDir, port, cwd }) {
  const args = serveArguments({ configPath, dataDir, port });
  if (mode === "dev") return { command: uv, args: ["run", "--project", repoRoot, "mindex", ...args], cwd: repoRoot };
  if (mode === "packaged") return { command: backendExecutable, args, cwd: cwd || path.dirname(configPath) };
  throw new Error(`未知的后端模式：${mode}`);
}

// 子进程环境：继承外壳环境，注入令牌与随包二进制；MINDEX_CONFIG 不传（显式 --config 优先，避免混淆）。
function buildEnv(base, { token, ffmpeg, ffprobe, browsersPath, extra = {} }) {
  const env = { ...base, ...extra, MINDEX_WEB_TOKEN: token, PYTHONUNBUFFERED: "1" };
  delete env.MINDEX_CONFIG;
  // Electron 自身的变量不应传给 Python 后端。
  delete env.ELECTRON_RUN_AS_NODE;
  if (ffmpeg) env.MINDEX_FFMPEG = ffmpeg;
  if (ffprobe) env.MINDEX_FFPROBE = ffprobe;
  if (browsersPath) env.PLAYWRIGHT_BROWSERS_PATH = browsersPath;
  return env;
}

// 按大小轮转的日志文件：超过 maxBytes 时 backend.log → backend.log.1 …（保留 keep 份）。
class RotatingLog {
  constructor(file, { maxBytes = 10 * 1024 * 1024, keep = 3 } = {}) {
    this.file = file;
    this.maxBytes = maxBytes;
    this.keep = keep;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    this.size = fs.existsSync(file) ? fs.statSync(file).size : 0;
  }

  rotate() {
    for (let index = this.keep - 1; index >= 1; index -= 1) {
      const from = index === 1 ? this.file : `${this.file}.${index - 1}`;
      if (fs.existsSync(from)) fs.renameSync(from, `${this.file}.${index}`);
    }
    this.size = 0;
  }

  write(chunk) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    if (this.size + data.length > this.maxBytes && this.size > 0) this.rotate();
    fs.appendFileSync(this.file, data);
    this.size += data.length;
  }

  tail(bytes = 4000) {
    try {
      const data = fs.readFileSync(this.file);
      return data.subarray(Math.max(0, data.length - bytes)).toString("utf8");
    } catch {
      return "";
    }
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class StartupError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

// 轮询 /api/health 直到就绪。pidMatches(healthPid) 判断响应是否来自自己启动的进程（防止连到端口上的其他后端）。
async function waitForHealth({ url, token, version, timeoutMs = 60000, intervalMs = 250, fetchImpl = fetch,
  isRunning = () => true, pidMatches = () => true }) {
  const deadline = Date.now() + timeoutMs;
  let last = "后端尚未响应";
  while (Date.now() < deadline) {
    if (!isRunning()) throw new StartupError("exited", "后端进程在就绪前退出");
    try {
      const response = await fetchImpl(url, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(2000) });
      if (response.status === 200) {
        const body = await response.json();
        if (!(await pidMatches(body.pid))) throw new StartupError("pid-mismatch", "端口上响应的不是本应用启动的后端");
        if (version && body.version !== version)
          throw new StartupError("version-mismatch", `后端版本 ${body.version} 与应用版本 ${version} 不一致`);
        return body;
      }
      last = `健康检查返回 HTTP ${response.status}`;
      if (response.status === 401) throw new StartupError("pid-mismatch", "端口上响应的后端不接受本应用的令牌");
    } catch (error) {
      if (error instanceof StartupError) throw error;
      last = error.message;
    }
    await sleep(intervalMs);
  }
  throw new StartupError("timeout", `后端在 ${Math.round(timeoutMs / 1000)} 秒内未就绪：${last}`);
}

// pid 是否为 root 本身或其后代（dev 模式下健康接口报告的是 uv 拉起的 python 进程）。
function isDescendant(pid, root, readParent = parentPid) {
  let current = pid;
  for (let depth = 0; depth < 8 && current && current > 1; depth += 1) {
    if (current === root) return true;
    current = readParent(current);
  }
  return false;
}

function parentPid(pid) {
  try {
    return Number(childProcess.execFileSync("ps", ["-o", "ppid=", "-p", String(pid)], { encoding: "utf8" }).trim()) || 0;
  } catch {
    return 0;
  }
}

function processCommand(pid) {
  try {
    return childProcess.execFileSync("ps", ["-o", "command=", "-p", String(pid)], { encoding: "utf8" }).trim();
  } catch {
    return "";
  }
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

// 停止进程组（detached 启动，pgid = pid）：SIGTERM，graceMs 后仍存活则 SIGKILL。返回是否被强制结束。
async function stopGroup(pid, { graceMs = 10000, pollMs = 100, isAlive = alive, kill = process.kill.bind(process) } = {}) {
  if (!pid || !isAlive(pid)) return false;
  try {
    kill(-pid, "SIGTERM");
  } catch {
    return false;
  }
  const deadline = Date.now() + graceMs;
  while (Date.now() < deadline) {
    if (!isAlive(pid)) return false;
    await sleep(pollMs);
  }
  try {
    kill(-pid, "SIGKILL");
  } catch {}
  return true;
}

// 上次外壳异常退出遗留的后端：确认命令行仍是 mindex 后再结束，避免误杀复用了该 pid 的其他进程。
async function reapStale(pid, { isAlive = alive, command = processCommand, stop = stopGroup } = {}) {
  if (!pid || !isAlive(pid)) return false;
  const line = command(pid);
  if (!/mindex/.test(line) || !/\bserve\b/.test(line)) return false;
  await stop(pid, { graceMs: 5000 });
  return true;
}

module.exports = {
  serveArguments, buildCommand, buildEnv, RotatingLog, StartupError, waitForHealth,
  isDescendant, parentPid, processCommand, alive, stopGroup, reapStale,
};
