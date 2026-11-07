"""首写前的持久绑定与阶段/对象/批次账本，不依赖节点本地日志。"""

from __future__ import annotations

import hashlib
import json

from shared.storage import dumps

STAGES = ("preflight", "identity", "media", "vectors", "metadata", "tasks", "validation", "activation")


class MigrationJournal:
    def __init__(self, store, manifest, source):
        self.store, self.manifest, self.source = store, manifest, source
        self.id = manifest["migration_id"]
        self.manifest_sha256 = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()

    def row(self):
        return self.store.one("SELECT * FROM migration_runs WHERE migration_id=?", (self.id,))

    def prepare(self):
        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
        from maintenance.migration import _empty

        rows = self.store.rows("SELECT * FROM migration_runs")
        if rows:
            row = rows[0]
            if (
                len(rows) != 1
                or row["migration_id"] != self.id
                or row["source_sha256"] != self.manifest["database_sha256"]
                or row["manifest_sha256"] != self.manifest_sha256
                or row["source_library_id"] != self.manifest["library_id"]
            ):
                raise ValueError("目标已有不同迁移/源清单/库身份的记录，拒绝覆盖")
            if row["status"] == "activated":
                raise ValueError("目标已激活并可能有新写入，禁止重放旧迁移")
            return
        if not _empty(self.store):
            raise ValueError("迁移目标非空，拒绝覆盖已有数据；请新建空数据库/目录")
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO "
                "migration_runs(migration_id,source_sha256,status,source_library_id,manifest_sha256,phase"
                ",source_tables) "
                "VALUES(?,?,'preparing',?,?,'preflight',?)",
                (
                    self.id,
                    self.manifest["database_sha256"],
                    self.manifest["library_id"],
                    self.manifest_sha256,
                    dumps(self.manifest["tables"]),
                ),
            )
            self.stage("preflight", "complete", connection=db)

    def stage(self, name, state, *, connection=None, details=None):
        if name not in STAGES:
            raise ValueError("未知迁移阶段")
        if connection is None:
            with self.store.connect() as db:
                self.stage(name, state, connection=db, details=details)
            return
        connection.execute(
            "INSERT INTO migration_stages(migration_id,stage,state,details) VALUES(?,?,?,?) "
            "ON CONFLICT(migration_id,stage) DO UPDATE SET state=excluded.state,details=excluded.details",
            (self.id, name, state, dumps(details or {})),
        )
        connection.execute("UPDATE migration_runs SET phase=? WHERE migration_id=?", (name, self.id))

    def done(self, name):
        row = self.store.one(
            "SELECT state FROM migration_stages WHERE migration_id=? AND stage=?", (self.id, name)
        )
        return row is not None and row["state"] == "complete"

    def object(self, kind, key, digest, *, state):
        old = self.store.one(
            "SELECT sha256 FROM migration_objects WHERE migration_id=? AND kind=? AND object_key=?",
            (self.id, kind, key),
        )
        if old and old["sha256"] != digest:
            raise ValueError(f"同迁移对象/批次的源内容发生变化：{kind}/{key}")
        self.store.execute(
            "INSERT INTO migration_objects(migration_id,kind,object_key,sha256,state) VALUES(?,?,?,?,?) "
            "ON CONFLICT(migration_id,kind,object_key) DO UPDATE SET state=excluded.state",
            (self.id, kind, key, digest, state),
        )

    def expected_tables(self):
        row = self.row()
        return json.loads(row["restored_tables"] or row["source_tables"]) if row else self.manifest["tables"]
