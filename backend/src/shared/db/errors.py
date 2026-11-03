"""统一数据库异常（plans/horizontal-scaling-plan.md §5）。

以 ``sqlite3`` 的异常体系为基类：现有业务代码的 ``except sqlite3.IntegrityError`` /
``except sqlite3.Error`` 分支在两种后端下都保持成立，PostgreSQL 适配器把
psycopg 的错误翻译成本模块的统一类型后再抛出，不把驱动异常直接漏给业务层。
"""

from __future__ import annotations

import sqlite3

__all__ = ["BackendError", "IntegrityViolation", "SchemaMismatch", "UnsupportedOperation"]


class IntegrityViolation(sqlite3.IntegrityError):
    """唯一键/NOT NULL/CHECK/外键冲突（sqlite3.IntegrityError 与 PG 23505/23502/23514/23503）。"""


class BackendError(sqlite3.OperationalError):
    """其余数据库执行/连接错误（含未适配的 SQLite 专属函数在 PG 上的显式失败）。"""


class SchemaMismatch(sqlite3.DatabaseError):
    """schema 版本与程序不匹配：集群形态启动只检查版本，迁移由 ``mindex migrate`` 执行。"""


class UnsupportedOperation(sqlite3.DatabaseError):
    """该后端不支持的维护能力（阶段 1 的显式拒绝，plans §11）。

    消息用中文说明缺口与替代入口，不静默跳过或退回本地文件。
    """
