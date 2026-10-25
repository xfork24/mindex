"""创作 API 的请求体模型与召回引用校验。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mindex.shared.config import StrictModel
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import workspace as workspace
from mindex.studio.workspace import stages as stage_registry

# 阶段取值由注册表生成（导入时固定）；顺序与取值同注册顺序：plan → shot → final。
# 版本选用另含 content（分镜内容版本），插在方案级阶段之后。
VERSION_STAGES = (
    *stage_registry.names(scope="plan", flag="choosable"),
    "content",
    *stage_registry.names(scope="shot", flag="choosable"),
    *stage_registry.names(scope="final", flag="choosable"),
)
NODE_STAGES = stage_registry.names(flag="buildable")
# picture 是虚拟的画面列：各分镜的召回 / 生成图片 / 生成视频 / 生成动画（见 stages.PICTURE_STAGES）。
COLUMN_STAGES = (*stage_registry.names(flag="column"), stage_registry.PICTURE_COLUMN)
VersionStage = Literal[VERSION_STAGES]  # type: ignore[valid-type]
NodeStage = Literal[NODE_STAGES]  # type: ignore[valid-type]
ColumnStage = Literal[COLUMN_STAGES]  # type: ignore[valid-type]


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
    source: studio.ShotSource | None = None  # 画面来源；None/recall = 素材召回
    music: studio.ShotMusic | None = None  # 背景音乐提示点；None = 顺延前面分镜


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


SOURCE_KIND_LABELS = {"clip": "视频片段", "image": "图片"}


def validate_source_references(config, job_id, shots):
    """PATCH 级校验：clip/image 来源的文件、aigc 来源的参考图必须已上传到 inputs/ 且类型匹配
    （执行前另有摘要复验）；animation 来源没有文件。"""
    for index, shot in enumerate(shots, 1):
        music = shot.music
        if music is not None and music.audio_id and not workspace.sources.present(
            config, job_id, music.audio_id, "audio"
        ):
            raise studio.StudioError(f"第 {index} 个分镜的翻唱参考音频缺失或类型不符，请先上传音频")
        source = shot.source
        if source is None or source.kind in ("recall", "animation"):
            continue
        if source.kind in ("aigc", "aigc_image"):
            reason = workspace.sources.missing_reference(config, job_id, source.model_dump())
            if reason:
                noun = "参考图片缺失或类型不符，请先上传参考图" if reason == workspace.sources.REFERENCE_MISSING \
                    else "参考素材缺失或类型不符，请先上传参考素材"
                raise studio.StudioError(f"第 {index} 个分镜的{noun}")
            continue
        if not workspace.sources.present(config, job_id, source.media_id, source.kind):
            raise studio.StudioError(
                f"第 {index} 个分镜的{SOURCE_KIND_LABELS[source.kind]}文件缺失或类型不符，请先上传画面文件"
            )


class AnimationBody(StrictModel):
    """人工保存动画源码：整份替换，经静态校验后成为该分镜选用的 origin=manual 动画版本。"""

    revision: str
    html: str = Field(min_length=1, max_length=10_000_000)
    dry_run: bool = False  # 只做静态校验并返回结果，不写版本（智能助手预览用）


class StoryboardBody(StrictModel):
    title: str | None = Field(default=None, max_length=60)
    shots: list[ShotBody] = Field(min_length=1)


class LyricsBody(StrictModel):
    """背景音乐歌词生成：write_full_song 按描述写完整歌词；edit 按指令修改/续写已有歌词。"""

    prompt: str | None = Field(default=None, max_length=2000)
    mode: Literal["write_full_song", "edit"] = "write_full_song"
    lyrics: str | None = Field(default=None, max_length=3500)
    title: str | None = Field(default=None, max_length=100)


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


class ReferenceBody(StrictModel):
    """粘贴的参考文稿；总字数上限按 studio.max_reference_chars 校验。"""

    revision: str
    name: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=1_000_000)


class VersionChoiceBody(StrictModel):
    revision: str
    version_id: str
    stage: VersionStage
    shot_id: str | None = None


class BuildBody(StrictModel):
    token: str
    force: bool = False


class NodeBody(StrictModel):
    revision: str
    inputs: dict[str, str | None]
    stage: NodeStage
    shot_id: str | None = None
    force: bool = False


class ColumnBody(StrictModel):
    force: bool = True
    revision: str
    stage: ColumnStage
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
