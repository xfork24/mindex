"""构建方案：方案选项、音色、文案与分镜编辑、版本选用，以及分镜工作台的快照与还原。"""

from __future__ import annotations

import copy
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from mindex.shared.config import StrictModel
from mindex.shared.types import SCRIPT_TEMPLATE_ID
from mindex.studio import studio

from .stages import shot_stages
from .versions import add_version, plan, version


class ReferenceRef(StrictModel):
    """方案登记的参考文稿：文件在任务 ``inputs/<id>.txt``，id 是规范化 UTF-8 正文的 sha256。"""

    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=200)
    chars: int = Field(ge=1)


DEFAULT_CANVAS = ("16:9", "pad")


class PlanOptions(StrictModel):
    voice: str | None = Field(default=None, min_length=1, max_length=100)
    voice_mode: Literal["inherit", "custom"] = "inherit"
    recall_limit: int = Field(default=20, ge=1, le=50)
    audio_mode: str = Field(default="tts_only", pattern="^(tts_only|ducking)$")
    burn: bool = True
    atempo: float | None = Field(default=1.0, ge=0.75, le=1.25)
    alignment: str = Field(default="auto", pattern="^(auto|freeze|strict)$")
    duck_gain: float = Field(default=0.15, gt=0, le=1)
    # 文案模板（studio/script_templates.py）；没有该键的旧方案按 base 处理。存在性由 API 校验。
    template_id: str = Field(default="base", pattern=SCRIPT_TEMPLATE_ID)
    # 参考文稿登记只经 /references 接口增删；PATCH 方案参数时服务端保留现有登记。
    references: list[ReferenceRef] = Field(default_factory=list, max_length=50)
    # 画幅与适配方式：没有这两个键的旧方案等价 16:9 + pad。
    canvas: Literal["16:9", "9:16", "1:1"] = "16:9"
    fit: Literal["pad", "crop"] = "pad"

    @model_validator(mode="before")
    @classmethod
    def explicit_voice(cls, value):
        # Older API callers that explicitly supply a voice still request an override.
        if isinstance(value, dict) and "voice_mode" not in value and value.get("voice"):
            return {**value, "voice_mode": "custom"}
        return value

    @model_validator(mode="after")
    def validate_voice(self):
        if self.voice_mode == "custom" and not self.voice:
            raise ValueError("指定音色模式需要填写音色")
        if self.voice_mode == "inherit":
            self.voice = None
        return self


def normalize_voice_options(p):
    if "voice_mode" not in p["options"]:
        # Old workspaces copied the default into every plan without recording intent.
        # Keep the old value for reference, but do not treat it as an explicit override.
        p.setdefault("legacy_voice", p["options"].get("voice"))
        p["options"] = {**p["options"], "voice": None, "voice_mode": "inherit"}


def canvas_of(p) -> tuple[str, str]:
    """方案画幅与适配方式；没有这两个键的旧方案等价 16:9 + pad。"""
    options = p.get("options") or {}
    return options.get("canvas") or DEFAULT_CANVAS[0], options.get("fit") or DEFAULT_CANVAS[1]


def canvas_render(config, p):
    """方案生效的渲染配置（宽高按画幅）与适配方式；默认画幅原样返回 ``config.studio.render``。"""
    canvas, fit = canvas_of(p)
    render = config.studio.render
    if canvas != DEFAULT_CANVAS[0]:
        width, height = render.size(canvas)
        render = render.model_copy(update={"width": width, "height": height})
    return render, fit


def effective_voice(config, p):
    if p["options"].get("voice_mode", "inherit") == "custom":
        voice = p["options"].get("voice")
        if not voice:
            raise studio.StudioError("指定音色模式需要填写音色")
        return voice
    return config.studio.tts.voice


def new_plan(name, options):
    return {
        "id": uuid4().hex[:12],
        "name": name,
        "created_at": studio.now(),
        "board": None,
        "shots": {},
        "options": PlanOptions.model_validate(options).model_dump(),
        "ranges": {},
        "anchors": {},
        "final": None,
        # 当前文案版本，以及当前分镜由哪个文案版本拆出；两者不同即分镜已过期。
        "script": None,
        "board_script": None,
    }


def remove_plan(w, plan_id):
    """从工作台删除方案记录，不触碰版本与构建记录；删的是当前方案时切到其余方案中最新创建的。"""
    plan(w, plan_id)
    if len(w["plans"]) == 1:
        raise studio.StudioError("至少保留一个方案")
    w["plans"].pop(plan_id)
    if w["active_plan"] == plan_id:
        w["active_plan"] = max(w["plans"].values(), key=lambda p: p.get("created_at") or "")["id"]
    return w["active_plan"]


def script_text(w, p):
    vid = p.get("script")
    return version(w, vid)["data"]["text"] if vid else None


def script_stale(p):
    """分镜是从别的文案拆出来的：保留产物，但提示并在构建时重新拆分镜。"""
    return bool(p.get("board")) and p.get("board_script") != p.get("script")


def add_script(config, job_id, w, p, text, origin):
    text = (text or "").strip()
    if not text:
        raise studio.StudioError("文案不能为空")
    if len(text) > config.studio.max_input_chars:
        raise studio.StudioError(f"文案超过 {config.studio.max_input_chars} 字上限，请精简后再试")
    if p.get("script") and script_text(w, p) == text:
        return p["script"]
    p["script"] = add_version(config, job_id, w, "script", data={"text": text, "origin": origin})
    return p["script"]


def set_board(config, job_id, w, p, shots, *, origin=None):
    board = studio.Storyboard(job_id=job_id, shots=shots)
    if len(board.shots) > config.studio.max_shots:
        raise studio.StudioError("分镜数量超过上限")
    selected = {}
    recall_free = set()  # 非素材召回来源：没有人工区间与参考锚点
    content_ids = []
    for shot in board.shots:
        old = p["shots"].get(shot.id, {})
        content = w["versions"].get(old.get("content"))
        if not content or content["data"] != shot.model_dump():
            cid = add_version(
                config,
                job_id,
                w,
                "content",
                shot.id,
                data=shot.model_dump(),
                inputs={"split": origin} if origin else {},
            )
        else:
            cid = content["id"]
        # 来源变化后不再属于该分镜的阶段（例如改为图片后的召回与选片）不保留选用。
        sequence = shot_stages(shot.model_dump())
        selected[shot.id] = {
            key: value for key, value in {**old, "content": cid}.items() if key == "content" or key in sequence
        }
        if "recall" not in sequence:
            recall_free.add(shot.id)
        content_ids.append(cid)
    bid = add_version(
        config,
        job_id,
        w,
        "board",
        data={"order": [s.id for s in board.shots]},
        inputs={str(i): cid for i, cid in enumerate(content_ids)},
    )
    p.update(board=bid, shots=selected)
    # Ranges belonging to removed shots remain in historical plans/versions only.
    p["ranges"] = {sid: value for sid, value in p["ranges"].items() if sid in selected and sid not in recall_free}
    p["anchors"] = {
        sid: value for sid, value in p.get("anchors", {}).items() if sid in selected and sid not in recall_free
    }


def needs_recall(p, sid):
    return sid not in p["ranges"] or bool(p["ranges"][sid].get("anchor_ref"))


BOARD_FIELDS = ("board", "board_script", "shots", "ranges", "anchors")


def board_snapshot(p):
    """本次构建完成后的分镜工作台状态：供日后切回对应文案版本时整体还原。

    只有分镜确实从当前文案版本拆出（``board_script == script``）才记录分镜内容；
    否则（例如本次只重新生成了文案、尚未拆分镜）记录为空工作台。
    """
    derived = bool(p.get("board")) and p.get("board_script") == p.get("script")
    return {
        "script": p.get("script"),
        "board": p.get("board") if derived else None,
        "board_script": p.get("board_script") if derived else None,
        "shots": copy.deepcopy(p.get("shots", {})) if derived else {},
        "ranges": copy.deepcopy(p.get("ranges", {})) if derived else {},
        "anchors": copy.deepcopy(p.get("anchors", {})) if derived else {},
    }


def empty_board(p):
    """清空分镜工作台：没有依赖此文案版本的构建时，分镜与其选用全部归零。"""
    p["board"] = None
    p["board_script"] = None
    p["shots"] = {}
    p["ranges"] = {}
    p["anchors"] = {}


def restore_script_board(w, p, vid):
    """切换当前文案版本，并把分镜工作台还原为依赖此版本的最后一次构建结果。

    没有任何从此文案版本拆出分镜的构建时，分镜工作台整体清空（该版本文案还没有处理）。
    构建结果快照只保留分镜选用，对应的产物版本仍在 ``w["versions"]`` 中，可照常查看与切换。
    """
    version(w, vid)  # 校验版本存在
    p["script"] = vid
    snapshot = next(
        (
            b["board_result"]
            for b in reversed(w["builds"])
            if b["plan_id"] == p["id"]
            and b.get("board_result", {}).get("script") == vid
            and b.get("board_result", {}).get("board")
        ),
        None,
    )
    if snapshot is None:
        empty_board(p)
        return
    for key in BOARD_FIELDS:
        p[key] = copy.deepcopy(snapshot.get(key))
    p.setdefault("anchors", {})


def choose(w, p, stage, sid, vid):
    item = version(w, vid)
    if item["status"] != "complete" or item["stage"] != stage or item["shot_id"] != sid:
        raise studio.StudioError("只能选用属于此阶段和分镜的成功版本")
    if stage == "compose":
        p["final"] = vid
    elif stage == "script":
        # 选用新的文案版本：若现有分镜不是从它拆出的，分镜工作台立即清空
        # （该版本文案尚未处理）。整体构建会紧接着重新拆分镜；切回旧版本由
        # restore_script_board 还原其最后一次构建结果。
        if p.get("board_script") != vid:
            empty_board(p)
        p["script"] = vid
    else:
        if sid not in p["shots"]:
            raise studio.StudioError("当前方案没有此分镜")
        if stage == "recall" and p["shots"][sid].get("recall") not in (None, vid):
            # A new candidate pool starts a new automatic selection, bound to
            # this recall rather than a previous anchor or explicit window.
            p.setdefault("anchors", {})[sid] = {"mode": "auto"}
            p["ranges"].pop(sid, None)
        p["shots"][sid][stage] = vid
        if stage == "select":
            if item["data"].get("manual"):
                p["ranges"][sid] = item["data"]["range"]
                if item["data"].get("anchor_choice"):
                    p.setdefault("anchors", {})[sid] = item["data"]["anchor_choice"]
            else:
                p["ranges"].pop(sid, None)


def restore(w, vid, name):
    item = version(w, vid)
    snapshot = item["data"].get("plan")
    if item["stage"] != "compose" or not snapshot:
        raise studio.StudioError("此成片记录不完整，无法还原构建方案；原文件仍可下载")
    p = copy.deepcopy(snapshot)
    normalize_voice_options(p)
    p.setdefault("anchors", {})
    if "script" not in p:  # 文案阶段之前的成片快照：分镜来自旧任务原文
        legacy = next((v["id"] for v in w["versions"].values()
                       if v["stage"] == "script" and v["data"].get("origin") == "legacy"), None)
        p["script"] = legacy
        p["board_script"] = legacy if p.get("board") else None
    p.update(id=uuid4().hex[:12], name=name, created_at=studio.now(), final=vid)
    w["plans"][p["id"]] = p
    w["active_plan"] = p["id"]
    return p
