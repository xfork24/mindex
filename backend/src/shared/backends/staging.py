"""节点 staging 工作集管理（plans §8 阶段 4A）。

远端媒体在处理前物化到节点缓存目录；20 GiB 原片的工作集（输入 + 输出 + 转码
临时文件）可能远超原片大小。本组件把「缓存预算」变成显式协议：

- ``reserve(bytes, purpose)``：任务运行前预约工作集（含输出与临时空间），并发
  预算与磁盘最低余量双重检查，超限**明确拒绝**（StagingFull，不静默淘汰运行中
  任务的数据——缓存 pin 与远端删除 lease 是两种保护，plans §8）；
- ``pin/unpin``：物化文件的活动保护，pin 中的文件不参与 LRU 淘汰；
- ``evict(need)``：从最久未用的非 pin 文件回收空间；
- 单文件大于总预算时直接拒绝（不可能通过淘汰满足）。

本地形态（storage.backend=local）事实存储即本地盘，staging 只服务「同目录里
的缓存/临时工作区」的预算控制；S3 形态（阶段 4B）在此基础上叠加下载物化。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

log = logging.getLogger(__name__)

__all__ = ["StagingArea", "StagingFull"]


class StagingFull(RuntimeError):
    """工作集预约超出预算或磁盘余量；任务应排队或降低并发，而不是挤掉运行中的数据。"""


class StagingArea:
    """跨进程工作集账本；操作由文件锁串行化，owner 活动锁保护预约与 pin。

    每个实例持有唯一 owner 锁；崩溃由操作系统释放，下一次访问回收其账本。
    暂停进程仍持锁，不能因心跳延迟删除它正在使用的缓存。
    """

    def __init__(self, root: Path, *, quota_bytes: int, min_free_bytes: int = 0,
                 disk_probe=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = quota_bytes
        self.min_free = min_free_bytes
        self._disk = disk_probe or shutil.disk_usage  # 测试可注入
        self._lock = threading.RLock()
        self._coordination = self.root / ".staging-state"
        self._coordination.mkdir(exist_ok=True)
        self._ledger_path = self._coordination / "ledger.json"
        self._ledger_lock = FileLock(str(self._coordination / "ledger.lock"), thread_local=False)
        self._owner = uuid4().hex
        self._owner_lock = FileLock(str(self._coordination / f"{self._owner}.lock"), thread_local=False)
        self._owner_lock.acquire()
        # 回收可能发生在其他实例持有 ledger.lock 的同一线程中；只释放 owner 锁，
        # 下一次账本操作按崩溃回收协议清理预约，避免 finalizer 重入账本。
        self._finalizer = weakref.finalize(self, self._owner_lock.release)
        self._closed = False
        self._reserved = 0  # 活动预约（尚未落盘的输出/临时空间）
        self._pins: dict[str, int] = {}  # key → 引用计数
        self._entries: OrderedDict[Path, int] = OrderedDict()  # LRU：路径 → 字节数
        try:
            with self._accounting():
                self._state["owners"][self._owner] = {"reserved": 0, "pins": {}}
        except BaseException:
            self._owner_lock.release()
            raise

    @contextmanager
    def _accounting(self):
        with self._lock, self._ledger_lock:
            if self._closed:
                raise RuntimeError("staging 账本已关闭")
            if self._ledger_path.exists():
                self._state = json.loads(self._ledger_path.read_text(encoding="utf-8"))
                if (self._state.get("version"), self._state.get("quota"), self._state.get("min_free")) != (
                        1, self.quota, self.min_free):
                    raise RuntimeError("同一 staging 目录的预算配置不一致；请统一配置后重启")
            else:
                self._state = {"version": 1, "quota": self.quota, "min_free": self.min_free, "owners": {}}
            for owner in list(self._state["owners"]):
                if owner == self._owner:
                    continue
                probe = FileLock(str(self._coordination / f"{owner}.lock"))
                try:
                    probe.acquire(timeout=0)
                except Timeout:
                    continue
                else:
                    # 唯一 owner 不复用；只有拿到其锁才允许回收，PID 不参与判断。
                    del self._state["owners"][owner]
                    probe.release()
                    Path(probe.lock_file).unlink(missing_ok=True)
            self._reserved = sum(owner["reserved"] for owner in self._state["owners"].values())
            self._pins = {}
            for owner in self._state["owners"].values():
                for key, count in owner["pins"].items():
                    self._pins[key] = self._pins.get(key, 0) + count
            self._rescan()
            try:
                yield
            finally:
                temporary = self._coordination / f".{self._owner}.partial"
                try:
                    with temporary.open("w", encoding="utf-8") as handle:
                        json.dump(self._state, handle, ensure_ascii=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, self._ledger_path)
                finally:
                    temporary.unlink(missing_ok=True)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._finalizer()

    # ------------------------------------------------------------ 盘点
    def _rescan(self) -> None:
        """启动时把既有文件按访问时间登记进 LRU（崩溃残留按可淘汰处理）。"""
        self._entries.clear()
        covered = {key for owner in self._state["owners"].values()
                   for key, size in owner.get("reservations", {}).items() if size > 0}
        found = []
        for path in self.root.rglob("*"):
            if (path.is_file() and not any(part in {".staging-state", "locks", "models", "chroma"}
                                           for part in path.relative_to(self.root).parts)
                    and self._key_of(path) not in covered
                    and not path.name.endswith((".lock", ".sqlite3", "-wal", "-shm"))):
                try:
                    stat = path.stat()
                    found.append((stat.st_mtime_ns, path, stat.st_size))
                except OSError:
                    continue
        self._entries = OrderedDict((path, size) for _, path, size in sorted(found))

    # ------------------------------------------------------------ 观测
    def usage(self) -> dict:
        with self._accounting():
            pinned = sum(size for path, size in self._entries.items() if self._is_pinned(path))
            return {"quota": self.quota, "reserved": self._reserved, "pinned": pinned,
                    "cached": sum(self._entries.values()),
                    "evictable": sum(size for path, size in self._entries.items()
                                     if not self._is_pinned(path)),
                    "files": len(self._entries)}

    def _is_pinned(self, path: Path) -> bool:
        return self._pins.get(self._key_of(path), 0) > 0

    @staticmethod
    def _key_of(path: Path) -> str:
        return str(path.resolve())

    # ------------------------------------------------------------ 预约
    def reserve(self, size: int, *, purpose: str = "", path=None) -> None:
        """预约工作集字节（输入物化 + 输出 + 临时空间的总和）。超限拒绝。"""
        if size < 0:
            raise ValueError("staging 预约字节数不能为负")
        if size > self.quota:
            raise StagingFull(
                f"单次工作集 {size} 字节超过 staging 总预算 {self.quota} 字节"
                f"{'（' + purpose + '）' if purpose else ''}；请提高 storage.staging_quota_gb"
                " 或拆分任务")
        with self._accounting():
            self._evict_locked(size - max(0, self.quota - self._reserved - self._used_locked()))
            written = 0
            for owner in self._state["owners"].values():
                for key, amount in owner.get("reservations", {}).items():
                    try:
                        written += min(amount, self._path(key).stat().st_size)
                    except FileNotFoundError:
                        pass
            free_needed = max(0, self._reserved - written) + size + self.min_free
            usage = self._disk(self.root)
            if self._reserved + size + self._used_locked() > self.quota:
                raise StagingFull(
                    f"staging 并发预算不足：预约 {size} 字节（{purpose}）后将超过总预算"
                    f" {self.quota} 字节；请降低并发或等待在跑任务释放")
            if usage.free < free_needed:
                self._evict_locked(min(free_needed - usage.free, self._evictable_locked()))
                usage = self._disk(self.root)
                if usage.free < free_needed:
                    raise StagingFull(
                        f"磁盘剩余 {usage.free} 字节不足（预约 {size} + 最低余量 "
                        f"{self.min_free}）；请清理节点磁盘或下调并发")
            owner = self._state["owners"][self._owner]
            owner["reserved"] += size
            if path is not None:
                key = self._key_of(self._path(path))
                reservations = owner.setdefault("reservations", {})
                reservations[key] = reservations.get(key, 0) + size

    def release(self, size: int, *, path=None) -> None:
        if size < 0:
            raise ValueError("staging 释放字节数不能为负")
        with self._accounting():
            owner = self._state["owners"][self._owner]
            owner["reserved"] = max(0, owner["reserved"] - size)
            if path is not None:
                key = self._key_of(self._path(path))
                reservations = owner.setdefault("reservations", {})
                reservations[key] = max(0, reservations.get(key, 0) - size)
                if not reservations[key]:
                    reservations.pop(key, None)

    def commit_path(self, partial, target, reserved):
        """预约到可见文件在同一账本锁内交接，避免并发重复计算或淘汰。"""
        partial, target = self._path(partial), self._path(target)
        with self._accounting():
            owner = self._state["owners"][self._owner]
            reservations = owner.setdefault("reservations", {})
            key = self._key_of(partial)
            if reservations.get(key, 0) != reserved:
                raise RuntimeError("工作集提交预约与文件不一致")
            os.replace(partial, target)
            owner["reserved"] -= reserved
            reservations.pop(key, None)

    # ------------------------------------------------------------ 登记
    def adopt(self, path: Path, *, touch: bool = True) -> int:
        """把 staging 内的文件登记进 LRU 账本（物化/输出完成后调用）。返回大小。"""
        path = self._path(path)
        with self._accounting():
            size = path.stat().st_size
            self._entries[path] = size
            self._entries.move_to_end(path)
            if touch:
                try:
                    os.utime(path, None)
                except OSError:
                    pass
        return size

    def _path(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root) or path.is_relative_to(self._coordination):
            raise ValueError("staging 文件必须位于工作集目录中")
        return path

    def pin(self, path: Path) -> None:
        key = self._key_of(self._path(path))
        with self._accounting():
            pins = self._state["owners"][self._owner]["pins"]
            pins[key] = pins.get(key, 0) + 1

    def unpin(self, path: Path) -> None:
        key = self._key_of(self._path(path))
        with self._accounting():
            pins = self._state["owners"][self._owner]["pins"]
            if pins.get(key, 0) > 0:
                pins[key] -= 1
                if pins[key] == 0:
                    del pins[key]

    # ------------------------------------------------------------ 淘汰
    def _used_locked(self) -> int:
        return sum(self._entries.values())

    def _evictable_locked(self) -> int:
        return sum(size for path, size in self._entries.items() if not self._is_pinned(path))

    def _evict_locked(self, need: int) -> int:
        """从 LRU 尾部删除非 pin 文件直至满足需求；返回实际释放量。"""
        if need <= 0:
            return 0
        freed = 0
        victims = [path for path in self._entries if not self._is_pinned(path)]
        for path in victims:
            if freed >= need:
                break
            size = self._entries.pop(path)
            try:
                path.unlink()
                freed += size
            except OSError:
                log.debug("staging 淘汰跳过已消失的文件：%s", path)
        return freed

    def evict(self, need: int) -> int:
        with self._accounting():
            freed = self._evict_locked(need)
        if freed:
            log.info("staging LRU 淘汰释放 %d 字节", freed)
        return freed
