"""持久迁移状态门闩；业务写入与领取必须等待完整验证和激活。"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar

from mindex.shared.db.maintenance_gate import MaintenanceBusy

_ACCESS: ContextVar[frozenset[str]] = ContextVar('migration_access', default=frozenset())


def backend_scope(store):
    return store.backend.dsn if store.pg else str(store.path.resolve())


@contextmanager
def access(store):
    token = _ACCESS.set(_ACCESS.get() | {backend_scope(store)})
    try:
        yield
    finally:
        _ACCESS.reset(token)


def pending(db):
    try:
        row = db.execute("SELECT migration_id,status FROM migration_runs ORDER BY updated_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return None  # 仅本地首次建库；PG schema 校验仍在连接入口强制执行。
        raise
    if row is None:
        return None
    # 原生 psycopg 与统一 Row/SQLite Row 都支持位置访问。
    return {'migration_id': row[0], 'status': row[1]} if row[1] != 'activated' else None


def guard(db, scope):
    from mindex.shared.db.maintenance_gate import _ACTIVE

    if scope in _ACCESS.get() or _ACTIVE.get() == scope:
        return
    row = pending(db)
    if row:
        raise MaintenanceBusy(f"素材库迁移 {row['migration_id']} 尚未激活，拒绝业务写入或领取任务；请继续 migration load/verify/activate")


class LocalConnection:
    """本地恢复目标同样有写准入；不改变 SQLite 行、游标及事务语义。"""
    def __init__(self, db, scope):
        self.db, self.scope = db, scope

    def _guard(self, sql):
        from mindex.shared.db.dialect import _segments

        code = ' '.join(text for is_code, text in _segments(str(sql)) if is_code)
        if re.search(r'\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|CREATE)\b', code, re.I):
            guard(self.db, self.scope)

    def execute(self, sql, args=()):
        self._guard(sql)
        return self.db.execute(sql, args)

    def executemany(self, sql, args):
        self._guard(sql)
        return self.db.executemany(sql, args)

    def executescript(self, sql):
        self._guard(sql)
        return self.db.executescript(sql)

    def cursor(self, *args, **kwargs):
        return LocalCursor(self.db.cursor(*args, **kwargs), self)

    def __getattr__(self, name):
        return getattr(self.db, name)


class LocalCursor:
    def __init__(self, cursor, connection):
        self.raw, self.connection = cursor, connection

    def execute(self, sql, args=()):
        self.connection._guard(sql)
        self.raw.execute(sql, args)
        return self

    def executemany(self, sql, args):
        self.connection._guard(sql)
        self.raw.executemany(sql, args)
        return self

    def __iter__(self):
        return iter(self.raw)

    def __getattr__(self, name):
        return getattr(self.raw, name)


def writable(store):
    with store.connect() as db:
        guard(db, backend_scope(store))


def writable_config(config):
    # 没有元数据库的旧本地文件工作流不存在迁移状态，不为检查创建一个新库。
    if config.database.backend == 'sqlite' and not (config.data_dir / 'metadata.sqlite3').is_file():
        return
    from mindex.shared.storage import Store

    with Store.open(config) as store:
        writable(store)
