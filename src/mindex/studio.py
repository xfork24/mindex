"""创作任务状态机：任务即目录，状态即文件（同 e2e run.json 模式）。

五个步骤按依赖组成工作流：split → recall → tts → select → render，
其中 recall 与 tts 都只依赖 split，select 需要前两者都完成。
所有状态变更都原子落盘；重跑某一步只作废依赖它的下游步骤。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import Field, model_validator

from .config import Config, StrictModel, fingerprint
from .curation import EntityNotFound

STEPS = ("split", "recall", "tts", "select", "render")
SPLIT_VERSION = 3
STEP_LABELS = {
    "split": "文本拆分镜",
    "recall": "语义召回",
    "tts": "生成配音",
    "select": "选片对齐",
    "render": "渲染合成",
}
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "split": (),
    "recall": ("split",),
    "tts": ("split",),
    "select": ("recall", "tts"),
    "render": ("select",),
}
_JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")  # YYYYMMDD-HHMMSS-hex6


class StudioError(ValueError):
    """用户可见的创作工作流错误（中文消息，映射为 422）。"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def save_bytes(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_bytes(payload)
    temporary.replace(path)


# ---------------------------------------------------------------- 数据模型


class Shot(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60)


class Storyboard(StrictModel):
    job_id: str = Field(min_length=1)
    shots: list[Shot] = Field(min_length=1)
    coverage_warning: str | None = None

    @model_validator(mode="after")
    def unique_ids(self):
        if len({shot.id for shot in self.shots}) != len(self.shots):
            raise ValueError("分镜 id 不能重复")
        return self


class TimelineAudio(StrictModel):
    file: str = Field(min_length=1)
    duration: float = Field(gt=0)
    atempo: float = Field(gt=0)
    render_duration: float = Field(gt=0)


class TimelineVideo(StrictModel):
    source_id: str = Field(min_length=1)
    source_name: str = ""
    segment_id: str = Field(min_length=1)
    segment_ids: list[str] = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    score: float = 0.0
    rank: int = 0
    alignment: Literal[
        "nearest",
        "extended",
        "atempo",
        "extended+atempo",
        "padded",
        "trimmed",
        "extended+padded",
        "extended+trimmed",
        "manual",
    ]

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("视频区间 end 必须大于 start")
        return self


class TimelineSubtitle(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("字幕区间 end 必须大于 start")
        return self


class TimelineEvent(StrictModel):
    shot_id: str = Field(min_length=1)
    order: int = Field(ge=1)
    audio: TimelineAudio
    video: TimelineVideo
    subtitle: TimelineSubtitle
    warning: str | None = None

    @model_validator(mode="after")
    def subtitle_within_event(self):
        if self.subtitle.end > self.audio.render_duration + 1e-6:
            raise ValueError("字幕结束时间不能超过分镜时长")
        return self


class Timeline(StrictModel):
    job_id: str = Field(min_length=1)
    mode: Literal["auto", "manual"]
    audio_mode: Literal["tts_only", "ducking"]
    burn: bool
    events: list[TimelineEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def sequential(self):
        if [event.order for event in self.events] != list(range(1, len(self.events) + 1)):
            raise ValueError("时间线事件 order 必须是 1..n 连续编号")
        if len({event.shot_id for event in self.events}) != len(self.events):
            raise ValueError("时间线事件的 shot_id 不能重复")
        return self

    @property
    def total_seconds(self) -> float:
        return sum(event.audio.render_duration for event in self.events)


class JobOptions(StrictModel):
    voice: str | None = Field(default=None, max_length=100)
    audio_mode: Literal["tts_only", "ducking"] | None = None
    recall_limit: int | None = Field(default=None, ge=1, le=50)
    burn: bool | None = None


# ---------------------------------------------------------------- 路径与目录


def studio_root(config: Config) -> Path:
    return config.data_dir / "studio"


def job_dir(config: Config, job_id: str) -> Path:
    _validate_job_id(job_id)
    directory = studio_root(config) / job_id
    if not directory.resolve().is_relative_to(studio_root(config).resolve()):
        raise EntityNotFound("找不到创作任务")
    return directory


def job_path(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "job.json"


def lock_path(config: Config) -> Path:
    return studio_root(config) / "studio.lock"


def storyboard_path(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "storyboard.json"


def timeline_path(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "timeline.json"


def candidates_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "candidates"


def audio_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "audio"


def render_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "render"


def attempts_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "attempts"


def _validate_job_id(job_id: str):
    if not _JOB_ID.match(job_id):
        raise EntityNotFound("找不到创作任务")


def _read_json(path: Path, missing_message: str) -> dict:
    if not path.is_file():
        raise EntityNotFound(missing_message)
    return json.loads(path.read_text())


def _write_model(path: Path, model, **extra):
    save(path, {"schema": 1, **model.model_dump(), **extra})


def _read_model(path: Path, model_cls, missing_message: str):
    data = _read_json(path, missing_message)
    if data.get("schema") != 1:
        raise StudioError(f"{path.name} 的 schema 版本不受支持")
    envelope = {"schema", "updated_at"}
    return model_cls.model_validate({k: v for k, v in data.items() if k not in envelope})


# ---------------------------------------------------------------- 任务生命周期


def fresh_step() -> dict:
    return {
        "status": "pending",
        "attempt": None,
        "started_at": None,
        "finished_at": None,
        "elapsed_ms": None,
        "error": None,
        "error_type": None,
    }


def invalidation_targets(step: str) -> tuple[str, ...]:
    """依赖传递闭包内的下游步骤；重跑 step 时它们全部作废。"""
    targets: set[str] = set()
    changed = True
    while changed:
        changed = False
        for other in STEPS:
            if other in targets or other == step:
                continue
            if set(DEPENDENCIES[other]) & ({step} | targets):
                targets.add(other)
                changed = True
    return tuple(s for s in STEPS if s in targets)


def _summary(state: dict) -> dict:
    steps = {step: info["status"] for step, info in state["steps"].items()}
    current = next((s for s in STEPS if steps[s] != "complete"), "done")
    return {
        "id": state["id"],
        "title": state["title"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "steps": steps,
        "current_step": current,
    }


def create_job(config: Config, text: str, title: str | None = None, options: dict | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise StudioError("请输入创作文案")
    if len(text) > config.studio.max_input_chars:
        raise StudioError(f"文案超过 {config.studio.max_input_chars} 字上限，请精简后再试")
    try:
        parsed = JobOptions.model_validate(options or {})
    except ValueError as exc:
        raise StudioError(f"任务选项无效：{exc}") from exc
    from .storage import Store

    active = Store(config.data_dir).active_index()
    job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    directory = job_dir(config, job_id)
    if directory.exists():
        raise StudioError("任务目录冲突，请重试")
    state = {
        "schema": 1,
        "id": job_id,
        "title": (title or text[:20]).strip()[:60] or "未命名创作",
        "input_text": text,
        "created_at": now(),
        "updated_at": now(),
        "voice_mode": "custom" if parsed.voice else "inherit",
        "options": {
            "voice": parsed.voice or config.studio.tts.voice,
            "audio_mode": parsed.audio_mode or config.studio.audio.mode,
            "recall_limit": parsed.recall_limit or config.studio.recall.limit,
            "burn": config.studio.subtitle.burn if parsed.burn is None else parsed.burn,
        },
        "config_snapshot": {
            "studio_key": config.studio_key,
            "generator_model": config.studio.generator.model,
            "tts_model": config.studio.tts.model,
            "index_id": active["id"] if active else None,
        },
        "steps": {step: fresh_step() for step in STEPS},
        "history": [],
        "execution": {"status": "idle", "current_step": None, "remaining_steps": []},
        "artifact_version": 1,
    }
    save(job_path(config, job_id), state)
    return state


def load_job(config: Config, job_id: str) -> dict:
    state = _read_json(job_path(config, job_id), "找不到创作任务")
    if state.get("schema") != 1:
        raise StudioError("任务状态 schema 版本不受支持")
    state.setdefault("execution", {"status": "idle", "current_step": None, "remaining_steps": []})
    return state


def list_jobs(config: Config) -> list[dict]:
    root = studio_root(config)
    if not root.is_dir():
        return []
    summaries = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            state = load_job(config, directory.name)
        except (EntityNotFound, StudioError, ValueError):
            continue
        summaries.append(_summary(state))
    return sorted(summaries, key=lambda item: item["created_at"], reverse=True)


def delete_job(config: Config, job_id: str):
    directory = job_dir(config, job_id)
    if not directory.is_dir():
        raise EntityNotFound("找不到创作任务")
    shutil.rmtree(directory)


# ---------------------------------------------------------------- 步骤状态机


def _validate_dependencies(state: dict, step: str):
    missing = [STEP_LABELS[d] for d in DEPENDENCIES[step] if state["steps"][d]["status"] != "complete"]
    if missing:
        raise StudioError(f"步骤 {STEP_LABELS[step]} 依赖的 {'、'.join(missing)} 尚未完成，请先执行")


def _archive_downstream(state: dict, step: str, reason: str):
    timestamp = now()
    for target in invalidation_targets(step):
        prior = state["steps"][target]
        if prior["status"] != "pending":
            state["history"].append({"step": target, "reason": reason, "at": timestamp, "previous": prior})
            state["steps"][target] = fresh_step()


def check_step(state: dict, step: str):
    """步骤执行前的同步预检：步骤名与依赖完整性（不修改状态）。"""
    if step not in STEPS:
        raise StudioError(f"未知创作步骤 {step}")
    _validate_dependencies(state, step)


def begin_step(config: Config, job_id: str, step: str) -> dict:
    state = reconcile(config, job_id)
    check_step(state, step)
    _archive_downstream(state, step, f"rerun-{step}")
    prior = state["steps"][step]
    if prior["status"] != "pending":
        state["history"].append({"step": step, "reason": f"rerun-{step}", "at": now(), "previous": prior})
    timestamp = now()
    state["steps"][step] = {
        **fresh_step(),
        "status": "running",
        "attempt": uuid4().hex[:12],
        "started_at": timestamp,
        "configuration": step_configuration(config, state, step),
    }
    state["updated_at"] = timestamp
    save(job_path(config, job_id), state)
    save(
        attempts_dir(config, job_id) / f"{step}-{state['steps'][step]['attempt']}.json",
        {"step": step, **state["steps"][step]},
    )
    return state


def _finish_step(config: Config, job_id: str, step: str, status: str, **fields):
    state = load_job(config, job_id)
    info = state["steps"][step]
    if info["status"] != "running":
        raise StudioError(f"步骤 {STEP_LABELS[step]} 不在执行中，无法记录 {status}")
    timestamp = now()
    try:
        elapsed = (
            datetime.fromisoformat(timestamp) - datetime.fromisoformat(info["started_at"])
        ).total_seconds()
    except (TypeError, ValueError):
        elapsed = 0.0
    info.update(status=status, finished_at=timestamp, elapsed_ms=round(elapsed * 1000), **fields)
    state["updated_at"] = timestamp
    record = {"step": step, **info}
    save(attempts_dir(config, job_id) / f"{step}-{info['attempt']}.json", record)
    save(job_path(config, job_id), state)
    return state


def complete_step(config: Config, job_id: str, step: str, result: dict | None = None) -> dict:
    return _finish_step(config, job_id, step, "complete", result=result or {})


def fail_step(config: Config, job_id: str, step: str, exc: Exception) -> dict:
    return _finish_step(config, job_id, step, "failed", error=str(exc)[:2000], error_type=type(exc).__name__)


def recover_stale(config: Config):
    """启动恢复：锁空闲却仍有步骤 running，说明服务重启中断了执行。"""
    root = studio_root(config)
    if not root.is_dir():
        return
    lock = FileLock(str(lock_path(config)), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return  # 有步骤在执行，不动状态
    try:
        for directory in root.iterdir():
            path = directory / "job.json" if directory.is_dir() else None
            if path is None or not path.is_file():
                continue
            try:
                state = json.loads(path.read_text())
            except ValueError:
                continue
            running = [s for s, v in state.get("steps", {}).items() if v.get("status") == "running"]
            if not running and state.get("execution", {}).get("status") != "running":
                continue
            for step in running:
                fail_step(config, directory.name, step, StudioError("服务重启导致步骤中断，请重跑该步骤"))
            set_execution(config, directory.name, "failed", None, [])
    finally:
        lock.release()


# ---------------------------------------------------------------- 产物读写


def write_storyboard(config: Config, job_id: str, storyboard: Storyboard):
    _write_model(storyboard_path(config, job_id), storyboard, updated_at=now())


def load_storyboard(config: Config, job_id: str) -> Storyboard:
    return _read_model(storyboard_path(config, job_id), Storyboard, "尚未生成分镜，请先执行文本拆分镜")


def save_storyboard(config: Config, job_id: str, storyboard: Storyboard) -> dict:
    """人工编辑分镜：保持 split 完成态，作废其下游步骤。"""
    state = load_job(config, job_id)
    if state["steps"]["split"]["status"] != "complete":
        raise StudioError("请先完成文本拆分镜，再编辑分镜")
    prior = load_storyboard(config, job_id) if storyboard_path(config, job_id).is_file() else None
    old = {s.id: s for s in prior.shots} if prior else {}
    new = {s.id: s for s in storyboard.shots}
    structural = old.keys() != new.keys()
    if structural or any(old[k].query != new[k].query for k in old.keys() & new.keys()):
        invalidate(state, ("recall", "select", "render"), "storyboard-query-edited")
    if structural or any(old[k].text != new[k].text for k in old.keys() & new.keys()):
        invalidate(state, ("tts", "select", "render"), "storyboard-text-edited")
    if prior and [s.id for s in prior.shots] != [s.id for s in storyboard.shots]:
        invalidate(state, ("select", "render"), "storyboard-reordered")
    storyboard.coverage_warning = coverage_warning(state["input_text"], [s.text for s in storyboard.shots])
    archive_edit(config, job_id, "storyboard", storyboard_path(config, job_id))
    registry = job_dir(config, job_id) / "checkpoints.json"
    if registry.is_file():
        checkpoints = json.loads(registry.read_text())
        checkpoints.pop("storyboard.json", None)
        save(registry, checkpoints)
    state["updated_at"] = now()
    write_storyboard(config, job_id, storyboard)
    save(job_path(config, job_id), state)
    return state


def write_timeline(config: Config, job_id: str, timeline: Timeline):
    _write_model(timeline_path(config, job_id), timeline, updated_at=now())


def load_timeline(config: Config, job_id: str) -> Timeline:
    return _read_model(timeline_path(config, job_id), Timeline, "尚未生成时间线，请先完成选片对齐")


def save_timeline(config: Config, job_id: str, timeline: Timeline) -> dict:
    """人工换选/微调：保持 select 完成态，仅作废渲染。"""
    state = load_job(config, job_id)
    if state["steps"]["select"]["status"] != "complete":
        raise StudioError("请先完成选片对齐，再调整时间线")
    _archive_downstream(state, "select", "timeline-edited")
    archive_edit(config, job_id, "timeline", timeline_path(config, job_id))
    state["updated_at"] = now()
    write_timeline(config, job_id, timeline)
    save(job_path(config, job_id), state)
    return state


def coverage_warning(original: str, shot_texts: list[str]) -> str | None:
    def normalize(value):
        return re.sub(r"\s+", "", value)

    if normalize(original) != normalize("".join(shot_texts)):
        return "分镜文本未按顺序完整覆盖原文，请核对遗漏、重复或改写内容"
    return None


def invalidate(state: dict, steps, reason: str):
    for target in steps:
        if state["steps"][target]["status"] != "pending":
            state["history"].append(
                {"step": target, "reason": reason, "at": now(), "previous": state["steps"][target]}
            )
            state["steps"][target] = fresh_step()


def archive_edit(config, job_id, name, path):
    if path.is_file():
        directory = attempts_dir(config, job_id) / f"edit-{name}-{uuid4().hex[:12]}"
        directory.mkdir(parents=True)
        shutil.copy2(path, directory / path.name)


def set_execution(config, job_id, status, current_step, remaining_steps):
    state = load_job(config, job_id)
    error = state.get("execution", {}).get("error")
    state["execution"] = {"status": status, "current_step": current_step, "remaining_steps": remaining_steps}
    if status == "failed" and error:
        state["execution"]["error"] = error
    state["updated_at"] = now()
    save(job_path(config, job_id), state)
    return state


def step_configuration(config, state, step):
    studio = config.studio
    values = {
        "split": {"generator": studio.generator.model_dump(), "version": SPLIT_VERSION},
        "recall": {
            "recall": studio.recall.model_dump(),
            "search": config.search.model_dump(),
            "embedding": config.embedding.model_dump(),
            "video": config.video.model_dump(),
            "limit": state["options"]["recall_limit"],
        },
        "tts": {"tts": studio.tts.model_dump(), "voice": state["options"]["voice"]},
        "select": studio.selection.model_dump(),
        "render": {
            "render": studio.render.model_dump(),
            "audio": studio.audio.model_dump(),
            "subtitle": studio.subtitle.model_dump(),
        },
    }
    return fingerprint(values[step])


def reconcile(config, job_id):
    """仅在持执行锁时迁移旧任务并使变化后的配置失效。"""
    state = load_job(config, job_id)
    changed = False
    if state.get("artifact_version") != 1:
        invalidate(state, ("recall", "tts", "select", "render"), "legacy-unverified")
        state["artifact_version"] = 1
        changed = True
    for step in STEPS:
        info = state["steps"][step]
        if info.get("configuration") and info["configuration"] != step_configuration(config, state, step):
            invalidate(state, (step, *invalidation_targets(step)), "configuration-changed")
            changed = True
    if changed:
        state["updated_at"] = now()
        save(job_path(config, job_id), state)
    return state
