"""创作页分镜四类召回：数据模型、执行指纹、查询文件与人物小节（plans/RECALL_MIGRATION_PLAN）。"""

from __future__ import annotations

import hashlib
import io

import numpy as np
import pytest
from conftest import FixtureEmbedder, FixtureSpeaker
from fastapi.testclient import TestClient
from PIL import Image

from mindex import studio
from mindex import studio_workspace as ws
from mindex.api import create_app
from mindex.indexing import IndexBuilder
from mindex.pipeline import Pipeline
from mindex.storage import Store
from mindex.studio import Shot, ShotRecall
from mindex.types import Filters


def test_shot_recall_mode_media_and_person_constraints():
    # text 不许带 media；image/video 必须带对应类型 media 且不许带人物设置。
    with pytest.raises(ValueError):
        ShotRecall(mode="text", media_id="a" * 64, media_kind="image")
    with pytest.raises(ValueError):
        ShotRecall(mode="text", media_kind="image")
    with pytest.raises(ValueError):
        ShotRecall(mode="image")
    with pytest.raises(ValueError):
        ShotRecall(mode="image", media_id="a" * 64, media_kind="video")
    with pytest.raises(ValueError):
        ShotRecall(mode="video", media_id="a" * 64, media_kind="video", person_ids=["p1"])
    with pytest.raises(ValueError):
        ShotRecall(mode="image", media_id="a" * 64, media_kind="image", person_mode="onscreen")
    with pytest.raises(ValueError):
        ShotRecall(mode="video", media_id="a" * 64, media_kind="video", person_relation="any")
    assert ShotRecall(mode="image", media_id="a" * 64, media_kind="image").mode == "image"
    assert ShotRecall().mode == "text"
    for value in ("abc", "../query", "*", "a" * 63, "A" * 64):
        with pytest.raises(ValueError):
            ShotRecall(mode="image", media_id=value, media_kind="image")


def test_shot_recall_defaults_and_person_limit():
    recall = ShotRecall()
    assert recall.person_ids == [] and recall.person_mode == "auto"
    assert recall.person_relation == "all" and recall.filters == Filters()
    assert recall.media_id is None and recall.media_kind is None
    with pytest.raises(ValueError):
        ShotRecall(person_ids=[f"p{i}" for i in range(31)])


def test_shot_recall_optional_for_legacy_content():
    legacy = Shot.model_validate({"id": "s1", "text": "文案", "query": "查询", "estimated_seconds": 3})
    assert legacy.recall is None
    shot = Shot.model_validate(
        {
            "id": "s1",
            "text": "文案",
            "query": "查询",
            "estimated_seconds": 3,
            "recall": {"person_ids": ["a"], "person_mode": "onscreen", "filters": {"source_id": "s"}},
        }
    )
    assert shot.recall.person_ids == ["a"] and shot.recall.filters.source_id == "s"


def test_source_selection_signatures_preserve_legacy_and_ignore_order():
    assert ws.recall_signature(ShotRecall()) is None
    legacy = ws.recall_signature(ShotRecall(filters={"source_id": "a"}))
    assert legacy == ws.recall_signature(ShotRecall(filters={"source_ids": ["a", "a"]}))
    multi = ws.recall_signature(ShotRecall(filters={"source_ids": ["b", "a"]}))
    assert multi == ws.recall_signature(ShotRecall(filters={"source_ids": ["a", "b", "a"]}))
    assert multi != legacy
    assert multi == ws.recall_signature(ShotRecall(filters={"source_id": "c", "source_ids": ["a", "b"]}))
    assert legacy == ws.recall_signature(ShotRecall(filters={"source_id": "a", "source_ids": []}))


# ---------------------------------------------------------------- 工作台召回执行与指纹


@pytest.fixture
def prepared(config, video_file):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 29.97
    Pipeline(config).prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    return config, embedder


def setup_workspace(config):
    job_id = studio.create_job(config, "红色开场。蓝色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            Shot(id="a", text="红色开场。", query="红色", estimated_seconds=1),
            Shot(id="b", text="蓝色结尾。", query="蓝色", estimated_seconds=1),
        ],
    )
    ws.persist(config, job_id, w)
    return job_id, w, p


def build(config, job_id, w, p, embedder):
    checked = ws.preflight(config, job_id, w, p)
    bid = str(len(w["builds"]))
    w["builds"].append(
        {
            "id": bid,
            "plan_id": p["id"],
            "status": "running",
            "force": False,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, bid, speaker=FixtureSpeaker(config), embedder=embedder)
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def shot_list(w, p):
    return [
        Shot.model_validate(ws.version(w, p["shots"][sid]["content"])["data"])
        for sid in ws.ordered_shots(w, p)
    ]


def set_recall(config, job_id, w, p, index, recall):
    shots = shot_list(w, p)
    shots[index].recall = recall
    ws.set_board(config, job_id, w, p, shots)
    ws.persist(config, job_id, w)


def test_default_recall_keeps_legacy_signature_and_reuse(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder)
    assert all(node["action"] == "reuse" for node in ws.preflight(config, jid, w, p)["nodes"])
    # 全默认 ShotRecall 不得改变签名（payload 与旧版逐字节一致）。
    set_recall(config, jid, w, p, 0, ShotRecall())
    actions = {node["key"]: node["action"] for node in ws.preflight(config, jid, w, p)["nodes"]}
    assert actions["recall:a"] == "reuse" and actions["recall:b"] == "reuse"


def test_document_auto_recall_invalidates_legacy_cache_only(prepared, monkeypatch):
    config, _embedder = prepared
    jid, w, p = setup_workspace(config)
    content = ws.version(w, p["shots"]["a"]["content"])["data"]
    content["query"] = "晓伟凝视着档案中关于阿公郑木生的记录，神色凝重"
    from mindex import persons

    current = ws.specification(config, jid, w, p, "recall", "a")["signature"]
    monkeypatch.setattr(persons, "has_document_reference", lambda _: False)
    legacy = ws.specification(config, jid, w, p, "recall", "a")["signature"]
    assert current != legacy
    content["recall"] = ShotRecall(person_mode="onscreen").model_dump()
    strict = ws.specification(config, jid, w, p, "recall", "a")["signature"]
    monkeypatch.setattr(persons, "has_document_reference", lambda _: True)
    assert ws.specification(config, jid, w, p, "recall", "a")["signature"] == strict


def test_recall_signature_invalidation_matrix(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder)

    def action():
        return {node["key"]: node["action"] for node in ws.preflight(config, jid, w, p)["nodes"]}["recall:a"]

    def signature():
        return ws.specification(config, jid, w, p, "recall", "a")["signature"]

    base = signature()
    set_recall(config, jid, w, p, 0, ShotRecall(person_ids=["p2", "p1"]))
    assert action() == "rebuild" and signature() != base
    ordered = signature()
    set_recall(config, jid, w, p, 0, ShotRecall(person_ids=["p1", "p2"]))
    assert signature() == ordered, "person_ids 重排不应失效"
    set_recall(config, jid, w, p, 0, ShotRecall(person_mode="onscreen"))
    assert action() == "rebuild", "无 ids 但 mode 非 auto 仍非默认"
    set_recall(config, jid, w, p, 0, ShotRecall(filters={"source_id": "s"}))
    assert action() == "rebuild", "换素材筛选应失效"
    set_recall(config, jid, w, p, 0, ShotRecall(mode="image", media_id="b" * 64, media_kind="image"))
    assert action() == "rebuild", "换查询方式应失效"
    set_recall(config, jid, w, p, 0, ShotRecall())
    assert action() == "reuse" and signature() == base, "改回默认应复用旧版本"


def test_image_video_recall_execute_and_missing_media(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)

    def put_media(payload: bytes, suffix: str) -> str:
        media_id = hashlib.sha256(payload).hexdigest()
        studio.save_bytes(ws.root(config, jid) / "inputs" / f"{media_id}{suffix}", payload)
        return media_id

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 0, 0)).save(buffer, format="PNG")
    image_id = put_media(buffer.getvalue(), ".png")
    set_recall(config, jid, w, p, 0, ShotRecall(mode="image", media_id=image_id, media_kind="image"))
    w, p = build(config, jid, w, p, embedder)
    data = ws.version(w, p["shots"]["a"]["recall"])["data"]
    assert data["mode"] == "image" and data["recall"]["media_id"] == image_id
    assert data["results"], "红色查询图应召回到镜头"
    first_shot = Store(config.data_dir).rows("SELECT id FROM segments WHERE kind='shot' ORDER BY start")[0][
        "id"
    ]
    assert data["results"][0]["segment_id"] == first_shot, "红色图与红色文本同向量，应首选红色镜头"

    source = Store(config.data_dir).one("SELECT path FROM sources LIMIT 1")
    video_id = put_media(Store(config.data_dir).resolve_media(source["path"]).read_bytes(), ".mp4")
    set_recall(config, jid, w, p, 0, ShotRecall(mode="video", media_id=video_id, media_kind="video"))
    w, p = build(config, jid, w, p, embedder)
    data = ws.version(w, p["shots"]["a"]["recall"])["data"]
    assert data["mode"] == "video" and data["results"]

    # 查询文件被清理后：按 digest 找不到且需复验，兜底报错而非静默换文件。
    set_recall(config, jid, w, p, 0, ShotRecall(mode="image", media_id=image_id, media_kind="image"))
    (ws.root(config, jid) / "inputs" / f"{image_id}.png").unlink()
    with pytest.raises(studio.StudioError, match="查询文件缺失或已损坏"):
        ws.execute(
            config,
            jid,
            ws.load(config, jid),
            ws.plan(ws.load(config, jid), p["id"]),
            "recall",
            "a",
            embedder=embedder,
        )


# ---------------------------------------------------------------- 查询文件上传端点与 PATCH 校验


@pytest.fixture
def studio_client(config, video_file):
    Pipeline(config).prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    client = TestClient(create_app(config, embedder))
    return config, client


def red_image():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def create_studio_job(client) -> str:
    response = client.post("/api/studio/jobs", json={"text": "红色开场。蓝色结尾。"})
    assert response.status_code == 201
    return response.json()["id"]


def test_query_media_upload_dedupe_and_validation(studio_client, video_file):
    config, client = studio_client
    job_id = create_studio_job(client)
    plan_id = ws.load(config, job_id)["active_plan"]
    url = f"/api/studio/jobs/{job_id}/plans/{plan_id}/query-media"
    payload = red_image()
    first = client.post(url, files={"file": ("red.png", payload, "image/png")}, data={"kind": "image"})
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["media_id"] == hashlib.sha256(payload).hexdigest() and body["kind"] == "image"
    assert body["preview_url"].endswith(f"inputs/{body['media_id']}.png")
    assert (ws.root(config, job_id) / "inputs" / f"{body['media_id']}.png").is_file()
    assert client.get(body["preview_url"]).status_code == 200
    # 内容寻址去重：同文件再传同 id，不产生第二个文件。
    second = client.post(url, files={"file": ("again.png", payload, "image/png")}, data={"kind": "image"})
    assert second.json()["media_id"] == body["media_id"]
    assert len(list((ws.root(config, job_id) / "inputs").glob("*"))) == 1
    renamed = client.post(url, files={"file": ("same.webp", payload, "image/webp")}, data={"kind": "image"})
    assert renamed.status_code == 201 and renamed.json()["preview_url"] == body["preview_url"]
    assert len(list((ws.root(config, job_id) / "inputs").glob("*"))) == 1
    # 非图片内容 422。
    assert (
        client.post(
            url, files={"file": ("note.txt", b"not an image", "text/plain")}, data={"kind": "image"}
        ).status_code
        == 422
    )
    # 超限 413。
    config.search.max_image_upload_mb = 1
    noisy = io.BytesIO()
    Image.fromarray(np.random.randint(0, 256, (1024, 1024, 3), dtype=np.uint8)).save(
        noisy, format="JPEG", quality=100
    )
    big = client.post(
        url, files={"file": ("big.jpg", noisy.getvalue(), "image/jpeg")}, data={"kind": "image"}
    )
    config.search.max_image_upload_mb = 20
    assert big.status_code == 413
    # 视频：正常上传、超时长 422、kind 非法 422。
    video = video_file.read_bytes()
    ok = client.post(url, files={"file": ("clip.mp4", video, "video/mp4")}, data={"kind": "video"})
    assert ok.status_code == 201 and ok.json()["kind"] == "video"
    config.search.max_query_seconds = 0.5
    long = client.post(url, files={"file": ("clip.mp4", video, "video/mp4")}, data={"kind": "video"})
    config.search.max_query_seconds = 30
    assert long.status_code == 422
    assert (
        client.post(
            url, files={"file": ("x", b"x", "application/octet-stream")}, data={"kind": "audio"}
        ).status_code
        == 422
    )


def test_patch_recall_validation(studio_client):
    config, client = studio_client
    job_id = create_studio_job(client)
    w = ws.load(config, job_id)
    plan_id = w["active_plan"]
    url = f"/api/studio/jobs/{job_id}/plans/{plan_id}"

    def patch(recall):
        fresh = ws.load(config, job_id)
        return client.patch(
            url,
            json={
                "revision": ws.revision(ws.plan(fresh, plan_id)),
                "shots": [
                    {
                        "id": "a",
                        "text": "红色开场。",
                        "query": "红色",
                        "estimated_seconds": 1,
                        "recall": recall,
                    }
                ],
            },
        )

    assert patch({"person_ids": ["missing"]}).status_code == 422
    work = client.post("/api/persons/entities", json={"kind": "work", "name": "作品"}).json()
    assert patch({"person_ids": [work["id"]]}).status_code == 422
    actor = client.post("/api/persons/entities", json={"kind": "actor", "name": "演员甲"}).json()
    assert patch({"person_ids": [actor["id"]]}).status_code == 200
    assert patch({"mode": "image", "media_id": "deadbeef", "media_kind": "image"}).status_code == 422
    payload = red_image()
    media_id = hashlib.sha256(payload).hexdigest()
    studio.save_bytes(ws.root(config, job_id) / "inputs" / f"{media_id}.png", payload)
    response = patch({"mode": "image", "media_id": media_id, "media_kind": "image"})
    assert response.status_code == 200
    contents = client.get(f"/api/studio/jobs/{job_id}/versions", params={"stage": "content"}).json()
    stored = [v for v in contents["items"] if v["shot_id"] == "a"][-1]
    assert stored["data"]["recall"]["media_id"] == media_id
    path = ws.root(config, job_id) / "inputs" / f"{media_id}.png"
    path.write_bytes(b"damaged")
    with pytest.raises(studio.StudioError, match="查询文件缺失或已损坏"):
        ws.query_media(config, job_id, ShotRecall(mode="image", media_id=media_id, media_kind="image"))


def test_person_recall_filters_by_published_evidence(prepared):
    """人物出镜召回由已发布证据约束候选：查询文本命中的镜头无证据时不可入选。"""
    from mindex.persons import Entity, Persons, ShotReview

    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    store = Store(config.data_dir)
    source_id = store.one("SELECT id FROM sources LIMIT 1")["id"]
    shots = store.rows("SELECT id FROM segments WHERE kind='shot' ORDER BY start")
    evidence_shot = shots[-1]["id"]  # 绿色镜头：与查询“红色”文本不相干
    catalogue = Persons(store)
    actor = catalogue.save(Entity(kind="actor", name="演员甲"))
    catalogue.shot_review(
        ShotReview(source_id=source_id, shot_id=evidence_shot, selections=[{"actor_id": actor["id"]}])
    )
    catalogue.publish()

    set_recall(config, jid, w, p, 0, ShotRecall(person_ids=[actor["id"]], person_mode="onscreen"))
    vid = ws.execute(
        config,
        jid,
        ws.load(config, jid),
        ws.plan(ws.load(config, jid), p["id"]),
        "recall",
        "a",
        embedder=embedder,
    )
    data = ws.version(ws.load(config, jid), vid)["data"]
    assert data["mode"] == "text" and data["meta"]["person_mode"] == "onscreen"
    assert data["results"] and {item["segment_id"] for item in data["results"]} == {evidence_shot}
    assert data["meta"]["person_version"]


def test_recall_reference_images_are_added_to_legacy_details_without_rewriting(prepared):
    from mindex.persons import Entity, Persons
    from mindex.storage import dumps

    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    store = Store(config.data_dir)
    catalogue = Persons(store)
    source = store.sources()[0]["id"]
    actor = catalogue.save(Entity(kind="actor", name="参考演员"))
    role = catalogue.save(
        Entity(kind="role", name="参考角色", work_ids=[catalogue.work_for(source)], actor_ids=[actor["id"]])
    )
    reference = catalogue.add_reference(actor["id"], red_image(), role_id=role["id"])
    shot = store.segments(source, "shot")[0]
    store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "reference-evidence",
            source,
            shot["id"],
            actor["id"],
            role["id"],
            None,
            shot["start"],
            shot["end"],
            "accepted",
            dumps({"reference_id": reference["id"], "score": 0.9, "image_path": shot["thumbnail"]}),
        ),
    )
    catalogue.publish()
    set_recall(config, jid, w, p, 0, ShotRecall(person_ids=[actor["id"]], person_mode="onscreen"))
    vid = ws.execute(config, jid, w, p, "recall", "a", embedder=embedder)
    w = ws.load(config, jid)
    evidence = ws.version(w, vid)["data"]["results"][0]["person_evidence"][0]
    assert evidence.pop("reference_url") == reference["url"]
    detail = ws.detail(config, jid, w)
    shown = detail["versions"][vid]["data"]["results"][0]["person_evidence"][0]
    assert shown["reference_url"] == reference["url"]
    assert "reference_url" not in evidence
    assert shown["image_url"] != shown["reference_url"]

def test_workspace_recall_pins_active_person_release_and_reuses_after_rollback(prepared):
    from mindex.persons import Entity, Persons, ShotReview

    config, _embedder = prepared
    jid, w, p = setup_workspace(config)
    store = Store(config.data_dir)
    source = store.sources()[0]["id"]
    shot = store.segments(source, "shot")[0]["id"]
    catalogue = Persons(store)
    actor = catalogue.save(Entity(kind="actor", name="演员甲"))
    catalogue.shot_review(ShotReview(source_id=source, shot_id=shot, selections=[{"actor_id": actor["id"]}]))
    first = catalogue.publish()
    shots = shot_list(w, p)
    shots[0].query = "演员甲"
    ws.set_board(config, jid, w, p, shots)
    ws.persist(config, jid, w)

    class Offline:
        def identity(self):
            pytest.fail("仅人物姓名不应初始化嵌入服务")

    vid = ws.execute(config, jid, w, p, "recall", "a", embedder=Offline())
    w = ws.load(config, jid)
    p = ws.plan(w, p["id"])
    first_signature = ws.version(w, vid)["signature"]
    catalogue.shot_review(ShotReview(source_id=source, shot_id=shot, selections=[]))
    second = catalogue.publish()
    assert ws.specification(config, jid, w, p, "recall", "a")["signature"] != first_signature
    second_vid = ws.execute(config, jid, w, p, "recall", "a", embedder=Offline())
    assert ws.version(ws.load(config, jid), second_vid)["data"]["results"] == []
    assert second["person_version"] != first["person_version"]
    store.activate_release(first["release_id"])
    w = ws.load(config, jid)
    p = ws.plan(w, p["id"])
    assert ws.specification(config, jid, w, p, "recall", "a")["signature"] == first_signature
    restored = ws.execute(config, jid, w, p, "recall", "a", embedder=Offline())
    data = ws.version(ws.load(config, jid), restored)["data"]
    assert data["meta"]["person_version"] == first["person_version"]
    assert [item["segment_id"] for item in data["results"]] == [shot]


def test_concurrent_query_uploads_do_not_block_event_loop(studio_client, monkeypatch):
    import asyncio
    import time

    import httpx

    config, client = studio_client
    jid = create_studio_job(client)
    pid = ws.load(config, jid)["active_plan"]
    from mindex import studio_api

    decode = studio_api.image_png

    def slow_decode(*args):
        time.sleep(0.1)
        return decode(*args)

    monkeypatch.setattr(studio_api, "image_png", slow_decode)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://test"
        ) as http:

            async def upload():
                return await http.post(
                    f"/api/studio/jobs/{jid}/plans/{pid}/query-media",
                    files={"file": ("r.png", red_image())},
                    data={"kind": "image"},
                )

            responses = await asyncio.wait_for(asyncio.gather(upload(), upload(), http.get("/api/status")), 5)
            assert responses[-1].status_code == 200
            assert all(r.status_code in {201, 409} for r in responses[:2])
            assert any(r.status_code == 201 for r in responses[:2])

    asyncio.run(exercise())
