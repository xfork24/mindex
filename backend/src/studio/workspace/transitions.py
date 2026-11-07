"""分镜转场：镜内转场（enter/leave，作用于单镜画面、不改时长）与重叠转场（next，本镜与下一镜交叉过渡）。

重叠转场靠"尾部余量"实现：声明了 ``next`` 的分镜在 render 阶段多渲染 ``duration``
秒画面（素材类优先从源继续取，
不够时冻结末帧），compose 用 xfade 吃掉这段余量，所以成片总帧数、配音、字幕与背景音乐时间线都与硬切完全相同。
过渡从切点开始：下一镜的配音与字幕照常在切点开始，画面在下一镜开头的 ``duration`` 秒内由本镜余量过渡到下一镜。

生效规则（``effective``）：
- 最后一镜的 ``next`` 没有下一镜可过渡，忽略；
- 本镜有生效的 ``next`` 时，本镜的 ``leave`` 被重叠转场取代；上一镜有生效的 ``next`` 时，本镜的 ``enter``
同样被取代；
- 镜内转场时长截到镜头时长的一半以内（执行时按实际配音时长截断并记录提示）。
"""

from __future__ import annotations

from .versions import ordered_shots

# 改动镜内转场滤镜或余量规则时递增：进入有转场分镜的 render 签名与有重叠转场的 compose 签名。
TRANSITION_VERSION = 1
EDGE_LABELS = {"fade": "黑场", "white": "白场", "zoom": "缩放", "slide": "滑动"}


def content_transition(w, p, sid) -> dict:
    selected = p["shots"].get(sid) or {}
    item = w["versions"].get(selected.get("content"))
    return dict((item or {}).get("data", {}).get("transition") or {})


def effective(w, p, sid) -> dict:
    """分镜实际生效的转场：``{"enter"?, "leave"?, "next"?}``，并附被忽略项的说明 ``ignored``（不进签名）。"""
    order = ordered_shots(w, p)
    if sid not in order:
        return {}
    index = order.index(sid)
    own = content_transition(w, p, sid)
    result, ignored = {}, []
    previous_next = index > 0 and bool(content_transition(w, p, order[index - 1]).get("next"))
    if own.get("next"):
        if index == len(order) - 1:
            ignored.append("最后一镜没有下一镜，重叠转场已忽略")
        else:
            result["next"] = own["next"]
    if own.get("enter"):
        if previous_next:
            ignored.append("上一镜设置了重叠转场，本镜入场转场已忽略")
        else:
            result["enter"] = own["enter"]
    if own.get("leave"):
        if "next" in result:
            ignored.append("本镜设置了重叠转场，出场转场已忽略")
        else:
            result["leave"] = own["leave"]
    if ignored:
        result["ignored"] = ignored
    return result


def render_payload(w, p, sid) -> dict | None:
    """进入 render 签名的转场参数；没有生效转场时为 None（签名与旧版一致）。"""
    value = {key: item for key, item in effective(w, p, sid).items() if key in ("enter", "leave", "next")}
    if not value:
        return None
    # 重叠转场只影响本镜的余量长度，过渡样式在 compose 里实现。
    if "next" in value:
        value["next"] = {"duration": value["next"]["duration"]}
    return value


def handle_seconds(w, p, sid) -> float:
    found = effective(w, p, sid).get("next")
    return float(found["duration"]) if found else 0.0


def overlaps(w, p) -> list[list]:
    """compose 签名里的重叠转场：``[[分镜, {kind, duration}], ...]``；没有时为空。"""
    return [
        [sid, found["next"]] for sid in ordered_shots(w, p) if (found := effective(w, p, sid)).get("next")
    ]


def clamp_edges(found: dict, seconds: float) -> tuple[dict | None, dict | None, list[str]]:
    """按镜头实际时长截断镜内转场：单个不超过一半，入场与出场合计不超过镜头时长。"""
    notes = []
    edges = {}
    for side in ("enter", "leave"):
        edge = found.get(side)
        if not edge:
            continue
        limit = seconds / 2
        if edge["duration"] > limit:
            notes.append(
                f"{'入场' if side == 'enter' else '出场'}转场 {edge['duration']:g} 秒超过镜头时长的一半，"
                f"已截到 {limit:.2f} 秒"
            )
            edge = {**edge, "duration": round(limit, 3)}
        edges[side] = edge
    return edges.get("enter"), edges.get("leave"), notes


def describe(transition: dict | None) -> str:
    """转场设置的一行中文摘要（agent 预览、CLI 输出）。"""
    if not transition:
        return "硬切（无转场）"
    parts = []
    for side, label in (("enter", "入场"), ("leave", "出场")):
        if edge := transition.get(side):
            parts.append(f"{label}{EDGE_LABELS.get(edge['kind'], edge['kind'])} {edge['duration']:g} 秒")
    if found := transition.get("next"):
        parts.append(f"到下一镜重叠转场 {found.get('kind', 'fade')} {found['duration']:g} 秒")
    return "，".join(parts) or "硬切（无转场）"


def shots_with_transitions(w, p) -> list[str]:
    return [sid for sid in ordered_shots(w, p) if content_transition(w, p, sid)]
