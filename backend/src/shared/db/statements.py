"""双方言语句对象：语义相关 SQL 的显式登记（plans §5）。

受控翻译器（dialect.rewrite_for_postgres）只处理可机械验证的改写；凡是涉及
``json_extract``、数值 OR 真值、布尔聚合、窗口语义或结果形状的语句，调用点用
``dual(...)`` 声明两种方言各自的原生 SQL，适配器按后端选择。SQLite 侧保持
原文不变（零行为变化），PG 侧使用原生写法（占位符用 ``%s``）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DualSQL", "dual"]


@dataclass(frozen=True)
class DualSQL:
    sqlite: str
    pg: str


def dual(*, sqlite: str, pg: str) -> DualSQL:
    return DualSQL(sqlite=sqlite, pg=pg)
