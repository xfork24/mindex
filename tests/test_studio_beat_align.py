"""节拍对齐（spec m3-post-production 需求 17）：切点推迟到容差内最近的节拍、与节拍距离 ≤ 1 帧、配音不被截断、
关闭时签名不变；有重叠转场或出场转场的切点不推迟。"""

from __future__ import annotations

from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from api import create_app
from studio import beats
from studio import workspace as ws
from studio.workspace import beat_align, post, signatures
from test_studio_ai_media import FakeImageGenerator, media, run, shot  # noqa: F401 — media 是夹具
from test_studio_beats import metronome
from test_studio_sources import frame_count, setup

FPS = Fraction(25)
TOL = 0.48  # 接近半个节拍周期（120 BPM）：每个切点的容差内都有节拍


def test_delays_unit():
    segments = [{"cue": "a", "shots": ["a", "b", "c"]}]
    # 节拍 0.12 s 起每 0.5 s 一拍；切点在 1.0 s 与 2.0 s（各 25 帧）。
    analyses = {"a": ([0.12 + 0.5 * k for k in range(4)], 2.0)}
    found = beat_align.delays([25, 25, 25], [10, 10, 0], ["a", "b", "c"], segments, analyses, FPS, 0.3, set())
    assert found[0] == 3  # 1.0 → 1.12
    # 第二个切点在推迟后的 2.12 s：下一拍是第二轮循环的 2.12 s，已对齐。
    assert found[1] == 0 and found[2] == 0
    # 容差不够、没有余量、跨段落、有重叠转场时都不推迟。
    assert (
        beat_align.delays([25, 25, 25], [10, 10, 0], ["a", "b", "c"], segments, analyses, FPS, 0.1, set())[0]
        == 0
    )
    assert (
        beat_align.delays([25, 25, 25], [0, 10, 0], ["a", "b", "c"], segments, analyses, FPS, 0.3, set())[0]
        == 0
    )
    assert (
        beat_align.delays(
            [25, 25, 25],
            [10, 10, 0],
            ["a", "b", "c"],
            [{"cue": "b", "shots": ["b", "c"]}],
            {"b": analyses["a"]},
            FPS,
            0.3,
            set(),
        )[0]
        == 0
    )
    assert (
        beat_align.delays([25, 25, 25], [10, 10, 0], ["a", "b", "c"], segments, analyses, FPS, 0.3, {"a"})[0]
        == 0
    )
    # 余量不足时最多推迟余量帧数。
    assert (
        beat_align.delays([25, 25, 25], [2, 10, 0], ["a", "b", "c"], segments, analyses, FPS, 0.3, set())[0]
        == 2
    )


def build_plan(config, client, *, tolerance=None, transition=None):
    track = client.post(
        "/api/studio/music-library",
        data={"source": "自制", "license": "CC0", "attribution": "节拍"},
        files={"file": ("beat.wav", metronome(120, 6, offset=0.25), "audio/wav")},
    ).json()
    second = {"source": {"kind": "aigc_image"}}
    if transition:
        second["transition"] = transition
    shots = [
        shot(
            "a",
            "红色开场。",
            source={"kind": "aigc_image"},
            music={"kind": "library", "track_id": track["id"], "volume": 0.3},
        ),
        shot("b", "蓝色中段。", **second),
        shot("c", "绿色结尾。", source={"kind": "aigc_image"}),
    ]
    job_id, w, p = setup(config, shots)
    if tolerance is not None:
        p["options"]["beat_align"] = {"tolerance": tolerance}
        ws.persist(config, job_id, w)
    return job_id, w, p


def test_beat_align_moves_cuts_onto_beats(media):  # noqa: F811
    with TestClient(create_app(media)) as client:
        job_id, w, p = build_plan(media, client)
        w, p = run(media, job_id, w, p, images=FakeImageGenerator())
        assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
        plain_render = signatures.specification(media, job_id, w, p, "render", "a")["signature"]
        plain_compose = signatures.specification(media, job_id, w, p, "compose")["signature"]
        assert plain_render == ws.version(w, p["shots"]["a"]["render"])["signature"]
        assert plain_compose == ws.version(w, p["final"])["signature"]
        plain = ws.version(w, p["final"])["data"]["boundaries"]
        assert all("beat_delay" not in item for item in plain)
        assert "handle_frames" not in ws.version(w, p["shots"]["a"]["render"])["data"]

        p["options"]["beat_align"] = {"tolerance": TOL}
        ws.persist(media, job_id, w)
        assert signatures.specification(media, job_id, w, p, "render", "a")["signature"] != plain_render
        assert (
            signatures.specification(media, job_id, w, p, "render", "c")["signature"]
            == ws.version(w, p["shots"]["c"]["render"])["signature"]
        )  # 末镜不留余量，渲染复用
        w, p = run(media, job_id, w, p, images=FakeImageGenerator())
        assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
        final = ws.version(w, p["final"])
        bounds = final["data"]["boundaries"]
        music = ws.version(w, p["shots"]["a"]["music"])
        analysis = beats.analyze(ws.file_path(media, job_id, music, "music"))
        grid = beat_align.beat_grid(
            analysis["beats"], analysis["duration"], 0.0, final["data"]["total_seconds"] + 1
        )
        for before, after in zip(plain[:-1], bounds[:-1], strict=True):
            render = ws.version(w, p["shots"][after["shot_id"]]["render"])["data"]
            assert render["handle_frames"] == round(TOL * 25)
            assert after["frames"] - before["frames"] == after.get("beat_delay", 0) <= render["handle_frames"]
            distance = min(abs(after["end"] - beat) for beat in grid)
            original = after["end"] - after.get("beat_delay", 0) / 25
            reachable = any(original - 0.5 / 25 <= beat <= original + TOL for beat in grid)
            # 容差内都有节拍：对齐后切点与节拍距离 ≤ 1 帧。
            assert reachable
            assert distance <= 1 / 25 + 1e-6, (after, distance)
        assert any(item.get("beat_delay") for item in bounds)
        # 配音不被截断：每镜帧数不少于渲染帧数；成片帧数与边界一致。
        for item in bounds:
            assert item["frames"] >= ws.version(w, p["shots"][item["shot_id"]]["render"])["data"]["frames"]
        frames, _, _ = frame_count(ws.file_path(media, job_id, final, "mp4"))
        assert frames == final["data"]["total_frames"] == sum(item["frames"] for item in bounds)
        # 关闭后恢复旧签名。
        p["options"].pop("beat_align")
        assert signatures.specification(media, job_id, w, p, "render", "a")["signature"] == plain_render
        assert "beat_align" not in post.compose_payload(
            media, w, p
        )  # 成片签名只多出 beat_align 一键（开启时）


@pytest.mark.parametrize(
    "transition",
    [{"next": {"kind": "dissolve", "duration": 0.2}}, {"leave": {"kind": "fade", "duration": 0.2}}],
)
def test_transition_cuts_are_not_delayed(media, transition):  # noqa: F811
    with TestClient(create_app(media)) as client:
        job_id, w, p = build_plan(media, client, tolerance=0.3, transition=transition)
        w, p = run(media, job_id, w, p, images=FakeImageGenerator())
        assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
        final = ws.version(w, p["final"])
        b = next(item for item in final["data"]["boundaries"] if item["shot_id"] == "b")
        assert "beat_delay" not in b
        handle = ws.version(w, p["shots"]["b"]["render"])["data"].get("handle_frames", 0)
        assert handle == (round(0.3 * 25) if "next" in transition else 0)
        frames, _, _ = frame_count(ws.file_path(media, job_id, final, "mp4"))
        assert frames == final["data"]["total_frames"]


def test_beat_align_option_is_omitted_when_off():
    from studio.workspace import PlanOptions

    assert "beat_align" not in PlanOptions().model_dump()
    assert PlanOptions(beat_align={"tolerance": 0.2}).model_dump()["beat_align"] == {"tolerance": 0.2}
    with pytest.raises(ValueError):
        PlanOptions(beat_align={"tolerance": 2})
