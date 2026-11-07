#!/usr/bin/env python3
"""在真实库上固定 review_shots 的输出，优化前后逐项比对（只读）。

uv run python scripts/golden_person_review.py record /tmp/golden_before.json
uv run python scripts/golden_person_review.py compare /tmp/golden_before.json
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from persons.persons import Persons
from shared.storage import Store

DATA = Path(os.environ.get("GOLDEN_DATA", "data")).resolve()
ONLY = os.environ.get("GOLDEN_ONLY", "")


class ReadOnlyStore(Store):
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "metadata.sqlite3"

    @contextmanager
    def connect(self):
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def requests(persons: Persons):
    sources = [
        r["source_id"]
        for r in persons.store.rows(
            "SELECT source_id FROM person_evidence GROUP BY source_id ORDER BY count(*) DESC"
        )
    ]
    yield "all/none", {}
    for source in sources:
        tag = source[:6]
        for status in (None, "candidate", "accepted", "rejected"):
            base = {"source_id": source, "status": status, "limit": 50}
            yield f"{tag}/{status}", base
            yield f"{tag}/{status}/offset50", {**base, "offset": 50}
            yield f"{tag}/{status}/compact", {**base, "compact": True}
            if status == "rejected":
                continue
            filters = persons.review_shots(**base)["filters"]
            pairs = sorted((f for f in filters if f["actor_id"]), key=lambda f: -f["count"])[:3]
            for index, f in enumerate(pairs):
                yield (
                    (f"{tag}/{status}/pair{index}"),
                    {**base, "actor_id": f["actor_id"], "role_id": f["role_id"]},
                )
            yield f"{tag}/{status}/no_candidate", {**base, "no_candidate": True}
    shots = persons.store.rows(
        "SELECT source_id,shot_id FROM person_evidence GROUP BY source_id,shot_id LIMIT 3"
    )
    for shot in shots:
        yield (
            (f"shot/{shot['shot_id'][:6]}"),
            {"source_id": shot["source_id"], "shot_id": shot["shot_id"], "limit": 1},
        )


def collect(persons: Persons) -> dict:
    out = {}
    for label, params in requests(persons):
        if ONLY and not any(token in label for token in ONLY.split(",")):
            continue
        started = time.perf_counter()
        payload = persons.review_shots(**params)
        elapsed = (time.perf_counter() - started) * 1000
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        out[label] = {
            "sha": hashlib.sha256(text.encode()).hexdigest(),
            "items": len(payload["items"]),
            "total": payload["total"],
            "scope_total": payload["scope_total"],
            "ms": round(elapsed),
        }
        print(
            f"{label:40s} {elapsed:7.0f}ms items={len(payload['items'])} total={payload['total']}", flush=True
        )
    return out


def main() -> int:
    mode, path = sys.argv[1], Path(sys.argv[2])
    persons = Persons(ReadOnlyStore(DATA))
    result = collect(persons)
    if mode == "record":
        path.write_text(json.dumps(result, indent=1))
        print("recorded", len(result))
        return 0
    before = json.loads(path.read_text())
    before = {k: v for k, v in before.items() if k in result}
    bad = [k for k in before if before[k]["sha"] != result.get(k, {}).get("sha")]
    old = sum(v["ms"] for v in before.values())
    new = sum(v["ms"] for v in result.values())
    print(f"\n{len(before) - len(bad)}/{len(before)} 项输出逐字一致；累计耗时 {old} ms -> {new} ms")
    for key in bad:
        print("DIFF", key)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
