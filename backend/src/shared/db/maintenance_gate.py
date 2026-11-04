"""分布式维护门闩（plans §8/§11 阶段 5）。

破坏性维护（clean/purge/备份导出）在集群形态下必须全局互斥：与处理任务、其它
维护进程、GC 相互排除。实现为 PG **事务级 advisory lock**（专用键 = library_id
摘要 + "MAINT" 常量）：持有期内其它维护请求快速失败（MaintenanceBusy，中文提示
稍后再试）；事务结束自动释放，崩溃不留残留。

本地/pg-single 过渡形态沿用 data_dir 文件锁语义（clean/purge 已持 pipeline+
library 独占锁），本门闩只在 ``database.backend=postgres`` 时生效——与
ProcessingLock 的「同库事务互斥」不同键，互不阻塞正常处理（维护另行独占
pipeline/library 文件锁的既有行为不变，多机文件的替代随 locks=postgres 的
全面接线在阶段 5 后续完成）。
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

__all__ = ["MaintenanceBusy", "maintenance_gate"]


class MaintenanceBusy(RuntimeError):
    """已有维护进程在运行；稍后再试（不排队等待，避免长时间占住 API 线程）。"""


def _keys(library_id: str) -> tuple[int, int]:
    digest = hashlib.sha256(f"maintenance:{library_id}".encode()).digest()
    return (int.from_bytes(digest[:4], "big", signed=True),
            int.from_bytes(digest[4:8], "big", signed=True))


@contextmanager
def maintenance_gate(store, *, name: str, wait: float = 0):
    """进入破坏性维护临界区；拿不到抛 ``MaintenanceBusy``。

    ``name`` 仅用于诊断信息。必须在 store 的事务连接上进入（调用方随后的维护
    写入与门闩同一事务，提交即释放）。
    """
    if not store.pg:
        yield {"mode": "file-locks", "name": name}
        return
    with store.connect() as db:
        row = db.execute(
            "SELECT pg_try_advisory_lock(%s, %s) AS got", _keys(store.library_id)
        ).fetchone()
        if not row["got"]:
            raise MaintenanceBusy(f"已有维护进程在运行（{name}）；请稍后再试")
        try:
            yield {"mode": "pg-advisory", "name": name}
        finally:
            db.execute("SELECT pg_advisory_unlock(%s, %s)", _keys(store.library_id))
