"""素材领域的任务与上传工具：发起、查询、重试、取消素材处理任务，上传素材与字幕。

任务类别与接口（与素材工作台、人物库页面同一套）：

- ``source``：分镜、分析、发布（``/api/admin/source-tasks/{id}``）；
- ``dialogue``：对白（``/api/admin/dialogue-tasks/{id}``）；
- ``person``：人物处理（``/api/persons/tasks/{id}``）。

发起或重试成功后由运行时跟踪任务（``ToolOutput.watch_task``），终态时发 followup。
不提供删除素材的工具（物理删除，Req 11.5）。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import check, clip
from agent.tools.library import LIBRARY, TASK_STATUS, WORK_NAMES, admin_focus
from agent.tools.search import ATTACHMENT_PREFIX, upload_check
from shared.config import StrictModel
from shared.task_progress import RETRYABLE

TaskKind = Literal["source", "dialogue", "person", "index"]
Work = Literal["shots", "analysis", "dialogue", "persons", "publish"]
PublishTarget = Literal["shots", "dialogue", "analysis", "persons"]
# 任务类别 → (任务接口前缀, 中文名)。
TASK_PATHS = {
    "source": ("/api/admin/source-tasks", "素材任务"),
    "dialogue": ("/api/admin/dialogue-tasks", "对白任务"),
    "person": ("/api/persons/tasks", "人物任务"),
    "index": ("/api/index-tasks", "索引任务"),
}
WORK_LABELS = {**WORK_NAMES, "publish": "发布检索"}
# 工作 → (发起接口模板, 任务类别)。
WORK_ROUTES = {
    "shots": ("/api/admin/sources/{}/shots", "source"),
    "analysis": ("/api/admin/sources/{}/analysis", "source"),
    "dialogue": ("/api/admin/sources/{}/dialogue", "dialogue"),
    "persons": ("/api/persons/sources/{}/process", "person"),
    "publish": ("/api/admin/sources/{}/publish", "source"),
}
# 各工作接受的选项（参数名 → 请求体字段名）；与对应接口的选项模型一致，原样透传。
WORK_OPTIONS = {
    "shots": {"publish": "publish"},
    "analysis": {"publish": "publish"},
    "dialogue": {
        "publish": "publish",
        "method": "method",
        "subtitle_stream": "subtitle_stream",
        "subtitle_file": "subtitle_file",
        "workers": "workers",
    },
    "persons": {"publish": "publish", "work_id": "work_id", "start": "start", "end": "end"},
    "publish": {"target": "work", "scope": "scope"},
}
OPTION_FIELDS = sorted({name for names in WORK_OPTIONS.values() for name in names})
OPTION_LABELS = {
    "publish": "完成后自动发布检索",
    "method": "对白来源",
    "subtitle_stream": "内嵌字幕轨道",
    "subtitle_file": "上传字幕 ID",
    "workers": "并发数",
    "work_id": "关联作品",
    "start": "起点（秒）",
    "end": "终点（秒）",
    "target": "发布的工作",
    "scope": "发布范围",
}
METHOD_LABELS = {"auto": "自动（优先字幕）", "subtitle": "字幕", "asr": "语音识别"}
SCOPE_LABELS = {"shot_moment": "镜头与精细片段", "scene": "场景"}
GPU_WORKS = {"shots", "analysis", "dialogue", "persons"}


def task_path(kind: str, task_id: str, action: str | None = None) -> str:
    return f"{TASK_PATHS[kind][0]}/{task_id}" + (f"/{action}" if action else "")


def relay(result: dict, what: str) -> object:
    """任务接口的 409 / 422 原样转述服务端原因（GPU 锁、前置条件、状态限制），其余交给 ``check``。"""
    if not result["ok"] and result["status"] in (409, 422):
        status = result["status"]
        raise ToolError(
            f"{what}失败（HTTP {status}）：{result['error']}",
            code=f"http_{status}",
            value={"status": status, "error": result["error"]},
        )
    return check(result, what)


def task_work(kind: str, task: dict) -> str | None:
    if kind == "dialogue":
        return "dialogue"
    if kind == "person":
        return "persons"
    return task.get("work")


def watch_target(kind: str, task: dict) -> dict:
    target = {"kind": kind, "task_id": task["id"]}
    if task.get("source_id"):
        target["source_id"] = task["source_id"]
    work = task_work(kind, task)
    if work:
        target["work"] = work
    return target


def compact_task(kind: str, task: dict) -> dict:
    value = {
        k: task.get(k)
        for k in (
            "id",
            "source_id",
            "status",
            "error",
            "progress",
            "options",
            "publication",
            "created_at",
            "updated_at",
        )
        if k in task
    }
    value["kind"] = kind
    value["work"] = task_work(kind, task)
    return value


def status_label(status: str | None) -> str:
    return TASK_STATUS.get(status, status or "未知")


def progress_text(progress) -> str | None:
    if not isinstance(progress, dict) or not progress:
        return None
    parts = [str(progress["stage"])] if progress.get("stage") else []
    if progress.get("total"):
        parts.append(
            f"{progress.get('completed') or 0}/{progress['total']}"
            + (f" {progress['unit']}" if progress.get("unit") else "")
        )
    if progress.get("message"):
        parts.append(clip(str(progress["message"]), 80))
    return "，".join(parts) or None


def task_lines(kind: str, task: dict) -> list[str]:
    work = task_work(kind, task)
    lines = [
        f"{TASK_PATHS[kind][1]} {task['id']}｜素材 {task.get('source_id') or '未知'}"
        + (f"｜工作 {WORK_LABELS.get(work, work)}" if work else "")
        + f"｜状态 {status_label(task.get('status'))}"
    ]
    if task.get("status") in ("queued", "running"):
        progress = progress_text(task.get("progress"))
        if progress:
            lines.append(f"进度：{progress}")
    if task.get("error"):
        lines.append(f"错误：{clip(str(task['error']), 300)}")
    publication = task.get("publication")
    if isinstance(publication, dict) and publication.get("status") not in (None, "not_requested"):
        line = f"检索发布：{status_label(publication['status'])}"
        if publication.get("task_ids"):
            line += f"，已链式创建发布任务 {', '.join(publication['task_ids'])}（kind=source）"
        if publication.get("error"):
            line += f"，{clip(str(publication['error']), 120)}"
        lines.append(line)
    return lines


def retryable(task: dict) -> bool:
    publication = task.get("publication") if isinstance(task.get("publication"), dict) else {}
    return task.get("status") in RETRYABLE or publication.get("status") == "failed"


# ---------- start_source_task ----------


class StartSourceTask(StrictModel):
    source_id: str = Field(min_length=1, max_length=200, description="素材 ID（list_sources 获取）")
    work: Work = Field(
        description="shots 分镜；analysis LLM 分析；dialogue 对白；persons 人物处理；publish 发布检索"
    )
    publish: bool | None = Field(default=None, description="处理完成后自动发布检索（work≠publish 时可用）")
    method: Literal["auto", "subtitle", "asr"] | None = Field(
        default=None, description="对白来源（仅 dialogue）：auto 优先字幕；subtitle 字幕；asr 语音识别"
    )
    subtitle_stream: int | None = Field(
        default=None, ge=0, description="内嵌字幕轨道序号（仅 dialogue，method=subtitle）"
    )
    subtitle_file: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description="upload_subtitle 返回的字幕 ID（仅 dialogue，method=subtitle）",
    )
    workers: int | None = Field(default=None, ge=1, le=8, description="对白并发数（仅 dialogue）")
    work_id: str | None = Field(
        default=None, max_length=200, description="关联作品 ID（仅 persons；会做资料预检）"
    )
    start: float | None = Field(default=None, ge=0, description="处理区间起点秒（仅 persons）")
    end: float | None = Field(default=None, ge=0, description="处理区间终点秒，0 表示至片尾（仅 persons）")
    target: PublishTarget | None = Field(default=None, description="要发布检索的工作（work=publish 时必填）")
    scope: Literal["shot_moment", "scene"] | None = Field(
        default=None,
        description="发布范围（仅 publish）：shot_moment 镜头与精细片段；scene 场景（分镜、对白、分析）",
    )

    @model_validator(mode="after")
    def applicable(self):
        allowed = WORK_OPTIONS[self.work]
        stray = [name for name in OPTION_FIELDS if getattr(self, name) is not None and name not in allowed]
        if stray:
            raise ValueError(f"work={self.work} 不接受参数：{', '.join(stray)}")
        if self.work == "publish" and self.target is None:
            raise ValueError("work=publish 时必须给出 target（要发布检索的工作）")
        return self


def task_body(args: StartSourceTask) -> dict:
    return {
        field: getattr(args, name)
        for name, field in WORK_OPTIONS[args.work].items()
        if getattr(args, name) is not None
    }


def option_value(name: str, value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if name == "method":
        return METHOD_LABELS.get(value, value)
    if name == "scope":
        return SCOPE_LABELS.get(value, value)
    if name == "target":
        return WORK_LABELS.get(value, value)
    if name == "end" and not value:
        return "片尾"
    return str(value)


async def preview_start_source_task(args: StartSourceTask, ctx: ToolContext) -> Preview:
    source = check(await ctx.client.get(f"/api/admin/sources/{args.source_id}", detail="work"), "读取素材")
    work_key = args.target if args.work == "publish" else args.work
    info = (source.get("work") or {}).get(work_key) or {}
    rows = [{"label": "工作", "after": WORK_LABELS[args.work]}]
    rows += [
        {"label": OPTION_LABELS[name], "after": option_value(name, getattr(args, name))}
        for name in WORK_OPTIONS[args.work]
        if getattr(args, name) is not None
    ]
    warnings = []
    if args.work in GPU_WORKS:
        warnings.append("这是 GPU 处理任务：会占用 GPU 独占锁与该素材的处理锁，耗时可能较长")
    if args.work != "shots" and not source.get("prepared"):
        warnings.append("该素材尚未完成分镜处理，服务端会拒绝启动；请先 start_source_task(work=shots)")
    if args.work == "publish":
        warnings.append("发布会生成新的内容索引版本或人物检索组合，并切换当前生效的检索")
        can = info.get("can_publish_scene" if args.scope == "scene" else "can_publish")
        if can is False:
            warnings.append(f"「{WORK_LABELS[work_key]}」目前没有可发布的处理结果，服务端可能拒绝")
    if args.publish:
        warnings.append("处理完成后会自动创建发布检索子任务（独立任务，取消本任务不会取消已创建的子任务）")
    if args.work == "persons":
        setup = await ctx.client.get(
            f"/api/admin/sources/{args.source_id}/person-setup", work_id=args.work_id
        )
        if setup["ok"] and not setup["data"].get("ready") and (args.work_id or setup["data"].get("work_id")):
            warnings.append("人物资料预检未通过：" + "；".join(setup["data"].get("missing") or []))
        if args.work_id:
            warnings.append("会把该素材归属到所选作品")
    if info.get("can_start") is False and info.get("start_reason"):
        warnings.append(f"当前不可启动：{info['start_reason']}")
    task = info.get("task") or {}
    if task.get("status") in ("queued", "running"):
        warnings.append(
            f"该工作已有{status_label(task['status'])}的任务 {task['id']}，再次启动可能因锁冲突被拒绝"
        )
    return Preview(
        f"对素材「{source['name']}」发起{WORK_LABELS[args.work]}任务",
        rows,
        warnings=warnings,
        focus=admin_focus(args.source_id),
    )


async def start_source_task(args: StartSourceTask, ctx: ToolContext) -> ToolOutput:
    path, kind = WORK_ROUTES[args.work]
    task = relay(
        await ctx.client.post(path.format(args.source_id), task_body(args)),
        f"发起{WORK_LABELS[args.work]}任务",
    )
    target = watch_target(kind, task)
    lines = task_lines(kind, task)
    lines.append(
        f"已提交，系统会跟踪进度并在结束时通知你；也可用 get_task_status(kind={kind}, task_id={task['id']}) "
        f"查询。"
    )
    return ToolOutput(
        compact_task(kind, task),
        text="\n".join(lines),
        meta={"kind": "task", **target},
        mutation={**LIBRARY, "source_id": args.source_id, "task_id": task["id"]},
        watch_task=target,
        focus=admin_focus(args.source_id),
    )


# ---------- get_task_status / retry_task / cancel_task ----------


class TaskRef(StrictModel):
    kind: TaskKind = Field(
        description="source 分镜/分析/发布任务；dialogue 对白任务；person 人物任务；index 独立索引任务"
    )
    task_id: str = Field(min_length=1, max_length=200)


async def read_task(args: TaskRef, ctx: ToolContext) -> dict:
    return relay(await ctx.client.get(task_path(args.kind, args.task_id)), f"读取{TASK_PATHS[args.kind][1]}")


async def get_task_status(args: TaskRef, ctx: ToolContext) -> ToolOutput:
    task = await read_task(args, ctx)
    lines = task_lines(args.kind, task)
    value = compact_task(args.kind, task)
    if task.get("status") == "complete" and task.get("result") is not None:
        value["result"] = task["result"]
    if task.get("status") in ("queued", "running"):
        lines.append("任务仍在进行中；需要停止可用 cancel_task。")
    elif retryable(task):
        lines.append(f"可用 retry_task(kind={args.kind}) 从断点重试。")
    return ToolOutput(value, text="\n".join(lines), focus=admin_focus(task.get("source_id")))


async def preview_retry_task(args: TaskRef, ctx: ToolContext) -> Preview:
    task = await read_task(args, ctx)
    work = task_work(args.kind, task)
    rows = [{"label": "状态", "before": status_label(task.get("status")), "after": "排队中"}]
    warnings = []
    if work in GPU_WORKS:
        warnings.append("重试会再次占用 GPU 独占锁，并从已有检查点继续")
    if task.get("error"):
        warnings.append(f"上次错误：{clip(str(task['error']), 200)}")
    if not retryable(task):
        warnings.append(
            f"当前状态为{status_label(task.get('status'))}，只有失败、中断、已取消或检索发布失败的任务可以重试，"
            "服务端会拒绝"
        )
    return Preview(
        f"重试{TASK_PATHS[args.kind][1]} {task['id']}（{WORK_LABELS.get(work, work or '未知工作')}）",
        rows,
        warnings=warnings,
        focus=admin_focus(task.get("source_id")),
    )


async def retry_task(args: TaskRef, ctx: ToolContext) -> ToolOutput:
    task = relay(
        await ctx.client.post(task_path(args.kind, args.task_id, "retry"), {}),
        f"重试{TASK_PATHS[args.kind][1]}",
    )
    target = watch_target(args.kind, task)
    lines = task_lines(args.kind, task) + ["已重新提交，结束时会通知你。"]
    return ToolOutput(
        compact_task(args.kind, task),
        text="\n".join(lines),
        meta={"kind": "task", **target},
        mutation={**LIBRARY, "source_id": task.get("source_id"), "task_id": task["id"]},
        watch_task=target,
        focus=admin_focus(task.get("source_id")),
    )


async def preview_cancel_task(args: TaskRef, ctx: ToolContext) -> Preview:
    task = await read_task(args, ctx)
    work = task_work(args.kind, task)
    status = task.get("status")
    rows = [{"label": "状态", "before": status_label(status), "after": "已取消"}]
    warnings = []
    if status == "running":
        warnings.append(
            "任务会在下一次上报进度（或等待 GPU 准入）时停止；已完成的检查点保留，之后可用 retry_task 续跑"
        )
    elif status != "queued":
        warnings.append(f"当前状态为{status_label(status)}，只有排队中或运行中的任务可以取消，服务端会拒绝")
    publication = task.get("publication") if isinstance(task.get("publication"), dict) else {}
    if publication.get("task_ids"):
        warnings.append("不会取消已链式创建的发布子任务：" + ", ".join(publication["task_ids"]))
    if work == "publish":
        warnings.append("取消发布不会改变当前生效的索引；未完成的索引版本可在重试时续建")
    return Preview(
        f"取消{TASK_PATHS[args.kind][1]} {task['id']}（{WORK_LABELS.get(work, work or '未知工作')}）",
        rows,
        warnings=warnings,
        focus=admin_focus(task.get("source_id")),
    )


async def cancel_task(args: TaskRef, ctx: ToolContext) -> ToolOutput:
    task = relay(
        await ctx.client.post(task_path(args.kind, args.task_id, "cancel"), {}),
        f"取消{TASK_PATHS[args.kind][1]}",
    )
    if task.get("status") == "cancelled":
        note = "任务已取消。"
    else:
        note = "已请求取消：任务会在下一次上报进度时停止并置为已取消，可用 get_task_status 确认。"
    lines = task_lines(args.kind, task) + [note]
    return ToolOutput(
        compact_task(args.kind, task),
        text="\n".join(lines),
        mutation={**LIBRARY, "source_id": task.get("source_id"), "task_id": task["id"]},
        focus=admin_focus(task.get("source_id")),
    )


# ---------- upload_source / upload_subtitle ----------

ATTACHMENT_FIELD = Field(
    pattern=r"^attachment://", description="附件引用 attachment://…（用户在面板上传的文件）"
)


async def attachment_meta(ctx: ToolContext, ref: str) -> dict:
    result = await ctx.client.get(f"/api/agent/attachments/{ref[len(ATTACHMENT_PREFIX) :]}")
    if not result["ok"]:
        raise ToolError(
            f"附件不可用：{result['error']}",
            code="attachment_invalid",
            value={"status": result["status"], "error": result["error"]},
        )
    return result["data"] or {}


def size_text(value) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def attachment_rows(meta: dict) -> list[dict]:
    return [
        {"label": "文件", "after": meta.get("filename") or "（未知）"},
        {"label": "类型", "after": meta.get("content_type") or "未知"},
        {"label": "大小", "after": size_text(meta.get("bytes"))},
    ]


class UploadSource(StrictModel):
    attachment: str = ATTACHMENT_FIELD


async def preview_upload_source(args: UploadSource, ctx: ToolContext) -> Preview:
    meta = await attachment_meta(ctx, args.attachment)
    warnings = [
        "登记只校验并保存原片；之后需要 start_source_task(work=shots) 才会分镜",
        "相同内容的文件会识别为已有素材，不会重复登记",
    ]
    ctype = str(meta.get("content_type") or "")
    if ctype and not ctype.startswith(("video/", "audio/", "application/octet-stream")):
        warnings.append(f"附件类型为 {ctype}，可能不是视频，服务端会在探测失败时拒绝")
    return Preview(
        f"把附件「{meta.get('filename')}」登记为新素材",
        attachment_rows(meta),
        warnings=warnings,
        focus={"page": "admin"},
    )


async def upload_source(args: UploadSource, ctx: ToolContext) -> ToolOutput:
    data = upload_check(
        await ctx.client.upload("/api/admin/sources/upload", files={"file": args.attachment}), "登记素材"
    )
    source_id = data["source_id"]
    text = (
        f"该文件与已有素材 {source_id} 内容相同，未重复登记。"
        if data.get("duplicate")
        else f"已登记素材 {source_id}。下一步可用 start_source_task(source_id={source_id}, work=shots) 分镜。"
    )
    return ToolOutput(
        data, text=text, mutation={**LIBRARY, "source_id": source_id}, focus=admin_focus(source_id)
    )


class UploadSubtitle(StrictModel):
    source_id: str = Field(min_length=1, max_length=200, description="字幕所属素材 ID")
    attachment: str = ATTACHMENT_FIELD


def subtitle_suffix_error(filename: str | None) -> str | None:
    from processing.subtitle_uploads import SUBTITLE_EXTENSIONS  # 与上传接口同一份后缀白名单

    suffix = PurePosixPath(filename or "").suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        return f"附件「{filename}」不是支持的字幕格式（支持 SRT、ASS、SSA、VTT、SUB、SMI、SAMI）"
    return None


async def preview_upload_subtitle(args: UploadSubtitle, ctx: ToolContext) -> Preview:
    meta = await attachment_meta(ctx, args.attachment)
    error = subtitle_suffix_error(meta.get("filename"))
    if error:
        raise ToolError(error, code="invalid_arguments")
    source = check(await ctx.client.get(f"/api/admin/sources/{args.source_id}", detail="summary"), "读取素材")
    warnings = [
        "上传只登记字幕文件；需要 start_source_task(work=dialogue, method=subtitle, subtitle_file=字幕 ID) "
        "才会应用",
        "该素材正在处理对白时服务端会拒绝上传",
    ]
    return Preview(
        f"为素材「{source['name']}」上传字幕",
        attachment_rows(meta),
        warnings=warnings,
        focus=admin_focus(args.source_id),
    )


async def upload_subtitle(args: UploadSubtitle, ctx: ToolContext) -> ToolOutput:
    data = upload_check(
        await ctx.client.upload(
            f"/api/admin/sources/{args.source_id}/subtitles", files={"file": args.attachment}
        ),
        "上传字幕",
    )
    text = (
        f"已登记字幕 {data['id']}（{data.get('name')}，{data.get('format')}，{data.get('utterances', '?')} "
        f"句）。"
        f"应用字幕：start_source_task(source_id={args.source_id}, work=dialogue, method=subtitle, "
        f"subtitle_file={data['id']})。"
    )
    return ToolOutput(
        data,
        text=text,
        mutation={**LIBRARY, "source_id": args.source_id, "subtitle_id": data["id"]},
        focus=admin_focus(args.source_id),
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "start_source_task",
            "library",
            "对素材发起处理任务：shots 分镜、analysis LLM 分析、dialogue 对白、persons 人物处理、publish "
            "发布检索；"
            "各工作的选项原样透传。提交后自动跟踪，结束时通知。",
            StartSourceTask,
            "danger",
            start_source_task,
            preview=preview_start_source_task,
        ),
        ToolSpec(
            "get_task_status",
            "library",
            "查询素材任务（source）、对白任务（dialogue）或人物任务（person）的状态、进度、错误与链式发布。",
            TaskRef,
            "read",
            get_task_status,
        ),
        ToolSpec(
            "retry_task",
            "library",
            "从断点重试失败、中断、已取消或检索发布失败的任务；重新提交后自动跟踪。",
            TaskRef,
            "write",
            retry_task,
            preview=preview_retry_task,
        ),
        ToolSpec(
            "cancel_task",
            "library",
            "取消排队中或运行中的任务：排队中立即取消；运行中在下一次上报进度时停止。不会取消已链式创建的发布子任务。",
            TaskRef,
            "write",
            cancel_task,
            preview=preview_cancel_task,
        ),
        ToolSpec(
            "upload_source",
            "library",
            "把用户上传的视频附件（attachment://…）登记为新素材（只登记，不分镜）。",
            UploadSource,
            "write",
            upload_source,
            preview=preview_upload_source,
        ),
        ToolSpec(
            "upload_subtitle",
            "library",
            "把字幕附件（SRT/ASS/SSA/VTT/SUB/SMI/SAMI）上传到素材，返回字幕 ID，"
            "供 start_source_task(work=dialogue, method=subtitle, subtitle_file=ID) 使用。",
            UploadSubtitle,
            "write",
            upload_subtitle,
            preview=preview_upload_subtitle,
        ),
    ]
