"""统一行对象：名称与位置访问兼容 ``sqlite3.Row``（plans §5）。

SQLite 路径直接使用 ``sqlite3.Row``（本模块不做包装，保持零开销）；PostgreSQL
路径由适配器把 psycopg 的元组包装成 ``Row``，业务代码的 ``row["x"]``、``row[0]``、
``dict(row)``、``tuple(row)`` 与解包写法保持不变。
"""

from __future__ import annotations

from collections.abc import Iterator

__all__ = ["Row"]


class Row:
    """一查询结果行：按列名或位置取值，迭代产出值（与 sqlite3.Row 一致）。"""

    __slots__ = ("_columns", "_values")

    def __init__(self, columns: tuple[str, ...], values: tuple):
        self._columns = columns
        self._values = values

    @classmethod
    def from_description(cls, description, values: tuple) -> Row:
        return cls(tuple(col.name for col in description), values)

    def keys(self) -> tuple[str, ...]:
        return self._columns

    def __len__(self) -> int:
        return len(self._columns)

    def __iter__(self) -> Iterator:
        return iter(self._values)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._columns.index(key)]
            except ValueError:
                raise IndexError(f"没有名为 {key} 的列") from None
        return self._values[key]

    def __eq__(self, other) -> bool:
        if isinstance(other, Row):
            return self._columns == other._columns and self._values == other._values
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._columns, self._values))

    def __repr__(self) -> str:  # pragma: no cover - 调试输出
        pairs = ", ".join(f"{col}={value!r}" for col, value in zip(self._columns, self._values))
        return f"Row({pairs})"
