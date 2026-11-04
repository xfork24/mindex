"""PostgreSQL 后端（plans §5；阶段 1 的 pg-single 形态）。

- 连接池按 DSN 进程内共享（``Store`` 在业务代码里随处短生命周期构造，每个实例
  建池会瞬间耗尽 PG 连接）；检出超时 30s，与 SQLite 的 busy timeout 对齐。
- ``connect()`` 事务语义与 ``SqliteBackend`` 一致：池代理在正常退出时提交、异常
  时回滚并归还连接。
- ``BEGIN IMMEDIATE`` 映射为同库事务级 advisory lock（阶段 1 的保守同库互斥，
  plans §5「短期保守使用同库事务互斥」；阶段 3 演进为资源级租约）。锁键来自
  library_id，不以路径为租户键（plans §2）。
- 唯一/非空/CHECK/外键冲突翻译为 ``IntegrityViolation``（sqlite3 异常体系，
  errors.py），现有 except 分支保持成立。
- ``lastrowid``：对 TABLE_SPECS 登记 ``returning`` 的表自动追加 ``RETURNING``，
  调用点零改动；多行插入取最后一行（与 sqlite3 的 lastrowid 一致）。
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from mindex.shared.db.dialect import TABLE_SPECS, UnsupportedSql, rewrite_for_postgres
from mindex.shared.db.errors import BackendError, IntegrityViolation
from mindex.shared.db.rows import Row
from mindex.shared.db.statements import DualSQL

__all__ = ["PgBackend"]

_CHECKOUT_TIMEOUT = 30.0
# 进程内共享池：同一 DSN 一个池，与 Store 实例的生命周期无关。
_POOLS: dict[str, Any] = {}
_POOLS_LOCK = threading.Lock()

_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


class CursorView:
    """psycopg 游标的业务视图：Row 行、rowcount、lastrowid。"""

    __slots__ = ("_cursor", "_lastrowid")

    def __init__(self, cursor, lastrowid: int | None = None):
        self._cursor = cursor
        self._lastrowid = lastrowid

    @staticmethod
    def _row(values) -> Row | None:
        if values is None or isinstance(values, Row):
            return values
        return values  # psycopg 默认元组行，在 fetch 时包装

    def fetchone(self):
        values = self._cursor.fetchone()
        return None if values is None else self._wrap(values)

    def fetchall(self):
        return [self._wrap(values) for values in self._cursor.fetchall()]

    def _wrap(self, values) -> Row:
        # 与 SQLite 的类型面保持一致：numeric → float、memoryview → bytes，
        # 避免 Decimal/内存视图漏到业务层与 JSON 响应里。
        from decimal import Decimal

        normalized = tuple(
            float(value) if isinstance(value, Decimal)
            else bytes(value) if isinstance(value, memoryview)
            else value
            for value in values)
        return Row.from_description(self._cursor.description, normalized)

    def __iter__(self):
        while (values := self._cursor.fetchone()) is not None:
            yield self._wrap(values)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    @property
    def description(self):
        return self._cursor.description


class PgConnection:
    """业务代码在 ``store.connect()`` 里拿到的统一连接。"""

    def __init__(self, connection, library_key: tuple[int, int]):
        self._connection = connection
        self._library_key = library_key

    def execute(self, sql, args: Sequence[Any] = ()) -> CursorView:
        if isinstance(sql, DualSQL):
            text = sql.pg  # 已是 PG 原生写法（%s 占位符），不再改写
            column = None
        else:
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                # 同库互斥：事务级 advisory lock，随事务提交/回滚自动释放。
                return CursorView(self._connection.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)", self._library_key))
            text = _prepare(sql)
            column = _returning_column(sql)
        if column and "RETURNING" not in text.upper():
            text = f"{text.rstrip().rstrip(';')} RETURNING {column}"
        try:
            cursor = self._connection.execute(text, tuple(args))
        except Exception as exc:
            raise _translate(exc) from exc
        lastrowid = None
        if column and cursor.description is not None:
            # INSERT ... RETURNING 的行在此取走；业务代码对 INSERT 语句不再 fetch。
            rows = cursor.fetchall()
            lastrowid = int(rows[-1][0]) if rows else None
        return CursorView(cursor, lastrowid)

    def executemany(self, sql, args) -> CursorView:
        text = sql.pg if isinstance(sql, DualSQL) else _prepare(sql)
        cursor = self._connection.cursor()
        try:
            cursor.executemany(text, list(args))
        except Exception as exc:
            raise _translate(exc) from exc
        return CursorView(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def _prepare(sql: str) -> str:
    try:
        return rewrite_for_postgres(sql)
    except UnsupportedSql:
        raise
    except Exception as exc:
        raise UnsupportedSql(f"SQL 方言改写失败：{sql[:120]}…：{exc}") from exc


def _returning_column(sql) -> str | None:
    """普通 INSERT 且目标表登记了整型主键时，追加 RETURNING 以合成 lastrowid。"""
    if isinstance(sql, DualSQL):
        return None  # 显式双方言语句自行负责取回主键
    match = _INSERT_TABLE_RE.match(sql)
    if not match:
        return None
    spec = TABLE_SPECS.get(match.group(1).lower())
    return spec.returning if spec else None


def _translate(exc: Exception) -> Exception:
    from psycopg import errors as pg_errors

    if isinstance(exc, pg_errors.UniqueViolation | pg_errors.NotNullViolation
                  | pg_errors.CheckViolation | pg_errors.ForeignKeyViolation):
        return IntegrityViolation(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if isinstance(exc, pg_errors.Error):
        # 其余驱动错误包装成 sqlite3.OperationalError 体系，业务 except 分支仍成立；
        # 未适配的 SQLite 专属函数（如 json_each）会以「函数不存在」在此显式失败。
        return BackendError(f"PostgreSQL 执行失败：{str(exc).splitlines()[0]}")
    return exc


class PgBackend:
    backend = "postgres"

    def __init__(self, dsn: str, *, library_id: str, pool_max: int = 8):
        self.dsn = dsn
        self.pool_max = pool_max
        digest = hashlib.sha256(library_id.encode("utf-8")).digest()
        # advisory lock 的 int4 对：取 library_id 摘要前 8 字节拆两个有符号 32 位。
        self.library_key = (
            int.from_bytes(digest[:4], "big", signed=True),
            int.from_bytes(digest[4:8], "big", signed=True),
        )

    def pool(self):
        with _POOLS_LOCK:
            pool = _POOLS.get(self.dsn)
            if pool is None:
                try:
                    from psycopg_pool import ConnectionPool
                except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
                    raise RuntimeError("PostgreSQL 后端需要 psycopg：uv sync --extra cluster") from exc
                pool = ConnectionPool(
                    self.dsn, min_size=0, max_size=self.pool_max,
                    timeout=_CHECKOUT_TIMEOUT, open=True,
                )
                _POOLS[self.dsn] = pool
            return pool

    @contextmanager
    def connect(self) -> Iterator[PgConnection]:
        with self.pool().connection() as conn:  # 正常退出提交、异常回滚并归还
            yield PgConnection(conn, self.library_key)

    def metrics(self) -> dict:
        """池等待/使用指标（plans §12 监控）；池未建时为空。

        psycopg_pool 3.3+ 用 ``get_stats()``（返回 requests_waiting 等计数）。
        """
        pool = _POOLS.get(self.dsn)
        if pool is None:
            return {}
        try:
            return {key: int(value) for key, value in pool.get_stats().items()}
        except Exception:  # noqa: BLE001 — 指标缺失不改变就绪语义
            return {}
