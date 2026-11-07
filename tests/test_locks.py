"""统一锁模块：规范顺序、失败释放、独占冲突，以及与 filelock 旧调用方的互斥兼容。"""

from __future__ import annotations

import itertools
import tempfile
from pathlib import Path

import pytest
from filelock import FileLock, Timeout
from hypothesis import given
from hypothesis import strategies as st

from shared import locks


def test_reverse_order_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="加锁顺序错误"):
        locks.acquire_ordered(tmp_path, ["studio", "pipeline"])
    with pytest.raises(ValueError, match="未知的锁名称"):
        locks.acquire_ordered(tmp_path, ["gpu"])
    assert not (tmp_path / "pipeline.lock").exists()  # 校验先于任何加锁


def test_acquires_all_and_releases(tmp_path):
    with locks.acquire_ordered(tmp_path, locks.ORDER, exclusive=True):
        for name in locks.ORDER:
            assert locks.path(tmp_path, name).is_file()
            with pytest.raises(Timeout):
                locks.named(tmp_path, name, exclusive=True).acquire()
    for name in locks.ORDER:
        with locks.named(tmp_path, name, exclusive=True):
            pass


def test_releases_acquired_locks_when_later_lock_is_busy(tmp_path):
    busy = locks.named(tmp_path, "index-write").acquire()
    try:
        with pytest.raises(Timeout) as caught:
            locks.acquire_ordered(tmp_path, ["pipeline", "index-write", "studio"], exclusive=True)
        assert caught.value.lock_file.endswith("index-write.lock")
        with locks.named(tmp_path, "pipeline", exclusive=True):  # 第一把已释放
            pass
    finally:
        busy.release()


def test_named_exclusive_conflict_and_shared_coexistence(tmp_path):
    first = locks.named(tmp_path, "pipeline", exclusive=True)
    second = locks.named(tmp_path, "pipeline", exclusive=True)
    with first:
        with pytest.raises(Timeout):
            second.acquire()
    readers = [locks.named(tmp_path, "library"), locks.named(tmp_path, "library")]
    for reader in readers:
        reader.acquire()
    with pytest.raises(Timeout):
        locks.named(tmp_path, "library", exclusive=True).acquire()
    for reader in readers:
        reader.release()
    with pytest.raises(ValueError, match="只能独占"):
        locks.named(tmp_path, "studio", exclusive=False)


def test_compatible_with_filelock_callers(tmp_path):
    """仍用 filelock 的代码（如外部工具）与具名锁在同一文件上互斥。"""
    path = locks.path(tmp_path, "studio")
    with locks.named(tmp_path, "studio"):
        with pytest.raises(Timeout):
            FileLock(str(path), timeout=0).acquire()
    with FileLock(str(path), timeout=0):
        with pytest.raises(Timeout):
            locks.named(tmp_path, "studio").acquire()


def test_timeout_waits_then_fails(tmp_path):
    holder = locks.named(tmp_path, "index-write").acquire()
    try:
        waiter = locks.named(tmp_path, "index-write", timeout=0.1)
        with pytest.raises(Timeout):
            waiter.acquire()
        with pytest.raises(Timeout):
            waiter.acquire(timeout=0)
    finally:
        holder.release()
    blocking = locks.named(tmp_path, "index-write", timeout=None)
    with blocking:
        assert blocking.is_locked


@given(st.lists(st.sampled_from(locks.ORDER), max_size=8))
def test_order_check_accepts_iff_non_decreasing(names):
    positions = [locks.ORDER.index(name) for name in names]
    expected = all(a <= b for a, b in itertools.pairwise(positions))
    try:
        locks.check_order(names)
    except ValueError:
        accepted = False
    else:
        accepted = True
    assert accepted == expected


@given(st.lists(st.sampled_from(locks.ORDER), unique=True))
def test_acquire_ordered_accepts_iff_in_order(names):
    expected = names == sorted(names, key=locks.ORDER.index)
    with tempfile.TemporaryDirectory() as root:
        try:
            held = locks.acquire_ordered(root, names, exclusive=True)
        except ValueError:
            assert not expected
            assert not any(Path(root).rglob("*.lock"))
            return
        assert expected
        with held:
            for name in names:
                with pytest.raises(Timeout):
                    locks.named(root, name, exclusive=True).acquire()
