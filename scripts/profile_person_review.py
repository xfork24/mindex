#!/usr/bin/env python3
"""人物库出镜审核读取链路的只读耗时基线。

用法：
    uv run python scripts/profile_person_review.py [--data-dir data] [--repeat 5]
        [--image-samples 20] [--output 报告.md]

对数据库严格只读：连接使用 ``mode=ro`` 加 ``PRAGMA query_only=ON``，并跳过 Store 的建表迁移，
任何写入都会直接报错。脚本前后会比对主库文件的大小与修改时间，并把结果写进报告。

测量内容：
1. 数据规模（行数、data 列体积、历史版本体积）。
2. 经 HTTP（TestClient，进程内）请求审核列表，按 素材 × 状态 × 是否带人物筛选 取 p50/p95，
   并汇总服务端的 Server-Timing 分段；``assemble`` = total - 各 SQL/解码阶段 - middleware。
3. 抽样 SQL 的 EXPLAIN QUERY PLAN（用 sqlite trace 回调抓取真实语句）。
4. 证据图片接口按分支（当前路径/历史版本扫描/缩略图回退）的耗时。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import mindex  # noqa: F401 — 安装根包自举，后续使用包内短导入
from fastapi.testclient import TestClient

os.environ["MINDEX_PROFILE_PERSONS"] = "1"

TIMING = re.compile(r'([A-Za-z_]+);dur=([\d.]+)(?:;desc="([^"]*)")?')
SQL_STAGES = ("filters_sql", "scope_total_sql", "page_sql", "evidence_sql", "json_decode")


def parse_timing(header: str) -> dict[str, tuple[float, str]]:
    return {m.group(1): (float(m.group(2)), m.group(3) or "") for m in TIMING.finditer(header or "")}


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def fmt(values: list[float]) -> str:
    return f"{statistics.median(values):.0f} / {percentile(values, 0.95):.0f}" if values else "-"


def build_app(data_dir: Path, traces: list[str]):
    import api as api
    from shared.config import Config
    from shared.storage import Store

    class ReadOnlyStore(Store):
        def __init__(self, root):
            self.root = Path(root).resolve()
            self.path = self.root / "metadata.sqlite3"

        @contextmanager
        def connect(self):
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.set_trace_callback(lambda sql: traces.append(sql) if len(sql) > 400 else None)
            try:
                yield db
            finally:
                db.close()

    api.Store = ReadOnlyStore
    return api.create_app(Config(data_dir=data_dir)), ReadOnlyStore(data_dir)


def file_state(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def scale_section(store) -> list[str]:
    def one(sql, args=()):
        return store.one(sql, args)

    lines = ["| 项目 | 数值 |", "| --- | --- |"]
    for table in ("person_evidence", "segments", "person_coverage", "person_entities", "person_versions"):
        lines.append(f"| {table} 行数 | {one(f'SELECT count(*) AS n FROM {table}')['n']:,} |")
    size = one("SELECT avg(length(data)) AS a, max(length(data)) AS m FROM person_evidence")
    lines.append(f"| person_evidence.data 平均 / 最大字节 | {size['a']:.0f} / {size['m']} |")
    versions = store.rows(
        "SELECT id,length(CAST(data AS BLOB)) AS n FROM person_versions ORDER BY n DESC LIMIT 3"
    )
    lines.append(
        "| person_versions 最大体积（字节） | " + (", ".join(f"{v['n']:,}" for v in versions) or "-") + " |"
    )
    for row in store.rows("SELECT status,count(*) AS n FROM person_evidence GROUP BY status"):
        lines.append(f"| 证据状态 {row['status']} | {row['n']:,} |")
    return lines


def pick_sources(store, limit=2) -> list[dict]:
    return store.rows(
        "SELECT source_id, count(*) AS rows_, count(DISTINCT shot_id) AS shots FROM person_evidence "
        "GROUP BY source_id ORDER BY rows_ DESC LIMIT ?",
        (limit,),
    )


def http_section(client, sources, repeat) -> tuple[list[str], list[str]]:
    header = [
        "| 素材(证据行/分镜) | 状态 | 筛选 | p50 / p95 total ms | "
        + " | ".join(s.replace("_sql", "") for s in SQL_STAGES)
        + " | assemble | middleware | 响应 KB |",
        "| --- | --- | --- | --- | " + " | ".join("---" for _ in SQL_STAGES) + " | --- | --- | --- |",
    ]
    rows, notes = list(header), []
    for src in sources:
        label = f"{src['source_id'][:6]}({src['rows_']}/{src['shots']})"
        pair = None
        for status in ("candidate", "accepted", "rejected"):
            base = {"source_id": src["source_id"], "status": status, "limit": 50}
            variants = [("无", base)]
            if status == "accepted":
                data = client.get("/api/persons/review/shots", params=base).json()
                if data["filters"]:
                    top = max(
                        (f for f in data["filters"] if f["actor_id"]), key=lambda f: f["count"], default=None
                    )
                    if top:
                        pair = {
                            "actor_id": top["actor_id"],
                            **({"role_id": top["role_id"]} if top["role_id"] else {}),
                        }
                        variants.append(("人物", {**base, **pair}))
            for name, params in variants:
                client.get("/api/persons/review/shots", params=params)  # 预热 OS 页缓存与 SQLite
                samples = []
                for _ in range(repeat):
                    started = time.perf_counter()
                    response = client.get("/api/persons/review/shots", params=params)
                    wall = (time.perf_counter() - started) * 1000
                    timing = parse_timing(response.headers.get("server-timing", ""))
                    samples.append((wall, timing, len(response.content)))
                cols = {
                    s: [t.get(s, (0.0, ""))[0] for _, t, _ in samples] for s in (*SQL_STAGES, "middleware")
                }
                assemble = [
                    max(
                        0.0,
                        t["total"][0]
                        - sum(t.get(s, (0.0, ""))[0] for s in SQL_STAGES)
                        - t.get("middleware", (0.0, ""))[0],
                    )
                    for _, t, _ in samples
                ]
                n = [t.get("evidence_sql", (0, ""))[1] for _, t, _ in samples][-1]
                rows.append(
                    f"| {label} | {status} | {name} | {fmt([w for w, _, _ in samples])} | "
                    + " | ".join(fmt(cols[s]) for s in SQL_STAGES)
                    + f" | {fmt(assemble)} | {fmt(cols['middleware'])} | "
                    f"{samples[-1][2] / 1024:.0f} |"
                )
                if n:
                    notes.append(f"{label} {status}/{name}: evidence_sql {n}")
    return rows, notes


def explain_section(client, store, traces, sources) -> list[str]:
    traces.clear()
    src = sources[0]["source_id"]
    client.get("/api/persons/review/shots", params={"source_id": src, "status": "accepted", "limit": 50})
    heavy = []
    for sql in traces:
        if "WITH scoped" in sql and sql not in heavy:
            heavy.append(sql)
    lines = []
    for index, sql in enumerate(heavy[:3], 1):
        lines.append(f"**查询 {index}**（{len(sql):,} 字符）")
        lines.append("```")
        for row in store.rows("EXPLAIN QUERY PLAN " + sql):
            lines.append(f"{row['id']:>4} {row['parent']:>4}  {row['detail']}")
        lines.append("```")
    return lines


def image_section(client, store, samples) -> list[str]:
    with_path = store.rows(
        "SELECT id FROM person_evidence WHERE json_extract(data,'$.image_path') IS NOT NULL LIMIT ?",
        (samples,),
    )
    without = store.rows(
        "SELECT id FROM person_evidence WHERE json_extract(data,'$.image_path') IS NULL LIMIT ?", (samples,)
    )
    lines = [
        "| 抽样 | 请求数 | 分支分布 | p50 / p95 ms | versions_scan p50 ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, ids in (("有 image_path", with_path), ("无 image_path", without)):
        walls, scans, branches = [], [], {}
        for row in ids:
            started = time.perf_counter()
            response = client.get(f"/api/persons/evidence/{row['id']}/image")
            walls.append((time.perf_counter() - started) * 1000)
            branch = response.headers.get("x-profile-branch", f"HTTP {response.status_code}")
            branches[branch] = branches.get(branch, 0) + 1
            scan = parse_timing(response.headers.get("server-timing", "")).get("versions_scan")
            if scan:
                scans.append(scan[0])
        lines.append(f"| {label} | {len(ids)} | {branches or '-'} | {fmt(walls)} | {fmt(scans)} |")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--image-samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--title", default="人物库出镜审核读取耗时基线")
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    db_file = data_dir / "metadata.sqlite3"
    before = file_state(db_file)
    traces: list[str] = []
    app, store = build_app(data_dir, traces)
    client = TestClient(app)
    sources = pick_sources(store)
    if not sources:
        print("数据库里没有 person_evidence，无法测量", file=sys.stderr)
        return 1
    http_rows, _ = http_section(client, sources, args.repeat)
    out = [
        f"# {args.title}",
        "",
        f"- 数据目录：`{data_dir}`；每组 1 次预热 + {args.repeat} 次计时；单位毫秒；进程内 "
        f"TestClient，不含网络与浏览器。",
        "- 单元格为 p50 / p95。`assemble` 为 Python 端组装（总耗时减去各阶段与 middleware）。",
        "",
        "## 数据规模",
        "",
        *scale_section(store),
        "",
        "## 审核列表 `GET /api/persons/review/shots`（limit=50）",
        "",
        *http_rows,
        "",
        "## 抽样 SQL 的 EXPLAIN QUERY PLAN（accepted、最大素材）",
        "",
        *explain_section(client, store, traces, sources),
        "",
        "## 证据图片 `GET /api/persons/evidence/{id}/image`",
        "",
        *image_section(client, store, args.image_samples),
        "",
    ]
    after = file_state(db_file)
    out.append(
        f"只读校验：主库大小与修改时间"
        f"{'未变化' if before == after else '发生变化（请检查是否有其他进程在写入）'}。"
    )
    text = "\n".join(out) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"已写入 {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
