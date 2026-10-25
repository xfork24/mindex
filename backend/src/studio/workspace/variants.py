"""多画幅重新渲染的关联子方案：主方案的某个导出画幅设为 rerender 时，工作台里有一个按该画幅设置的子方案。

子方案记录 ``variant_of = {"plan": 主方案 ID, "canvas": 画幅}``，与主方案共享分镜内容（content 版本）与文案，
方案参数除画幅、适配方式与导出设置外与主方案一致；它像普通方案一样构建，配音、选片等未受画幅影响的产物按签名
直接复用。主方案的分镜或参数变化后由 ``sync`` 同步到子方案（逐镜指针保留，签名决定是否需要重建）。
"""

from __future__ import annotations

import copy
from uuid import uuid4

from mindex.studio import studio

SYNCED = ("board", "board_script", "script", "ranges", "anchors")
# 与画幅无关的逐镜阶段：沿用主方案的选用；画面生成、叠加层与渲染与画幅有关，保留子方案自己的（签名决定复用）。
SHARED_STAGES = ("content", "recall", "tts", "music", "select")


def child_plan(w, p, canvas) -> dict | None:
    for item in w["plans"].values():
        link = item.get("variant_of") or {}
        if link.get("plan") == p["id"] and link.get("canvas") == canvas:
            return item
    return None


def children(w, p) -> list[dict]:
    return [item for item in w["plans"].values() if (item.get("variant_of") or {}).get("plan") == p["id"]]


def wanted(p) -> dict[str, dict]:
    return {item["canvas"]: item for item in p["options"].get("exports") or [] if item.get("mode") == "rerender"}


def child_options(p, item) -> dict:
    return {**copy.deepcopy(p["options"]), "canvas": item["canvas"], "fit": item["fit"], "exports": None}


def sync(w, p) -> list[str]:
    """按主方案的 rerender 导出设置创建或同步子方案；返回子方案 ID。不再需要的子方案保留（解除关联）。"""
    if p.get("variant_of"):
        return []
    found = []
    targets = wanted(p)
    for canvas, item in targets.items():
        child = child_plan(w, p, canvas)
        if child is None:
            child = {
                "id": uuid4().hex[:12], "name": f"{p['name']} · {canvas}", "created_at": studio.now(), "shots": {},
                "final": None, "variant_of": {"plan": p["id"], "canvas": canvas},
            }
            w["plans"][child["id"]] = child
        child["options"] = child_options(p, item)
        for key in SYNCED:
            child[key] = copy.deepcopy(p.get(key))
        shots = {}
        for sid, selected in p["shots"].items():
            mine = child["shots"].get(sid) or {}
            shots[sid] = {**{k: v for k, v in mine.items() if k not in SHARED_STAGES},
                          **{k: v for k, v in selected.items() if k in SHARED_STAGES}}
        child["shots"] = shots
        found.append(child["id"])
    for child in children(w, p):
        if child["variant_of"]["canvas"] not in targets:
            child.pop("variant_of", None)
            child["name"] = f"{child['name']}（已解除关联）"
    return found


def preflight_all(config, job_id, w, p, *, force=False) -> dict:
    """构建全部画幅的预检：先是各关联子方案的逐镜与成片节点（键加 ``子方案ID/`` 前缀、带 ``plan_id``），
    再是主方案自己的节点；主方案的重新渲染导出不再因子方案尚无成片而阻塞（同一次构建里先构建子方案）。"""
    from mindex.shared.config import fingerprint

    from .preflight import preflight
    from .stages import names
    from .versions import revision

    plan_stages = set(names(scope="plan"))
    nodes = []
    for canvas in wanted(p):
        child = child_plan(w, p, canvas)
        if child is None:
            raise studio.StudioError(f"还没有 {canvas} 画幅的重新渲染子方案，请先保存导出设置")
        for node in preflight(config, job_id, w, child, force=force)["nodes"]:
            if node["stage"] in plan_stages:
                continue
            nodes.append({**node, "key": f"{child['id']}/{node['key']}", "plan_id": child["id"]})
    own = preflight(config, job_id, w, p, force=force)
    for node in own["nodes"]:
        if node["stage"] == "export" and node["action"] == "blocked" and node["shot_id"] in wanted(p):
            node = {**node, "action": "rebuild", "reason": "子方案在本次构建中先完成"}
        nodes.append(node)
    result = {"plan_id": p["id"], "revision": revision(p), "force": force, "all_canvases": True, "nodes": nodes}
    result["token"] = fingerprint(result)
    return result
