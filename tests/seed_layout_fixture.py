"""Seed only the isolated UI fixture; generate portable pages using production renderers."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from PIL import Image

from diagnostics.film import export_source
from diagnostics.label_review import render_review
from persons.person_models import CastEntry, Entity
from persons.persons import Persons
from shared.config import Config
from shared.storage import Store, dumps


def seed(root: Path):
    # Refuse paths outside /tmp: this fixture must never seed a real workspace library.
    root = root.resolve()
    if not root.is_relative_to(Path("/tmp").resolve()):
        raise ValueError("UI fixture data must live under /tmp")
    config = Config(data_dir=root)
    config.embedding.dimensions = 8
    store = Store(root)
    persons = Persons(store)
    source = store.sources()[0]
    work = persons.work_for(source["id"])
    actors = []
    for i in range(18):
        actor = persons.save(
            Entity(kind="actor", name=f"布局测试演员 {i:02d} · 长姓名 LongName", aliases=["Layout fixture"])
        )
        role = persons.save(
            Entity(
                kind="role",
                name=f"布局角色 {i:02d} · 很长的作品角色名称",
                work_ids=[work],
                actor_ids=[actor["id"]],
            )
        )
        raw = io.BytesIO()
        Image.new("RGB", (160, 120), (50 + i * 8, 110, 80)).save(raw, format="PNG")
        persons.add_reference(actor["id"], raw.getvalue())
        persons.add_reference(actor["id"], raw.getvalue(), role_id=role["id"])
        actors.append((actor, role))
    shots = store.segments(source["id"], "shot")
    for i, shot in enumerate(shots):
        actor, role = actors[i]
        store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"layout-evidence-{i}",
                source["id"],
                shot["id"],
                actor["id"],
                role["id"],
                None,
                shot["start"],
                shot["end"],
                "candidate",
                dumps({"image_path": shot["thumbnail"], "score": 0.9}),
            ),
        )
    raw = io.BytesIO()
    Image.new("RGB", (640, 360), (70, 130, 90)).save(raw, format="PNG")
    draft = persons.create_import([raw.getvalue()])
    entries = [
        CastEntry(
            image_id=draft["images"][0]["id"],
            actor_name=f"候选演员 {i}",
            foreign_name="Long English candidate name",
            role_name=f"候选角色 {i}",
            bbox=[10, 10, 150, 180],
        ).model_dump()
        for i in range(12)
    ]
    store.execute(
        "UPDATE person_imports SET data=? WHERE id=?",
        (
            dumps(
                {**draft, "entries": entries, "warnings": ["工程测试：模拟长内容，需要逐条人工核对。" * 8]}
            ),
            draft["id"],
        ),
    )
    # A separate source supplies pagination without changing indexed source segments.
    paged_id = "layout-paged-source"
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        (paged_id, "长素材标题_LayoutFixture_" * 8, source["path"], source["media"], source["layout_key"]),
    )
    for i in range(60):
        s = shots[i % len(shots)]
        store.execute(
            "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"layout-shot-{i}",
                paged_id,
                "shot",
                s["start"],
                s["end"],
                s["path"],
                s["thumbnail"],
                s["generation"],
                None,
                "测试切点 " * 20,
            ),
        )
    # Independent review pages are created by the real renderers, including relative media URLs.
    film = export_source(config, paged_id, root / "reviews" / "film")
    run = root / "reviews" / "labels"
    run.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({"source_id": source["id"]}))
    queries = run / "queries.jsonl"
    queries.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": f"query-{i}",
                    "split": "validation",
                    "text": "长查询文本：绿色画面及人物出镜 " * 12,
                    "relevant": [],
                },
                ensure_ascii=False,
            )
            for i in range(12)
        )
    )
    labels = render_review(config, run, queries)
    result = {
        "source_id": source["id"],
        "paged_source_id": paged_id,
        "draft_id": draft["id"],
        "actors": [a["id"] for a, _ in actors],
        "work_id": work,
        "film": film["review"],
        "labels": str(labels),
    }
    (root / "layout-fixture.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    seed(parser.parse_args().data_dir)
