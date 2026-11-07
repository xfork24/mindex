"""本机模型服务管理：启停配置文件里定义的本机服务（WeMM、ASR、TTS、音乐生成等），查看状态、健康与日志。

- 服务只能在配置文件 ``local_services.services`` 里定义；接口只能启停、看状态与日志、改
autostart/stop_on_exit，
  不能修改命令、工作目录或环境变量，因此不会成为远程执行任意命令的入口。``local_services.enabled`` 为 false 时
  所有操作被拒绝（桌面版配置缺省开启）。
- 进程以独立会话（进程组）启动，日志写入 ``data_dir/logs/services/<name>.log``；pid 文件
  ``data_dir/locks/services/<name>.json`` 记录 pid 与进程启动时刻，后端重启后据此重新接管仍在运行的服务
  （启动时刻不符视为 pid 已被复用）。停止时向整个进程树发 SIGTERM、超时后 SIGKILL（Windows 经
  ``shared.process_ops`` 用 ``taskkill /T /F`` 强杀整树，见该模块的停止语义说明）。
- autostart/stop_on_exit 的修改保存在数据目录 settings 表的 ``local_services.flags``（不属于 ``override.*``，
  “恢复默认”不会清除），没有保存时取配置文件里的缺省值。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from settings.runtime_settings import get_setting, put_setting
from shared import process_ops
from shared.config import Config, LocalServiceConfig
from shared.storage import Store

log = logging.getLogger(__name__)

FLAGS_KEY = "local_services.flags"
LOG_LIMIT = 20 * 1024 * 1024  # 单个服务日志超过此大小时启动前轮转为 .1
START_CHECK_SECONDS = 1.0  # 启动后观察这么久，立即退出的服务直接报错
STOP_GRACE_SECONDS = 8.0


class ServiceError(ValueError):
    """启停失败的中文原因（接口层转为 422/409）。"""


class ServiceConflict(ServiceError):
    pass


def _process_start(pid: int) -> str | None:
    """进程启动时刻指纹（平台实现见 ``shared.process_ops``）；pid 复用后会不同。进程不存在时返回 None。"""
    return process_ops.process_start(pid)


def _alive(pid: int) -> bool:
    return process_ops.alive(pid)


def _executable(service: LocalServiceConfig) -> str | None:
    """解析 argv[0]：含路径分隔符时相对 cwd，否则从 PATH 查找。"""
    program = service.argv[0]
    if os.sep in program:
        path = Path(program) if Path(program).is_absolute() else Path(service.cwd or ".") / program
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(program)


class LocalServiceManager:
    """一个数据目录的服务管理器（进程内单例，见 ``manager_for``）。"""

    def __init__(self, store: Store):
        self.store = store
        self.logs = store.root / "logs" / "services"
        self.pids = store.root / "locks" / "services"
        self._children: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 标志

    def flags(self, service: LocalServiceConfig) -> dict:
        saved = self._saved_flags().get(service.name, {})
        return {
            "autostart": bool(saved.get("autostart", service.autostart)),
            "stop_on_exit": bool(saved.get("stop_on_exit", service.stop_on_exit)),
        }

    def _saved_flags(self) -> dict:
        raw = get_setting(self.store, FLAGS_KEY)
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def set_flags(self, service: LocalServiceConfig, **changes) -> dict:
        with self._lock:
            saved = self._saved_flags()
            entry = {
                **saved.get(service.name, {}),
                **{k: bool(v) for k, v in changes.items() if v is not None},
            }
            saved[service.name] = entry
            put_setting(self.store, FLAGS_KEY, json.dumps(saved, ensure_ascii=False, sort_keys=True))
        return self.flags(service)

    # ------------------------------------------------------------------ 进程

    def _pid_file(self, name: str) -> Path:
        return self.pids / f"{name}.json"

    def _record(self, name: str) -> dict | None:
        try:
            value = json.loads(self._pid_file(name).read_text(encoding="utf-8"))
            return value if isinstance(value.get("pid"), int) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _running_pid(self, service: LocalServiceConfig) -> int | None:
        """本应用启动（或重启后接管）且仍在运行的服务进程 pid。"""
        child = self._children.get(service.name)
        if child is not None:
            if child.poll() is None:
                return child.pid
            self._children.pop(service.name, None)
            self._pid_file(service.name).unlink(missing_ok=True)
            return None
        record = self._record(service.name)
        if record is None:
            return None
        pid = record["pid"]
        if _alive(pid) and record.get("started") and _process_start(pid) == record["started"]:
            return pid
        self._pid_file(service.name).unlink(missing_ok=True)
        return None

    def healthy(self, service: LocalServiceConfig, timeout: float = 1.5) -> tuple[bool, str]:
        url = f"http://127.0.0.1:{service.port}{service.health_path}"
        try:
            # 回环探针必须直连，不能继承环境或 macOS/Windows 系统代理。
            response = httpx.get(url, timeout=timeout, trust_env=False)
        except httpx.HTTPError as exc:
            return False, f"健康检查无响应：{type(exc).__name__}"
        if 200 <= response.status_code < 300:
            return True, ""
        return False, f"健康检查返回 HTTP {response.status_code}"

    def status(self, service: LocalServiceConfig) -> dict:
        with self._lock:
            pid = self._running_pid(service)
        healthy, detail = self.healthy(service)
        if pid is None and healthy:
            detail = "端口上已有服务在运行（不是由本应用启动的）"
        elif pid is not None and not healthy:
            detail = detail or "正在启动"
        elif pid is None:
            detail = ""
        return {
            "name": service.name,
            "label": service.label or service.name,
            "role": service.role,
            "port": service.port,
            "health_path": service.health_path,
            "command": service.argv[0],
            **self.flags(service),
            "running": pid is not None,
            "pid": pid,
            "healthy": healthy,
            "external": pid is None and healthy,
            "detail": detail,
        }

    def log_path(self, service: LocalServiceConfig) -> Path:
        return self.logs / f"{service.name}.log"

    def log_tail(self, service: LocalServiceConfig, lines: int = 200) -> str:
        path = self.log_path(service)
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def start(self, service: LocalServiceConfig) -> dict:
        with self._lock:
            if self._running_pid(service) is not None:
                return self.status(service)
            if self.healthy(service)[0]:
                raise ServiceConflict(
                    f"端口 {service.port} 上已有服务在运行（不是由本应用启动的），请先停止它"
                )
            if service.cwd and not Path(service.cwd).is_dir():
                raise ServiceError(f"工作目录不存在：{service.cwd}")
            if _executable(service) is None:
                raise ServiceError(f"找不到可执行文件：{service.argv[0]}（请检查配置文件 local_services）")
            self.logs.mkdir(parents=True, exist_ok=True)
            self.pids.mkdir(parents=True, exist_ok=True)
            path = self.log_path(service)
            if path.is_file() and path.stat().st_size > LOG_LIMIT:
                path.replace(path.with_suffix(".log.1"))
            with path.open("ab") as handle:
                handle.write(
                    f"\n===== {datetime.now(UTC).isoformat()} 启动：{' '.join(service.argv)}\n".encode()
                )
                handle.flush()
                try:
                    child = process_ops.spawn_detached(
                        service.argv,
                        cwd=service.cwd or None,
                        env={**os.environ, **service.env},
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                    )
                except OSError as exc:
                    raise ServiceError(f"无法启动 {service.name}：{exc}") from exc
            self._children[service.name] = child
            self._pid_file(service.name).write_text(
                json.dumps(
                    {
                        "pid": child.pid,
                        "started": _process_start(child.pid),
                        "argv": service.argv,
                        "at": datetime.now(UTC).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        deadline = time.monotonic() + START_CHECK_SECONDS
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.05)
        if child.poll() is not None:
            with self._lock:
                self._children.pop(service.name, None)
                self._pid_file(service.name).unlink(missing_ok=True)
            tail = self.log_tail(service, 20)
            raise ServiceError(
                f"{service.label or service.name} 启动后立即退出（退出码 {child.returncode}）：{tail[-800:]}"
            )
        log.info("本机服务 %s 已启动，pid=%s", service.name, child.pid)
        return self.status(service)

    def stop(self, service: LocalServiceConfig, grace: float = STOP_GRACE_SECONDS) -> dict:
        with self._lock:
            pid = self._running_pid(service)
            child = self._children.get(service.name)
            if pid is not None:
                process_ops.stop_tree(pid, grace=grace, child=child)
                log.info("本机服务 %s 已停止", service.name)
            self._children.pop(service.name, None)
            self._pid_file(service.name).unlink(missing_ok=True)
        return self.status(service)

    def stop_on_exit(self, config: Config) -> list[str]:
        """停止所有标记为退出时停止、且由本应用启动或接管的服务。"""
        stopped = []
        for service in config.local_services.services:
            if self.flags(service)["stop_on_exit"] and self._running_pid(service) is not None:
                self.stop(service)
                stopped.append(service.name)
        return stopped

    def autostart(self, config: Config) -> list[str]:
        """启动所有标记为自动启动的服务；单个失败只记日志。"""
        started = []
        for service in config.local_services.services:
            if not self.flags(service)["autostart"]:
                continue
            try:
                self.start(service)
                started.append(service.name)
            except ServiceError as exc:
                log.warning("本机服务 %s 自动启动失败：%s", service.name, exc)
        return started


_MANAGERS: dict[Path, LocalServiceManager] = {}
_MANAGERS_LOCK = threading.Lock()


def manager_for(store: Store) -> LocalServiceManager:
    """同一数据目录在进程内共用一个管理器（共享子进程句柄，避免僵尸进程与重复启动）。"""
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(store.root)
        if manager is None:
            manager = _MANAGERS[store.root] = LocalServiceManager(store)
        return manager


def autostart_in_background(config: Config) -> threading.Thread | None:
    """``mindex serve`` 启动时在后台拉起 autostart 服务（不阻塞监听）。未开启或没有服务时不做任何事。"""
    if (
        config.database.backend != "sqlite"
        or not config.local_services.enabled
        or not config.local_services.services
    ):
        return None
    manager = manager_for(Store(config.data_dir))
    thread = threading.Thread(
        target=manager.autostart, args=(config,), daemon=True, name="local-services-autostart"
    )
    thread.start()
    return thread


def stop_on_exit(config: Config) -> list[str]:
    """``mindex serve`` 退出时停止标记为退出时停止的服务。"""
    if (
        config.database.backend != "sqlite"
        or not config.local_services.enabled
        or not config.local_services.services
    ):
        return []
    return manager_for(Store(config.data_dir)).stop_on_exit(config)
