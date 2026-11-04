"""PG 维护门闩：正常写事务持共享锁，维护持独占 session 锁。

专用连接保证异常/断连时锁被服务器释放，不把 session 锁遗留在连接池中。
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE = ContextVar("mindex_maintenance", default=None)

__all__ = ["MaintenanceBusy", "maintenance_gate"]


class MaintenanceBusy(RuntimeError):
    pass


def _keys(library_id):
    # PG advisory 锁本身按 database 隔离；迁移更换 library.id 时仍必须持同一门闩。
    digest = hashlib.sha256(b"maintenance:mindex").digest()
    return (int.from_bytes(digest[:4], "big", signed=True),
            int.from_bytes(digest[4:8], "big", signed=True))


def guard_write(connection, library_id, scope):
    if _ACTIVE.get() == scope:
        return
    row = connection.execute("SELECT pg_try_advisory_xact_lock_shared(%s,%s)", _keys(library_id)).fetchone()
    if not row[0]:
        raise MaintenanceBusy("素材库正在维护，暂不接受写入；请稍后重试")


@contextmanager
def maintenance_gate(store, *, name, wait=0):
    if not store.pg:
        yield {"mode": "file-locks", "name": name}
        return
    if _ACTIVE.get() == store.backend.dsn:
        raise MaintenanceBusy(f"已有维护进程在运行（{name}）；请稍后再试")
    import psycopg
    from filelock import Timeout

    from mindex.shared import lock_backend

    try:
        # 排空长任务与发布；同线程内后续维护事务豁免共享门闩。
        with lock_backend.acquire_ordered(store, ["pipeline", "index-write", "library", "studio", "topics"],
                                          exclusive=True, timeout=wait):
            with psycopg.connect(store.backend.dsn, autocommit=True) as db:
                got = db.execute("SELECT pg_try_advisory_lock(%s,%s)", _keys(store.library_id)).fetchone()[0]
                if not got:
                    raise MaintenanceBusy(f"已有写入或维护进程在运行（{name}）；请稍后重试")
                token = _ACTIVE.set(store.backend.dsn)
                try:
                    yield {"mode": "pg-advisory", "name": name}
                finally:
                    _ACTIVE.reset(token)
                    # 无论业务事务是否 aborted，此独立连接均可解锁；关闭连接再次兜底。
                    db.execute("SELECT pg_advisory_unlock(%s,%s)", _keys(store.library_id))
    except Timeout as exc:
        raise MaintenanceBusy(f"有处理或发布任务正在运行（{name}）；请稍后重试") from exc
