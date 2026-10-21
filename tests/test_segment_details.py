from __future__ import annotations

from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.processing.pipeline import Pipeline
from mindex.shared.storage import dumps
from mindex.shared.types import Annotation


def test_detail_independent_asr_subtitle_and_llm_with_time_clipping(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    store = pipeline.store
    shot = store.segments(source, "shot")[1]
    assert shot["start"] == 1 and shot["end"] == 2
    store.put_json(
        "transcripts",
        "source_id",
        source,
        "asr1",
        {
            "status": "complete",
            "utterances": [
                {"start": 0, "end": 1, "text": "前镜头"},
                {"start": 0.8, "end": 1.3, "text": "ASR 边界"},
                {"start": 1.4, "end": 1.8, "text": "ASR 本镜头"},
                {"start": 2, "end": 3, "text": "后镜头"},
            ],
        },
    )
    # Current dialogue is subtitle-first; it must not erase stored ASR in details.
    store.execute(
        "INSERT INTO channel_data(source_id,channel,profile,status,data) VALUES(?,'dialogue','sub','complete',?)",
        (
            source,
            dumps(
                {
                    "source": "subtitle",
                    "status": "complete",
                    "utterances": [{"start": 1, "end": 2, "text": "当前优先字幕"}],
                }
            ),
        ),
    )
    store.put_json(
        "annotations",
        "segment_id",
        shot["id"],
        "llm1",
        {
            "annotation": Annotation(
                description="两人交谈", actions=["走路"], visual_evidence=["近景"], uncertainty=["身份不确定"]
            ).model_dump()
        },
    )
    from mindex.processing.subtitle_archive import archive_path
    archive_path(store, source).write_text(dumps({
        "version": 1, "files": {},
        "tracks": [
            {"index": 2, "codec": "subrip", "text": True, "language": "zho", "title": "中文"},
            {"index": 3, "codec": "hdmv_pgs_subtitle", "text": False, "language": "eng", "title": "English"},
        ],
        "utterances": {"2": [
            {"start": 0, "end": 1, "text": "字幕前镜头"},
            {"start": 1.2, "end": 2.2, "text": "字幕本镜头"},
        ]},
    }))
    client = TestClient(create_app(config))
    endpoint = f"/api/admin/segments/{shot['id']}/details"
    data = client.get(endpoint).json()
    assert data["asr"]["status"] == "complete"
    assert [u["text"] for u in data["asr"]["utterances"]] == ["ASR 边界", "ASR 本镜头"]
    assert data["asr"]["utterances"][0]["evidence_start"] == 1
    assert data["subtitles"]["tracks"][0]["utterances"][0]["text"] == "字幕本镜头"
    assert data["subtitles"]["tracks"][0]["utterances"][0]["evidence_end"] == 2
    assert data["subtitles"]["tracks"][1]["message"] == "位图字幕，无法提取文本"
    assert data["llm"]["annotation"]["actions"] == ["走路"]
    assert client.get(endpoint).status_code == 200
    assert store.active_index() is None
    assert client.get("/api/admin/sources/" + source + "/media").status_code == 200


def test_detail_missing_outputs_no_models_and_button_order(config, video_file):
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    shot = pipeline.store.segments(source, "shot")[0]
    client = TestClient(create_app(config))
    data = client.get(f"/api/admin/segments/{shot['id']}/details").json()
    assert data["asr"]["message"] == "ASR 未处理"
    assert data["subtitles"]["message"] == "无字幕轨道"
    assert data["llm"]["message"] == "LLM 未处理"
    assert client.get("/api/admin/segments/missing/details").status_code == 404
    script = client.get("/assets/admin/admin.js").text
    assert (
        script.index('action(MindexI18n.msg("ui.4f55ee1e687f")')
        < script.index('action(MindexI18n.msg("ui.e0361480e3a5")')
        < script.index("action(item.favorite")
        < script.index("action(item.hidden")
    )
    assert "名称 / 备注" not in script
    page = client.get("/admin").text
    assert "analysis-dialog" in page


def test_missing_source_media_preserves_stored_llm_and_asr(config, video_file):
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    shot = pipeline.store.segments(source, "shot")[0]
    pipeline.store.put_json(
        "transcripts",
        "source_id",
        source,
        "asr",
        {"status": "complete", "utterances": [{"start": 0, "end": 0.5, "text": "已保存对白"}]},
    )
    pipeline.store.put_json(
        "annotations",
        "segment_id",
        shot["id"],
        "llm",
        {"annotation": Annotation(description="已有结果").model_dump()},
    )
    row = pipeline.store.one("SELECT * FROM sources WHERE id=?", (source,))
    pipeline.store.resolve_media(row["path"]).unlink()
    response = TestClient(create_app(config)).get(f"/api/admin/segments/{shot['id']}/details")
    assert response.status_code == 200
    data = response.json()
    assert data["asr"]["utterances"][0]["text"] == "已保存对白"
    assert data["llm"]["annotation"]["description"] == "已有结果"
    assert data["subtitles"]["status"] == "no_tracks"


def test_person_details_merge_clip_coverage_count_evidence_without_publication(config, video_file):
    from mindex.persons.persons import Entity, Persons

    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    store = pipeline.store
    shot = store.segments(source, "shot")[1]
    client = TestClient(create_app(config))
    endpoint = f"/api/admin/segments/{shot['id']}/details"
    person = client.get(endpoint).json()["person"]
    assert person["status"] == "not_processed"
    assert person["uncovered"] == [{"start": 1, "end": 2}]
    for key, start, end in [("a", 0, 1.2), ("b", 1.1, 1.4), ("c", 1.6, 2.5), ("d", 3, 4)]:
        store.execute(
            "INSERT INTO person_coverage VALUES(?,?,?)", (source, key, dumps({"start": start, "end": end}))
        )
    actor = Persons(store).save(Entity(kind="actor", name="测试演员"))
    bystander = Persons(store).save(Entity(kind="actor", name="低分候选演员"))
    for key, start, end, status, who in [
        ("accepted", 0.8, 1.2, "accepted", actor["id"]),
        ("candidate", 1.5, 1.7, "candidate", actor["id"]),
        ("rejected", 1.8, 2.2, "rejected", actor["id"]),
        ("before", 0, 1, "accepted", actor["id"]),
        ("after", 2, 3, "candidate", actor["id"]),
        ("bystander", 1.5, 1.7, "candidate", bystander["id"]),
    ]:
        store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                source,
                shot["id"],
                who,
                None,
                None,
                start,
                end,
                status,
                "{}",
            ),
        )
    person = client.get(endpoint).json()["person"]
    assert person["status"] == "partial"
    assert person["covered"] == [{"start": 1, "end": 1.4}, {"start": 1.6, "end": 2}]
    assert person["uncovered"] == [{"start": 1.4, "end": 1.6}]
    # 计数按分镜内去重人物：测试演员已有 accepted 行，不再计入待确认；只有低分候选的
    # bystander 计入待确认；已接受的证据才进入关联人物（与发布快照口径一致）。
    assert person["counts"] == {"accepted": 1, "candidate": 1, "rejected": 0}
    assert [p["name"] for p in person["people"]] == ["测试演员"]
    store.execute(
        "INSERT INTO person_coverage VALUES(?,?,?)", (source, "bridge", dumps({"start": 1.4, "end": 1.6}))
    )
    person = client.get(endpoint).json()["person"]
    assert person["status"] == "processed" and person["covered"] == [{"start": 1, "end": 2}]
    assert person["uncovered"] == [] and store.active_index() is None
