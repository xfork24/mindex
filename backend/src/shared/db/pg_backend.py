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

import atexit
import hashlib
import re
import threading
import weakref
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from shared.db.dialect import TABLE_SPECS, UnsupportedSql, rewrite_for_postgres
from shared.db.errors import BackendError, IntegrityViolation
from shared.db.rows import Row
from shared.db.statements import DualSQL
from shared.query_budget import QueryBudgetExceeded, active, remaining

__all__ = ["PgBackend"]

# 进程内共享池：同一 DSN 一个池，与 Store 实例的生命周期无关。
_POOLS: dict[tuple[str, bool], Any] = {}
_POOL_SETTINGS: dict[str, tuple] = {}
_POOLS_LOCK = threading.RLock()
BUSINESS_CONNECTION: ContextVar[object] = ContextVar("business_connection", default=None)
_POOL_OWNERS: dict[str, int] = {}

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

        normalized = tuple(
            float(value)
            if isinstance(value, Decimal)
            else bytes(value)
            if isinstance(value, memoryview)
            else value
            for value in values
        )
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

    def __init__(
        self,
        connection,
        library_key: tuple[int, int],
        library_id=None,
        scope=None,
        backend=None,
        execution=None,
        control=False,
    ):
        self._connection = connection
        self._library_key = library_key
        self._library_id = library_id
        self._scope = scope
        self._backend = backend
        self._execution = execution
        self._control = control

    def _guard_write(self, sql, values=(), *, batches=None):
        # 包含写 CTE；只读 SELECT 不受维护影响。字符串/注释使用同一词法扫描器排除。
        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
        from shared.db.dialect import _segments
        from shared.db.maintenance_gate import guard_write

        code = " ".join(text for is_code, text in _segments(str(sql)) if is_code or text.startswith('"'))
        if self._control:
            # 控制连接只能维护调度与租约，不提供业务写入的 fencing/维护绕过入口。
            targets = re.findall(
                (
                    "\\b(?:INSERT(?:\\s+OR\\s+(?:REPLACE|IGNORE))?\\s+INTO|UPDATE|DELETE\\s+FROM|ALTER\\s+T"
                    'ABLE|DROP\\s+TABLE|TRUNCATE(?:\\s+TABLE)?)\\s+(?!SET\\b)([\\w"]+)'
                ),
                code,
                re.I,
            )
            if (
                any(
                    target.lower().strip('"')
                    not in {
                        "task_queue",
                        "lock_resources",
                        "lock_holders",
                        "gpu_resource_groups",
                        "gpu_allocations",
                        "resource_objects",
                        "resource_pins",
                    }
                    for target in targets
                )
                or re.search(r"\b(CREATE|ALTER|DROP|TRUNCATE|CALL|COPY)\b", code, re.I)
                or re.match(r"\s*DO\b", code, re.I)
            ):
                raise RuntimeError("控制连接只允许队列和租约维护，业务写入必须使用普通事务")
        if self._library_id and re.search(
            r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE)\b", code, re.I
        ):
            guard_write(self._connection, self._library_id, self._scope)
            self._verify()
            if (
                self._backend is not None
                and getattr(self._backend, "object_coordination", False)
                and not re.match(
                    r"\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+resource_(?:objects|pins)\b", code, re.I
                )
            ):
                # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
                from shared.object_coordination import publication_guard, reference_arguments

                reference_values = (
                    reference_arguments(str(sql), values)
                    if batches is None
                    else (value for row in batches for value in reference_arguments(str(sql), row))
                )
                publication_guard(self._connection, self._library_id, reference_values)

    def _deadline(self):

        seconds = remaining()
        if seconds is not None:
            self._connection.execute(
                "SELECT set_config('statement_timeout',%s,true)", (str(max(1, int(seconds * 1000))),)
            )

    def _verify(self):
        # 延迟导入：PG 是可选后端，仅在实际借出连接时加载驱动。
        from psycopg.errors import IdleInTransactionSessionTimeout

        try:
            self._deadline()
            if self._backend is not None and not self._control:
                # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
                from shared.db.fencing import verify_transaction
                from shared.db.maintenance_gate import _ACTIVE

                if getattr(self._backend, "enforce_identity", False) and _ACTIVE.get() != self._backend.dsn:
                    identity = self._connection.execute(
                        "SELECT value FROM settings WHERE key='library.id'"
                    ).fetchone()
                    if identity is None or identity[0] != self._backend.library_id:
                        raise RuntimeError(
                            "运行中素材库身份已变化；拒绝使用旧上下文，请停止节点并完成迁移激活"
                        )
                verify_transaction(self._connection, self._backend, execution=self._execution)
        except IdleInTransactionSessionTimeout as exc:
            raise _translate(exc, execution=self._execution) from exc

    def execute(self, sql, args: Sequence[Any] = ()) -> CursorView:
        self._deadline()
        self._guard_write(sql.pg if isinstance(sql, DualSQL) else sql, args)
        if isinstance(sql, DualSQL):
            text = sql.pg  # 已是 PG 原生写法（%s 占位符），不再改写
            column = None
        else:
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                # 同库互斥：事务级 advisory lock，随事务提交/回滚自动释放。
                return CursorView(
                    self._connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", self._library_key)
                )
            text = _prepare(sql)
            column = _returning_column(sql)
        if column and "RETURNING" not in text.upper():
            text = f"{text.rstrip().rstrip(';')} RETURNING {column}"
        try:
            cursor = self._connection.execute(text, tuple(args))
        except Exception as exc:
            raise _translate(exc, execution=self._execution) from exc
        lastrowid = None
        if column and cursor.description is not None:
            # INSERT ... RETURNING 的行在此取走；业务代码对 INSERT 语句不再 fetch。
            rows = cursor.fetchall()
            lastrowid = int(rows[-1][0]) if rows else None
        return CursorView(cursor, lastrowid)

    def executemany(self, sql, args) -> CursorView:
        self._deadline()
        args = list(args)
        self._guard_write(sql.pg if isinstance(sql, DualSQL) else sql, batches=args)
        text = sql.pg if isinstance(sql, DualSQL) else _prepare(sql)
        cursor = self._connection.cursor()
        try:
            cursor.executemany(text, list(args))
        except Exception as exc:
            raise _translate(exc, execution=self._execution) from exc
        return CursorView(cursor)

    def commit(self) -> None:
        self._verify()
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


def _translate(exc: Exception, *, execution=None) -> Exception:
    # 延迟导入：独立模型或扩展功能使用可选依赖，仅在实际调用时加载。
    from psycopg import errors as pg_errors

    if (
        isinstance(exc, pg_errors.IdleInTransactionSessionTimeout)
        and execution is not None
        and (execution.attempt is not None or execution.leases)
    ):
        # 延迟导入：租约模块依赖 PG 后端；在驱动报错时才转换为执行层的失联语义。
        from shared.db.locks_pg import LeaseLost

        if execution.attempt is not None:
            execution.attempt.lost.set()
        return LeaseLost("执行事务超过租约空闲期限，已由数据库回滚；旧执行不得继续提交")
    if isinstance(exc, pg_errors.QueryCanceled) and remaining(default=None) is not None:
        return QueryBudgetExceeded("查询 SQL 已超过总期限，请缩小范围或稍后重试")
    if isinstance(
        exc,
        pg_errors.UniqueViolation
        | pg_errors.NotNullViolation
        | pg_errors.CheckViolation
        | pg_errors.ForeignKeyViolation,
    ):
        return IntegrityViolation(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    if isinstance(exc, pg_errors.Error):
        # 其余驱动错误包装成 sqlite3.OperationalError 体系，业务 except 分支仍成立；
        # 未适配的 SQLite 专属函数（如 json_each）会以「函数不存在」在此显式失败。
        return BackendError(f"PostgreSQL 执行失败：{str(exc).splitlines()[0]}")
    return exc


class PgBackend:
    backend = "postgres"
    enforce_identity = False
    object_coordination = False

    def __init__(
        self,
        dsn: str,
        *,
        library_id: str,
        pool_max: int = 8,
        pool_min: int = 0,
        control_pool_max: int = 2,
        connection_budget: int = 10,
        checkout_timeout: float = 30,
        control_checkout_timeout: float = 5,
        queue_backend: str = "inline",
    ):
        if not 0 <= pool_min <= pool_max or control_pool_max < 1:
            raise ValueError("PostgreSQL 连接池容量无效")
        if pool_max + control_pool_max > connection_budget:
            raise ValueError("PostgreSQL 查询池与控制池超过总连接预算")
        self.dsn = dsn
        with _POOLS_LOCK:
            _POOL_OWNERS[dsn] = _POOL_OWNERS.get(dsn, 0) + 1
        self._finalizer = weakref.finalize(self, _release_pools, dsn)
        self.pool_max = pool_max
        self.pool_min = pool_min
        self.control_pool_max = control_pool_max
        self.checkout_timeout = checkout_timeout
        self.control_checkout_timeout = control_checkout_timeout
        self.queue_backend = queue_backend
        self.library_id = library_id
        digest = hashlib.sha256(library_id.encode("utf-8")).digest()
        # advisory lock 的 int4 对：取 library_id 摘要前 8 字节拆两个有符号 32 位。
        self.library_key = (
            int.from_bytes(digest[:4], "big", signed=True),
            int.from_bytes(digest[4:8], "big", signed=True),
        )

    def close(self):
        self._finalizer()

    def pool(self, *, control=False):
        if not self._finalizer.alive:
            raise RuntimeError("PostgreSQL 后端已关闭")
        key = (self.dsn, control)
        settings = (
            self.pool_min,
            self.pool_max,
            self.control_pool_max,
            self.checkout_timeout,
            self.control_checkout_timeout,
        )
        with _POOLS_LOCK:
            if self.dsn in _POOL_SETTINGS and _POOL_SETTINGS[self.dsn] != settings:
                raise RuntimeError("同一 PostgreSQL 连接的池配置不一致；修改连接预算后请重启进程")
            pool = _POOLS.get(key)
            if pool is None:
                try:
                    # 延迟导入：保留可选依赖探测或异常捕获时的降级行为。
                    from psycopg_pool import ConnectionPool
                except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
                    raise RuntimeError("PostgreSQL 后端需要 psycopg：uv sync --extra cluster") from exc
                pool = ConnectionPool(
                    self.dsn,
                    min_size=0 if control else self.pool_min,
                    max_size=self.control_pool_max if control else self.pool_max,
                    timeout=self.control_checkout_timeout if control else self.checkout_timeout,
                    # PG 重启/网络中断会让池中连接变成失效 TCP：checkout 时先做一次空查询探针，
                    # 失效连接被丢弃并换新，业务请求不因残留坏连接失败。
                    check=ConnectionPool.check_connection,
                    open=True,
                )
                _POOLS[key] = pool
                _POOL_SETTINGS[self.dsn] = settings
            return pool

    @contextmanager
    def connect(self, *, control=False, execution=None, snapshot=False) -> Iterator[PgConnection]:
        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
        from shared.db.fencing import ExecutionFence

        fence = execution if execution is not None else ExecutionFence.capture()
        # 延迟导入：独立模型或扩展功能使用可选依赖，仅在实际调用时加载。
        from psycopg.errors import IdleInTransactionSessionTimeout
        from psycopg_pool import PoolTimeout

        try:
            timeout = remaining(self.control_checkout_timeout if control else self.checkout_timeout)
            with self.pool(control=control).connection(
                timeout=timeout
            ) as conn:  # 正常退出提交、异常回滚并归还
                if snapshot:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                view = PgConnection(
                    conn,
                    self.library_key,
                    None if control else self.library_id,
                    self.dsn,
                    backend=self,
                    execution=fence,
                    control=control,
                )
                view._verify()
                token = BUSINESS_CONNECTION.set(view) if not control else None
                try:
                    yield view
                    view._verify()
                finally:
                    if token is not None:
                        BUSINESS_CONNECTION.reset(token)
        except IdleInTransactionSessionTimeout as exc:
            raise _translate(exc, execution=fence) from exc
        except PoolTimeout as exc:
            if active():
                raise QueryBudgetExceeded("查询连接池等待预算已耗尽，请稍后重试") from exc
            raise

    def metrics(self) -> dict:
        """池等待/使用指标（plans §12 监控）；池未建时为空。

        psycopg_pool 3.3+ 用 ``get_stats()``（返回 requests_waiting 等计数）。
        """
        pool = _POOLS.get((self.dsn, False))
        if pool is None:
            return {}
        try:
            return {key: int(value) for key, value in pool.get_stats().items()}
        except Exception:  # noqa: BLE001 — 指标缺失不改变就绪语义
            return {}


def _release_pools(dsn: str) -> None:
    with _POOLS_LOCK:
        remaining = _POOL_OWNERS.get(dsn, 1) - 1
        if remaining:
            _POOL_OWNERS[dsn] = remaining
            return
        _POOL_OWNERS.pop(dsn, None)
        pools = [_POOLS.pop(key) for key in list(_POOLS) if key[0] == dsn]
        _POOL_SETTINGS.pop(dsn, None)
    for pool in pools:
        pool.close()


def close_pools() -> None:
    """仅由进程退出或独立测试环境调用；短生命周期 Store 不关闭共享池。"""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
        _POOL_SETTINGS.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)
