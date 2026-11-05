"""请求级 eligible SQL 关系：512 行分页、聚合统计及候选级水合。"""
from __future__ import annotations

import json
from uuid import uuid4

from mindex.shared.query_budget import check, connection_for

_COLUMNS = ("segment_id", "source_id", "parent_id", "kind", "begin_time", "end_time",
            "annotation_projection", "indexed_views", "extra", "quality")


class EligibleIds:
    def __init__(self, relation):
        self.relation = relation

    def __len__(self):
        return len(self.relation)

    def __iter__(self):
        for page in self.pages():
            yield from page

    def pages(self):
        cursor = None
        while True:
            check()
            where, args = ("", ()) if cursor is None else (" WHERE segment_id>?", (cursor,))
            rows = self.relation.db.execute(f"SELECT segment_id FROM {self.relation.table}" + where +
                " ORDER BY segment_id LIMIT ?", (*args, self.relation.page_size)).fetchall()
            if not rows:
                return
            yield [row[0] for row in rows]
            cursor = rows[-1][0]

    def matching(self, identifiers):
        found = set()
        identifiers = list(identifiers)
        for offset in range(0, len(identifiers), self.relation.page_size):
            check()
            batch = identifiers[offset:offset + self.relation.page_size]
            marks = ",".join("?" for _ in batch)
            found.update(row[0] for row in self.relation.db.execute(
                f"SELECT segment_id FROM {self.relation.table} WHERE segment_id IN ({marks})", batch).fetchall())
        return found

    @property
    def where(self):
        return self.relation.vector_where


class EligibleSet:
    def __init__(self, store, *, page_size=512, vector_where=None):
        self.store, self.db = store, connection_for(store)
        if self.db is None:
            raise RuntimeError("eligible 关系必须位于显式查询作用域")
        self.table = "_query_eligible_" + uuid4().hex
        self.page_size, self.vector_where = page_size, vector_where
        self._count = None
        self.db.execute(f"CREATE TEMP TABLE {self.table}(segment_id TEXT PRIMARY KEY,source_id TEXT,"
            "parent_id TEXT,kind TEXT,begin_time DOUBLE PRECISION,end_time DOUBLE PRECISION,"
            "annotation_projection TEXT,indexed_views TEXT,extra TEXT,quality DOUBLE PRECISION)"
            + (" ON COMMIT DROP" if store.pg else ""))

    @property
    def ids(self):
        return EligibleIds(self)

    @classmethod
    def build(cls, store, index_id, granularity, filters, selected_sources, *, page_size=512):
        from mindex.search.lexical_query import eligible_sql, projected_row

        query, args = eligible_sql(store, index_id, granularity, filters, selected_sources)
        where = {"granularity": granularity}
        if selected_sources:
            where = {"$and": [where, {"source_id": {"$in": sorted(selected_sources)}}]}
        result = cls(store, page_size=page_size, vector_where=where)
        target = f"INSERT INTO {result.table}({','.join(_COLUMNS)}) "
        fields = ("SELECT g.segment_id,g.source_id,g.parent_id,g.kind,g.start,g.end,"
                  "g.annotation_projection,g.indexed_views,'{}',0 ")
        if not any((filters.scene, filters.emotion, filters.shot_size)):
            result.db.execute(target + fields + query, args)
        else:
            cursor = None
            while True:
                check()
                boundary, bound = ("", ()) if cursor is None else (" AND g.segment_id>?", (cursor,))
                rows = result.db.execute(fields + query + boundary + " ORDER BY g.segment_id LIMIT ?",
                                         (*args, *bound, page_size)).fetchall()
                if not rows:
                    break
                # Unicode casefold 与 Python 参考实现一致，不能用 SQL lower 改变语义。
                accepted = []
                for row in rows:
                    item = result.decode(row)
                    if projected_row(item, filters) is not None:
                        accepted.append(item)
                result.insert(accepted)
                cursor = rows[-1][0]
        check()
        return result

    def __len__(self):
        if self._count is None:
            check()
            self._count = self.db.execute(f"SELECT count(*) FROM {self.table}").fetchone()[0]
        return self._count

    def decode(self, row):
        # INSERT SELECT 的无别名行也按固定列序解码，不依赖 PG/SQLite 表达式列名差异。
        values = dict(zip(_COLUMNS, tuple(row)))
        extra = json.loads(values["extra"] or "{}")
        return {"id": values["segment_id"], "source_id": values["source_id"], "parent_id": values["parent_id"],
                "kind": values["kind"], "start": values["begin_time"], "end": values["end_time"],
                "annotation": json.loads(values["annotation_projection"]),
                "indexed_views": json.loads(values["indexed_views"]), **extra}

    def pages(self):
        cursor = None
        while True:
            check()
            where, args = ("", ()) if cursor is None else (" WHERE segment_id>?", (cursor,))
            rows = self.db.execute(f"SELECT {','.join(_COLUMNS)} FROM {self.table}" + where +
                                  " ORDER BY segment_id LIMIT ?", (*args, self.page_size)).fetchall()
            if not rows:
                return
            yield [self.decode(row) for row in rows]
            cursor = rows[-1][0]

    def __iter__(self):
        for page in self.pages():
            yield from page

    def insert(self, rows):
        data = []
        basic = {"id", "source_id", "parent_id", "kind", "start", "end", "annotation", "indexed_views"}
        for row in rows:
            data.append((row["id"], row["source_id"], row.get("parent_id"), row["kind"], row["start"], row["end"],
                         json.dumps(row.get("annotation") or {}, ensure_ascii=False),
                         json.dumps(row.get("indexed_views") or []),
                         json.dumps({key: value for key, value in row.items() if key not in basic}, ensure_ascii=False),
                         row.get("person_quality", 0)))
        if data:
            self.db.executemany(f"INSERT INTO {self.table}({','.join(_COLUMNS)}) VALUES(?,?,?,?,?,?,?,?,?,?)", data)
            self._count = None

    def transform(self, callback):
        result = type(self)(self.store, page_size=self.page_size, vector_where=self.vector_where)
        for page in self.pages():
            result.insert(callback(page))
        check()
        return result

    def select(self, predicate):
        return self.transform(lambda page: (row for row in page if predicate(row)))

    def lookup(self, identifiers):
        result = {}
        identifiers = list(identifiers)
        for offset in range(0, len(identifiers), self.page_size):
            check()
            batch = identifiers[offset:offset + self.page_size]
            marks = ",".join("?" for _ in batch)
            for row in self.db.execute(f"SELECT {','.join(_COLUMNS)} FROM {self.table} "
                                       f"WHERE segment_id IN ({marks})", batch).fetchall():
                item = self.decode(row)
                result[item["id"]] = item
        return result

    def available(self):
        views = set()
        for row in self.db.execute(f"SELECT DISTINCT indexed_views FROM {self.table}"):
            check()
            views.update(json.loads(row[0]))
        return views

    def person_ranked(self, settings, target, count, *, offset=0):
        query = (f"WITH ordered AS (SELECT *,row_number() OVER(ORDER BY quality DESC,segment_id) AS ordinal "
                 f"FROM {self.table}) SELECT segment_id,quality,ordinal,begin_time,end_time FROM ordered")
        # 排序只保留候选，但所有 eligible 行都进入 SQL 排名，时长混排使用全局 person rank。
        if target is not None and settings.duration_weight:
            score = (f"(1-{settings.duration_weight!r})*({settings.rrf_k}+1.0)/({settings.rrf_k}+ordinal)"
                     f"+{settings.duration_weight!r}*power(CASE WHEN (end_time-begin_time)/{target!r}>1 "
                     f"THEN 1 ELSE (end_time-begin_time)/{target!r} END,{settings.duration_exponent!r})")
            query = query.replace("SELECT segment_id,quality,ordinal,begin_time,end_time FROM ordered",
                                  f"SELECT segment_id,quality,ordinal,begin_time,end_time,{score} AS blended FROM ordered")
            query += " ORDER BY blended DESC,segment_id LIMIT ?"
        else:
            query += " ORDER BY quality DESC,segment_id LIMIT ?"
        hits = []
        for row in self.db.execute(query + " OFFSET ?", (count, offset)).fetchall():
            check()
            hit = {"id": row["segment_id"], "score": row["quality"],
                   "matches": [{"route": "person", "rank": row["ordinal"]}]}
            if target is not None and settings.duration_weight:
                relevance = (settings.rrf_k + 1) / (settings.rrf_k + row["ordinal"])
                duration = min(1.0, max(0.0, row["end_time"] - row["begin_time"]) / target) ** settings.duration_exponent
                hit.update(score=(1 - settings.duration_weight) * relevance + settings.duration_weight * duration,
                           relevance_score=relevance, duration_score=duration, target_duration=target)
            hits.append(hit)
        hits.sort(key=lambda item: (-item["score"], item["id"]))
        return hits
