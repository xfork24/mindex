"""创作细节领域工具：方案诊断、版本依赖、召回词建议、素材摘要、版本预览、人工选片与整列重做。

全部经回环调用 ``/api/studio/...``：

- ``diagnose_plan``：``GET /plans/{pid}/diagnostics``，解释分镜检查、字幕环境、已选节点为什么过期或需要重建；
- ``get_version_lineage``：``GET /versions/{vid}/dependencies``，把依赖树整理成可读的缩进清单；
- ``suggest_shot_query``：``POST /plans/{pid}/shots/{sid}/query-suggestion``，只返回建议、不改分镜；
- ``get_material_digest``：``GET /material-digest``，文案与召回词共用的可用素材摘要；
- ``preview_version``：``GET /versions/{vid}/preview``，选片版本生成带配音的快速预览，渲染版本直接返回视频；
- ``run_column``（danger）：预览读取诊断接口的 ``inputs_by_stage[stage]`` 与 revision，执行时原样提交给
  execute-column / regenerate-column，服务端发现输入已变化时拒绝并原样转述，提交成功后跟踪构建；
- 人工选片（与创作页“调整选片”窗口同一套接口）：
  ``get_selection_candidates`` 读召回候选池与 ``GET /shots/{sid}/selection-context``；
  ``preview_selection`` 必要时 ``POST /anchor`` 设置参考候选，再 ``POST /range-preview``（定长窗口或任意区间）；
  ``confirm_selection``（write）以预览时的请求体与 ``preview_token`` 调 ``POST /range-confirm``。

  预览与确认是两次独立的工具调用（``ToolContext`` 不共享），预览结果按调用方作用域（面板会话 ID，MCP 为
  ``mcp``）与 ``preview_token`` 存在进程内 ``SELECTION_PREVIEWS``；确认工具的预览与执行之间仍经
  ``ctx.extra["planned"]`` 共享。服务端以 revision 与 token 重新校验，方案或对齐结果变化时要求重新预览。

公共助手（``plan_url``、``shot_ref``、``shot_label``、``version_ref``、``plan_diagnostics``、``require_idle``）
供人工选片工具复用。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Literal

from pydantic import Field, model_validator

from mindex.agent.ids import resolve_shot, resolve_version, short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import STAGE_LABELS, STAGES, PlanView, check, clip, load_view
from mindex.agent.tools.media import item
from mindex.agent.tools.studio_read import summarize_nodes
from mindex.agent.tools.studio_write import JobId, PlanId, mutation, planned, studio_focus
from mindex.shared.config import StrictModel

NODE_STATES = {"ok": "正常", "missing": "版本记录缺失", "failed": "失败", "corrupt": "文件损坏", "stale": "已过期"}
MAX_LINEAGE_LINES = 80


# ------------------------------------------------------------ 公共助手
def plan_url(view: PlanView, suffix: str = "") -> str:
    return f"/api/studio/jobs/{view.job['id']}/plans/{view.plan_id}{suffix}"


def shot_ref(view: PlanView, ref: str) -> str:
    """把完整 ID、短 ID 或 #序号解析为分镜 ID；有歧义或找不到时报错，由模型向用户澄清。"""
    found = resolve_shot(view.order, ref)
    if found["status"] == "ok":
        return found["shot_id"]
    if found["status"] == "ambiguous":
        options = "、".join(f"#{c['index']} {c['shot_id']}" for c in found["candidates"])
        raise ToolError(f"分镜 {ref} 有歧义，候选：{options}（请向用户确认）", code="unresolved_shot", value=found)
    reason = f"：{found['reason']}" if found.get("reason") else ""
    raise ToolError(f"找不到分镜 {ref}{reason}", code="unresolved_shot", value=found)


def shot_label(view: PlanView, sid: str | None) -> str:
    if not sid:
        return ""
    index = view.order.index(sid) + 1 if sid in view.order else "?"
    return f"#{index} {short(sid)}"


def version_ref(view: PlanView, ref: str, *, stage: str | None = None) -> dict:
    """解析版本短 ID / 完整 ID，返回版本记录（来自工作台详情，含文件 URL）。"""
    found = resolve_version(view.w["versions"], ref, stage=stage)
    if found["status"] == "ok":
        return view.w["versions"][found["version_id"]]
    if found["status"] == "ambiguous":
        raise ToolError(f"版本 {ref} 有歧义，候选：{'、'.join(found['candidates'])}", code="unresolved_version",
                        value=found)
    scope = f"{STAGE_LABELS.get(stage, stage)}阶段的" if stage else ""
    raise ToolError(f"找不到{scope}版本 {ref}；可用 list_versions 查看版本历史", code="unresolved_version", value=found)


def version_label(view: PlanView, version: dict) -> str:
    stage = STAGE_LABELS.get(version["stage"], version["stage"])
    shot = shot_label(view, version.get("shot_id"))
    return f"{stage} {short(version['id'])}" + (f"（分镜 {shot}）" if shot else "")


def selected_versions(view: PlanView) -> set[str]:
    chosen = {view.plan.get("script"), view.plan.get("final"), view.plan.get("board")}
    for stages in view.plan["shots"].values():
        chosen.update(stages.values())
    chosen.discard(None)
    return chosen


async def plan_diagnostics(ctx: ToolContext, view: PlanView) -> dict:
    return check(await ctx.client.get(plan_url(view, "/diagnostics")), "读取方案诊断")


def require_idle(view: PlanView, running: str | None = None) -> None:
    """有构建正在执行时拒绝：构建期间方案输入随时会变，提交大概率被服务端拒绝。"""
    running = running or next((b["id"] for b in view.w["builds"] if b["status"] == "running"), None)
    if running:
        raise ToolError(f"构建 {running} 正在执行，请等待结束（或用 cancel_build 取消）后再操作",
                        code="build_running", value={"build_id": running})


def node_line(view: PlanView, node: dict) -> str:
    stage = STAGE_LABELS.get(node["stage"], node["stage"])
    target = f" {shot_label(view, node['shot_id'])}" if node.get("shot_id") else ""
    reasons = "；".join(node.get("reasons") or [])
    return f"- {stage}{target}：{NODE_STATES.get(node['state'], node['state'])}" + (f"（{reasons}）" if reasons else "")


# ------------------------------------------------------------ 方案诊断
class DiagnosePlan(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId


async def diagnose_plan(args: DiagnosePlan, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    data = await plan_diagnostics(ctx, view)
    board, subtitle, preflight = data["storyboard"], data["subtitle"], data["preflight"]
    lines = [f"方案「{view.plan['name']}」({view.plan_id}) 诊断，revision={data['revision']}"
             + ("" if data["active"] else "（非当前方案）")]
    if data["running_build"]:
        lines.append(f"构建 {data['running_build']} 正在执行：以下状态会随构建变化。")

    lines.append(f"分镜检查：{'硬性问题：' + board['hard'] if board['hard'] else '无硬性问题'}")
    for issue in board["soft"]:
        lines.append(f"- 建议 {shot_label(view, issue['shot_id'])}：{'、'.join(issue['reasons'])}")
    if board["coverage"]:
        lines.append(f"文案覆盖：{board['coverage']}")

    if not subtitle["burn"]:
        lines.append("字幕：方案未开启烧录字幕，不检查字幕环境")
    else:
        font = "正常" if subtitle["font"] == "ok" else subtitle["font"]
        lines.append(f"字幕：subtitles 滤镜{'可用' if subtitle['filter'] else '不可用'}；字体渲染：{font}")

    problems = [n for n in data["nodes"] if n["state"] != "ok"]
    lines.append(f"已选节点：{len(data['nodes'])} 个，正常 {len(data['nodes']) - len(problems)} 个"
                 + ("，问题如下：" if problems else ""))
    lines += [node_line(view, n) for n in problems[:60]]
    if len(problems) > 60:
        lines.append(f"…（另有 {len(problems) - 60} 个问题节点未列出）")

    if preflight["error"]:
        lines.append(f"构建预检无法完成：{preflight['error']}")
    else:
        counts, changed = summarize_nodes(preflight["nodes"])
        lines.append(f"构建预检：重建 {counts.get('rebuild', 0)}，复用 {counts.get('reuse', 0)}，"
                     f"阻塞 {counts.get('blocked', 0)}，跳过 {counts.get('skip', 0)}")
        lines += changed[:40]
        if len(changed) > 40:
            lines.append(f"…（另有 {len(changed) - 40} 项未列出）")

    shots = [issue["shot_id"] for issue in board["soft"]] + [n["shot_id"] for n in problems if n["shot_id"]]
    shots = list(dict.fromkeys(shots))[:20]
    value = {k: v for k, v in data.items() if k != "inputs_by_stage"}
    return ToolOutput(value, text="\n".join(lines),
                      meta={"kind": "diagnostics", "job_id": view.job["id"], "plan_id": view.plan_id},
                      focus=studio_focus(view.job["id"], view.plan_id, shots=shots or None,
                                         section=None if shots else "storyboard"))


# ------------------------------------------------------------ 版本依赖
class VersionLineage(StrictModel):
    version: str = Field(min_length=1, description="版本 ID：完整 ID 或界面显示的 8 位短 ID")
    job_id: str | None = JobId
    plan_id: str | None = PlanId


def lineage_lines(view: PlanView, root: str, nodes: dict, selected: set[str]) -> list[str]:
    lines, seen = [], set()

    def visit(vid, depth, name):
        prefix = "  " * depth + "- " + (f"{name} ← " if name else "")
        node = nodes.get(vid)
        if node is None:
            lines.append(f"{prefix}{short(vid)}（版本记录缺失）")
            return
        if vid in seen:
            lines.append(f"{prefix}{version_label(view, node)}（见上）")
            return
        seen.add(vid)
        mark = " 【当前选用】" if vid in selected else ""
        error = f" 错误：{clip(node.get('error'), 80)}" if node.get("error") else ""
        lines.append(f"{prefix}{version_label(view, node)} · {node['status']} · {node.get('created_at') or '—'}"
                     f"{mark}{error}")
        for dep_name, dep in (node.get("inputs") or {}).items():
            if dep:
                visit(dep, depth + 1, dep_name)

    visit(root, 0, None)
    return lines


async def get_version_lineage(args: VersionLineage, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    root = version_ref(view, args.version)
    data = check(await ctx.client.get(f"/api/studio/jobs/{view.job['id']}/versions/{root['id']}/dependencies"),
                 "读取版本依赖")
    nodes = data["nodes"]
    selected = selected_versions(view)
    lines = lineage_lines(view, data["root"], nodes, selected)
    total = len(lines)
    if total > MAX_LINEAGE_LINES:
        lines = lines[:MAX_LINEAGE_LINES] + [f"…（依赖树共 {total} 行，已截断）"]
    rows = [{"version_id": vid, "stage": n["stage"], "shot_id": n.get("shot_id"), "status": n["status"],
             "created_at": n.get("created_at"), "inputs": n.get("inputs") or {}, "selected": vid in selected}
            for vid, n in nodes.items()]
    text = (f"版本 {version_label(view, root)} 的依赖链（共 {len(nodes)} 个版本；“名称 ← 版本”表示该输入使用的版本）：\n"
            + "\n".join(lines))
    sid = root.get("shot_id")
    if sid and sid in view.order:
        focus = studio_focus(view.job["id"], view.plan_id, shots=[sid],
                             stage=root["stage"] if root["stage"] in ("content", *STAGES) else None)
    else:
        section = {"script": "script", "compose": "final"}.get(root["stage"], "storyboard")
        focus = studio_focus(view.job["id"], view.plan_id, section=section)
    return ToolOutput({"root": data["root"], "nodes": rows}, text=text, focus=focus)


# ------------------------------------------------------------ 召回词建议
class SuggestShotQuery(StrictModel):
    shot: str = Field(min_length=1, description="分镜：完整 ID、短 ID 或 #序号")
    visual: str | None = Field(default=None, description="按这段画面意象重写；省略时使用分镜已保存的画面意象")
    job_id: str | None = JobId
    plan_id: str | None = PlanId


async def suggest_shot_query(args: SuggestShotQuery, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = shot_ref(view, args.shot)
    body = {} if args.visual is None else {"visual": args.visual}
    data = check(await ctx.client.post(plan_url(view, f"/shots/{sid}/query-suggestion"), body), "重写召回词")
    current = view.content(sid).get("query")
    text = (f"分镜 {shot_label(view, sid)} 的建议召回词：{data['query']}\n当前召回词：{current or '（空）'}\n"
            "这只是建议，分镜没有被修改；要采用请用 edit_shots 把该分镜的 query 改为建议值"
            + ("（如果使用了新的画面意象，也一并更新 visual）" if args.visual is not None else "") + "。")
    return ToolOutput({"shot_id": sid, "query": data["query"], "current_query": current}, text=text,
                      focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="content"))


# ------------------------------------------------------------ 素材摘要
class MaterialDigestArgs(StrictModel):
    job_id: str | None = JobId


async def get_material_digest(args: MaterialDigestArgs, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    data = check(await ctx.client.get(f"/api/studio/jobs/{job}/material-digest"), "读取素材摘要")
    stats = data["stats"]
    if data["empty_reason"]:
        text = f"任务 {job} 的可用素材摘要为空：{data['empty_reason']}。写文案与重写召回词时不会参考素材。"
    else:
        lines = [f"任务 {job} 的可用素材摘要：{stats['sources']} 个素材、{stats['segments']} 个已分析片段"
                 f"（内容索引 {stats.get('content_index_id') or '—'}）"]
        for source in data["sources"][:30]:
            words = "、".join(source["vocabulary"][:10])
            lines.append(f"- {source['name']}（{source['source_id']}，{source['duration']:.0f} 秒，"
                         f"{source['segments']} 个片段）" + (f" 画面词：{words}" if words else ""))
        if len(data["sources"]) > 30:
            lines.append(f"…（另有 {len(data['sources']) - 30} 个素材未列出）")
        lines.append("摘要正文（文案与召回词生成时参考）：\n" + clip(data["text"], 3000))
        text = "\n".join(lines)
    return ToolOutput(data, text=text, focus=studio_focus(job))


# ------------------------------------------------------------ 版本预览
class PreviewVersion(StrictModel):
    version: str = Field(min_length=1, description="选片或渲染版本 ID：完整 ID 或 8 位短 ID")
    job_id: str | None = JobId
    plan_id: str | None = PlanId


async def preview_version(args: PreviewVersion, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    target = version_ref(view, args.version)
    if target["stage"] not in ("select", "render"):
        raise ToolError(f"只有选片与渲染版本可以预览；{version_label(view, target)} 可用 list_versions 查看产物文件",
                        code="unsupported_version")
    data = check(await ctx.client.get(f"/api/studio/jobs/{view.job['id']}/versions/{target['id']}/preview"),
                 "生成版本预览")
    label = version_label(view, target)
    media = [entry for entry in [item("video", data["url"], f"{label} 预览", collapsed=False)] if entry]
    sid = target.get("shot_id")
    focus = studio_focus(view.job["id"], view.plan_id, shots=[sid] if sid in view.order else None,
                         stage=target["stage"] if sid in view.order else None)
    return ToolOutput({"version_id": target["id"], "url": data["url"]},
                      text=f"已生成 {label} 的预览（{'带配音的选片片段' if target['stage'] == 'select' else '渲染结果'}），"
                           "可在面板播放。",
                      focus=focus, media=media)


# ------------------------------------------------------------ 整列执行 / 重做
class RunColumn(StrictModel):
    stage: Literal["recall", "tts", "select", "render"] = Field(description="要整列执行的阶段：召回/配音/选片/渲染")
    force: bool = Field(
        default=False,
        description="false=执行/复用：已有匹配版本的分镜直接选用，只生成缺失或过期的；"
                    "true=全部重新生成（耗时且可能产生费用）")
    job_id: str | None = JobId
    plan_id: str | None = PlanId


def column_blocked(view: PlanView, stage: str, inputs: dict) -> str | None:
    """与服务端 column_nodes 一致：依赖缺失或失败的分镜会被阻塞。"""
    versions = view.w["versions"]
    for name, vid in inputs.items():
        dep = versions.get(vid) if vid else None
        if not dep or dep["status"] != "complete":
            return f"依赖 {STAGE_LABELS.get(name, name)} 缺失或失败"
    if stage == "render":
        selection = versions.get(inputs["select"]) or {}
        if (selection.get("inputs") or {}).get("tts") != inputs.get("tts"):
            return "选片绑定的配音与当前选择不同，请先重新对齐"
    return None


async def column_plan(args: RunColumn, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    data = await plan_diagnostics(ctx, view)
    require_idle(view, data["running_build"])
    if not view.order:
        raise ToolError("当前方案还没有分镜，无法整列执行", code="no_shots")
    inputs = data["inputs_by_stage"].get(args.stage) or {}
    missing = [sid for sid in view.order if sid not in inputs]
    if missing:
        names = "、".join(shot_label(view, sid) for sid in missing[:10])
        raise ToolError(f"{STAGE_LABELS[args.stage]}列无法执行：分镜 {names} 的输入无法确定（通常是前置阶段尚未完成）；"
                        "可用 diagnose_plan 查看原因", code="column_unavailable", value={"shots": missing})
    body = {"revision": data["revision"], "stage": args.stage,
            "inputs_by_shot": {sid: inputs[sid] for sid in view.order}}
    return view, data, body


async def preview_run_column(args: RunColumn, ctx: ToolContext) -> Preview:
    view, data, body = await column_plan(args, ctx)
    planned(ctx, "column", (view.job["id"], view.plan_id, body))
    label = STAGE_LABELS[args.stage]
    nodes = {n["key"]: n for n in data["nodes"]}
    actions = {n["key"]: n for n in data["preflight"]["nodes"]}
    rows, blocked = [], []
    for sid in view.order:
        key = f"{args.stage}:{sid}"
        reason = column_blocked(view, args.stage, body["inputs_by_shot"][sid])
        node = nodes.get(key)
        if reason:
            blocked.append(sid)
            state = f"将被阻塞（{reason}）"
        elif node is None:
            state = "尚未生成"
        else:
            reasons = "；".join(node["reasons"])
            state = NODE_STATES.get(node["state"], node["state"]) + (f"（{reasons}）" if reasons else "")
        if not reason and not args.force and key in actions:
            state += "，预计" + {"reuse": "复用已有版本", "rebuild": "重新生成"}.get(actions[key]["action"], "按预检处理")
        rows.append({"label": f"- {shot_label(view, sid)}：{state}"})
    changes = [{"label": "阶段", "after": label},
               {"label": "方式", "after": "全部重新生成" if args.force else "执行/复用（只生成缺失或过期的）"},
               {"label": "分镜数", "after": len(view.order)}] + rows[:40]
    warnings = []
    if args.force:
        warnings.append("全部重新生成会忽略已有版本，耗时且可能调用付费模型服务")
    if blocked:
        warnings.append(f"{len(blocked)} 个分镜的依赖缺失或失败，执行时会被阻塞")
    warnings.append(f"完成后依赖{label}结果的后续阶段与成片会在下次构建时重建")
    return Preview(f"整列{'重新生成' if args.force else '执行'}方案「{view.plan['name']}」的{label}（后台执行）",
                   changes, warnings=warnings, value={"blocked": blocked},
                   focus=studio_focus(view.job["id"], view.plan_id, section="storyboard"))


async def run_column(args: RunColumn, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "column")
    if stored is None:
        view, _data, body = await column_plan(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    endpoint = "regenerate-column" if args.force else "execute-column"
    try:
        build = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/{endpoint}", body), "提交整列执行")
    except ToolError as exc:
        if "已变化" in str(exc):
            raise ToolError(f"{exc}；请重新调用 run_column 预览最新输入后再确认", code=exc.code, value=exc.value) from exc
        raise
    target = {"job_id": job, "build_id": build["id"], "plan_id": pid}
    label = STAGE_LABELS[body["stage"]]
    return ToolOutput({"build_id": build["id"], "stage": body["stage"], "nodes": len(build["nodes"])},
                      text=f"已提交{label}列{'重新生成' if args.force else '执行'} {build['id']}"
                           f"（{len(build['nodes'])} 个分镜）。系统会跟踪进度，结束后通知你总结。",
                      meta={"kind": "build", **target}, mutation=mutation(job, pid), watch_build=target,
                      focus=studio_focus(job, pid, section="log", build_id=build["id"]))


# ------------------------------------------------------------ 人工选片
class SelectionPreviews:
    """跨工具调用保存选片预览：``(作用域, preview_token) → 请求体与摘要``。

    作用域是面板会话 ID（MCP 调用为 ``mcp``），避免不同会话互相确认对方的预览。
    只是便利缓存：服务端在确认时以 revision 与 token 重新校验，缓存丢失（重启、过期）时提示重新预览。
    """

    def __init__(self, limit: int = 64, ttl: float = 3600):
        self.limit, self.ttl = limit, ttl
        self._items: OrderedDict[tuple[str, str], dict] = OrderedDict()

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._items.items() if now - v["created"] > self.ttl]:
            self._items.pop(key, None)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def put(self, scope: str, entry: dict) -> None:
        self._items[(scope, entry["token"])] = {**entry, "created": time.monotonic()}
        self._items.move_to_end((scope, entry["token"]))
        self._prune()

    def get(self, scope: str, token: str) -> dict | None:
        self._prune()
        if (scope, token) in self._items:
            return self._items[(scope, token)]
        found = [v for (s, t), v in self._items.items() if s == scope and t.startswith(token)]
        return found[0] if len(found) == 1 else None

    def pop(self, scope: str, token: str) -> None:
        self._items.pop((scope, token), None)


SELECTION_PREVIEWS = SelectionPreviews()
TTS_HINT = "选片窗口按配音时长定长，请先为该分镜配音（run_node stage=tts 或 run_column stage=tts）再选片"


def preview_scope(ctx: ToolContext) -> str:
    return ctx.session_id or "mcp"


def secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}s"


def stage_version(view: PlanView, sid: str, stage: str) -> dict | None:
    return view.version(view.plan["shots"].get(sid, {}).get(stage))


def complete_speech(view: PlanView, sid: str) -> dict | None:
    speech = stage_version(view, sid, "tts")
    return speech if speech and speech["status"] == "complete" else None


def recall_pool(view: PlanView, sid: str) -> tuple[dict | None, list[dict]]:
    recall = stage_version(view, sid, "recall")
    if not recall or recall["status"] != "complete" or recall.get("shot_id") != sid:
        return None, []
    return recall, list((recall.get("data") or {}).get("results") or [])


def candidate_key(candidate: dict) -> str:
    return candidate.get("id") or candidate["segment_id"]


def candidate_ref(pool: list[dict], ref: str) -> dict:
    """候选引用：``#序号``（get_selection_candidates 列表顺序）、完整候选 ID 或唯一前缀。"""
    text = ref.strip()
    number = text[1:] if text.startswith("#") else text
    if number.isdigit() and (text.startswith("#") or not any(candidate_key(c) == text for c in pool)):
        n = int(number)
        if 1 <= n <= len(pool):
            return pool[n - 1]
        raise ToolError(f"候选 {ref} 超出范围：召回共 {len(pool)} 个候选", code="unresolved_candidate")
    exact = [c for c in pool if candidate_key(c) == text]
    found = exact or [c for c in pool if len(text) >= 4 and candidate_key(c).startswith(text)]
    if len(found) == 1:
        return found[0]
    if found:
        raise ToolError(f"候选 {ref} 有歧义：{'、'.join(candidate_key(c) for c in found[:6])}",
                        code="unresolved_candidate")
    raise ToolError(f"召回候选中找不到 {ref}；可用 get_selection_candidates 查看候选", code="unresolved_candidate")


def selection_summary(version: dict | None) -> dict | None:
    """当前选用的选片版本：素材区间与是否人工。"""
    if not version or version["status"] != "complete":
        return None
    data = version.get("data") or {}
    video = (data.get("event") or {}).get("video") or {}
    anchor = data.get("anchor") or {}
    return {"version_id": version["id"], "source_id": video.get("source_id"),
            "source_name": anchor.get("source_name"), "start": video.get("start"), "end": video.get("end"),
            "manual": bool(data.get("manual"))}


def selection_line(selection: dict | None) -> str:
    if not selection:
        return "无"
    name = selection.get("source_name") or selection.get("source_id")
    return (f"选片 {short(selection['version_id'])}：{name} {secs(selection['start'])}–{secs(selection['end'])}"
            f"（{'人工' if selection['manual'] else '自动'}）")


async def selection_context(ctx: ToolContext, view: PlanView, sid: str) -> dict:
    return check(await ctx.client.get(plan_url(view, f"/shots/{sid}/selection-context")), "读取选片上下文")


class SelectionCandidates(StrictModel):
    shot: str = Field(min_length=1, description="分镜：完整 ID、短 ID 或 #序号")
    limit: int = Field(default=12, ge=1, le=30, description="最多列出的候选数")
    job_id: str | None = JobId
    plan_id: str | None = PlanId


async def get_selection_candidates(args: SelectionCandidates, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = shot_ref(view, args.shot)
    require_idle(view)
    label = shot_label(view, sid)
    recall, pool = recall_pool(view, sid)
    speech = complete_speech(view, sid)
    choice = (view.plan.get("anchors") or {}).get(sid, {"mode": "auto"})
    context, reason = None, None
    if recall is None:
        reason = "该分镜尚无成功的召回，请先召回（run_node stage=recall）"
    else:
        try:
            context = await selection_context(ctx, view, sid)
        except ToolError as exc:
            reason = str(exc)
        else:
            reason = None if context["ready"] else context["reason"]
    if speech is None and recall is not None:
        reason = f"尚无成功的配音：{TTS_HINT}"
    anchor = (context or {}).get("anchor")
    anchor_id = candidate_key(anchor) if anchor else None

    rows, lines = [], []
    for n, c in enumerate(pool[:args.limit], 1):
        cid = candidate_key(c)
        row = {"n": n, "candidate_id": cid, "segment_id": c.get("segment_id") or cid, "source_id": c["source_id"],
               "source_name": c.get("source_name") or "", "start": c["start"], "end": c["end"],
               "score": c.get("score"), "rank": c.get("rank"), "anchor": cid == anchor_id}
        rows.append(row)
        score = f" · 分数 {c['score']:.3f}" if isinstance(c.get("score"), int | float) else ""
        lines.append(f"#{n} {cid} · {row['source_name'] or c['source_id']} {secs(c['start'])}–{secs(c['end'])}"
                     f"（{c['end'] - c['start']:.2f}s）{score}" + (" 【参考锚点】" if row["anchor"] else ""))

    selection = selection_summary(stage_version(view, sid, "select"))
    head = [f"分镜 {label} 的人工选片信息："]
    tts_line = f"{speech['data']['duration']:.2f} 秒（版本 {short(speech['id'])}）" if speech else "无成功配音"
    head.append(f"配音：{tts_line}")
    head.append(f"当前选片：{selection_line(selection)}")
    mode = "自动（按评分选择）" if choice["mode"] == "auto" else f"人工指定 {choice.get('candidate_id')}"
    head.append(f"参考候选：{mode}" + (f"，当前锚点 {anchor_id}" if anchor_id else ""))
    window = None
    if context and context.get("anchor") and context.get("legal_start"):
        window = {"duration": context["window_duration"], "legal_start": context["legal_start"],
                  "initial_start": context["initial_start"], "context_range": context["context_range"],
                  "source_id": context["source_id"]}
        if context["ready"]:
            head.append(f"定长窗口：{context['window_duration']:.2f} 秒；合法起点 "
                        f"{secs(context['legal_start']['min'])}–{secs(context['legal_start']['max'])}，"
                        f"默认起点 {secs(context['initial_start'])}（素材上下文 "
                        f"{secs(context['context_range']['start'])}–{secs(context['context_range']['end'])}）")
    head.append("选片就绪" if reason is None else f"选片未就绪：{reason}")
    if pool:
        head.append(f"召回候选（共 {len(pool)} 个" + (f"，列出前 {len(rows)} 个" if len(rows) < len(pool) else "") + "）：")
    else:
        head.append("召回候选：无")
    tail = ["用 preview_selection 预览：给 candidate（#序号或候选 ID）按定长窗口选片，可再给 start 调整起点；"
            "或给 source_id + start + end 按任意区间选片。"]
    text = "\n".join(head + lines + tail)

    media = []
    for row in rows[:6]:
        segment = row["segment_id"]
        media.append(item("video", f"/api/admin/segments/{segment}/playlist", f"{label} 候选 #{row['n']}",
                          poster=f"/api/admin/segments/{segment}/thumbnail", playlist=True, collapsed=False))
    if context and context.get("video_url"):
        media.append(item("video", context["video_url"], f"{label} 选片上下文（无声）", collapsed=True))
    value = {"shot_id": sid, "recall_version": recall["id"] if recall else None, "anchor_choice": choice,
             "anchor_candidate_id": anchor_id, "candidates": rows, "total": len(pool),
             "tts": {"version_id": speech["id"], "duration": speech["data"]["duration"]} if speech else None,
             "selection": selection, "manual_range": (view.plan.get("ranges") or {}).get(sid),
             "ready": reason is None, "reason": reason, "window": window}
    return ToolOutput(value, text=text, media=[m for m in media if m],
                      images=[f"/api/admin/segments/{row['segment_id']}/thumbnail" for row in rows[:6]] or None,
                      focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="select"))


class PreviewSelection(StrictModel):
    shot: str = Field(min_length=1, description="分镜：完整 ID、短 ID 或 #序号")
    candidate: str | None = Field(
        default=None,
        description="定长窗口方式的参考候选：get_selection_candidates 列表中的 #序号或候选 ID（会设为该分镜的人工锚点）；"
                    "\"auto\" 恢复自动选择；省略时沿用当前参考候选")
    start: float | None = Field(
        default=None, ge=0, allow_inf_nan=False,
        description="起点秒（素材内绝对时间）。窗口方式：窗口起点，须在合法起点范围内，省略时用默认起点；区间方式必填")
    source_id: str | None = Field(default=None, description="区间方式：素材 ID；给出时按素材任意区间选片（需同时给 start、end）")
    end: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="区间方式：终点秒")
    job_id: str | None = JobId
    plan_id: str | None = PlanId

    @model_validator(mode="after")
    def mode(self):
        if self.source_id is None:
            if self.end is not None:
                raise ValueError("end 只用于区间方式（需同时给 source_id）")
        else:
            if self.candidate is not None:
                raise ValueError("区间方式不使用 candidate")
            if self.start is None or self.end is None:
                raise ValueError("区间方式需要同时给 start 与 end")
            if self.end <= self.start:
                raise ValueError("end 必须大于 start")
        return self


async def window_request(args: PreviewSelection, ctx: ToolContext, view: PlanView, sid: str) -> tuple[dict, bool]:
    """定长窗口：必要时先设置人工锚点，再按选片上下文组装 WindowBody。返回 (请求体, 锚点是否改变)。"""
    recall, pool = recall_pool(view, sid)
    if recall is None:
        raise ToolError(f"分镜 {shot_label(view, sid)} 尚无成功的召回，请先召回（run_node stage=recall）",
                        code="recall_missing")
    choice = (view.plan.get("anchors") or {}).get(sid, {"mode": "auto"})
    desired = None
    if args.candidate is not None:
        if args.candidate.strip().lower() == "auto":
            desired = {"mode": "auto"}
        else:
            chosen = candidate_ref(pool, args.candidate)
            desired = {"mode": "manual", "recall_version": recall["id"], "candidate_id": candidate_key(chosen)}
    changed = desired is not None and desired != choice
    if changed:
        check(await ctx.client.post(plan_url(view, "/anchor"), {"revision": view.revision, "shot_id": sid, **desired}),
              "设置参考候选")
    context = await selection_context(ctx, view, sid)
    if not context["ready"]:
        note = "（参考候选已改为所选候选）" if changed else ""
        raise ToolError(f"选片窗口不可用：{context['reason']}{note}", code="selection_unavailable",
                        value={"anchor_changed": changed})
    legal = context["legal_start"]
    if args.start is None:
        existing = (view.plan.get("ranges") or {}).get(sid) or {}
        same = (not changed and existing.get("anchor_ref") == context["anchor_ref"]
                and existing.get("tts_version") == context["tts_version"] and existing.get("atempo") == context["atempo"])
        start = existing["start"] if same and "start" in existing else context["initial_start"]
        start = min(legal["max"], max(legal["min"], start))
    else:
        start = args.start
        if not legal["min"] - 1e-6 <= start <= legal["max"] + 1e-6:
            raise ToolError(f"窗口起点 {secs(start)} 超出合法范围 {secs(legal['min'])}–{secs(legal['max'])}"
                            f"（窗口长 {context['window_duration']:.2f} 秒）", code="out_of_range",
                            value={"legal_start": legal, "anchor_changed": changed})
        start = min(legal["max"], max(legal["min"], start))
    body = {"revision": context["revision"], "shot_id": sid, "context_token": context["context_token"],
            "anchor_ref": context["anchor_ref"], "tts_version": context["tts_version"], "start": start}
    return body, changed


async def preview_selection(args: PreviewSelection, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = shot_ref(view, args.shot)
    require_idle(view)
    label = shot_label(view, sid)
    if complete_speech(view, sid) is None:
        raise ToolError(f"分镜 {label} 尚无成功的配音：{TTS_HINT}", code="tts_missing")
    if args.source_id is None:
        mode = "window"
        body, changed = await window_request(args, ctx, view, sid)
        sent = {**body, "render_preview": True}
    else:
        mode, changed = "range", False
        body = {"revision": view.revision, "shot_id": sid, "source_id": args.source_id,
                "start": args.start, "end": args.end}
        sent = body
    try:
        result = check(await ctx.client.post(plan_url(view, "/range-preview"), sent), "生成选片预览")
    except ToolError as exc:
        if changed:
            raise ToolError(f"{exc}（参考候选已改为所选候选）", code=exc.code, value=exc.value) from exc
        raise
    token = result["preview_token"]
    actual = result["actual_range"]
    anchor = result.get("anchor") or {}
    name = anchor.get("source_name") or actual["source_id"]
    SELECTION_PREVIEWS.put(preview_scope(ctx), {
        "token": token, "job_id": view.job["id"], "plan_id": view.plan_id, "shot_id": sid, "mode": mode,
        "body": body, "range": actual, "source_name": name, "anchor_ref": result.get("anchor_ref")})

    lines = [f"已生成分镜 {label} 的选片预览（{'定长窗口' if mode == 'window' else '任意区间'}）："
             f"{name}（{actual['source_id']}）{secs(actual['start'])}–{secs(actual['end'])}"]
    if changed:
        lines.append("参考候选已" + ("恢复为自动选择" if args.candidate.strip().lower() == "auto"
                                    else f"改为 {result['anchor_ref']['candidate_id']}") + "（已清除该分镜之前的人工区间）")
    if mode == "window":
        legal = (result.get("range") or {}).get("context_range")
        lines.append(f"窗口长 {actual['end'] - actual['start']:.2f} 秒，参考候选 "
                     f"{(result.get('anchor_ref') or {}).get('candidate_id')}"
                     + (f"，素材上下文 {secs(legal['start'])}–{secs(legal['end'])}" if legal else ""))
    else:
        extra = []
        if result.get("trim_seconds"):
            extra.append(f"超出配音部分裁掉 {result['trim_seconds']:.2f} 秒")
        if result.get("freeze_seconds"):
            extra.append(f"画面不足将定格 {result['freeze_seconds']:.2f} 秒")
        if result.get("atempo") not in (None, 1, 1.0):
            extra.append(f"配音变速 {result['atempo']:g}")
        if extra:
            lines.append("；".join(extra))
    lines.append(f"preview_token={token}。用户满意后调用 confirm_selection(preview_token=\"{token}\") 确认；"
                 "不满意可换 start 或候选再预览。")
    media = [m for m in [item("video", result.get("preview_url") or "", f"{label} 选片预览", collapsed=False)] if m]
    value = {"preview_token": token, "mode": mode, "shot_id": sid, "range": actual,
             "anchor_ref": result.get("anchor_ref"), "anchor_changed": changed,
             "trim_seconds": result.get("trim_seconds", 0), "freeze_seconds": result.get("freeze_seconds", 0),
             "atempo": result.get("atempo"), "preview_url": result.get("preview_url")}
    return ToolOutput(value, text="\n".join(lines), media=media,
                      mutation=mutation(view.job["id"], view.plan_id) if changed else None,
                      focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="select"))


class ConfirmSelection(StrictModel):
    preview_token: str = Field(min_length=8, description="preview_selection 返回的 preview_token")


STALE_MARKERS = ("已变化", "请先预览")


def stale_error(exc: ToolError) -> ToolError:
    if any(marker in str(exc) for marker in STALE_MARKERS):
        return ToolError(f"{exc}；请重新调用 preview_selection 生成新的预览后再确认", code="preview_stale",
                         value=exc.value)
    return exc


def selection_entry(args: ConfirmSelection, ctx: ToolContext) -> dict:
    entry = SELECTION_PREVIEWS.get(preview_scope(ctx), args.preview_token.strip())
    if entry is None:
        raise ToolError("找不到该选片预览（已过期、已确认或来自其他会话）；请重新调用 preview_selection",
                        code="preview_stale")
    return entry


async def preview_confirm_selection(args: ConfirmSelection, ctx: ToolContext) -> Preview:
    entry = selection_entry(args, ctx)
    view = await load_view(ctx, entry["job_id"], entry["plan_id"])
    sid = entry["shot_id"]
    if sid not in view.order:
        raise ToolError("该分镜已不在方案中；请重新预览", code="preview_stale")
    require_idle(view)
    label = shot_label(view, sid)
    if complete_speech(view, sid) is None:
        raise ToolError(f"分镜 {label} 尚无成功的配音：{TTS_HINT}", code="tts_missing")
    sent = {**entry["body"], "render_preview": False} if entry["mode"] == "window" else entry["body"]
    try:
        result = check(await ctx.client.post(plan_url(view, "/range-preview"), sent), "校验选片预览")
    except ToolError as exc:
        raise stale_error(exc) from exc
    if result["preview_token"] != entry["token"]:
        raise ToolError("预览之后对齐结果或选片设置已变化；请重新调用 preview_selection", code="preview_stale")
    planned(ctx, "selection", entry)
    actual = entry["range"]
    changes = [{"label": f"分镜 {label} 选片",
                "before": selection_line(selection_summary(stage_version(view, sid, "select"))),
                "after": f"{entry['source_name']} {secs(actual['start'])}–{secs(actual['end'])}（人工，"
                         f"{'定长窗口' if entry['mode'] == 'window' else '任意区间'}）"}]
    if entry["mode"] == "window" and entry.get("anchor_ref"):
        changes.append({"label": "参考候选", "after": entry["anchor_ref"]["candidate_id"]})
    return Preview(f"确认分镜 {label} 的人工选片（生成新的选片版本并选用）", changes,
                   warnings=["该分镜的渲染与成片会在下次构建时按新选片重建"],
                   value={"preview_token": entry["token"], "range": actual},
                   focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="select"))


async def confirm_selection(args: ConfirmSelection, ctx: ToolContext) -> ToolOutput:
    entry = planned(ctx, "selection") or selection_entry(args, ctx)
    job, pid, sid = entry["job_id"], entry["plan_id"], entry["shot_id"]
    try:
        detail = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/range-confirm",
                                             {**entry["body"], "preview_token": entry["token"]}), "确认选片")
    except ToolError as exc:
        raise stale_error(exc) from exc
    SELECTION_PREVIEWS.pop(preview_scope(ctx), entry["token"])
    view = PlanView(detail, pid)
    vid = view.plan["shots"][sid].get("select")
    actual = entry["range"]
    return ToolOutput({"shot_id": sid, "select_version": vid, "range": actual},
                      text=f"已确认分镜 {shot_label(view, sid)} 的人工选片，生成并选用选片版本 {short(vid)}"
                           f"（{entry['source_name']} {secs(actual['start'])}–{secs(actual['end'])}）。"
                           "渲染与成片会在下次构建时按新选片重建；可用 preview_version 预览该选片版本。",
                      mutation=mutation(job, pid),
                      focus=studio_focus(job, pid, shots=[sid], stage="select"))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("diagnose_plan", "studio_detail",
                 "方案诊断：分镜硬性/软性检查与文案覆盖、烧录字幕时的滤镜与字体检查、各已选节点是否缺失/失败/损坏/过期"
                 "及变化原因（例如“旁白已变化”）、构建预检摘要。用于解释“为什么这个分镜要重建”。只读。",
                 DiagnosePlan, "read", diagnose_plan),
        ToolSpec("get_version_lineage", "studio_detail",
                 "查看某个产物版本的依赖链：它用到的上游版本（阶段、短 ID、分镜、状态、创建时间、是否当前选用）。只读。",
                 VersionLineage, "read", get_version_lineage),
        ToolSpec("suggest_shot_query", "studio_detail",
                 "按分镜画面意象（或给定的新画面意象）重写召回词，只返回建议、不修改分镜；采用时再用 edit_shots。",
                 SuggestShotQuery, "read", suggest_shot_query),
        ToolSpec("get_material_digest", "studio_detail",
                 "读取任务的可用素材摘要（素材、片段数、画面词与摘要正文），即写文案和重写召回词时参考的资料。只读。",
                 MaterialDigestArgs, "read", get_material_digest),
        ToolSpec("preview_version", "studio_detail",
                 "预览选片或渲染版本：选片版本生成带配音的快速预览，渲染版本直接返回视频，可在面板播放。",
                 PreviewVersion, "read", preview_version),
        ToolSpec("run_column", "studio_detail",
                 "整列执行某个阶段（召回/配音/选片/渲染）：覆盖方案全部分镜，按当前输入提交；force=true 全部重新生成。"
                 "后台执行，结束时系统会通知你。输入已变化时服务端会拒绝，需要重新预览。",
                 RunColumn, "danger", run_column, preview_run_column),
        ToolSpec("get_selection_candidates", "studio_detail",
                 "人工选片前查看某分镜的召回候选（#序号、素材、区间、分数、缩略图）、当前参考锚点与选片、配音时长、"
                 "定长窗口的合法起点范围，以及选片是否就绪和原因。只读。",
                 SelectionCandidates, "read", get_selection_candidates),
        ToolSpec("preview_selection", "studio_detail",
                 "生成人工选片预览（带配音的视频）并返回 preview_token，不改选片。两种方式："
                 "定长窗口——给 candidate（#序号或候选 ID，会设为该分镜的参考锚点，并清除之前的人工区间）与可选 start；"
                 "任意区间——给 source_id、start、end。需要成功配音；构建执行中不可用。",
                 PreviewSelection, "read", preview_selection),
        ToolSpec("confirm_selection", "studio_detail",
                 "确认 preview_selection 生成的选片预览：生成新的选片版本并选用。方案或对齐结果在预览后变化时会被拒绝，"
                 "需要重新预览。",
                 ConfirmSelection, "write", confirm_selection, preview_confirm_selection),
    ]
