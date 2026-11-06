"""SQLite 版本化迁移：``PRAGMA user_version`` 加有序步骤列表。

``steps[i]`` 把库从版本 ``i`` 升到 ``i + 1``；``migrate`` 读取当前版本，按顺序执行尚未执行的步骤，
每一步与对应的 ``user_version`` 递增在同一个 ``BEGIN IMMEDIATE`` 事务内提交（SQLite 允许在事务内
设置 ``user_version``，回滚时一并撤销），因此中断后重跑只会从失败的那一步继续。

约定：

- 版本 1 是引入本机制时的完整 schema，必须对 ``user_version=0`` 的旧库幂等（表与索引用
  ``IF NOT EXISTS``，补列先查 ``PRAGMA table_info``），既能建新库也能补齐旧库缺的列。
- 之后的变更只追加步骤，不修改已发布的步骤。
- 步骤内不得调用 ``executescript``（它会先提交当前事务），多语句脚本用 ``execute_script``。
- 已是最新版本时只读一次 ``user_version``，不再重复检查表结构。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

Step = Callable[[sqlite3.Connection], None]


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def execute_script(conn: sqlite3.Connection, script: str, *, skip_existing_columns: bool = False) -> None:
    """在当前事务内逐条执行多语句脚本（替代会隐式提交的 ``executescript``）。

    ``skip_existing_columns`` 时按列存在性跳过 ``ALTER TABLE … ADD COLUMN``：备份/装载等
    路径会把快照的 ``user_version`` 重置为 0 重跑迁移，无条件补列会因重复列名失败。
    """
    statement = ""
    for piece in script.split(";"):
        statement += piece + ";"
        if sqlite3.complete_statement(statement):
            body = statement.strip(" \t\r\n;")
            if body:
                skip = False
                if skip_existing_columns:
                    match = re.fullmatch(r"ALTER TABLE (\w+) ADD COLUMN (\w+) .+", body, re.S)
                    if match and match[2] in {row[1] for row in conn.execute(f"PRAGMA table_info({match[1]})")}:
                        skip = True
                if not skip:
                    conn.execute(statement)
            statement = ""
    if statement.strip(" \t\r\n;"):
        raise ValueError("迁移脚本末尾有不完整的 SQL 语句")


def migrate(conn: sqlite3.Connection, steps: Sequence[Step]) -> int:
    """把库升级到 ``len(steps)``，返回最终版本。

    调用时连接不得处于事务中。并发打开同一个库时，``BEGIN IMMEDIATE`` 串行化各进程，
    拿到写锁后重新读取版本，已被其他进程执行过的步骤直接跳过。
    """
    target = len(steps)
    current = user_version(conn)
    if current >= target:
        if current > target:
            log.warning("数据库版本 %s 高于当前程序支持的 %s，可能由更新的版本创建", current, target)
        return current
    if conn.in_transaction:
        raise RuntimeError("迁移前连接不能处于未提交的事务中")
    for version in range(current + 1, target + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if user_version(conn) < version:
                steps[version - 1](conn)
                conn.execute(f"PRAGMA user_version={version:d}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return user_version(conn)
