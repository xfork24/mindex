"""进程操作的平台抽象：脱离启动、存活、pid 指纹与整树停止。

``local_services``（本机模型服务）与 ``cli/persons``（人物扫描 worker）共用；业务代码不直接
触碰 ``start_new_session`` / ``os.killpg`` / ``taskkill`` 等平台细节。

- ``spawn_detached``：POSIX 新会话（进程组）；Windows ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW``。
- ``alive``：POSIX ``os.kill(pid, 0)``；Windows ``OpenProcess`` + ``GetExitCodeProcess``。
  绝不在 Windows 用 ``os.kill``——它对普通信号是 ``TerminateProcess``，语义完全不同。
- ``process_start``：进程启动时刻字符串，作 pid 指纹防 pid 复用（服务/worker 的 pid 文件记录它，
  后端重启接管时比对）。POSIX 用 ``ps -o lstart=``；Windows 用 ``GetProcessTimes`` 的创建时刻。
  指纹只在同平台的后端重启之间比较，无跨平台格式互通问题。
- ``stop_tree``：POSIX ``killpg`` SIGTERM → 宽限轮询 → SIGKILL；Windows 没有对无控制台进程投递
  SIGTERM 的可靠机制，软停（``taskkill`` 无 ``/F``）观察宽限后 ``taskkill /PID <pid> /T /F`` 强杀
  整树。优雅停止由调用方先走 REST ``stop-on-exit``，进程层接受强杀（SQLite journal 兜底）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # 仅 Windows 导入；本模块必须能在 POSIX 上导入。
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _CREATE_NO_WINDOW = 0x08000000

    # 与 locks.py 一致：显式声明签名，避免依赖默认 int 转换截断 64 位句柄。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _open_process(pid: int) -> int:
        """查询权限的进程句柄；进程不存在或无法访问时返回 0。调用方负责 CloseHandle。"""
        return _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)


def spawn_detached(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    stdin=subprocess.DEVNULL,
    stdout=None,
    stderr=None,
) -> subprocess.Popen:
    """以脱离启动方进程组的方式运行 ``argv``（启动方退出后子进程继续运行）。"""
    if IS_WINDOWS:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
        )
    return subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True
    )


def alive(pid: int) -> bool:
    """pid 对应的进程是否仍在运行。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        handle = _open_process(pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_start(pid: int) -> str | None:
    """进程启动时刻的指纹字符串；进程不存在时返回 None。"""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if IS_WINDOWS:
        handle = _open_process(pid)
        if not handle:
            return None
        try:
            creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            # FILETIME（100ns 刻度，双 DWORD 拼接），同平台重启间比较。
            return str(creation.dwLowDateTime | (creation.dwHighDateTime << 32))
        finally:
            _kernel32.CloseHandle(handle)
    try:
        value = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return value or None


def _exited(pid: int, child: subprocess.Popen | None) -> bool:
    """进程是否已退出：有 Popen 时以 poll 为准（接管的服务没有 child，用存活探测）。"""
    if child is not None:
        return child.poll() is not None
    return not alive(pid)


def stop_tree(pid: int, *, grace: float, child: subprocess.Popen | None = None) -> bool:
    """停止 ``pid`` 及其整棵子进程树。返回是否动用了强制手段（SIGKILL / ``taskkill /F``）。

    ``child`` 为启动方持有的 ``Popen`` 时优先以其 ``poll`` 判断退出；接管（重启后按 pid 文件找回）
    的进程没有 ``Popen``，退回 ``alive`` 探测。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        # 软停对无窗口进程基本无效，但与 POSIX 分支结构一致：先给温和机会再观察宽限。
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if _exited(pid, child):
                return False
            time.sleep(0.1)
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=30
        )
        if child is not None:
            child.wait(5)
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _exited(pid, child):
            return False
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS：组内含未回收的僵尸成员（如已退出但未被父进程 wait 的 worker）时，killpg 对整组
        # 返回 EPERM。此时除组长外成员均已退出，退化为直接杀组长；仍失败则按已强杀处理。
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if child is not None:
        child.wait(5)
    return True
