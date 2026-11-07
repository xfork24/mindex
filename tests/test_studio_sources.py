"""分镜画面来源：recall（缺省）/ clip（上传片段）/ image（静态图片推拉）。

快速用例覆盖模型校验、dump 省略、阶段序列、预检阻塞、上传限制与 PATCH 校验；构建出片的重媒体用例标记 slow。
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import TypeAdapter, ValidationError

from api import create_app
from conftest import FixtureSpeaker, legacy_job
from shared.media import command, probe
from studio import studio
from studio import workspace as ws
from studio.artifacts import digest

slow = pytest.mark.slow


# ---------------------------------------------------------------- 工具


def make_clip(path: Path, seconds: float, *, audio: bool = True, color: str = "green", size="160x120"):
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s={size}:r=25:d={seconds}",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    command([*args, str(path)], timeout=60)
    return path


def make_image(path: Path, size=(300, 200), color=(200, 30, 30, 255)):
    image = Image.new("RGBA", size, color)
    for x in range(0, size[0], 10):  # 竖条纹：平移与推拉都会改变画面
        for y in range(size[1]):
            image.putpixel((x, y), (0, 0, 255, 255))
    image.save(path)
    return path


def store_input(config, job_id, path: Path) -> str:
    media_id = digest(path)
    target = studio.job_dir(config, job_id) / "inputs" / f"{media_id}{path.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    return media_id


def setup(config, shots, text="红色开场。蓝色结尾。"):
    job_id = legacy_job(config, text, options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, shots)
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id, w, p


def build(config, job_id, w, p, *, embedder=None, force=False):
    checked = ws.preflight(config, job_id, w, p, force=force)
    bid = str(len(w["builds"]))
    w["builds"].append(
        {
            "id": bid,
            "plan_id": p["id"],
            "status": "running",
            "force": force,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, bid, speaker=FixtureSpeaker(config), embedder=embedder)
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def frame_count(path: Path) -> tuple[int, int, int]:
    raw = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames,width,height",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    stream = raw["streams"][0]
    return int(stream["nb_read_frames"]), int(stream["width"]), int(stream["height"])


def frame_hashes(path: Path) -> list[str]:
    output = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.rsplit(",", 1)[-1].strip() for line in output.splitlines() if not line.startswith("#")]


@pytest.fixture
def small(config):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 25
    return config


def shot(sid, text, **extra):
    return studio.Shot(id=sid, text=text, query="画面", estimated_seconds=1, **extra)


# ---------------------------------------------------------------- 模型


def test_shot_source_validation_and_dump_omission():
    plain = shot("a", "旁白")
    assert "source" not in plain.model_dump()
    explicit = shot("a", "旁白", source={"kind": "recall"})
    assert explicit.source is None and explicit.model_dump() == plain.model_dump()
    clip = shot("a", "旁白", source={"kind": "clip", "media_id": "a" * 64, "start": 1.5})
    assert clip.model_dump()["source"] == {"kind": "clip", "media_id": "a" * 64, "start": 1.5}
    image = shot("a", "旁白", source={"kind": "image", "media_id": "b" * 64})
    assert image.model_dump()["source"] == {
        "kind": "image",
        "media_id": "b" * 64,
        "motion": "zoom-in",
        "strength": 0.3,
    }
    assert studio.Shot.model_validate(image.model_dump()) == image
    for bad in (
        {"kind": "clip", "media_id": "xyz"},
        {"kind": "clip", "media_id": "a" * 64, "start": -1},
        {"kind": "image", "media_id": "a" * 64, "strength": 1.5},
        {"kind": "image", "media_id": "a" * 64, "motion": "spin"},
        {"kind": "image", "media_id": "a" * 64, "start": 1},
        {"kind": "aigc", "prompt": "x" * 1001},
        {"kind": "aigc", "image_id": "xyz"},
        {"kind": "aigc", "seed": -1},
        {"kind": "aigc", "media_id": "a" * 64},
        {"kind": "animation", "brief": "x" * 2001},
        {"kind": "animation", "media_id": "a" * 64},
    ):
        with pytest.raises(ValidationError):
            shot("a", "旁白", source=bad)


def test_timeline_video_variants_keep_library_dumps_identical():
    library = {
        "source_id": "s",
        "source_name": "",
        "segment_id": "g",
        "segment_ids": ["g"],
        "start": 0.0,
        "end": 1.0,
        "score": 0.0,
        "rank": 0,
        "alignment": "nearest",
    }
    adapter = TypeAdapter(studio.TimelineVideo)
    parsed = adapter.validate_python(library)
    assert parsed.kind == "library" and adapter.dump_python(parsed) == library
    assert adapter.dump_python(adapter.validate_python({**library, "kind": "library"})) == library
    file = {
        "kind": "file",
        "ref": {"input": "c" * 64 + ".mp4"},
        "start": 0.5,
        "end": 2.0,
        "alignment": "padded",
    }
    parsed = adapter.validate_python(file)
    assert parsed.media_id == "c" * 64 and adapter.dump_python(parsed) == file
    version_ref = {**file, "ref": {"version": "d" * 32, "file": "video.mp4"}}
    assert adapter.dump_python(adapter.validate_python(version_ref)) == version_ref
    with pytest.raises(ValidationError):
        adapter.validate_python({**file, "ref": {"input": "c" * 64 + ".mp4", "version": "d" * 32}})
    with pytest.raises(ValidationError):
        adapter.validate_python({**file, "source_id": "s"})


def test_stage_sequences_per_source():
    assert ws.shot_stages({}) == ws.STAGES
    assert ws.shot_stages({"source": {"kind": "clip", "media_id": "a" * 64}}) == ("tts", "select", "render")
    assert ws.shot_stages({"source": {"kind": "image", "media_id": "a" * 64}}) == ("tts", "render")
    assert ws.shot_stages({"source": {"kind": "aigc"}}) == ("tts", "generate", "select", "render")


def test_recall_shots_keep_content_versions_when_source_is_explicit_recall(small):
    job_id, w, p = setup(small, [shot("a", "红色开场。"), shot("b", "蓝色结尾。")])
    before = {sid: p["shots"][sid]["content"] for sid in ("a", "b")}
    ws.set_board(
        small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "recall"}), shot("b", "蓝色结尾。")]
    )
    assert {sid: p["shots"][sid]["content"] for sid in ("a", "b")} == before
    assert "source" not in ws.version(w, before["a"])["data"]


# ---------------------------------------------------------------- 预检（无索引）


def test_clip_and_image_preflight_need_no_index_and_block_missing_files(small, tmp_path):
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    clip_id = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 1.0))
    image_id = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            shot("a", "红色开场。", source={"kind": "clip", "media_id": clip_id}),
            shot("b", "蓝色结尾。", source={"kind": "image", "media_id": image_id}),
        ],
    )
    nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert set(nodes) == {"tts:a", "select:a", "render:a", "tts:b", "render:b", "compose"}
    assert all(n["action"] == "rebuild" for n in nodes.values()), nodes
    assert ws.node_dependencies(w, p, "render", "b") == ["tts:b"]
    assert ws.node_dependencies(w, p, "select", "a") == ["tts:a"]
    # recall 分镜在没有索引时阻塞；clip/image 不受影响。
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            shot("a", "红色开场。", source={"kind": "clip", "media_id": clip_id}),
            shot("r", "召回镜头。"),
        ],
    )
    nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes["recall:r"]["action"] == "blocked" and nodes["select:a"]["action"] == "rebuild"
    # 文件缺失 → 阻塞并给出中文原因。
    for path in (studio.job_dir(small, job_id) / "inputs").glob(clip_id + ".*"):
        path.unlink()
    nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes["select:a"]["action"] == "blocked"
    assert nodes["select:a"]["reason"] == ws.sources.MISSING
    assert nodes["render:a"]["action"] == "blocked"


def test_set_board_drops_selections_and_ranges_outside_new_source(small, tmp_path):
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image_id = store_input(small, job_id, make_image(tmp_path / "i.png"))
    p["shots"]["a"].update(recall="r" * 32, select="s" * 32, tts="t" * 32)
    p["ranges"]["a"] = {"source_id": "x", "start": 0, "end": 1}
    p["anchors"]["a"] = {"mode": "auto"}
    ws.set_board(
        small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image_id})]
    )
    assert set(p["shots"]["a"]) == {"content", "tts"}
    assert "a" not in p["ranges"] and "a" not in p["anchors"]


# ---------------------------------------------------------------- API


def test_upload_purpose_source_limits_and_patch_validation(small, tmp_path):
    job_id, _w, p = setup(small, [shot("a", "红色开场。")])
    small.search.max_query_seconds = 1
    small.studio.max_clip_seconds = 3
    client = TestClient(create_app(small))
    base = f"/api/studio/jobs/{job_id}/plans/{p['id']}"
    clip = make_clip(tmp_path / "c.mp4", 2.0)

    def upload(path, kind, purpose=None, name=None):
        data = {"kind": kind, **({"purpose": purpose} if purpose else {})}
        with path.open("rb") as handle:
            return client.post(f"{base}/query-media", data=data, files={"file": (name or path.name, handle)})

    rejected = upload(clip, "video")
    assert rejected.status_code == 422 and "查询视频不能超过 1 秒" in rejected.json()["detail"]
    accepted = upload(clip, "video", "source")
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["purpose"] == "source" and abs(body["duration"] - 2.0) < 0.2
    longer = upload(make_clip(tmp_path / "long.mp4", 4.0, audio=False), "video", "source")
    assert longer.status_code == 422 and "画面视频不能超过 3 秒" in longer.json()["detail"]
    assert upload(clip, "video", "other").status_code == 422
    small.studio.max_clip_mb = 1
    big = tmp_path / "big.mp4"
    big.write_bytes(clip.read_bytes() + b"\0" * (1024 * 1024 + 1))
    assert upload(big, "video", "source").status_code == 413
    image = upload(make_image(tmp_path / "i.png"), "image", "source")
    assert image.status_code == 201

    detail = client.get(f"/api/studio/jobs/{job_id}/workspace").json()

    def patch(source):
        current = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        return client.patch(
            base,
            json={
                "revision": current["revision"],
                "shots": [
                    {
                        "id": "a",
                        "text": "红色开场。",
                        "query": "画面",
                        "estimated_seconds": 1,
                        "source": source,
                    }
                ],
            },
        )

    missing = patch({"kind": "clip", "media_id": "e" * 64})
    assert missing.status_code == 422 and "文件缺失或类型不符" in missing.json()["detail"]
    mismatch = patch({"kind": "clip", "media_id": image.json()["media_id"]})
    assert mismatch.status_code == 422
    ok = patch({"kind": "clip", "media_id": body["media_id"], "start": 0.5})
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["shot_stages"]["a"] == ["tts", "select", "render"]
    content = data["versions"][data["plans"][data["active_plan"]]["shots"]["a"]["content"]]["data"]
    assert content["source"] == {"kind": "clip", "media_id": body["media_id"], "start": 0.5}
    # 非召回分镜不能人工选片。
    window = client.get(f"{base}/shots/a/selection-context")
    assert window.status_code == 422 and "不是素材召回" in window.json()["detail"]
    assert detail["shot_stages"]["a"] == list(ws.STAGES)
    # 显式 recall 恢复素材召回：content 不落 source 键。
    reset = patch({"kind": "recall"}).json()
    content = reset["versions"][reset["plans"][reset["active_plan"]]["shots"]["a"]["content"]]["data"]
    assert "source" not in content


# ---------------------------------------------------------------- 构建（重媒体）


@slow
def test_clip_only_build_without_index_trims_and_ducks_clip_audio(small, tmp_path):
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    clip_id = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 3.0))
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [shot("a", "红色开场。", source={"kind": "clip", "media_id": clip_id, "start": 0.5})],
    )
    p["options"]["audio_mode"] = "ducking"
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    assert {n["stage"] for n in w["builds"][-1]["nodes"]} == {"tts", "select", "render", "compose"}
    selection = ws.version(w, p["shots"]["a"]["select"])
    video = selection["data"]["event"]["video"]
    assert video["kind"] == "file" and video["ref"] == {"input": f"{clip_id}.mp4"}
    speech = ws.version(w, p["shots"]["a"]["tts"])["data"]["duration"]
    assert video["start"] == 0.5 and abs(video["end"] - (0.5 + speech)) < 0.01
    assert video["alignment"] == "trimmed" and selection["data"]["freeze_seconds"] == 0
    render = ws.version(w, p["shots"]["a"]["render"])
    # ducking 使用片段原声。
    assert (ws.root(small, job_id) / "versions" / render["id"] / "ambient.wav").is_file()
    frames, width, height = frame_count(ws.file_path(small, job_id, render, "video"))
    assert (frames, width, height) == (render["data"]["frames"], 160, 120)
    final = ws.version(w, p["final"])
    assert final["data"]["total_frames"] == render["data"]["frames"]
    assert probe(ws.file_path(small, job_id, final, "mp4"))["audio"]
    # 再次构建全部复用；改起点只重建选片、渲染与成片。
    nodes = {n["key"]: n["action"] for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert set(nodes.values()) == {"reuse"}
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [shot("a", "红色开场。", source={"kind": "clip", "media_id": clip_id, "start": 1.0})],
    )
    nodes = {n["key"]: n["action"] for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes == {"tts:a": "reuse", "select:a": "rebuild", "render:a": "rebuild", "compose": "rebuild"}


@slow
def test_short_clip_is_freeze_padded_and_tampered_clip_fails(small, tmp_path):
    job_id, w, p = setup(small, [shot("a", "这是一句比较长的旁白用于测试冻结帧。")])
    clip_id = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 0.6, audio=False))
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [shot("a", "这是一句比较长的旁白用于测试冻结帧。", source={"kind": "clip", "media_id": clip_id})],
    )
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    selection = ws.version(w, p["shots"]["a"]["select"])["data"]
    assert selection["event"]["video"]["alignment"] == "padded" and selection["freeze_seconds"] > 1
    assert "冻结末帧" in selection["event"]["warning"]
    render = ws.version(w, p["shots"]["a"]["render"])
    assert frame_count(ws.file_path(small, job_id, render, "video"))[0] == render["data"]["frames"]
    # strict 对齐拒绝补帧。
    p["options"]["alignment"] = "strict"
    with pytest.raises(studio.StudioError, match="严格对齐"):
        ws.execute(small, job_id, w, p, "select", "a")
    p["options"]["alignment"] = "auto"
    # 片段被改动：摘要不符 → 选片失败并提示重新上传。
    target = next((ws.root(small, job_id) / "inputs").glob(clip_id + ".*"))
    target.write_bytes(target.read_bytes() + b"tampered")
    w, p = build(small, job_id, w, p, force=True)
    failed = next(n for n in w["builds"][-1]["nodes"] if n["key"] == "select:a")
    assert failed["status"] == "failed" and failed["error"] == ws.sources.MISSING
    assert next(n for n in w["builds"][-1]["nodes"] if n["key"] == "render:a")["status"] == "blocked"


@slow
@pytest.mark.parametrize("motion", ["zoom-in", "zoom-out", "pan-left", "pan-right", "none"])
def test_still_shot_frames_dimensions_and_determinism(small, tmp_path, motion):
    from studio.render import still_shot

    image = make_image(tmp_path / "i.png", size=(400, 200))
    render = small.studio.render.model_copy(update={"width": 120, "height": 160})
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    for output in (first, second):
        still_shot(image, render, output, frames=31, motion=motion, strength=0.6, fit="crop")
    assert frame_count(first) == (31, 120, 160)
    hashes = frame_hashes(first)
    assert hashes == frame_hashes(second)
    if motion == "none":
        assert len(set(hashes[:1] + hashes[-1:])) <= 2
    else:
        assert hashes[0] != hashes[-1]
    assert not list(tmp_path.glob("*.still.png"))


@slow
def test_image_build_motion_signature_and_rebuilds_only_that_shot(small, tmp_path):
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image_id = store_input(small, job_id, make_image(tmp_path / "i.png"))
    clip_id = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 2.0))
    board = [
        shot("a", "红色开场。", source={"kind": "image", "media_id": image_id}),
        shot("b", "蓝色结尾。", source={"kind": "clip", "media_id": clip_id}),
    ]
    ws.set_board(small, job_id, w, p, board)
    p["options"]["audio_mode"] = "ducking"
    p["options"]["canvas"] = "9:16"
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    render = ws.version(w, p["shots"]["a"]["render"])
    assert render["inputs"] == {"content": p["shots"]["a"]["content"], "tts": p["shots"]["a"]["tts"]}
    assert render["parameters"]["canvas"] == "9:16"
    width, height = small.studio.render.size("9:16")
    assert frame_count(ws.file_path(small, job_id, render, "video")) == (
        render["data"]["frames"],
        width,
        height,
    )
    assert render["data"]["event"]["video"]["kind"] == "file"
    # 图片没有原声：ducking 也只有配音。
    assert not (ws.root(small, job_id) / "versions" / render["id"] / "ambient.wav").exists()
    final = ws.version(w, p["final"])
    frames = [ws.version(w, p["shots"][sid]["render"])["data"]["frames"] for sid in ("a", "b")]
    assert final["data"]["total_frames"] == sum(frames)
    signature = ws.specification(small, job_id, w, p, "render", "a")["signature"]
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            shot("a", "红色开场。", source={"kind": "image", "media_id": image_id, "motion": "pan-left"}),
            board[1],
        ],
    )
    assert ws.specification(small, job_id, w, p, "render", "a")["signature"] != signature
    nodes = {n["key"]: n["action"] for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes == {
        "tts:a": "reuse",
        "render:a": "rebuild",
        "tts:b": "reuse",
        "select:b": "reuse",
        "render:b": "reuse",
        "compose": "rebuild",
    }
    # 强度变化同样只重建该镜渲染。
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            shot(
                "a",
                "红色开场。",
                source={"kind": "image", "media_id": image_id, "motion": "pan-left", "strength": 0.8},
            ),
            board[1],
        ],
    )
    nodes = {n["key"]: n["action"] for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes["render:a"] == "rebuild" and nodes["render:b"] == "reuse"


@slow
def test_mixed_recall_clip_image_compose(small, tmp_path, video_file):
    from conftest import FixtureEmbedder
    from processing.indexing import IndexBuilder
    from processing.pipeline import Pipeline

    Pipeline(small).prepare(video_file)
    embedder = FixtureEmbedder(small)
    IndexBuilder(small, embedder).build(mode="visual", kinds=("shot",), activate=True)
    job_id, w, p = setup(small, [shot("r", "红色开场。")])
    clip_id = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 2.0))
    image_id = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            studio.Shot(id="r", text="红色开场。", query="红色", estimated_seconds=1),
            shot("c", "片段中段。", source={"kind": "clip", "media_id": clip_id}),
            shot("i", "图片结尾。", source={"kind": "image", "media_id": image_id, "motion": "zoom-out"}),
        ],
    )
    w, p = build(small, job_id, w, p, embedder=embedder)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    final = ws.version(w, p["final"])
    frames = [ws.version(w, p["shots"][sid]["render"])["data"]["frames"] for sid in ("r", "c", "i")]
    assert final["data"]["total_frames"] == sum(frames)
    assert [b["shot_id"] for b in final["data"]["boundaries"]] == ["r", "c", "i"]
    assert frame_count(ws.file_path(small, job_id, final, "mp4"))[0] == sum(frames)
    kinds = [
        ws.version(w, p["shots"][sid]["render"])["data"]["event"]["video"].get("kind", "library")
        for sid in ("r", "c", "i")
    ]
    assert kinds == ["library", "file", "file"]
    assert "kind" not in ws.version(w, p["shots"]["r"]["select"])["data"]["event"]["video"]
    detail = ws.detail(small, job_id, w)
    assert detail["shot_stages"] == {
        "r": list(ws.STAGES),
        "c": ["tts", "select", "render"],
        "i": ["tts", "render"],
    }
    assert detail["conflicts"].get("render:i") == []
    # 片段选片版本可生成带声预览。
    client = TestClient(create_app(small, embedder, speaker=FixtureSpeaker(small)))
    preview = client.get(f"/api/studio/jobs/{job_id}/versions/{p['shots']['c']['select']}/preview")
    assert preview.status_code == 200, preview.text
    assert client.get(preview.json()["url"]).status_code == 200


def test_clean_and_backup_keep_source_inputs(small, tmp_path):
    """清理从不触碰任务 inputs/ 中的来源文件；备份按任务整目录导出并能恢复它们。"""

    from maintenance.backup import export_backup, import_backup
    from maintenance.clean import clean_library
    from shared.config import Config

    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image_id = store_input(small, job_id, make_image(tmp_path / "i.png"))
    clip_id = store_input(small, job_id, make_clip(tmp_path / "c.mp4", 0.5, audio=False))
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            shot("a", "红色开场。", source={"kind": "image", "media_id": image_id}),
            shot("b", "蓝色结尾。", source={"kind": "clip", "media_id": clip_id}),
        ],
    )
    ws.persist(small, job_id, w)
    inputs = studio.job_dir(small, job_id) / "inputs"
    names = sorted(path.name for path in inputs.iterdir())
    report = clean_library(small, apply=True)
    assert not any("inputs/" in json.dumps(c, ensure_ascii=False, default=str) for c in report["categories"])
    assert sorted(path.name for path in inputs.iterdir()) == names
    config_path = tmp_path / "origin.yaml"
    config_path.write_text(yaml.safe_dump({"data_dir": str(small.data_dir)}))
    bundle = tmp_path / "bundle"
    export_backup(small, bundle, all_sources=True, config_path=config_path)
    target = tmp_path / "restored" / "data"
    import_backup(Config(data_dir=target), bundle, all_sources=True, config_path=target.parent / "data.yaml")
    restored = target / "studio" / job_id / "inputs"
    assert sorted(path.name for path in restored.iterdir()) == names
    assert all(digest(path) == path.name.split(".")[0] for path in restored.iterdir())


def test_image_png_normalizes_orientation_and_alpha(tmp_path):
    from shared.images import image_png

    path = tmp_path / "rot.png"
    Image.new("RGBA", (40, 20), (0, 0, 0, 0)).save(path)
    out = Image.open(io.BytesIO(image_png(path, 1_000_000)))
    assert out.mode == "RGB" and out.getpixel((0, 0)) == (255, 255, 255)


# ---------------------------------------------------------------- 智能助手


@pytest.fixture
def agent_job(small, tmp_path):
    job_id, _, _ = setup(
        small, [shot("shot-a1daad59aa", "红色开场。"), shot("shot-925281d5bb", "蓝色结尾。")]
    )
    with TestClient(create_app(small)) as client:
        yield small, client, job_id


def board_content(client, job_id, sid):
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    p = data["plans"][data["active_plan"]]
    return data["versions"][p["shots"][sid]["content"]]["data"]


def test_agent_edit_shots_sets_and_clears_source(agent_job, tmp_path):
    from agent.registry import ToolError
    from test_agent_studio_recall import run

    config, client, job_id = agent_job
    image_id = store_input(config, job_id, make_image(tmp_path / "i.png"))
    preview, _output, _ = run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {
                    "op": "update",
                    "shot": "#1",
                    "fields": {
                        "source": {
                            "kind": "image",
                            "media_id": image_id,
                            "motion": "pan-right",
                            "strength": 0.5,
                        }
                    },
                },
                {
                    "op": "insert",
                    "after": "#1",
                    "fields": {"text": "插入片段。", "query": "片段", "source": {"kind": "recall"}},
                },
            ],
        },
    )
    change = next(c for c in preview.changes if "画面来源" in c["label"])
    assert (
        change["before"] == "素材召回（默认）"
        and "静态图片" in change["after"]
        and "向右平移" in change["after"]
    )
    assert any("图片画面渲染及成片" in warning for warning in preview.warnings)
    source = board_content(client, job_id, "shot-a1daad59aa")["source"]
    assert source == {"kind": "image", "media_id": image_id, "motion": "pan-right", "strength": 0.5}
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    inserted = data["versions"][
        data["plans"][data["active_plan"]]["shots"][
            data["versions"][data["plans"][data["active_plan"]]["board"]]["data"]["order"][1]
        ]["content"]
    ]["data"]
    assert "source" not in inserted
    run(
        client,
        config,
        "edit_shots",
        {"job_id": job_id, "operations": [{"op": "update", "shot": "#1", "clear": ["source"]}]},
    )
    assert "source" not in board_content(client, job_id, "shot-a1daad59aa")
    with pytest.raises(ToolError, match="media_id"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [{"op": "update", "shot": "#1", "fields": {"source": {"kind": "clip"}}}],
            },
        )
    with pytest.raises(ToolError, match="不支持字段"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [
                    {
                        "op": "update",
                        "shot": "#1",
                        "fields": {"source": {"kind": "clip", "media_id": image_id, "motion": "zoom-in"}},
                    }
                ],
            },
        )
    # 服务端校验：图片文件不能用作 clip 片段。
    with pytest.raises(ToolError, match="类型不符"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [
                    {
                        "op": "update",
                        "shot": "#1",
                        "fields": {"source": {"kind": "clip", "media_id": image_id}},
                    }
                ],
            },
        )
    from agent.tools.common import PlanView
    from agent.tools.shot_details import render_detail, shot_detail

    run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {"op": "update", "shot": "#2", "fields": {"source": {"kind": "image", "media_id": image_id}}}
            ],
        },
    )
    view = PlanView(client.get(f"/api/studio/jobs/{job_id}/workspace").json())
    detail = shot_detail(view, view.order[1], 2)
    assert detail["source"]["kind"] == "image" and "画面来源：{" in render_detail(detail)


def test_agent_attach_media_as_source(agent_job, tmp_path):
    from test_agent_studio_recall import attachment, run

    config, client, job_id = agent_job
    clip = make_clip(tmp_path / "c.mp4", 1.0)
    ref = attachment(client, clip.read_bytes(), "片段.mp4", "video/mp4")
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    pid = data["active_plan"]
    preview, output, sent = run(
        client,
        config,
        "attach_query_media",
        {"job_id": job_id, "shot": "#2", "attachment": ref, "purpose": "source"},
    )
    assert "上传片段" in preview.summary and "一次确认" in preview.summary
    assert preview.changes[0]["label"] == "上传画面文件"
    assert any("片段选窗" in warning for warning in preview.warnings)
    writes = [entry for entry in sent if entry[0] != "GET"]
    assert writes == [
        ("POST", f"/api/studio/jobs/{job_id}/plans/{pid}/query-media"),
        ("PATCH", f"/api/studio/jobs/{job_id}/plans/{pid}"),
    ]
    media_id = output.value["media_id"]
    assert board_content(client, job_id, "shot-925281d5bb")["source"] == {
        "kind": "clip",
        "media_id": media_id,
        "start": None,
    }
    # 图片附件作为画面：image 来源，缺省推近。
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buffer, "PNG")
    ref = attachment(client, buffer.getvalue(), "图.png", "image/png")
    run(
        client,
        config,
        "attach_query_media",
        {"job_id": job_id, "shot": "#1", "attachment": ref, "purpose": "source"},
    )
    source = board_content(client, job_id, "shot-a1daad59aa")["source"]
    assert source["kind"] == "image" and source["motion"] == "zoom-in"
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    assert data["shot_stages"] == {
        "shot-a1daad59aa": ["tts", "render"],
        "shot-925281d5bb": ["tts", "select", "render"],
    }
