"""进程操作平台抽象（shared/process_ops）：脱离启动、存活、pid 指纹与树杀。

POSIX 用例在 macOS/Linux 上跑真实子进程；Windows 用例只在 Windows 上运行（与本机开发环境互补，
CI 三平台矩阵各跑各的分支）。两套用例都只测公共 API 的真实行为。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mindex.shared import process_ops

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows 分支用例")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX 分支用例")

# 父进程把自己的 pid 与孙进程 pid 写进文件后常驻，模拟“服务进程带着 worker”。
PARENT_SCRIPT = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[1], "w").write(f"{os.getpid()} {child.pid}")
time.sleep(600)
"""
# 无视 SIGTERM 的父进程（仅 POSIX）：验证升级到 SIGKILL。
IGNORE_TERM_SCRIPT = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[1], "w").write(f"{os.getpid()} {child.pid}")
time.sleep(600)
"""


def spawn_parent(tmp_path: Path, script: str = PARENT_SCRIPT):
    """启动带孙进程的父进程，返回 (Popen, 父 pid, 孙 pid)。调用方停止时应传回 Popen（与
    LocalServiceManager 的用法一致：自己启动的进程持有 Popen，poll 判断退出并回收）。"""
    marker = tmp_path / "pids"
    child = process_ops.spawn_detached(
        [sys.executable, "-c", script, str(marker)], cwd=None, env=None,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if marker.is_file() and marker.read_text().strip():
            break
        time.sleep(0.05)
    parent_pid, grandchild_pid = (int(value) for value in marker.read_text().split())
    assert parent_pid == child.pid
    return child, parent_pid, grandchild_pid


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return process_ops.alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"pid {pid} 未退出")


@posix_only
def test_spawn_detached_starts_new_session_and_writes_stdout(tmp_path):
    marker = tmp_path / "out.txt"
    with marker.open("wb") as handle:
        child = process_ops.spawn_detached(
            ["bash", "-c", "echo $$; sleep 30"], cwd=None, env=None,
            stdout=handle, stderr=subprocess.DEVNULL,
        )
    try:
        for _ in range(100):
            if marker.is_file() and marker.read_text().strip():
                break
            time.sleep(0.05)
        pid = int(marker.read_text().strip())
        assert pid == child.pid
        # 脱离会话：与测试进程不在同一 session（后续 stop_tree 才能按进程组管理）。
        assert os.getsid(child.pid) != os.getsid(os.getpid())
    finally:
        process_ops.stop_tree(child.pid, grace=5, child=child)


@posix_only
def test_alive_and_process_start_track_process(tmp_path):
    child, parent_pid, _ = spawn_parent(tmp_path)
    try:
        assert process_ops.alive(parent_pid) is True
        assert process_ops.process_start(parent_pid)  # 非空指纹
        assert process_ops.process_start(parent_pid) == process_ops.process_start(parent_pid)
    finally:
        process_ops.stop_tree(parent_pid, grace=5, child=child)
    child.wait(5)  # 回收后 pid 不再以僵尸形式存活
    assert process_ops.alive(parent_pid) is False
    assert process_ops.process_start(parent_pid) is None


@posix_only
def test_alive_for_nonexistent_pid_is_false():
    assert process_ops.alive(1) in (True, False)  # pid 1 必然存在，冒烟分支覆盖
    # 找一个已退出且未被复用的 pid。
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert process_ops.alive(finished.pid) is False


@posix_only
def test_stop_tree_kills_process_group_with_grandchild(tmp_path):
    child, parent_pid, grandchild_pid = spawn_parent(tmp_path)
    assert process_ops.stop_tree(parent_pid, grace=8, child=child) is False  # SIGTERM 内退出，未强杀
    child.wait(5)
    wait_gone(grandchild_pid)  # 整个进程组一起结束（孙进程由 init 回收）


@posix_only
def test_stop_tree_escalates_when_sigterm_ignored(tmp_path):
    child, parent_pid, grandchild_pid = spawn_parent(tmp_path, IGNORE_TERM_SCRIPT)
    assert process_ops.stop_tree(parent_pid, grace=0.5, child=child) is True  # 升级 SIGKILL
    child.wait(5)
    wait_gone(grandchild_pid)


@posix_only
def test_stop_tree_tolerates_missing_process():
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    assert process_ops.stop_tree(finished.pid, grace=0.5) is False


@windows_only
def test_windows_spawn_detached_and_tree_stop(tmp_path):
    child, parent_pid, grandchild_pid = spawn_parent(tmp_path)
    try:
        assert process_ops.alive(parent_pid) is True
        assert process_ops.process_start(parent_pid)
    finally:
        forced = process_ops.stop_tree(parent_pid, grace=8, child=child)
    child.wait(5)
    wait_gone(grandchild_pid)  # taskkill /T 连孙进程一起结束
    # 无窗 python 进程不响应 taskkill 软停（WM_CLOSE 只对有消息循环的窗口进程有效），
    # 宽限超时后必走 /F 强杀。若 Windows 实测推翻（软停生效返回 False），翻转本断言即可——
    # 这也是 spike S6 的验证点之一。
    assert forced is True


@windows_only
def test_windows_alive_and_process_start_for_dead_process(tmp_path):
    child, parent_pid, _ = spawn_parent(tmp_path)
    process_ops.stop_tree(parent_pid, grace=8, child=child)
    child.wait(5)
    assert process_ops.alive(parent_pid) is False
    assert process_ops.process_start(parent_pid) is None
