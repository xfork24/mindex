"""构建预检：节点依赖、整体预检与列重生成节点。复用判断经 signatures 模块属性调用。

各阶段的节点、依赖与阻塞原因来自 ``stages.REGISTRY``；每个分镜的阶段序列由 ``stages.shot_stages``
按镜头来源决定。"""

from __future__ import annotations

import copy

from shared.config import fingerprint
from studio import studio
from studio.workspace import signatures, stages
from studio.workspace.stages import handler, names
from studio.workspace.versions import ordered_shots, revision, valid_files


def node_dependencies(w, p, stage, sid, *, resplit=False):
    return handler(stage).dependencies(w, p, sid, resplit=resplit)


def preflight(config, job_id, w, p, *, force=False):
    draft = copy.deepcopy(p)
    nodes = [node for name in names(scope="plan") if (node := handler(name).plan_node(config, job_id, w, p))]
    resplit = any(n["stage"] == "split" for n in nodes)
    # 待重新拆分时旧分镜的逐镜节点没有意义：拆分完成后由执行器按新分镜重新预检。
    shots = [] if resplit else ordered_shots(w, p)
    sequences = {sid: stages.plan_shot_stages(w, p, sid) for sid in shots}
    # 按注册顺序逐阶段展开（同一阶段的全部分镜相邻），每个分镜只取其来源包含的阶段。
    for stage in names(scope="shot"):
        stage_handler = handler(stage)
        for sid in shots:
            if stage not in sequences[sid]:
                continue
            key = f"{stage}:{sid}"
            found = None if force else signatures.match(config, job_id, w, draft, stage, sid)
            spec = signatures.specification(config, job_id, w, draft, stage, sid)
            node = {
                "key": key,
                "stage": stage,
                "shot_id": sid,
                "action": "reuse" if found else "rebuild",
                "version": found,
                "reason": "匹配成功版本" if found else "缺少匹配的成功版本",
            }
            stage_handler.preflight(config, job_id, w, p, sid, spec, found, node)
            nodes.append(node)
            draft["shots"][sid][stage] = found or f"@{key}"
    # 成片阶段按注册顺序、逐变体展开；草稿写入占位，下游成片阶段（导出、封面、发布包）的签名据此看到上游。
    for stage in names(scope="final"):
        stage_handler = handler(stage)
        for variant in stage_handler.variants(config, w, p):
            key = stages.final_key(stage, variant)
            found = (
                None
                if force or not p["board"] or resplit
                else signatures.match(config, job_id, w, draft, stage, variant)
            )
            node = {
                "key": key,
                "stage": stage,
                "shot_id": variant,
                "action": "reuse" if found else "rebuild",
                "version": found,
                "reason": stage_handler.reason,
            }
            if p["board"] and not resplit:
                spec = signatures.specification(config, job_id, w, draft, stage, variant)
                stage_handler.preflight(config, job_id, w, p, variant, spec, found, node)
            nodes.append(node)
            stage_handler.assign(draft, variant, found or f"@{key}")
    # Predict blockers transitively; the worker still rechecks every dependency.
    blocked = {n["key"] for n in nodes if n["action"] == "blocked"}
    for n in nodes:
        sid, stage = n["shot_id"], n["stage"]
        deps = node_dependencies(w, p, stage, sid, resplit=resplit)
        if any(d in blocked for d in deps):
            n.update(action="blocked", reason="必需依赖阻塞")
            blocked.add(n["key"])
    result = {"plan_id": p["id"], "revision": revision(p), "force": force, "nodes": nodes}
    result["token"] = fingerprint(result)
    return result


def column_nodes(config, job_id, w, p, stage, inputs_by_shot):
    if stage == stages.PICTURE_COLUMN:
        # 画面列：每个分镜执行各自的画面阶段（召回 / 生成图片 / 生成视频 / 生成动画），合在一次构建里。
        targets = [
            (sid, stages.picture_stage(stages.plan_shot_stages(w, p, sid))) for sid in ordered_shots(w, p)
        ]
        targets = [(sid, name) for sid, name in targets if name]
    else:
        stage_handler = handler(stage)
        if stage_handler.scope != "shot":
            return stage_handler.column_nodes(config, job_id, w, p, inputs_by_shot)
        # 只有画面来源包含该阶段的分镜参与（例如图片分镜没有召回与选片）。
        targets = [(sid, stage) for sid in ordered_shots(w, p) if stage in stages.plan_shot_stages(w, p, sid)]
    ids = [sid for sid, _ in targets]
    if not ids or set(inputs_by_shot) != set(ids):
        raise studio.StudioError("列重生成必须绑定当前方案全部分镜")
    nodes = []
    for sid, stage in targets:
        stage_handler = handler(stage)
        spec = signatures.specification(config, job_id, w, p, stage, sid)
        if inputs_by_shot[sid] != spec["inputs"]:
            raise studio.StudioError(f"分镜 {sid} 输入版本已变化，请刷新后重试")
        reason = None
        for dependency, vid in spec["inputs"].items():
            item = w["versions"].get(vid)
            if not item or item["status"] != "complete" or not valid_files(config, job_id, item):
                reason = f"依赖 {dependency} 缺失、失败或文件损坏"
                break
        if reason is None:
            reason = stage_handler.column_reason(w, p, sid, spec)
        nodes.append(
            {
                "key": f"{stage}:{sid}",
                "stage": stage,
                "shot_id": sid,
                "action": "blocked" if reason else "rebuild",
                "reason": reason,
            }
        )
    return nodes
