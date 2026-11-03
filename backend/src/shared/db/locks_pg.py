"""PostgreSQL 租约锁（plans §7.1 阶段 3）。

表：``lock_resources``（每个 (library_id, name) 一行，epoch 永不归零、counter 单调
分配 fencing token）与 ``lock_holders``（共享持有为多行、独占单行；TTL 到期即失效）。

- 获取/续期/释放/回收都在**单条短事务**内对资源行 ``FOR UPDATE`` 串行化；时间用
  ``clock_timestamp()``（事务内逐语句真实时钟，不用固定于事务开始的 now()）。
- 独占授予要求无有效持有（含过期未清理者先清理）；共享授予要求无有效独占。
- fencing token 单调递增、不复用；旧 token 的续期/释放/提交校验一律拒绝
  （``verify`` 供业务在提交可变状态前复验）。
- 失去租约（心跳线程故障、进程暂停超 TTL）后持有者必须停止提交：``Lease.renew``
  失败即失联，业务侧据 ``valid`` 中止。
- 等待为应用层有界轮询（与本地文件锁一致的语义），避免共享请求饿死独占维护。

锁名与规范顺序沿用 ``shared/locks.py``（pipeline → index-write → library →
register → studio → topics）；本地形态仍用文件锁，本实现只在 locks.backend=postgres
（阶段 5 集群装配）时启用。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import AbstractContextManager as ContextManager
from contextlib import contextmanager
from typing import TYPE_CHECKING

from mindex.shared import locks as file_locks
from mindex.shared.db.pg_backend import PgBackend

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

__all__ = ["HEARTBEAT_SECONDS", "LEASE_TTL_SECONDS", "Lease", "LeaseLost", "PgLockProvider"]

LEASE_TTL_SECONDS = 90.0   # plans §7.1 初始值；恢复 = TTL + 回收周期 + 调度等待
HEARTBEAT_SECONDS = 20.0
_POLL_SECONDS = 0.05


class LeaseLost(RuntimeError):
    """租约已失效（超时/被回收/令牌过旧）；持有方必须停止提交可变状态。"""


class Lease:
    """一次持有的租约：token 单调、续期/释放必须匹配 owner+token 且未过期。"""

    def __init__(self, provider: PgLockProvider, name: str, owner_id: str,
                 mode: str, token: int, expires_at: float):
        self.provider, self.name, self.owner_id = provider, name, owner_id
        self.mode, self.token = mode, token
        self.expires_at = expires_at  # epoch 秒（本进程时钟，仅供诊断）
        self._heartbeat: threading.Thread | None = None
        self._stop = threading.Event()
        self._acquired_at = time.monotonic()

    # ------------------------------------------------------------ 保活
    def start_heartbeat(self, interval: float = HEARTBEAT_SECONDS, ttl: float = LEASE_TTL_SECONDS):
        """守护线程续期；线程故障或续期失败即停止（业务在下一检查点发现失联）。"""

        def beat():
            while not self._stop.wait(interval):
                try:
                    if not self.renew(ttl):
                        log.warning("租约 %s（%s/%s）续期失败：已失联，停止心跳", self.name, self.owner_id, self.token)
                        return
                except Exception:
                    log.exception("租约 %s 心跳异常，停止心跳", self.name)
                    return

        self._heartbeat = threading.Thread(target=beat, daemon=True,
                                           name=f"lease-{self.name}-{self.owner_id[:8]}")
        self._heartbeat.start()

    def stop_heartbeat(self):
        self._stop.set()

    # ------------------------------------------------------------ 操作
    def renew(self, ttl: float = LEASE_TTL_SECONDS) -> bool:
        """续期；holder/token 不匹配或已过期返回 False（不能复活失效租约）。"""
        return self.provider.renew(self.name, self.owner_id, self.token, ttl)

    def release(self) -> bool:
        return self.provider.release(self.name, self.owner_id, self.token)

    def verify(self) -> bool:
        """提交可变状态前的 fencing 复验（plans §7.1：旧 token 不得提交）。"""
        return self.provider.holds(self.name, self.owner_id, self.token)


class PgLockProvider:
    """LockProvider 协议的 PostgreSQL 租约实现。"""

    backend = "postgres"

    def __init__(self, backend: PgBackend, library_id: str):
        self.backend_pool = backend
        self.library_id = library_id
        self.owner_id = str(uuid.uuid4())  # 进程启动身份（plans §2：worker_id 为进程 UUID）

    # ------------------------------------------------------------ 原语
    @contextmanager
    def connect(self):
        with self.backend_pool.connect() as db:
            yield db

    def lease(self, name: str, *, mode: str = "exclusive", ttl: float = LEASE_TTL_SECONDS,
              wait: float | None = 0, owner_id: str | None = None) -> Lease | None:
        """获取租约；``wait`` 为有界等待秒数（None 一直等）。拿不到返回 None。

        持有者身份默认**每次租约唯一**（进程 UUID + 租约序号）：与 flock 按 open-file
        计锁的语义一致，同进程并发获取同一资源也互斥；显式 ``owner_id`` 用于跨请求
        续期同一租约（如 worker 心跳）。
        """
        if mode not in ("shared", "exclusive"):
            raise ValueError(f"未知的锁模式：{mode}")
        if name not in file_locks.ORDER:
            raise ValueError(f"未知的锁名称：{name}（可用：{'、'.join(file_locks.ORDER)}）")
        owner = owner_id or f"{self.owner_id}:{uuid.uuid4().hex[:12]}"
        deadline = None if wait is None or wait < 0 else time.monotonic() + wait
        while True:
            got = self._try_lease(name, mode, ttl, owner)
            if got is not None:
                return got
            if deadline is None or time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_SECONDS)

    def _try_lease(self, name: str, mode: str, ttl: float, owner: str) -> Lease | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")  # 同库事务互斥（阶段 3 保守；后续按资源收窄）
            db.execute(
                "DELETE FROM lock_holders WHERE library_id=%s AND name=%s"
                " AND expires_at < clock_timestamp()", (self.library_id, name))
            db.execute(
                "INSERT INTO lock_resources(library_id,name) VALUES(%s,%s)"
                " ON CONFLICT DO NOTHING", (self.library_id, name))
            resource = db.execute(
                "SELECT epoch, counter FROM lock_resources WHERE library_id=%s AND name=%s"
                " FOR UPDATE", (self.library_id, name)).fetchone()
            holders = db.execute(
                "SELECT owner_id, mode FROM lock_holders WHERE library_id=%s AND name=%s",
                (self.library_id, name)).fetchall()
            if mode == "exclusive":
                if any(row["owner_id"] != owner for row in holders):
                    return None  # 存在其他有效持有（共享或独占）
            else:
                if any(row["mode"] == "exclusive" and row["owner_id"] != owner for row in holders):
                    return None
            token = int(resource["counter"]) + 1
            epoch = int(resource["epoch"]) + (1 if mode == "exclusive" else 0)
            db.execute(
                "UPDATE lock_resources SET epoch=%s, counter=%s WHERE library_id=%s AND name=%s",
                (epoch, token, self.library_id, name))
            row = db.execute(
                "INSERT INTO lock_holders(library_id,name,owner_id,mode,fencing_token,expires_at)"
                " VALUES(%s,%s,%s,%s,%s,clock_timestamp() + make_interval(secs => %s))"
                " ON CONFLICT (library_id,name,owner_id) DO UPDATE SET"
                " mode=excluded.mode, fencing_token=excluded.fencing_token,"
                " expires_at=excluded.expires_at"
                " RETURNING extract(epoch from expires_at) AS expires_at",
                (self.library_id, name, owner, mode, token, ttl)).fetchone()
            return Lease(self, name, owner, mode, token, float(row["expires_at"]))

    def renew(self, name: str, owner_id: str, token: int, ttl: float) -> bool:
        with self.connect() as db:
            row = db.execute(
                "UPDATE lock_holders SET expires_at=clock_timestamp() + make_interval(secs => %s)"
                " WHERE library_id=%s AND name=%s AND owner_id=%s AND fencing_token=%s"
                " AND expires_at >= clock_timestamp()"
                " RETURNING extract(epoch from expires_at) AS expires_at",
                (ttl, self.library_id, name, owner_id, token)).fetchone()
            return row is not None

    def release(self, name: str, owner_id: str, token: int) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "DELETE FROM lock_holders WHERE library_id=%s AND name=%s AND owner_id=%s"
                " AND fencing_token=%s AND expires_at >= clock_timestamp()",
                (self.library_id, name, owner_id, token))
            return cursor.rowcount > 0

    def holds(self, name: str, owner_id: str, token: int) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM lock_holders WHERE library_id=%s AND name=%s AND owner_id=%s"
                " AND fencing_token=%s AND expires_at >= clock_timestamp()",
                (self.library_id, name, owner_id, token)).fetchone() is not None

    # ------------------------------------------------------------ LockProvider 兼容
    def acquire(self, name: str, *, exclusive: bool | None = None,
                timeout: float | None = 0) -> ContextManager[Lease]:
        """``LockProvider.acquire`` 语义：进入时获取（自动心跳），退出时释放。

        本地文件锁的默认模式沿用 ``locks.named``：pipeline/library 缺省共享，
        其余独占；拿不到在 timeout 后抛 ``filelock.Timeout``（与既有调用方一致）。
        """
        from filelock import Timeout

        mode = "exclusive" if (exclusive if exclusive is not None else name not in file_locks.SHAREABLE) else "shared"
        lease: Lease | None = None

        @contextmanager
        def held():
            nonlocal lease
            lease = self.lease(name, mode=mode, wait=timeout if timeout else 0)
            if lease is None:
                raise Timeout(f"postgres-lease:{name}")
            lease.start_heartbeat()
            try:
                yield lease
            finally:
                lease.stop_heartbeat()
                if not lease.release():
                    log.warning("锁 %s 释放失败（租约已失效或被回收）", name)

        return held()

    def acquire_ordered(self, names, *, exclusive=None, timeout=0):
        """按规范顺序获取多把锁（名称顺序仍由 ``locks.check_order`` 校验）。"""
        names = list(names)
        file_locks.check_order(names)
        stack: list[Lease] = []

        @contextmanager
        def held_all():
            from filelock import Timeout

            try:
                for name in names:
                    mode = "exclusive" if (
                        exclusive.get(name) if isinstance(exclusive, dict) else
                        (exclusive if exclusive is not None else name not in file_locks.SHAREABLE)
                    ) else "shared"
                    wait = timeout.get(name, 0) if isinstance(timeout, dict) else timeout
                    got = self.lease(name, mode=mode, wait=wait if wait else 0)
                    if got is None:
                        raise Timeout(f"postgres-lease:{name}")
                    got.start_heartbeat()
                    stack.append(got)
                yield stack
            finally:
                for got in reversed(stack):
                    got.stop_heartbeat()
                    got.release()

        return held_all()
