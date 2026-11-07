#!/usr/bin/env python3
"""静态检查业务数据库构造边界；允许项均为本地入口、暂存库或底层实现。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_STORES = {
    "shared/backends/context.py": "统一后端入口",
    "persons/person_worker.py": "仅本地 CLI 使用的独立人物进程",
    "settings/local_services.py": "已拒绝 PG 的本机服务入口",
    "maintenance/backup_files.py": "备份暂存 SQLite 升级",
    "maintenance/pg_export.py": "PG 导出的暂存 SQLite",
    "shared/db/transfer.py": "PG 到本地库的显式迁移",
}
RAW_SQLITE = {
    "agent/session_store.py": "单实例网关会话库",
    "maintenance/backup_db.py": "备份暂存格式",
    "maintenance/migration.py": "bundle 只读检查",
    "maintenance/clean_index.py": "本地 Chroma 维护",
    "maintenance/clean.py": "本地 VACUUM",
    "maintenance/clips.py": "本地备份分支",
    "processing/shot_repair.py": "本地备份分支",
    "processing/indexing.py": "仅本地向量映射分支",
    "shared/db/sqlite_backend.py": "SQLite 驱动实现",
}


def violations(root=ROOT / "backend/src"):
    errors = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        stores, sqlite, raw_connects = set(), set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "mindex.shared.storage":
                stores.update(
                    item.asname or item.name for item in node.names if item.name in {"Store", "PgStore"}
                )
            if isinstance(node, ast.Import):
                sqlite.update(item.asname or item.name for item in node.names if item.name == "sqlite3")
            if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                raw_connects.update(item.asname or item.name for item in node.names if item.name == "connect")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in stores and relative not in LOCAL_STORES:
                errors.append(
                    f"{relative}:{node.lineno}：业务入口直构 {node.func.id}；请使用 Store.open(config)"
                )
            raw = (
                isinstance(node.func, ast.Name)
                and node.func.id in raw_connects
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in sqlite
            )
            if raw and relative not in RAW_SQLITE:
                errors.append(f"{relative}:{node.lineno}：未登记的 SQLite 直连")
    return errors


if __name__ == "__main__":
    found = violations()
    print("\n".join(found) if found else "数据库构造边界检查通过（不替代媒体 pin 与运行链路验收）")
    raise SystemExit(bool(found))
