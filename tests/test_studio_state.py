from __future__ import annotations

import json

import pytest
from conftest import legacy_job
from filelock import FileLock

from mindex.admin.curation import EntityNotFound
from mindex.shared.config import RenderConfig, SelectionConfig
from mindex.studio import studio
from mindex.studio.studio import Shot, Storyboard


def complete(config, job_id, step, result=None):
    studio.begin_step(config, job_id, step)
    studio.complete_step(config, job_id, step, result or {})


def test_studio_config_defaults_and_validation(config):
    assert config.studio.recall.granularity == "shot"
    assert config.studio.recall.limit == 20
    assert config.studio.selection.atempo_limit == 0.1
    assert set(config.studio.selection.weights) == {"semantic", "duration", "extension", "repeat", "overlap"}
    assert config.studio.audio.mode == "tts_only"
    # 创建默认软字幕轨道；烧录可在方案参数里开启。
    assert config.studio.subtitle.burn is False
    assert config.studio.tts.speech_path == "/audio/speech"
    assert len(config.studio_key) == 24
    with pytest.raises(ValueError):
        RenderConfig(width=1281)
    with pytest.raises(ValueError):
        SelectionConfig(weights={"semantic": 1.0})
    with pytest.raises(ValueError):
        SelectionConfig(atempo_limit=0.5)


def test_create_job_validates_input_and_persists(config):
    state = studio.create_job(config, "  夜色沉了下去。远处有火车经过。  ")
    job_id = state["id"]
    # 新任务只保存视频大纲；文案在构建方案里创作。
    assert state["outline"] == "夜色沉了下去。远处有火车经过。" and "input_text" not in state
    assert state["title"].startswith("夜色沉了下去")
    assert state["steps"]["split"]["status"] == "pending"
    assert state["options"] == {
        "voice": config.studio.tts.voice,
        "audio_mode": "tts_only",
        "recall_limit": 20,
        "burn": False,
    }
    assert (config.data_dir / "studio" / job_id / "job.json").is_file()
    assert studio.load_job(config, job_id)["id"] == job_id
    listing = studio.list_jobs(config)
    assert listing[0]["id"] == job_id and listing[0]["current_step"] == "split"
    with pytest.raises(studio.StudioError):
        studio.create_job(config, "   ")
    with pytest.raises(studio.StudioError):
        studio.create_job(config, "长" * (config.studio.max_input_chars + 1))
    with pytest.raises(EntityNotFound):
        studio.load_job(config, "不存在")


def test_create_job_options_override_defaults(config):
    state = studio.create_job(
        config,
        "带选项的文案。",
        options={"audio_mode": "ducking", "recall_limit": 10, "burn": False, "voice": "Ethan"},
    )
    assert state["options"] == {"voice": "Ethan", "audio_mode": "ducking", "recall_limit": 10, "burn": False}
    with pytest.raises(studio.StudioError):
        studio.create_job(config, "非法选项。", options={"audio_mode": "invalid"})


def test_step_lifecycle_attempts_and_dependencies(config):
    job_id = legacy_job(config, "山风吹过山谷。")["id"]
    state = studio.begin_step(config, job_id, "split")
    assert state["steps"]["split"]["status"] == "running"
    attempt = state["steps"]["split"]["attempt"]
    assert len(attempt) == 12
    with pytest.raises(studio.StudioError, match="尚未完成"):
        studio.begin_step(config, job_id, "recall")
    with pytest.raises(studio.StudioError, match="未知创作步骤"):
        studio.begin_step(config, job_id, "unknown")
    studio.complete_step(config, job_id, "split", {"shots": 2})
    state = studio.load_job(config, job_id)
    assert state["steps"]["split"]["status"] == "complete"
    assert state["steps"]["split"]["elapsed_ms"] >= 0
    record = json.loads((studio.job_dir(config, job_id) / "attempts" / f"split-{attempt}.json").read_text())
    assert record["status"] == "complete" and record["result"]["shots"] == 2


def test_fail_step_records_error_and_allows_retry(config):
    job_id = legacy_job(config, "示例文案。")["id"]
    studio.begin_step(config, job_id, "split")
    studio.fail_step(config, job_id, "split", RuntimeError("模型超时"))
    info = studio.load_job(config, job_id)["steps"]["split"]
    assert info["status"] == "failed" and "模型超时" in info["error"]
    assert info["error_type"] == "RuntimeError"
    studio.begin_step(config, job_id, "split")  # 失败后可重试
    assert studio.load_job(config, job_id)["steps"]["split"]["status"] == "running"
    studio.complete_step(config, job_id, "split", {})
    with pytest.raises(studio.StudioError, match="不在执行中"):  # 已完成的步骤不能再次记录
        studio.complete_step(config, job_id, "split", {})


def test_rerun_invalidates_only_dependency_downstream(config):
    job_id = legacy_job(config, "分镜一。分镜二。")["id"]
    for step in studio.STEPS:
        complete(config, job_id, step)
    state = studio.begin_step(config, job_id, "recall")
    assert state["steps"]["split"]["status"] == "complete"
    assert state["steps"]["tts"]["status"] == "complete"  # tts 只依赖 split，不受影响
    assert state["steps"]["select"]["status"] == "pending"
    assert state["steps"]["render"]["status"] == "pending"
    assert state["history"][-1]["reason"] == "rerun-recall"
    with pytest.raises(studio.StudioError):
        studio.begin_step(config, job_id, "select")


def test_storyboard_edit_invalidates_downstream(config):
    job_id = legacy_job(config, "原本文案。")["id"]
    complete(config, job_id, "split", {"shots": 1})
    complete(config, job_id, "recall", {"empty": []})
    storyboard = Storyboard(
        job_id=job_id,
        shots=[Shot(id="shot-01", text="新文案", query="新查询", estimated_seconds=4.0)],
    )
    state = studio.save_storyboard(config, job_id, storyboard)
    assert state["steps"]["split"]["status"] == "complete"
    assert state["steps"]["recall"]["status"] == "pending"
    assert state["history"][-1]["reason"].startswith("storyboard-")
    loaded = studio.load_storyboard(config, job_id)
    assert loaded.shots[0].text == "新文案"
    # 未完成 split 的任务不能编辑分镜
    other = legacy_job(config, "另一个任务。")["id"]
    with pytest.raises(studio.StudioError, match="先完成文本拆分镜"):
        studio.save_storyboard(config, other, storyboard)
    with pytest.raises(EntityNotFound):
        studio.load_storyboard(config, other)


def test_timeline_edit_requires_select_and_invalidates_render(config):
    job_id = legacy_job(config, "时间线任务。")["id"]
    timeline = studio.Timeline(
        job_id=job_id,
        mode="manual",
        audio_mode="tts_only",
        burn=True,
        events=[
            studio.TimelineEvent(
                shot_id="shot-01",
                order=1,
                audio=studio.TimelineAudio(
                    file="audio/shot-01.wav", duration=4.0, atempo=1.0, render_duration=4.0
                ),
                video=studio.TimelineVideo(
                    source_id="src",
                    segment_id="seg",
                    segment_ids=["seg"],
                    start=0.0,
                    end=4.0,
                    alignment="manual",
                ),
                subtitle=studio.TimelineSubtitle(start=0.0, end=4.0, text="时间线任务。"),
            )
        ],
    )
    with pytest.raises(studio.StudioError, match="先完成选片"):
        studio.save_timeline(config, job_id, timeline)
    for step in ("split", "recall", "tts", "select", "render"):
        complete(config, job_id, step)
    state = studio.save_timeline(config, job_id, timeline)
    assert state["steps"]["select"]["status"] == "complete"
    assert state["steps"]["render"]["status"] == "pending"
    assert state["history"][-1]["reason"] == "timeline-edited"
    assert studio.load_timeline(config, job_id).total_seconds == 4.0


def test_recover_stale_marks_interrupted_steps(config):
    job_id = legacy_job(config, "恢复检测。")["id"]
    studio.begin_step(config, job_id, "split")
    studio.recover_stale(config)
    info = studio.load_job(config, job_id)["steps"]["split"]
    assert info["status"] == "failed" and "中断" in info["error"]
    held = legacy_job(config, "执行中的任务。")["id"]
    studio.begin_step(config, held, "split")
    with FileLock(str(studio.lock_path(config)), timeout=0):
        studio.recover_stale(config)
    assert studio.load_job(config, held)["steps"]["split"]["status"] == "running"


def test_delete_job_removes_directory(config):
    job_id = legacy_job(config, "待删除。")["id"]
    studio.delete_job(config, job_id)
    assert not studio.job_dir(config, job_id).exists()
    with pytest.raises(EntityNotFound):
        studio.delete_job(config, job_id)


def test_invalidation_targets_follow_dependency_closure():
    assert studio.invalidation_targets("split") == ("recall", "tts", "select", "render")
    assert studio.invalidation_targets("recall") == ("select", "render")
    assert studio.invalidation_targets("tts") == ("select", "render")
    assert studio.invalidation_targets("select") == ("render",)
    assert studio.invalidation_targets("render") == ()


@pytest.mark.parametrize("shot_id", ["../escape", "../../victim/job", "/tmp/audio", "a/b", "a\\b", "."])
def test_shot_ids_cannot_be_paths(shot_id):
    with pytest.raises(ValueError):
        Shot(id=shot_id, text="文案", query="画面", estimated_seconds=1)


@pytest.mark.parametrize("number", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_configuration_rejected(number):
    with pytest.raises(ValueError):
        RenderConfig(fps=number)
    with pytest.raises(ValueError):
        SelectionConfig(
            weights={"semantic": number, "duration": 1, "extension": 0, "repeat": 0, "overlap": 0}
        )


def test_query_only_and_reorder_preserve_tts(config):
    board = Storyboard(
        job_id="placeholder",
        shots=[
            Shot(id="shot-01", text="第一句。", query="红色", estimated_seconds=1),
            Shot(id="shot-02", text="第二句。", query="蓝色", estimated_seconds=1),
        ],
    )
    job_id = legacy_job(config, "第一句。第二句。")["id"]
    board.job_id = job_id
    studio.write_storyboard(config, job_id, board)
    for step in studio.STEPS:
        complete(config, job_id, step)
    board.shots[0].query = "更新查询"
    studio.save_storyboard(config, job_id, board)
    state = studio.load_job(config, job_id)
    assert state["steps"]["tts"]["status"] == "complete"
    assert state["steps"]["recall"]["status"] == "pending"
    for step in ("recall", "select", "render"):
        complete(config, job_id, step)
    board.shots.reverse()
    studio.save_storyboard(config, job_id, board)
    state = studio.load_job(config, job_id)
    assert state["steps"]["recall"]["status"] == state["steps"]["tts"]["status"] == "complete"
    assert state["steps"]["select"]["status"] == "pending"


def test_changed_config_and_legacy_are_invalidated_before_run(config):
    job_id = legacy_job(config, "文案。")["id"]
    for step in studio.STEPS:
        complete(config, job_id, step)
    config.studio.tts.model = "another-model"
    state = studio.reconcile(config, job_id)
    assert state["steps"]["tts"]["status"] == "pending"
    assert state["steps"]["recall"]["status"] == "complete"
    assert state["steps"]["render"]["status"] == "pending"
    state.pop("artifact_version")
    studio.save(studio.job_path(config, job_id), state)
    state = studio.reconcile(config, job_id)
    assert state["steps"]["split"]["status"] == "complete"
    assert state["steps"]["recall"]["status"] == "pending"
