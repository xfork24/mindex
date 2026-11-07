#!/usr/bin/env python3
"""固定规模工程容量工装：隔离空库生成、确定性嵌入服务、双 API 20 并发与原始证据。

工程向量不代表真实模型质量。原定规模为每版本 100万/300万向量、三个版本、4096维；
--smoke 的结果始终标为工装验证，不用于容量或 Hit@5 验收。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {"100k": (100_000, 1_000_000), "1m": (1_000_000, 3_000_000)}
MODEL = "mindex-capacity-engineering-v1"


def vector(identifier, dimensions):
    seed = int.from_bytes(hashlib.sha256(identifier.encode()).digest()[:8], "big")
    values = np.random.default_rng(seed).standard_normal(dimensions).astype(np.float32)
    return (values / np.linalg.norm(values)).tolist()


def space(config):
    return {
        "model": MODEL,
        "dimensions": config.embedding.dimensions,
        "quantization": "float32",
        "normalized": True,
        "revision": "capacity-engineering-v1",
        "implementation": "capacity-pcg64-v1",
        "fps": config.video.fps,
        "max_frames": config.video.max_frames,
        "max_pixels": config.video.max_pixels,
    }


def dimensions(args, config):
    if not args.smoke and config.embedding.dimensions != 4096:
        raise ValueError("正式容量必须为 4096 维；较小维度只能使用 --smoke")
    if config.embedding.model != MODEL or config.embedding.quantization != "float32":
        raise ValueError(
            f"工程配置必须显式使用 embedding.model={MODEL}、quantization=float32，禁止冒充真实模型"
        )
    return (1025, 3075) if args.smoke else PROFILES[args.profile]


def preflight(args):
    segments, vectors = (1025, 3075) if args.smoke else PROFILES[args.profile]
    dim = args.dimensions if args.smoke else 4096
    raw = vectors * 3 * dim * 4
    free = shutil.disk_usage(args.vector_storage).free
    return {
        "profile": args.profile,
        "segments": segments,
        "vectors_per_version": vectors,
        "versions": 3,
        "dimensions": dim,
        "raw_vector_bytes": raw,
        "available_bytes": free,
        "fits_raw_vectors_only": free >= raw,
        "smoke": args.smoke,
        "status": "preflight_only" if free >= raw else "environment_insufficient",
        "note": "裸向量尚未包含 HNSW、元数据、WAL、备份和媒体；预检不代表容量通过",
    }


def seed(args):
    from maintenance.migration import _empty, offline
    from processing.indexing import VectorIndex, vector_client
    from shared.backends import open_context
    from shared.config import load_config
    from shared.lexical_store import build_lexical_index
    from shared.storage import dumps

    config = load_config(args.config)
    segments, vectors = dimensions(args, config)
    required = vectors * 3 * config.embedding.dimensions * 4
    if shutil.disk_usage(args.vector_storage).free < required:
        raise ValueError("向量测试盘连裸向量容量都不足；未写入任何测试数据")
    with open_context(config) as context:
        if context.form != "cluster" or not _empty(context.store):
            raise ValueError("seed 仅允许专用空 cluster 目标；禁止覆盖或混入业务库")
        store = context.store
        identity = {
            "kind": "engineering-only",
            "profile": args.profile,
            "smoke": args.smoke,
            "segments": segments,
            "vectors_per_version": vectors,
            "versions": 3,
            "dimensions": config.embedding.dimensions,
            "library_id": store.library_id,
        }
        with offline(context, "capacity-seed"):
            # 在首份写入前标记；中断后的半库不能误作正式容量数据，需新建隔离目标。
            store.execute(
                "INSERT INTO settings(key,value) VALUES('capacity.dataset',?)",
                (dumps({**identity, "state": "seeding"}),),
            )
            with store.connect() as db:
                for number in range(1000):
                    db.execute(
                        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
                        (
                            f"cap-source-{number:04}",
                            f"工程素材 {number}",
                            f"capacity/{number}.mp4",
                            dumps(
                                {
                                    "duration": (segments // 1000 + 1) * 10,
                                    "fps": 10,
                                    "width": 160,
                                    "height": 90,
                                    "audio": False,
                                }
                            ),
                            "capacity",
                        ),
                    )
                for offset in range(0, segments, 250):
                    db.executemany(
                        "INSERT INTO "
                        "segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
                        "VALUES(?,?,'shot',?,?,?,'','capacity','engineering-only')",
                        [
                            (
                                f"cap-seg-{n:08}",
                                f"cap-source-{n % 1000:04}",
                                n // 1000 * 10,
                                n // 1000 * 10 + 10,
                                f"capacity/{n}.mp4",
                            )
                            for n in range(offset, min(offset + 250, segments))
                        ],
                    )
            for version in range(3):
                index_id = f"capacity-{args.profile}-{version}"
                store.execute(
                    "INSERT INTO index_versions(id,spec,status) VALUES(?,?,'building')",
                    (
                        index_id,
                        dumps(
                            {
                                "format": 2,
                                "mode": "auto",
                                "space": space(config),
                                "preprocessing": config.video.model_dump(),
                            }
                        ),
                    ),
                )
                client = vector_client(config, library_id=store.library_id)
                index = VectorIndex(store.root, index_id, create=True, client=client)
                quotient, remainder = divmod(vectors, segments)
                for offset in range(0, segments, 128):
                    members, representations, entries = [], [], []
                    for number in range(offset, min(segments, offset + 128)):
                        sid, source = f"cap-seg-{number:08}", f"cap-source-{number % 1000:04}"
                        snapshot = {
                            "id": sid,
                            "source_id": source,
                            "source_name": source,
                            "source_duration": (segments // 1000 + 1) * 10,
                            "kind": "shot",
                            "start": number // 1000 * 10,
                            "end": number // 1000 * 10 + 10,
                            "path": f"capacity/{number}.mp4",
                            "thumbnail": "",
                            "views": {"analysis": "工程容量 城市", "dialogue": "工程对白"},
                            "indexed_views": ["video", "analysis", "dialogue"],
                            "annotation": {
                                "scene": [f"group-{number % 1000}"],
                                "emotions": [],
                                "shot_size": [],
                            },
                        }
                        members.append((index_id, sid, dumps(snapshot)))
                        embedded = vector(sid, config.embedding.dimensions)
                        for window in range(quotient + (number < remainder)):
                            view = ("video", "analysis", "dialogue")[window % 3]
                            rid = hashlib.sha256(f"{index_id}:{sid}:{window}".encode()).hexdigest()
                            entries.append((rid, embedded, snapshot, view, f"engineering-{number}-{window}"))
                            representations.append((rid, index_id, sid, view, "engineering", rid))
                    index.upsert_many(entries)
                    with store.connect() as db:
                        db.executemany(
                            "INSERT INTO index_members(index_id,segment_id,snapshot,complete) "
                            "VALUES(?,?,?,1)",
                            members,
                        )
                        db.executemany(
                            "INSERT INTO "
                            "representations(id,index_id,segment_id,view,document,cache_key) "
                            "VALUES(?,?,?,?,?,?)",
                            representations,
                        )
                if index.collection.count() != vectors:
                    raise ValueError("实际向量条数与固定规模不一致")
                build_lexical_index(store, index_id)
                store.execute("UPDATE index_versions SET status='ready' WHERE id=?", (index_id,))
            store.execute(
                "INSERT INTO settings(key,value) VALUES('active',?)", (f"capacity-{args.profile}-2",)
            )
            store.execute(
                "UPDATE settings SET value=? WHERE key='capacity.dataset'",
                (dumps({**identity, "state": "ready"}),),
            )
        return identity


def embedder(args):
    import uvicorn

    from shared.config import fingerprint, load_config

    config = load_config(args.config)
    dimensions(args, config)
    identity = space(config)
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ready": True, "space": identity, "engineering_only": True}

    @app.post("/embed/text")
    def text(body: dict):
        return {
            "space_id": fingerprint(identity),
            "vectors": [vector(value, config.embedding.dimensions) for value in body["texts"]],
        }

    uvicorn.run(app, host=args.host, port=args.port)


def summary(rows):
    # 包括失败、503 和客户端超时的实际耗时，不能只统计成功请求。
    values = [row["elapsed_ms"] for row in rows]
    return {
        "requests": len(rows),
        "failures": sum(not row["ok"] for row in rows),
        "p50_ms": float(np.percentile(values, 50)) if values else None,
        "p95_ms": float(np.percentile(values, 95)) if values else None,
        "p99_ms": float(np.percentile(values, 99)) if values else None,
    }


def run(args):
    from shared.config import load_config
    from shared.storage import Store

    config = load_config(args.config)
    with Store.open(config) as store:
        marker = store.one("SELECT value FROM settings WHERE key='capacity.dataset'")
        if marker is None:
            raise ValueError("缺少工程数据身份；真实模型质量请使用 mindex e2e，不得将工程结果标成真实质量")
        dataset = json.loads(marker["value"])
        wanted = dimensions(args, config)
        if (
            dataset.get("state") != "ready"
            or (dataset["segments"], dataset["vectors_per_version"]) != wanted
            or dataset["smoke"] != args.smoke
        ):
            raise ValueError("数据规模/状态与指定 profile 不一致")
        from processing.indexing import VectorIndex, vector_client

        actual = store.rows("SELECT index_id,count(*) AS n FROM representations GROUP BY index_id")
        if len(actual) != 3 or any(row["n"] != wanted[1] for row in actual):
            raise ValueError("实际三个版本的向量元数据与规模不一致")
        collections = {
            row["index_id"]: VectorIndex(
                store.root, row["index_id"], client=vector_client(config, library_id=store.library_id)
            ).collection.count()
            for row in actual
        }
        if any(count != wanted[1] for count in collections.values()):
            raise ValueError("实际向量集合条数与固定规模不一致")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "dataset": dataset,
        "concurrency": 20,
        "code_version": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "memory_bytes": (
            int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
            if sys.platform == "darwin"
            else os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        ),
        "rss_sampler": "ps RSS KiB / 200ms，结合每 API OS 峰值 RSS",
        "status": "running",
        "collection_counts": collections,
        "model_quality": "未运行；工程向量不代表真实模型 Hit@5",
    }

    spec = importlib.util.spec_from_file_location("scaling_verifier", ROOT / "scripts/verify-scaling.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    report["source_sha256"] = verifier.source_identity()
    rows, stop, peaks = [], threading.Event(), {str(pid): 0 for pid in args.api_pid}

    def sample():
        while not stop.wait(0.2):
            try:
                values = subprocess.check_output(
                    ["ps", "-o", "pid=,rss=", "-p", ",".join(map(str, args.api_pid))], text=True, timeout=2
                )
                found = set()
                for line in values.splitlines():
                    pid, rss = line.split()
                    peaks[pid] = max(peaks.get(pid, 0), int(rss) * 1024)
                    found.add(pid)
                for pid in peaks.keys() - found:
                    peaks[pid] = 2**63
            except (subprocess.SubprocessError, ValueError):
                for pid in peaks:
                    peaks[pid] = 2**63

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    token = os.environ.get("MINDEX_WEB_TOKEN", "")
    clients = [
        httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=10,
            trust_env=False,
        )
        for url in args.api_url
    ]

    def query(number, fraction, phase):
        source_count = 10 if fraction == 0.01 else 1
        target = (number % max(1, wanted[0] // 1000)) * 1000 + number % source_count
        sid = f"cap-seg-{target:08}"
        body = {
            "text": sid,
            "granularity": "shot",
            "channels": ["visual"],
            "limit": 5,
            "filters": {"source_ids": [f"cap-source-{n:04}" for n in range(source_count)]},
        }
        started = time.monotonic()
        row = {"number": number, "fraction": fraction, "phase": phase, "api": number % 2, "expected": sid}
        try:
            response = clients[number % 2].post("/api/search/text", json=body)
            data = response.json() if response.status_code == 200 else {}
            hits = data.get("results", [])
            within = all(hit["source_id"] in body["filters"]["source_ids"] for hit in hits)
            row.update(
                status=response.status_code,
                ok=response.status_code == 200 and within,
                scope_ok=within,
                hit_at5=any(hit["id"] == sid for hit in hits[:5]),
                eligible=data.get("eligible_segments"),
                error=response.text[:500] if response.status_code != 200 else None,
            )
        except Exception as exc:  # noqa: BLE001 — 失败请求必须保留并计入耗时
            row.update(ok=False, error=type(exc).__name__, status=None, hit_at5=False, scope_ok=False)
        row["elapsed_ms"] = (time.monotonic() - started) * 1000
        return row

    try:
        for fraction in (0.01, 0.001):
            rows.extend(query(n, fraction, "cold") for n in range(2))
            with ThreadPoolExecutor(max_workers=20) as workers:
                rows.extend(
                    f.result()
                    for f in as_completed(
                        [workers.submit(query, n, fraction, "warm") for n in range(args.requests)]
                    )
                )
        for n, client in enumerate(clients):
            try:
                report[f"api_{n}_metrics"] = client.get("/api/metrics").json()
                if report[f"api_{n}_metrics"].get("peak_rss_bytes"):
                    pid = str(args.api_pid[n])
                    peaks[pid] = max(peaks[pid], report[f"api_{n}_metrics"]["peak_rss_bytes"])
                if report[f"api_{n}_metrics"].get("pid") != args.api_pid[n]:
                    raise ValueError("采样 PID 与 API 实际 PID 不一致")
            except Exception as exc:  # noqa: BLE001
                report[f"api_{n}_metrics_error"] = type(exc).__name__
        report["profiles"] = {
            str(fraction): summary([r for r in rows if r["fraction"] == fraction and r["phase"] == "warm"])
            for fraction in (0.01, 0.001)
        }
        report["all_requests"] = summary(rows)
        report["api_peak_rss_bytes"] = peaks
        report["source_unchanged"] = verifier.source_identity() == report["source_sha256"]
        report["engineering_nearest_hit_at5"] = sum(row["hit_at5"] for row in rows) / len(rows)
        passed = (
            not any(key.endswith("metrics_error") for key in report)
            and report["source_unchanged"]
            and not report["all_requests"]["failures"]
            and all(v <= 2 * 1024**3 for v in peaks.values())
            and all(v["p95_ms"] <= 2000 and v["p99_ms"] <= 5000 for v in report["profiles"].values())
        )
        report["capacity_passed"] = passed and not args.smoke
        report["status"] = (
            ("smoke_passed_not_acceptance" if args.smoke else "engineering_passed_quality_pending")
            if passed
            else "failed"
        )
    finally:
        stop.set()
        sampler.join(5)
        for client in clients:
            client.close()
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output / "requests.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        )
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "seed", "embedder", "run"])
    parser.add_argument("--profile", choices=PROFILES, default="1m")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dimensions", type=int, default=4096)
    parser.add_argument("--vector-storage", type=Path, default=ROOT)
    parser.add_argument("--api-url", nargs=2)
    parser.add_argument("--api-pid", nargs=2, type=int)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=49110)
    args = parser.parse_args()
    if args.command != "preflight" and args.config is None:
        parser.error("需要隔离环境 --config")
    if args.command == ("run") and (
        not args.api_url
        or not args.api_pid
        or not args.output
        or args.requests < 20
        or len(set(args.api_pid)) != 2
        or len(set(args.api_url)) != 2
    ):
        parser.error("run 需要两个 --api-url、两个 --api-pid、空 --output 目录与至少20请求")
    if args.command == "embedder":
        embedder(args)
        return 0
    result = globals()[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") in {"failed", "environment_insufficient"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
