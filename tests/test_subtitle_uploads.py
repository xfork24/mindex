"""真实 FFmpeg 字幕文件上传、任务应用、版本复用与镜头定位。"""

import time

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.adapters import ModelError
from mindex.api import create_app
from mindex.dialogue import current_dialogue, relative_dialogue
from mindex.pipeline import Pipeline
from mindex.processing_lock import ProcessingLock

SRT = "1\n00:00:00,200 --> 00:00:01,400\n<i>你好</i>，牛车水！\n第二行\n\n"
ASS = """[Script Info]
ScriptType: v4.00+
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:01.40,Default,,0,0,0,,{\\i1}你好{\\i0}，牛车水！\\N第二行
"""
VTT = "WEBVTT\n\n00:00.200 --> 00:01.400\n<i>你好</i>，牛车水！\n第二行\n\n"
SMI = "<SAMI><BODY><SYNC Start=200><P>你好，牛车水！<br>第二行<SYNC Start=1400><P>&nbsp;</BODY></SAMI>"
SUB = "{0}{0}25\n{5}{35}你好，牛车水！|第二行\n"


def wait_task(client, task_id):
    for _ in range(300):
        task = client.get(f"/api/admin/dialogue-tasks/{task_id}").json()
        if task["status"] in {"complete", "failed"}:
            return task
        time.sleep(0.01)
    pytest.fail("对白任务未结束")


@pytest.mark.parametrize("name,payload", [
    ("测试.SRT", SRT.encode("utf-8-sig")),
    ("测试.ass", ASS.encode()),
    ("测试.ssa", ASS.encode()),
    ("测试.vtt", VTT.encode()),
    ("测试.smi", SMI.encode()),
    ("测试.sami", SMI.encode()),
    ("测试.sub", SUB.encode()),
    ("GB18030.srt", SRT.encode("gb18030")),
    ("Big5.srt", SRT.replace("车", "車").encode("big5")),
    ("UTF16.srt", SRT.encode("utf-16")),
])
def test_uploaded_subtitle_applies_only_on_start_and_locates_dialogue(
    config, video_file, monkeypatch, name, payload,
):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    monkeypatch.setattr("mindex.dialogue.make_transcriber", lambda *_: pytest.fail("文件字幕不调用 ASR"))
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    endpoint = f"/api/admin/sources/{sid}/subtitles"
    response = client.post(endpoint, files={"file": (name, payload, "text/plain")})
    assert response.status_code == 201, response.text
    uploaded = response.json()
    assert uploaded["name"] == name and uploaded["utterances"] == 1
    assert current_dialogue(pipeline.store, sid) is None
    assert client.get(f"/api/admin/sources/{sid}").json()["subtitle_files"] == [uploaded]
    assert client.post(endpoint, files={"file": (name, payload)}).json() == uploaded
    response = client.post(f"/api/admin/sources/{sid}/dialogue", json={
        "method": "subtitle", "subtitle_file": uploaded["id"], "publish": False,
    })
    assert response.status_code == 202, response.text
    task = wait_task(client, response.json()["id"])
    assert task["status"] == "complete", task
    data = current_dialogue(pipeline.store, sid)
    assert data["source"] == "subtitle" and data["subtitle_file"] == uploaded
    assert not data["fallback_reason"] and data["words"] == []
    row = data["utterances"][0]
    assert row["start"] == pytest.approx(0.2) and row["end"] == pytest.approx(1.4)
    assert "你好" in row["text"] and "第二行" in row["text"] and "<i>" not in row["text"]
    assert relative_dialogue(data, 0.5, 0.8)[0]["start"] == 0
    segment = pipeline.store.segments(sid, "shot")[0]
    details = client.get(f"/api/admin/segments/{segment['id']}/details").json()
    assert details["subtitles"]["tracks"][-1]["title"] == name
    assert details["subtitles"]["tracks"][-1]["utterances"][0]["text"] == row["text"]
    source = client.get(f"/api/admin/sources/{sid}").json()
    assert source["work"]["dialogue"]["result"]["subtitle_file"] == uploaded
    assert pipeline.enrich_dialogue(sid, method="subtitle", subtitle_file=uploaded["id"])["sources"][0]["reused"]


def wait_publication(client, task_id):
    for _ in range(1000):
        task = client.get(f'/api/admin/source-tasks/{task_id}').json()
        if task['status'] in ('complete','failed'):
            return task
        time.sleep(.01)
    pytest.fail('embedding subtask did not finish')


def test_subtitle_upload_errors_do_not_replace_dialogue_and_are_cleaned(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    endpoint = f"/api/admin/sources/{sid}/subtitles"
    for name, payload, status in [
        ("test.srt", b"", 422), ("test.srt", b"broken", 422),
        ("test.exe", SRT.encode(), 422),
        ("late.srt", b"1\n00:01:00,000 --> 00:01:01,000\noutside\n\n", 422),
        ("bitmap.sub", b"\x00\xff\x00\xff", 422),
    ]:
        response = client.post(endpoint, files={"file": (name, payload)})
        assert response.status_code == status, response.text
        assert current_dialogue(pipeline.store, sid) is None
    monkeypatch.setattr("mindex.subtitle_uploads.SUBTITLE_MAX_BYTES", 4)
    assert client.post(endpoint, files={"file": ("big.srt", SRT.encode())}).status_code == 413
    assert not list((config.data_dir / "subtitles" / ".uploads").iterdir())
    assert client.post("/api/admin/sources/missing/subtitles", files={"file": ("test.srt", SRT)}).status_code == 404
    with ProcessingLock(config, source_id=sid, work="dialogue"):
        assert client.post(endpoint, files={"file": ("test.srt", SRT)}).status_code == 409


def test_subtitle_identity_source_binding_and_tamper_protection(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    endpoint = f"/api/admin/sources/{sid}/subtitles"
    first = client.post(endpoint, files={"file": ("a.srt", SRT)}).json()
    options = {"method": "subtitle", "subtitle_file": first["id"], "publish": False}
    other = "another-source"
    row = pipeline.store.one("SELECT * FROM sources WHERE id=?", (sid,))
    pipeline.store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,?)",
        (other, "other.mp4", row["path"], row["media"], row["layout_key"], 1),
    )
    assert client.post(f"/api/admin/sources/{other}/dialogue", json=options).status_code == 422
    assert client.post(f"/api/admin/sources/{sid}/dialogue", json={**options, "method": "asr"}).status_code == 422
    assert client.post(f"/api/admin/sources/{sid}/dialogue", json={**options, "subtitle_stream": 0}).status_code == 422
    assert client.post(f"/api/admin/sources/{sid}/dialogue", json={**options, "subtitle_file": "../outside"}).status_code == 422
    assert wait_task(client, client.post(f"/api/admin/sources/{sid}/dialogue", json=options).json()["id"])["status"] == "complete"
    previous = current_dialogue(pipeline.store, sid)["content_version"]
    second = client.post(endpoint, files={"file": ("b.srt", SRT.replace("你好", "再见"))}).json()
    assert current_dialogue(pipeline.store, sid)["content_version"] == previous
    options["subtitle_file"] = second["id"]
    assert wait_task(client, client.post(f"/api/admin/sources/{sid}/dialogue", json=options).json()["id"])["status"] == "complete"
    assert current_dialogue(pipeline.store, sid)["content_version"] != previous
    file = config.data_dir / "subtitles" / sid / "uploads" / second["id"] / "subtitle.srt"
    file.write_text(SRT)
    response = client.post(f"/api/admin/sources/{sid}/dialogue", json=options)
    assert response.status_code == 422 and "内容已变化" in response.text
    assert "再见" in current_dialogue(pipeline.store, sid)["utterances"][0]["text"]
    repaired = client.post(endpoint, files={"file": ("b.srt", SRT.replace("你好", "再见"))}).json()
    assert repaired == second
    assert pipeline.enrich_dialogue(sid, method="subtitle", subtitle_file=second["id"])["sources"][0]["reused"]


def test_subtitle_publish_retry_reuses_saved_dialogue(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)

    class RetryEmbedder(FixtureEmbedder):
        fail = True

        def text(self, texts, role="document"):
            if self.fail:
                raise ModelError("工程嵌入失败")
            return super().text(texts, role)

    embedder = RetryEmbedder(config)
    client = TestClient(create_app(config, embedder))
    uploaded = client.post(f"/api/admin/sources/{sid}/subtitles", files={"file": ("test.srt", SRT)}).json()
    options = {"method": "subtitle", "subtitle_file": uploaded["id"], "publish": True}
    task_id = client.post(f"/api/admin/sources/{sid}/dialogue", json=options).json()["id"]
    task = wait_task(client, task_id)
    assert task["status"] == "complete" and task["publication"]["status"] == "queued"
    child_id = task["publication"]["task_ids"][0]
    assert wait_publication(client, child_id)["status"] == "failed"
    version = current_dialogue(pipeline.store, sid)["content_version"]
    embedder.fail = False
    monkeypatch.setattr("mindex.subtitle_uploads.parse_subtitle_file", lambda *_args, **_kw: pytest.fail("发布重试不解析字幕"))
    assert client.post(f"/api/admin/source-tasks/{child_id}/retry").status_code == 202
    child = wait_publication(client, child_id)
    assert child["status"] == "complete", child
    assert wait_task(client, task_id)["status"] == "complete"
    assert current_dialogue(pipeline.store, sid)["content_version"] == version
    snapshot = next(s for s in pipeline.store.snapshots(pipeline.store.active_index()["id"])
                    if s["views"].get("dialogue"))
    assert "你好" in snapshot["views"]["dialogue"]
    assert snapshot["channel_versions"]["dialogue"] == version


def test_frame_based_subtitle_uses_source_fps_when_file_omits_it(tmp_path):
    from mindex.subtitle_uploads import parse_subtitle_file

    path = tmp_path / "frames.sub"
    path.write_text("{6}{42}按帧字幕\n", encoding="utf-8")
    rows = parse_subtitle_file(path, ".sub", 2, fps=30)
    assert rows[0]["start"] == pytest.approx(0.2) and rows[0]["end"] == pytest.approx(1.4)


def test_repeat_completed_dialogue_task_reuses_parsing_and_embeddings(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    embedder = FixtureEmbedder(config)
    client = TestClient(create_app(config, embedder))
    uploaded = client.post(f'/api/admin/sources/{sid}/subtitles', files={'file': ('repeat.srt', SRT)}).json()
    options = {'method': 'subtitle', 'subtitle_file': uploaded['id'], 'publish': True}
    first = wait_task(client, client.post(f'/api/admin/sources/{sid}/dialogue', json=options).json()['id'])
    assert first['status'] == 'complete' and first['publication']['status'] == 'queued'
    assert wait_publication(client, first['publication']['task_ids'][0])['status'] == 'complete'
    version = current_dialogue(pipeline.store, sid)['content_version']

    def unexpected(*args, **kwargs):
        pytest.fail('unchanged completed dialogue must reuse parsing and embeddings')

    monkeypatch.setattr('mindex.subtitle_uploads.parse_subtitle_file', unexpected)
    for method in ('text', 'video', 'image'):
        monkeypatch.setattr(embedder, method, unexpected)
    second = wait_task(client, client.post(f'/api/admin/sources/{sid}/dialogue', json=options).json()['id'])
    assert second['id'] != first['id']
    assert second['status'] == 'complete' and second['publication']['status'] == 'queued'
    assert wait_publication(client, second['publication']['task_ids'][0])['status'] == 'complete'
    assert current_dialogue(pipeline.store, sid)['content_version'] == version
