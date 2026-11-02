"""媒体组的 SQLite 事实与动态成员范围，供管理、检索、创作和备份共用。"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid

UNGROUPED = {"source": "ungrouped-source", "music": "ungrouped-music"}


def membership_id(group_id, member_id):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"mindex:group:{group_id}:{member_id}").hex


def now():
    return dt.datetime.now(dt.UTC).isoformat()


class Groups:
    def __init__(self, store):
        self.store = store

    def _ungrouped_rows(self, kind, *, limit=None, offset=0):
        table, column = ("sources", "source_id") if kind == "source" else ("music_tracks", "music_id")
        query = (f"SELECT t.* FROM {table} t WHERE NOT EXISTS "
                 f"(SELECT 1 FROM media_group_members m WHERE m.{column}=t.id) ORDER BY t.id")
        return self.store.rows(query + " LIMIT ? OFFSET ?", (limit if limit is not None else -1, offset))

    def get(self, group_id):
        if group_id in UNGROUPED.values():
            kind = next(kind for kind, value in UNGROUPED.items() if value == group_id)
            return {"id": group_id, "type": kind, "name": "未分类", "description": "自动包含未加入其他组的成员",
                    "system": True, "updated_at": now(), "member_count": len(self._ungrouped_rows(kind))}
        value = self.store.one("SELECT * FROM media_groups WHERE id=?", (group_id,))
        if value is None:
            raise ValueError(f"组已删除或不存在：{group_id}，请重新选择")
        value["member_count"] = self.store.one(
            "SELECT count(*) AS n FROM media_group_members WHERE group_id=?", (group_id,)
        )["n"]
        return value

    def list(self, kind, *, include_members=False):
        # 筛选目录一次读取成员关系，避免逐组开连接；前端可直接取并集。
        column = "source_id" if kind == "source" else "music_id"
        table = "sources" if kind == "source" else "music_tracks"
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT g.*,count(m.id) AS member_count FROM media_groups g "
                "LEFT JOIN media_group_members m ON m.group_id=g.id "
                "WHERE g.type=? GROUP BY g.id ORDER BY g.name,g.id", (kind,)
            )]
            ungrouped = [row[0] for row in db.execute(
                f"SELECT t.id FROM {table} t WHERE NOT EXISTS "
                f"(SELECT 1 FROM media_group_members m WHERE m.{column}=t.id) ORDER BY t.id"
            )]
            system = {"id": UNGROUPED[kind], "type": kind, "name": "未分类",
                      "description": "自动包含未加入其他组的成员", "system": True,
                      "updated_at": now(), "member_count": len(ungrouped)}
            if include_members:
                members = {row["id"]: [] for row in rows}
                for row in db.execute(
                    f"SELECT m.group_id,m.{column} FROM media_group_members m "
                    "JOIN media_groups g ON g.id=m.group_id WHERE g.type=?", (kind,)
                ):
                    members[row[0]].append(row[1])
                for row in rows:
                    row["member_ids"] = members[row["id"]]
                system["member_ids"] = ungrouped
        return rows + [system]

    def save(self, kind, name, description="", group_id=None):
        if group_id in UNGROUPED.values():
            raise ValueError("未分类组由系统自动管理，不能编辑")
        name = name.strip()
        if name == "未分类":
            raise ValueError("未分类是系统默认组名称，请使用其他名称")
        if not name:
            raise ValueError("组名称不能为空")
        if kind not in ("source", "music"):
            raise ValueError("组类型错误")
        if group_id:
            if self.get(group_id)["type"] != kind:
                raise ValueError("组类型不能修改")
        else:
            group_id = uuid.uuid4().hex
        try:
            self.store.execute(
                "INSERT INTO media_groups(id,type,name,description,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,updated_at=excluded.updated_at",
                (group_id, kind, name, description, now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("同类型组名称已存在") from exc
        return self.get(group_id)

    def members(self, group_id, page=1, page_size=48):
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("成员分页参数无效")
        group = self.get(group_id)
        column = "source_id" if group["type"] == "source" else "music_id"
        table = "sources" if group["type"] == "source" else "music_tracks"
        if group.get("system"):
            rows = self._ungrouped_rows(group["type"], limit=page_size, offset=(page - 1) * page_size)
        else:
            rows = self.store.rows(
                f"SELECT t.* FROM {table} t JOIN media_group_members m ON t.id=m.{column} "
                "WHERE m.group_id=? ORDER BY t.id LIMIT ? OFFSET ?",
                (group_id, page_size, (page - 1) * page_size),
            )
        if group["type"] == "source":
            names = {
                row["id"]: row["display_name"]
                for row in self.store.rows(
                    "SELECT id,display_name FROM source_curation WHERE display_name IS NOT NULL"
                )
            }
            rows = [{"id": row["id"], "name": names.get(row["id"]) or row["name"]} for row in rows]
        else:
            rows = [{key: row[key] for key in ("id", "title", "artist", "duration")} for row in rows]
        return {**group, "items": rows, "page": page, "page_size": page_size}

    def _validate(self, db, kind, ids):
        table = "sources" if kind == "source" else "music_tracks"
        for member_id in ids:
            if not db.execute(f"SELECT 1 FROM {table} WHERE id=?", (member_id,)).fetchone():
                raise ValueError(f"成员不存在或类型不符：{member_id}")

    def change_members(self, group_id, ids, remove=False):
        group = self.get(group_id)
        if group.get("system"):
            raise ValueError("未分类组成员由系统自动管理，请通过普通组调整归属")
        column = "source_id" if group["type"] == "source" else "music_id"
        with self.store.connect() as db:
            self._validate(db, group["type"], ids)
            for member_id in set(ids):
                if remove:
                    db.execute(
                        f"DELETE FROM media_group_members WHERE group_id=? AND {column}=?",
                        (group_id, member_id),
                    )
                else:
                    db.execute(
                        f"INSERT OR IGNORE INTO media_group_members(id,group_id,{column}) VALUES(?,?,?)",
                        (membership_id(group_id, member_id), group_id, member_id),
                    )
            db.execute("UPDATE media_groups SET updated_at=? WHERE id=?", (now(), group_id))
        return self.get(group_id)

    def delete(self, group_id):
        if self.get(group_id).get("system"):
            raise ValueError("未分类组由系统自动管理，不能删除")
        self.store.execute("DELETE FROM media_groups WHERE id=?", (group_id,))
        return {"deleted": group_id}

    def for_member(self, kind, member_id):
        column = "source_id" if kind == "source" else "music_id"
        found = [
            self.get(row["group_id"])
            for row in self.store.rows(
                f"SELECT group_id FROM media_group_members WHERE {column}=?", (member_id,)
            )
        ]

        table = "sources" if kind == "source" else "music_tracks"
        if not found and self.store.one(f"SELECT 1 FROM {table} WHERE id=?", (member_id,)):
            return [self.get(UNGROUPED[kind])]
        return found

    def set_groups(self, kind, member_id, group_ids):
        column = "source_id" if kind == "source" else "music_id"
        for group_id in set(group_ids):
            if self.get(group_id)["type"] != kind:
                raise ValueError(f"组类型不符：{group_id}")
        group_ids = [group_id for group_id in group_ids if group_id not in UNGROUPED.values()]
        with self.store.connect() as db:
            self._validate(db, kind, [member_id])
            old = [
                row[0]
                for row in db.execute(
                    f"SELECT group_id FROM media_group_members WHERE {column}=?", (member_id,)
                )
            ]
            db.execute(f"DELETE FROM media_group_members WHERE {column}=?", (member_id,))
            for group_id in set(group_ids):
                db.execute(
                    f"INSERT INTO media_group_members(id,group_id,{column}) VALUES(?,?,?)",
                    (membership_id(group_id, member_id), group_id, member_id),
                )
            for group_id in set(old) | set(group_ids):
                db.execute("UPDATE media_groups SET updated_at=? WHERE id=?", (now(), group_id))
        return self.for_member(kind, member_id)

    def state(self, kind, group_ids):
        """签名用当前成员与失效状态；不包含名称，且只读取被引用的组。"""
        result = []
        for group_id in sorted(set(group_ids or [])):
            if group_id in UNGROUPED.values():
                result.append({"id": group_id, "members": sorted(self.resolve(kind, [group_id]))}
                              if group_id == UNGROUPED[kind] else {"id": group_id, "invalid": "type"})
                continue
            row = self.store.one("SELECT type FROM media_groups WHERE id=?", (group_id,))
            if row is None or row["type"] != kind:
                result.append({"id": group_id, "invalid": "missing" if row is None else "type"})
                continue
            column = "source_id" if kind == "source" else "music_id"
            members = self.store.rows(
                f"SELECT {column} FROM media_group_members WHERE group_id=? ORDER BY {column}", (group_id,)
            )
            result.append({"id": group_id, "members": [item[column] for item in members]})
        return result

    def resolve(self, kind, group_ids):
        """None 表示不限；空集合表示所选组没有成员，绝不能扩大为全库。"""
        if not group_ids:
            return None
        result = set()
        column = "source_id" if kind == "source" else "music_id"
        for group_id in set(group_ids):
            if self.get(group_id)["type"] != kind:
                raise ValueError(f"组类型不符：{group_id}")
            if group_id == UNGROUPED[kind]:
                result.update(row["id"] for row in self._ungrouped_rows(kind))
                continue
            result.update(
                row[column]
                for row in self.store.rows(
                    f"SELECT {column} FROM media_group_members WHERE group_id=?", (group_id,)
                )
            )
        return result
