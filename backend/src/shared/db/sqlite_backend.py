"""SQLite 后端：``Store.connect`` 现有语义的等价搬移（阶段 1，零行为变化）。

每调用开新连接（timeout=30、Row 工厂、外键开启），上下文正常退出提交、异常
回滚、最终关闭；迁移与 WAL 设置仍由 ``Store.__init__`` 负责。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["SqliteBackend"]


class SqliteBackend:
    backend = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
