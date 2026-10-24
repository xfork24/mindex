"""创作 API 的请求体模型与召回引用校验。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mindex.shared.config import StrictModel
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import studio_workspace as workspace


class CreateJobBody(StrictModel):
    text: str  # 视频大纲；文案在任务内由文案创作模型生成
    title: str | None = Field(default=None, max_length=60)
    options: dict | None = None


class ShotBody(StrictModel):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60)
    recall: studio.ShotRecall | None = None  # 与 Shot 严格同步（StrictModel extra=forbid）
    granularity: studio.Granularity | None = None
    recall_limit: int | None = Field(default=None, ge=1, le=50)
    # 只约束类型；长度由处理函数用 studio.visual_error 检查，保证 422 detail 为中文字符串。
    visual: str | None = None


def validate_recall_references(config, store: Store, job_id, shots):
    """PATCH 级校验：人物必须已登记且非作品；查询文件必须已存在于 inputs/（执行前另有 digest 复验）；素材不得越出任务范围。"""
    from mindex.persons.persons import Persons

    catalogue = Persons(store)
    inputs = studio.job_dir(config, job_id) / "inputs"
    scope = studio.material_scope(studio.load_job(config, job_id))
    for shot in shots:
        recall = shot.recall
        if not recall:
            continue
        for identifier in recall.person_ids:
            entity = catalogue.entity(identifier)
            if entity["kind"] == "work":
                raise studio.StudioError("人物召回不能选择作品，请选择演员或角色")
        outside = recall.filters.selected_sources() - scope if scope else set()
        if outside:
            raise studio.StudioError(
                f"素材超出本任务允许的素材范围：{'、'.join(sorted(outside)[:3])}"
            )
        if recall.mode in {"image", "video"} and not any(
            path.is_file() and path.suffix != ".partial" for path in inputs.glob(recall.media_id + ".*")
        ):
            raise studio.StudioError("查询文件缺失，请先上传查询图片或视频")


class StoryboardBody(StrictModel):
    title: str | None = Field(default=None, max_length=60)
    shots: list[ShotBody] = Field(min_length=1)


class QuerySuggestionBody(StrictModel):
    # 省略时使用该分镜已保存的画面意象；只约束类型，空值与长度由处理函数给出中文 422。
    visual: str | None = None


class ManualVideoBody(StrictModel):
    segment_id: str | None = None
    source_id: str | None = None
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def one_choice(self):
        explicit = any(value is not None for value in (self.source_id, self.start, self.end))
        if self.segment_id and explicit:
            raise ValueError("候选片段和人工区间不能同时提供")
        if not self.segment_id and not all(
            value is not None for value in (self.source_id, self.start, self.end)
        ):
            raise ValueError("请选择候选片段，或提供完整人工区间")
        return self


class RunStepBody(StrictModel):
    mode: Literal["resume", "force"] = "resume"
    shot_ids: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def unique_shots(self):
        if self.shot_ids is not None and (
            self.mode != "force" or len(self.shot_ids) != len(set(self.shot_ids))
        ):
            raise ValueError("指定分镜必须使用 force 模式，且分镜不能重复")
        return self


class EventBody(StrictModel):
    shot_id: str
    video: ManualVideoBody


class TimelineBody(StrictModel):
    events: list[EventBody] = Field(min_length=1)
    audio_mode: Literal["tts_only", "ducking"] | None = None
    burn: bool | None = None


class TitleBody(StrictModel):
    title: str = Field(min_length=1, max_length=60)


class PlanBody(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    copy_from: str | None = None
    from_final: str | None = None


class PlanPatchBody(StrictModel):
    revision: str
    name: str | None = Field(default=None, min_length=1, max_length=60)
    options: workspace.PlanOptions | None = None
    shots: list[ShotBody] | None = Field(default=None, min_length=1)
    script: str | None = Field(default=None, max_length=20000)  # 手动编辑的文案；字数上限按配置校验


class VersionChoiceBody(StrictModel):
    revision: str
    version_id: str
    stage: Literal["script", "content", "recall", "tts", "select", "render", "compose"]
    shot_id: str | None = None


class BuildBody(StrictModel):
    token: str
    force: bool = False


class NodeBody(StrictModel):
    revision: str
    inputs: dict[str, str | None]
    stage: Literal["script", "split", "recall", "tts", "select", "render"]
    shot_id: str | None = None
    force: bool = False


class ColumnBody(StrictModel):
    force: bool = True
    revision: str
    stage: Literal["split", "recall", "tts", "select", "render"]
    inputs_by_shot: dict[str, dict[str, str | None]]


class RangeBody(StrictModel):
    revision: str
    shot_id: str
    source_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    segment_ids: list[str] | None = Field(default=None, min_length=1)
    preview_token: str | None = None


class AnchorBody(StrictModel):
    revision: str
    shot_id: str
    mode: Literal["auto", "manual"]
    recall_version: str | None = None
    candidate_id: str | None = None


class WindowBody(StrictModel):
    revision: str
    shot_id: str
    context_token: str
    anchor_ref: dict[str, str]
    tts_version: str
    start: float = Field(ge=0, allow_inf_nan=False)
    preview_token: str | None = None
    render_preview: bool = True
