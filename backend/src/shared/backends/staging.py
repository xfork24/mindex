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

import logging
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["StagingArea", "StagingFull"]


class StagingFull(RuntimeError):
    """工作集预约超出预算或磁盘余量；任务应排队或降低并发，而不是挤掉运行中的数据。"""


class StagingArea:
    """线程安全的节点工作集账本；目录 ``<root>``（由调用方指定缓存目录）。"""

    def __init__(self, root: Path, *, quota_bytes: int, min_free_bytes: int = 0,
                 disk_probe=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = quota_bytes
        self.min_free = min_free_bytes
        self._disk = disk_probe or shutil.disk_usage  # 测试可注入
        self._lock = threading.Lock()
        self._reserved = 0  # 活动预约（尚未落盘的输出/临时空间）
        self._pins: dict[str, int] = {}  # key → 引用计数
        self._entries: OrderedDict[Path, int] = OrderedDict()  # LRU：路径 → 字节数
        self._rescan()

    # ------------------------------------------------------------ 盘点
    def _rescan(self) -> None:
        """启动时把既有文件按访问时间登记进 LRU（崩溃残留按可淘汰处理）。"""
        self._entries.clear()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                try:
                    self._entries[path] = path.stat().st_size
                except OSError:
                    continue

    # ------------------------------------------------------------ 观测
    def usage(self) -> dict:
        with self._lock:
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
        return str(path)

    # ------------------------------------------------------------ 预约
    def reserve(self, size: int, *, purpose: str = "") -> None:
        """预约工作集字节（输入物化 + 输出 + 临时空间的总和）。超限拒绝。"""
        if size > self.quota:
            raise StagingFull(
                f"单次工作集 {size} 字节超过 staging 总预算 {self.quota} 字节"
                f"{'（' + purpose + '）' if purpose else ''}；请提高 storage.staging_quota_gb"
                " 或拆分任务")
        with self._lock:
            self._evict_locked(size - max(0, self.quota - self._reserved - self._used_locked()))
            free_needed = size + self.min_free
            usage = self._disk(self.root)
            if self._reserved + size > self.quota:
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
            self._reserved += size

    def release(self, size: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - size)

    # ------------------------------------------------------------ 登记
    def adopt(self, path: Path, *, touch: bool = True) -> int:
        """把 staging 内的文件登记进 LRU 账本（物化/输出完成后调用）。返回大小。"""
        path = Path(path)
        size = path.stat().st_size
        with self._lock:
            self._entries[path] = size
            self._entries.move_to_end(path)
        if touch:
            try:  # 访问时间作为 LRU 依据（失败不影响正确性）
                import os

                os.utime(path, None)
            except OSError:
                pass
        return size

    def pin(self, path: Path) -> None:
        key = self._key_of(Path(path))
        with self._lock:
            self._pins[key] = self._pins.get(key, 0) + 1

    def unpin(self, path: Path) -> None:
        key = self._key_of(Path(path))
        with self._lock:
            if self._pins.get(key, 0) > 0:
                self._pins[key] -= 1

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
        with self._lock:
            freed = self._evict_locked(need)
        if freed:
            log.info("staging LRU 淘汰释放 %d 字节", freed)
        return freed
