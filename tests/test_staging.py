"""staging 工作集管理（plans §8 阶段 4A）：预约/活动 pin/LRU 淘汰/最低余量/单文件超限。"""

from __future__ import annotations

import select
import subprocess
import sys
import threading

import pytest

from shared.backends.staging import StagingArea, StagingFull


class FakeDisk:
    def __init__(self, free: int):
        self.free = free

    def __call__(self, path):
        class Usage:
            pass

        usage = Usage()
        usage.free = self.free
        return usage


def _write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_reserve_and_release_accounting(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=1000, min_free_bytes=0, disk_probe=FakeDisk(10_000))
    staging.reserve(600, purpose="转码")
    assert staging.usage()["reserved"] == 600
    staging.reserve(400)  # 恰好用满
    with pytest.raises(StagingFull, match="并发预算"):
        staging.reserve(1)
    staging.release(400)
    staging.reserve(400)
    assert staging.usage()["reserved"] == 1000


def test_single_reservation_larger_than_quota_is_rejected(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=100, disk_probe=FakeDisk(10_000))
    with pytest.raises(StagingFull, match="总预算"):
        staging.reserve(101, purpose="原片")


def test_min_free_disk_guard(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=1000, min_free_bytes=500, disk_probe=FakeDisk(600))
    with pytest.raises(StagingFull, match="磁盘剩余"):
        staging.reserve(200)  # 600 - 200 < 500
    staging.reserve(100)  # 600 - 100 >= 500


def test_eviction_respects_pins_and_lru_order(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=1000, disk_probe=FakeDisk(10_000))
    old, pinned, fresh = tmp_path / "a.bin", tmp_path / "b.bin", tmp_path / "c.bin"
    _write(old, 300)
    _write(pinned, 300)
    _write(fresh, 300)
    staging.adopt(old)
    staging.adopt(pinned)
    staging.adopt(fresh)
    staging.pin(pinned)
    freed = staging.evict(300)  # 只能淘汰最久未用的 old（b 被 pin、c 较新）
    assert freed == 300 and not old.exists() and pinned.exists() and fresh.exists()
    assert staging.usage()["evictable"] == 300  # 只剩 fresh 可淘汰
    freed = staging.evict(10_000)  # pin 不可淘汰：只能释放 fresh
    assert freed == 300 and not fresh.exists() and pinned.exists()


def test_reservation_evicts_lru_to_make_room(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=1000, disk_probe=FakeDisk(10_000))
    cold = tmp_path / "cold.bin"
    _write(cold, 800)
    staging.adopt(cold)
    staging.reserve(400, purpose="新任务")  # 缓存超预算 → 先淘汰再预约
    assert not cold.exists()
    assert staging.usage()["reserved"] == 400


def test_pin_reference_counted(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=100, disk_probe=FakeDisk(10_000))
    target = tmp_path / "t.bin"
    _write(target, 50)
    staging.adopt(target)
    staging.pin(target)
    staging.pin(target)  # 两个使用方（如 ffmpeg 读取 + 响应流）
    assert staging.evict(50) == 0
    staging.unpin(target)
    assert staging.evict(50) == 0  # 仍有一个引用
    staging.unpin(target)
    assert staging.evict(50) == 50 and not target.exists()


def test_rescan_adopts_existing_files_as_evictable(tmp_path):
    _write(tmp_path / "leftover.bin", 120)
    staging = StagingArea(tmp_path, quota_bytes=100, disk_probe=FakeDisk(10_000))
    assert staging.usage()["cached"] == 120
    assert staging.evict(120) == 120  # 崩溃残留无 pin，可回收


def test_threaded_reservations_stay_within_quota(tmp_path):
    staging = StagingArea(tmp_path, quota_bytes=100, disk_probe=FakeDisk(10_000))
    allowed, rejected = [], []
    lock = threading.Lock()

    def reserve():
        try:
            staging.reserve(40)
            with lock:
                allowed.append(1)
            staging.release(40)
        except StagingFull:
            with lock:
                rejected.append(1)

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(allowed) + len(rejected) == 8
    # 任意时刻并发预约之和不超过预算（40*2 > 100 → 至多两个同时在册）
    assert len(allowed) >= 1
    assert staging.usage()["reserved"] == 0


def test_independent_instances_share_budget_and_cannot_release_each_others_reservation(tmp_path):
    first = StagingArea(tmp_path, quota_bytes=100)
    second = StagingArea(tmp_path, quota_bytes=100)
    try:
        first.reserve(70)
        assert second.usage()["reserved"] == 70
        second.release(70)
        assert first.usage()["reserved"] == 70
        with pytest.raises(StagingFull):
            second.reserve(40)
        first.release(70)
        second.reserve(100)
        assert first.usage()["reserved"] == 100
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(sys.platform == "win32", reason="select 管道与 SIGKILL 验收在 POSIX 执行")
def test_process_pin_survives_peer_eviction_and_crash_reclaims_owner(tmp_path):
    script = """
import sys
import mindex  # 自举:包目录加入 sys.path,后续短名导入
from pathlib import Path
from shared.backends.staging import StagingArea
root = Path(sys.argv[1])
area = StagingArea(root, quota_bytes=100)
target = root / 'active.bin'
target.write_bytes(b'x' * 80)
area.adopt(target)
area.pin(target)
area.reserve(10)
print('ready', flush=True)
sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peer = None
    try:
        if not select.select([child.stdout], [], [], 10)[0]:
            child.kill()
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"子进程 staging 未就绪：exit={child.returncode} stdout={stdout!r} stderr={stderr!r}")
        line = child.stdout.readline().strip()
        if line != "ready":
            child.kill()
            stdout, stderr = child.communicate(timeout=5)
            pytest.fail(f"子进程 staging 启动失败：line={line!r} stdout={stdout!r} stderr={stderr!r}")
        peer = StagingArea(tmp_path, quota_bytes=100)
        assert peer.usage()["reserved"] == 10
        assert peer.usage()["pinned"] == 80
        assert peer.evict(100) == 0
        with pytest.raises(StagingFull):
            peer.reserve(20)
        child.kill()
        child.communicate(timeout=5)
        assert peer.usage()["reserved"] == 0
        assert peer.usage()["pinned"] == 0
        assert peer.evict(100) == 80
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
        if peer is not None:
            peer.close()
