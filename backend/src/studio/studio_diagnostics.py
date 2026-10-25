"""创作方案诊断：只读汇总分镜检查、字幕环境、已选节点状态与变化原因。

全部复用已有规则——拆分镜硬/软校验（models.storyboard）、画面意象上限（studio.visual_error）、
召回引用校验（studio_models.validate_recall_references）、节点签名冲突（workspace.conflicts）、
构建预检（workspace.preflight）、字幕烟测（studio_render.check_font）——不另立一套判断。
调用方负责只读加载工作台；这里不加锁、不 persist。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mindex.admin.curation import EntityNotFound
from mindex.models.storyboard import SOFT_REASONS, coverage_warning, is_long, soft_issues
from mindex.shared.config import Config
from mindex.shared.media import MediaError
from mindex.shared.storage import Store
from mindex.shared.types import ShotDraft
from mindex.studio import studio
from mindex.studio import workspace as workspace
from mindex.studio.studio_models import validate_recall_references

SOFT_LABELS = {**SOFT_REASONS, "long": "文本过长"}
# 逐镜阶段按注册顺序（含只属于部分来源的 generate）；不属于某分镜来源的阶段在该分镜上没有选用版本。
SHOT_STAGES = workspace.stages.names(scope="shot")
NODE_STAGES = (*SHOT_STAGES, "compose")


def _contents(w, p):
    """按方案顺序返回 (分镜 ID, content 数据)；content 版本缺失时数据为 None。"""
    result = []
    for sid in workspace.ordered_shots(w, p):
        vid = p["shots"].get(sid, {}).get("content")
        item = w["versions"].get(vid) if vid else None
        result.append((sid, item["data"] if item else None))
    return result


def storyboard_checks(config: Config, store: Store, job_id, w, p) -> dict:
    """hard：阻止保存或构建的第一个问题；soft：逐镜建议；coverage：分镜是否按顺序覆盖文案。"""
    contents = _contents(w, p)
    hard = None
    if not contents:
        hard = "方案还没有分镜，请先拆分镜"
    elif len(contents) > config.studio.max_shots:
        hard = f"分镜数量 {len(contents)} 超过上限 {config.studio.max_shots}"
    if hard is None:
        for index, (sid, data) in enumerate(contents, 1):
            if data is None:
                hard = f"第 {index} 个分镜（{sid}）的内容版本缺失"
                break
            if error := studio.visual_error([data]):
                hard = error.replace("第 1 个分镜", f"第 {index} 个分镜", 1)
                break
            try:
                shot = studio.Shot.model_validate(data)
                validate_recall_references(config, store, job_id, [shot])
            except (studio.StudioError, EntityNotFound, ValueError) as exc:
                hard = f"第 {index} 个分镜：{exc}"
                break

    valid = [(sid, data) for sid, data in contents if data is not None]
    drafts = [
        ShotDraft(
            visual=data.get("visual") or "",
            text=data["text"],
            query=data["query"],
            estimated_seconds=data["estimated_seconds"],
        )
        for _sid, data in valid
    ]
    issues = {issue.index: list(issue.kinds or (issue.kind,)) for issue in soft_issues(drafts)}
    for index, draft in enumerate(drafts, 1):
        if is_long(draft.text):
            issues.setdefault(index, []).append("long")
    soft = [
        {
            "index": index,
            "shot_id": valid[index - 1][0],
            "kinds": kinds,
            "reasons": [SOFT_LABELS[kind] for kind in kinds],
        }
        for index, kinds in sorted(issues.items())
    ]

    coverage = None
    if p.get("script") and valid:
        if workspace.script_stale(p):
            coverage = "文案已变化，分镜来自旧文案，构建时会重新拆分镜"
        else:
            coverage = coverage_warning(workspace.script_text(w, p) or "", [d.text for d in drafts])
    elif not p.get("script"):
        coverage = "方案还没有文案"
    return {"hard": hard, "soft": soft, "coverage": coverage}


def subtitle_checks(config: Config, p) -> dict:
    """只在方案烧录字幕时做环境烟测：滤镜存在性与 1 帧字体渲染。"""
    from mindex.studio.studio_render import check_font, has_subtitles_filter

    burn = bool(p["options"].get("burn"))
    if not burn:
        return {"burn": False, "filter": None, "font": None}
    try:
        available = has_subtitles_filter()
    except MediaError:
        available = False
    with tempfile.TemporaryDirectory(prefix="studio-diagnostics-") as directory:
        srt = Path(directory) / "check.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,200\n字幕测试 Aa\n", encoding="utf-8")
        try:
            check_font(srt, config.studio.subtitle)
            font = "ok"
        except MediaError as exc:
            font = str(exc)
    return {"burn": True, "filter": available, "font": font}


def _selected(p, stage, sid):
    if stage == "compose":
        return p.get("final")
    return p["shots"].get(sid, {}).get(stage)


def node_states(config: Config, job_id, w, p, context) -> list[dict]:
    """每个已选节点：ok / missing / failed / corrupt / stale，stale 的原因与工作台 conflicts 一致。"""
    nodes = []
    targets = [(stage, sid) for sid in workspace.ordered_shots(w, p) for stage in SHOT_STAGES]
    targets.append(("compose", None))
    for stage, sid in targets:
        vid = _selected(p, stage, sid)
        if not vid:
            continue
        key = f"{stage}:{sid}" if sid else stage
        node = {"key": key, "stage": stage, "shot_id": sid, "version": vid, "state": "ok", "reasons": []}
        item = w["versions"].get(vid)
        if item is None:
            node.update(state="missing", reasons=["已选版本记录缺失"])
        elif item["status"] != "complete":
            node.update(state="failed", reasons=[item.get("error") or "已选版本未成功完成"])
        elif not workspace.valid_files(config, job_id, item):
            node.update(state="corrupt", reasons=["版本文件缺失或损坏"])
        else:
            try:
                reasons = workspace.conflicts(config, job_id, w, p, stage, sid, vid, context=context)
            except (studio.StudioError, EntityNotFound) as exc:
                reasons = [str(exc)]
            if reasons:
                node.update(state="stale", reasons=list(reasons))
        nodes.append(node)
    return nodes


def inputs_by_stage(config: Config, job_id, w, p, context) -> dict:
    """各逐镜阶段每个分镜当前规格的 inputs：与 column_nodes 校验的值相同，可直接作为 inputs_by_shot 提交。"""
    result = {}
    for stage in SHOT_STAGES:
        values = {}
        for sid in workspace.ordered_shots(w, p):
            if stage not in workspace.STAGES and stage not in workspace.stages.plan_shot_stages(w, p, sid):
                continue  # 来源专属阶段（generate）只对含该阶段的分镜给出 inputs
            try:
                values[sid] = workspace.specification(config, job_id, w, p, stage, sid, context=context)["inputs"]
            except (studio.StudioError, EntityNotFound):
                continue
        result[stage] = values
    return result


def build_plan(config: Config, job_id, w, p) -> dict:
    """构建预检摘要：每个节点会复用、重建、跳过还是阻塞，以及原因。"""
    try:
        checked = workspace.preflight(config, job_id, w, p)
    except (studio.StudioError, EntityNotFound) as exc:
        return {"error": str(exc), "nodes": []}
    return {
        "error": None,
        "nodes": [
            {key: n.get(key) for key in ("key", "stage", "shot_id", "action", "version", "reason")}
            for n in checked["nodes"]
        ],
    }


def diagnostics(config: Config, job_id, w, p, *, store: Store | None = None) -> dict:
    store = store or Store(config.data_dir)
    context = workspace.DetailContext(store=store, display=True)
    running = next((b["id"] for b in w["builds"] if b["status"] == "running"), None)
    return {
        "plan_id": p["id"],
        "revision": workspace.revision(p),
        "active": w["active_plan"] == p["id"],
        "running_build": running,
        "storyboard": storyboard_checks(config, store, job_id, w, p),
        "subtitle": subtitle_checks(config, p),
        "nodes": node_states(config, job_id, w, p, context),
        "preflight": build_plan(config, job_id, w, p),
        "inputs_by_stage": inputs_by_stage(config, job_id, w, p, context),
    }
