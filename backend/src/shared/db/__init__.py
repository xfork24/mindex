"""关系数据库统一适配层（plans/horizontal-scaling-plan.md §5）。

阶段 1 交付：统一异常、行对象、受控方言翻译、双方言语句对象、SQLite/PostgreSQL
后端与 PG 基线迁移器。业务代码继续面对 ``Store`` 的既有 API；此处是后端适配的
实现层，与 ``shared/backends`` 的协议（RelationalDatabase）对接。
"""

from mindex.shared.db.dialect import TABLE_SPECS, UnsupportedSql, rewrite_for_postgres
from mindex.shared.db.errors import BackendError, IntegrityViolation, SchemaMismatch, UnsupportedOperation
from mindex.shared.db.pg_backend import PgBackend
from mindex.shared.db.rows import Row
from mindex.shared.db.sqlite_backend import SqliteBackend
from mindex.shared.db.statements import DualSQL, dual

__all__ = [
    "TABLE_SPECS",
    "BackendError",
    "DualSQL",
    "IntegrityViolation",
    "PgBackend",
    "Row",
    "SchemaMismatch",
    "SqliteBackend",
    "UnsupportedOperation",
    "UnsupportedSql",
    "dual",
    "rewrite_for_postgres",
]
