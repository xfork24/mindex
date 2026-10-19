from __future__ import annotations

import copy
import io
import wave

import numpy as np
import pytest
from conftest import FixtureSpeaker
from fastapi.testclient import TestClient

from mindex import studio
from mindex import studio_window as window
from mindex import studio_workspace as ws
from mindex.api import create_app
from mindex.media import command, probe
from mindex.pipeline import Pipeline
from mindex.storage import Store


def rows(count=10):
    return [{"id": str(i), "start": float(i), "end": float(i + 1), "visible": True} for i in range(count)]


@pytest.mark.parametrize(
    "start,end,length,expected,extra",
    [
        (2, 3, 1, list(range(5)), (0, 0)),
        (2.2, 4.7, 1, list(range(7)), (0, 0)),
        (2, 3, 8, list(range(8)), (0, 3)),
        (8, 9, 7, list(range(3, 10)), (3, 0)),
        (0, 1, 1, list(range(3)), (0, 0)),
        (9, 10, 1, [7, 8, 9], (0, 0)),
    ],
)
def test_default_neighborhood_anchor_group_and_minimal_extension(start, end, length, expected, extra):
    result, _, extension = window.neighborhood(rows(), {"start": start, "end": end}, length)
    assert [int(r["id"]) for r in result] == expected
    assert (extension["before"], extension["after"]) == extra


def test_hidden_shots_and_gaps_stop_extension():
    timeline = rows()
    timeline[6]["visible"] = False
    timeline[0]["end"] = 0.9
    result, _, extension = window.neighborhood(timeline, {"start": 2, "end": 3}, 9)
    assert [r["id"] for r in result] == [str(i) for i in range(1, 6)]
    assert extension == {"before": 0, "after": 1}
    timeline[2]["visible"] = False
    with pytest.raises(studio.StudioError, match="隐藏"):
        window.neighborhood(timeline, {"start": 2, "end": 3}, 1)
    timeline = rows()
    timeline[3]["start"] += 0.1
    with pytest.raises(studio.StudioError, match="不连续"):
        window.neighborhood(timeline, {"start": 2.2, "end": 4.7}, 1)


@pytest.fixture
def refinement(config, video_file, tmp_path):
    # Exercise a real rational source frame rate as well as a noninteger render frame rate.
    source_file = tmp_path / "rational.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(video_file),
            "-r",
            "30000/1001",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source_file),
        ]
    )
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.fps = 29.97
    config.studio.render.preset = "ultrafast"
    Pipeline(config).prepare(source_file)
    store = Store(config.data_dir)
    source = store.one("SELECT * FROM sources")
    template = store.one("SELECT * FROM segments WHERE kind='shot'")
    with store.connect() as db:
        db.execute("DELETE FROM segments")
        for i in range(10):
            from mindex.media import cut
            clip = store.root / f"fixture-shot-{i}.mp4"
            cut(source_file, i, i + 1, clip)
            row = {**template, "id": f"shot-{i}", "start": i, "end": i + 1, "parent_id": None,
                   "path": clip.name}
            db.execute(
                f"INSERT INTO segments ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    jid = studio.create_job(config, "红色开场。蓝色结尾。", options={"burn": False})["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        jid,
        w,
        p,
        [studio.Shot(id=sid, text="旁白", query="红色", estimated_seconds=1) for sid in ("a", "b")],
    )
    for sid in ("a", "b"):
        pool = [
            {
                "id": f"shot-{i}",
                "segment_id": f"shot-{i}",
                "source_id": source["id"],
                "source_name": source["name"],
                "start": i,
                "end": i + 1,
                "rank": rank,
                "score": 1 / rank,
            }
            for rank, i in enumerate((0, 1, 2, 8), 1)
        ]
        rid = ws.add_version(
            config,
            jid,
            w,
            "recall",
            sid,
            data={"results": pool},
            **ws.specification(config, jid, w, p, "recall", sid),
        )
        ws.choose(w, p, "recall", sid, rid)
    ws.persist(config, jid, w)
    return config, jid, w, p, store


def manual(w, p, sid="a", candidate="shot-1"):
    window.set_anchor(
        w, p, sid, {"mode": "manual", "recall_version": p["shots"][sid]["recall"], "candidate_id": candidate}
    )


def speech(config, jid, w, p, sid="a"):
    return ws.execute(config, jid, w, p, "tts", sid, speaker=FixtureSpeaker(config))


def test_nonfirst_anchor_no_voice_isolation_and_recall_conflict(refinement):
    config, jid, w, p, _ = refinement
    other = copy.deepcopy(p)
    other["id"] = "other"
    w["plans"]["other"] = other
    manual(w, p)
    ctx = window.context(config, jid, w, p, "a")
    assert not ctx["ready"] and ctx["tts_version"] is None
    assert ctx["anchor"]["segment_id"] == "shot-1"
    assert other["anchors"] == {} and "b" not in p["anchors"]
    speech(config, jid, w, p)
    selected = ws.execute(config, jid, w, p, "select", "a")
    old = copy.deepcopy(p["shots"]["a"])
    assert ws.version(w, selected)["data"]["anchor"]["segment_id"] == "shot-1"
    manual(w, p, candidate="shot-2")
    assert p["shots"]["a"] == old
    assert ws.conflicts(config, jid, w, p, "select", "a", selected)
    new_recall = ws.add_version(config, jid, w, "recall", "a", data={"results": []})
    ws.choose(w, p, "recall", "a", new_recall)
    error = window.anchor_conflict(w, p, "a")
    assert new_recall in error and p["anchors"]["a"]["recall_version"] in error
    with pytest.raises(studio.StudioError, match="召回版本"):
        ws.alignment(config, jid, w, p, "a")
    window.set_anchor(w, p, "a", {"mode": "auto"})
    assert not window.anchor_conflict(w, p, "a")


def test_automatic_mode_uses_existing_scoring(refinement):
    config, jid, w, p, _ = refinement
    speech(config, jid, w, p)
    pool = ws.version(w, p["shots"]["a"]["recall"])["data"]["results"]
    pool[0]["rank"] = 50
    pool[1]["rank"] = 1
    ctx = window.context(config, jid, w, p, "a")
    assert ctx["anchor_ref"]["candidate_id"] == "shot-1"
    selected, _ = window.selected_range(
        config,
        jid,
        w,
        p,
        "a",
        ctx["context_token"],
        ctx["initial_start"],
        ref=ctx["anchor_ref"],
        tts_version=ctx["tts_version"],
    )
    p["ranges"]["a"] = selected
    vid = ws.execute(config, jid, w, p, "select", "a")
    assert ws.match(config, jid, w, p, "select", "a") == vid


@pytest.mark.parametrize("speed,extra_samples", [(None, 0), (1.2, 0), (None, 10), (0.999, 0)])
def test_window_preview_confirm_exact_bounds_real_media_and_history(refinement, speed, extra_samples):
    config, jid, w, p, _ = refinement
    p["options"]["atempo"] = speed
    manual(w, p)
    if extra_samples:

        class SamplePrecisionSpeaker:
            def speak(self, text, voice=None):
                payload = FixtureSpeaker(config).speak(text, voice)
                with wave.open(io.BytesIO(payload), "rb") as handle:
                    params = handle.getparams()
                    frames = handle.readframes(handle.getnframes())
                output = io.BytesIO()
                with wave.open(output, "wb") as handle:
                    handle.setparams(params)
                    handle.writeframes(frames + frames[-params.sampwidth :] * extra_samples)
                return output.getvalue()

        ws.execute(config, jid, w, p, "tts", "a", speaker=SamplePrecisionSpeaker())
    else:
        speech(config, jid, w, p)
    old_select = ws.execute(config, jid, w, p, "select", "a")
    ws.persist(config, jid, w)
    client = TestClient(create_app(config))
    base = f"/api/studio/jobs/{jid}"
    url = f"{base}/plans/{p['id']}"
    response = client.get(f"{url}/shots/a/selection-context")
    assert response.status_code == 200, response.text
    ctx = response.json()
    assert ctx["ready"] and ctx["fps"] == pytest.approx(30000 / 1001)
    assert ctx["window_duration"] == ws.version(w, p["shots"]["a"]["tts"])["data"]["duration"] / (speed or 1)
    if extra_samples or speed == 0.999:
        assert round(ctx["window_duration"], 3) > ctx["window_duration"] + 1e-6
    assert client.get(ctx["video_url"]).status_code == 200
    assert client.get(ctx["thumbnails_url"]).headers["content-type"] == "image/jpeg"
    assert client.get(f"{url}/shots/a/selection-context").json()["context_token"] == ctx["context_token"]
    # A window may move completely away from its reference candidate.
    body = {
        "revision": ctx["revision"],
        "shot_id": "a",
        "context_token": ctx["context_token"],
        "anchor_ref": ctx["anchor_ref"],
        "tts_version": ctx["tts_version"],
        "start": 2.5,
    }
    assert client.post(f"{url}/range-confirm", json=body).status_code == 422
    invalid = {**body, "start": ctx["legal_start"]["max"] + 0.1}
    assert client.post(f"{url}/range-preview", json=invalid).status_code == 422
    assert client.post(f"{url}/range-preview", json={**body, "end": 999}).status_code == 422
    invalid = {**body, "anchor_ref": {**ctx["anchor_ref"], "candidate_id": "shot-0"}}
    assert client.post(f"{url}/range-preview", json=invalid).status_code == 422
    preview = client.post(f"{url}/range-preview", json=body)
    assert preview.status_code == 200, preview.text
    preview = preview.json()
    event = studio.TimelineEvent.model_validate(preview["event"])
    assert event.subtitle.end == event.audio.render_duration == ctx["window_duration"]
    assert preview["freeze_seconds"] == preview["trim_seconds"] == 0
    assert preview["range"]["end"] - preview["range"]["start"] == pytest.approx(ctx["window_duration"])
    assert client.get(f"{base}/workspace").json()["plans"][p["id"]]["shots"]["a"]["select"] == old_select
    body["preview_token"] = preview["preview_token"]
    confirmed = client.post(f"{url}/range-confirm", json=body)
    assert confirmed.status_code == 200, confirmed.text
    fresh = ws.load(config, jid)
    current = fresh["plans"][p["id"]]
    item = ws.version(fresh, current["shots"]["a"]["select"])
    assert item["data"]["anchor"]["segment_id"] == "shot-1"
    assert item["data"]["event"]["video"]["segment_id"] == "shot-1"
    assert item["data"]["event"]["video"]["segment_ids"] == (
        ["shot-2", "shot-3"] if ctx["window_duration"] > 0.5 else ["shot-2"]
    )
    assert item["data"]["anchor_ref"] == ctx["anchor_ref"]
    assert item["inputs"]["recall"] == ctx["anchor_ref"]["recall_version"]
    rendered = ws.execute(config, jid, fresh, current, "render", "a")
    rendered_item = ws.version(fresh, rendered)
    clip = ws.file_path(config, jid, rendered_item, "preview")
    assert abs(rendered_item["data"]["samples"] / 48000 - ctx["window_duration"]) < 1 / 29.97
    command(["ffmpeg", "-v", "error", "-i", str(clip), "-f", "null", "-"])
    assert abs(probe(clip)["duration"] - ctx["window_duration"]) < 1 / 29.97
    pixel = command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    assert (
        pixel[1] > pixel[0] + 50 and pixel[1] > pixel[2] + 50
    )  # Green at the moved window, not blue anchor.
    raw = command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(clip),
            "-t",
            "0.4",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ]
    )
    samples = np.frombuffer(raw, dtype="<i2")
    peak = np.fft.rfftfreq(len(samples), 1 / 48000)[np.abs(np.fft.rfft(samples)).argmax()]
    assert abs(peak - 440) < 10  # atempo changes duration while preserving the original pitch.
    # The historical selection continues to produce a preview/download after a new window is confirmed.
    historical = client.get(f"{base}/versions/{old_select}/preview")
    assert historical.status_code == 200, historical.text
    download = client.get(historical.json()["url"] + "?download=true")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    old_pointers = copy.deepcopy(current["shots"]["a"])
    manual(fresh, current, candidate="shot-2")
    assert "a" not in current["ranges"]
    assert current["shots"]["a"] == old_pointers


def test_stale_voice_speed_visibility_and_insufficient_context(refinement):
    config, jid, w, p, store = refinement
    manual(w, p)
    speech(config, jid, w, p)
    ctx = window.context(config, jid, w, p, "a")

    def select(old):
        return window.selected_range(
            config,
            jid,
            w,
            p,
            "a",
            old["context_token"],
            old["initial_start"],
            ref=old["anchor_ref"],
            tts_version=old["tts_version"],
        )

    speech(config, jid, w, p)
    with pytest.raises(studio.StudioError, match="已变化"):
        select(ctx)
    ctx = window.context(config, jid, w, p, "a")
    p["options"]["atempo"] = 1.2
    changed = window.context(config, jid, w, p, "a")
    assert changed["window_duration"] == pytest.approx(ctx["window_duration"] / 1.2)
    with pytest.raises(studio.StudioError, match="已变化"):
        select(ctx)
    with store.connect() as db:
        db.execute("INSERT INTO segment_curation(id,hidden) VALUES ('shot-2',1)")
    assert window.context(config, jid, w, p, "a")["context_range"]["end"] == 2
    with pytest.raises(studio.StudioError, match="已变化"):
        select(changed)
    ws.version(w, p["shots"]["a"]["tts"])["data"]["duration"] = 20
    too_short = window.context(config, jid, w, p, "a")
    assert not too_short["ready"] and "不足" in too_short["reason"]
    with pytest.raises(studio.StudioError, match="不足"):
        select(too_short)


def test_legacy_manual_requires_explicit_candidate_and_anchor_api_revision(refinement):
    config, jid, w, p, store = refinement
    speech(config, jid, w, p)
    source_id = store.one("SELECT id FROM sources")["id"]
    p["ranges"]["a"] = {"source_id": source_id, "start": 1, "end": 3}
    ws.execute(config, jid, w, p, "select", "a")
    with pytest.raises(studio.StudioError, match="历史人工"):
        window.context(config, jid, w, p, "a")
    client = TestClient(create_app(config))
    url = f"/api/studio/jobs/{jid}/plans/{p['id']}"
    choice = {
        "revision": ws.revision(p),
        "shot_id": "a",
        "mode": "manual",
        "recall_version": p["shots"]["a"]["recall"],
        "candidate_id": "shot-1",
    }
    assert client.post(f"{url}/anchor", json={**choice, "shot_id": "b"}).status_code == 422
    result = client.post(f"{url}/anchor", json=choice)
    assert result.status_code == 200, result.text
    updated = result.json()["plans"][p["id"]]
    assert "a" not in updated["ranges"]
    assert updated["shots"]["a"]["select"] == p["shots"]["a"]["select"]
    assert client.post(f"{url}/anchor", json=choice).status_code == 422
    assert client.get(f"{url}/shots/a/selection-context").json()["ready"]


def test_save_without_transcoding_and_cached_low_resolution_preview(refinement, monkeypatch):
    from mindex import media, studio_render

    config, jid, w, p, _ = refinement
    config.studio.render.width = 1920
    config.studio.render.height = 1080
    config.studio.render.preset = "slow"
    manual(w, p, candidate="shot-8")
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    client = TestClient(create_app(config))
    url = f"/api/studio/jobs/{jid}/plans/{p['id']}"
    ctx = client.get(f"{url}/shots/a/selection-context").json()
    assert ctx["context_range"]["start"] == 6
    body = {
        "revision": ctx["revision"],
        "shot_id": "a",
        "context_token": ctx["context_token"],
        "anchor_ref": ctx["anchor_ref"],
        "tts_version": ctx["tts_version"],
        "start": 7.25,
    }

    media_calls, cuts, contexts = [], [], []
    atomic, cut = media.atomic_media, studio_render.cut_shot
    original_context = window.context

    def record_context(*args, **kwargs):
        contexts.append(True)
        return original_context(*args, **kwargs)

    def record_media(args, output, **kwargs):
        media_calls.append(output)
        return atomic(args, output, **kwargs)

    def record_cut(source, event, render, output, **kwargs):
        cuts.append((source, event.model_dump(), render.model_dump()))
        return cut(source, event, render, output, **kwargs)

    monkeypatch.setattr(media, "atomic_media", record_media)
    monkeypatch.setattr(studio_render, "cut_shot", record_cut)
    monkeypatch.setattr(window, "context", record_context)
    validated = client.post(f"{url}/range-preview", json={**body, "render_preview": False})
    assert validated.status_code == 200, validated.text
    assert "preview_url" not in validated.json()
    assert len(contexts) == 1
    confirmed = client.post(
        f"{url}/range-confirm", json={**body, "preview_token": validated.json()["preview_token"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert not media_calls and not cuts  # Saving performs no ffmpeg media work.
    assert len(contexts) == 3  # Confirm rechecks dependencies, without redundant probes within an alignment.

    def fresh_body():
        context = client.get(f"{url}/shots/a/selection-context").json()
        return {**body, "revision": context["revision"], "context_token": context["context_token"]}

    body = fresh_body()
    preview = client.post(f"{url}/range-preview", json=body)
    assert preview.status_code == 200, preview.text
    source, event, render = cuts[0]
    assert source.name == "context.mp4" and source.parent.name.startswith("context-")
    assert event["video"]["start"] == pytest.approx(1.25)  # Global 7.25 minus cached context origin 6.
    assert render["width"] == 640 and render["height"] == 360 and render["preset"] == "ultrafast"
    assert config.studio.render.width == 1920 and config.studio.render.preset == "slow"
    count = len(media_calls)
    confirmed = client.post(
        f"{url}/range-confirm", json={**body, "preview_token": preview.json()["preview_token"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert len(media_calls) == count
    replay = client.post(f"{url}/range-preview", json=fresh_body())
    assert replay.status_code == 200, replay.text
    assert replay.json()["preview_token"] != preview.json()["preview_token"]
    assert replay.json()["preview_url"] == preview.json()["preview_url"]
    assert len(cuts) == 1 and len(media_calls) == count


def test_range_save_overlays_calls_bounded(refinement, monkeypatch):
    """保存定长窗口的可见性检查必须是常数级，不随源内镜头数线性放大。"""
    from mindex import curation, media

    config, jid, w, p, store = refinement
    template = store.one("SELECT * FROM segments WHERE kind='shot'")
    with store.connect() as db:
        # 模拟真实主源的镜头规模；视频仅 10 秒，多出的行会被 context 过滤但逐行参与可见性检查。
        for i in range(10, 200):
            row = {**template, "id": f"shot-{i}", "start": i, "end": i + 1, "parent_id": None}
            db.execute(
                f"INSERT INTO segments ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
    manual(w, p)
    speech(config, jid, w, p)
    ws.persist(config, jid, w)

    ctx = window.context(config, jid, w, p, "a")  # 预热 probe 缓存后再开始计数。
    body = {
        "revision": ctx["revision"],
        "shot_id": "a",
        "context_token": ctx["context_token"],
        "anchor_ref": ctx["anchor_ref"],
        "tts_version": ctx["tts_version"],
        "start": 2.5,
    }
    calls = {"overlays": 0, "ffprobe": 0}
    original_overlays, original_command = curation.Library.overlays, media.command

    def count_overlays(self):
        calls["overlays"] += 1
        return original_overlays(self)

    def count_command(args, **kwargs):
        if args[0] == "ffprobe":
            calls["ffprobe"] += 1
        return original_command(args, **kwargs)

    monkeypatch.setattr(curation.Library, "overlays", count_overlays)
    monkeypatch.setattr(media, "command", count_command)
    client = TestClient(create_app(config))
    url = f"/api/studio/jobs/{jid}/plans/{p['id']}"
    validated = client.post(f"{url}/range-preview", json={**body, "render_preview": False})
    assert validated.status_code == 200, validated.text
    confirmed = client.post(
        f"{url}/range-confirm", json={**body, "preview_token": validated.json()["preview_token"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    # 修复前 ≈ 2400+（三遍 context × 每遍 2×200 次逐行全表扫描）；修复后 ≈ 18。
    assert calls["overlays"] <= 40
    assert calls["ffprobe"] == 0  # probe 缓存命中，保存不再重复探测同一原片。

    source_id = store.one("SELECT id FROM sources")["id"]
    rows, original_rows = {"n": 0}, Store.rows

    def count_rows(self, sql, args=()):
        rows["n"] += 1
        return original_rows(self, sql, args)

    monkeypatch.setattr(Store, "rows", count_rows)
    assert len(ws.source_shots(config, source_id)) == 200
    # 可见性必须批量查一次，而不是逐镜头一次区间查询（overlays 2 + 隐藏区间 1 + 镜头 1）。
    assert rows["n"] <= 4


def test_source_shots_visibility_matches_predicate(refinement):
    """批量可见性必须与逐行 interval_visible 逐点等价，含 parent 隐藏传播与脏 parent_id。"""
    from mindex import studio_steps

    config, _jid, _w, _p, store = refinement
    source_id = store.one("SELECT id FROM sources")["id"]
    store.execute("UPDATE segments SET parent_id='shot-6' WHERE id IN ('shot-7','shot-9')")
    store.execute("UPDATE segments SET parent_id='missing-parent' WHERE id='shot-8'")
    for seg_id in ("shot-2", "shot-5", "shot-6"):
        store.execute("INSERT INTO segment_curation(id,hidden) VALUES(?,1)", (seg_id,))
    fresh = Store(config.data_dir)

    rows = ws.source_shots(config, source_id)
    assert [r["visible"] for r in rows] == [
        studio_steps.interval_visible(fresh, source_id, r["start"], r["end"]) for r in rows
    ]
    # 自身隐藏、parent 隐藏传播到子镜头；parent 不存在或为 None 不影响可见性。
    assert {r["id"]: r["visible"] for r in rows} == {
        "shot-0": True,
        "shot-1": True,
        "shot-2": False,
        "shot-3": True,
        "shot-4": True,
        "shot-5": False,
        "shot-6": False,
        "shot-7": False,
        "shot-8": True,
        "shot-9": False,
    }
    # 整个源被隐藏时全部不可见。
    store.execute("INSERT INTO source_curation(id,hidden) VALUES(?,1)", (source_id,))
    assert not any(r["visible"] for r in ws.source_shots(config, source_id))
