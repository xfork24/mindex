"""关系数据库协议：业务元数据的后端抽象（plans/horizontal-scaling-plan.md §5）。

阶段 0 只定义协议并声明本地实现；PostgreSQL 适配器（查询池/控制池、统一行对象、
双方言 SQL 登记）在阶段 1 的 ``shared/db/`` 引入。协议刻意与 ``Store`` 现有 API
同构（``connect``/``rows``/``one``/``execute``），业务调用点因此无需修改即可在
后端间切换；SQL 语句本身的方言差异不做通用转译，逐条登记、显式双方言。

``root`` 在 local 形态是事实存储目录；在集群形态仅是节点缓存/临时目录，权威
检查点必须进入数据库或对象清单（plans §4）。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager as ContextManager
from pathlib import Path
from typing import Any, Protocol

__all__ = ["DatabaseConnection", "RelationalDatabase"]


class DatabaseConnection(Protocol):
    """``connect()`` 事务上下文里业务代码可用的连接接口。

    现有业务代码直接持有 ``sqlite3.Connection``（``sqlite3.Row`` 工厂）。阶段 1 起
    统一行对象必须同时支持名称与位置访问，并覆盖 execute/fetchone/fetchall/迭代、
    rowcount、lastrowid、executemany 与提交/回滚；业务代码改捕获统一的完整性
    异常，不能漏掉现有 ``sqlite3.Error`` 分支。本地实现即 sqlite3 连接本身。
    """

    def execute(self, sql: str, args: Sequence[Any] = ()) -> Any: ...

    def executemany(self, sql: str, args: Sequence[Sequence[Any]]) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class RelationalDatabase(Protocol):
    """元数据库协议。

    ``connect()`` 是一个事务：正常退出提交、异常回滚（``Store.connect`` 语义）；
    需要写串行化的调用点在事务内显式 ``BEGIN IMMEDIATE``（阶段 1 演进为声明资源
    的事务接口）。

    本地实现：``mindex.shared.storage.Store``（metadata.sqlite3）。
    集群实现（阶段 1）：``shared/db/`` 的 PostgreSQL 适配器。
    """

    root: Path

    def connect(self) -> ContextManager[DatabaseConnection]: ...

    def rows(self, sql: str, args: Sequence[Any] = ()) -> list[dict]: ...

    def one(self, sql: str, args: Sequence[Any] = ()) -> dict | None: ...

    def execute(self, sql: str, args: Sequence[Any] = ()) -> None: ...
