"""分镜详情：不截断的旁白/查询/画面意象，以及字幕时间轴与配音时长。

全部为不依赖 I/O 的纯函数，输入 ``PlanView``，供 ``get_shot_details`` 工具使用。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from mindex.agent.ids import resolve_shot, short
from mindex.agent.registry import MAX_TEXT
from mindex.agent.tools.common import STAGE_LABELS, PlanView, shot_stage_names, stage_mark


def plan_header(view: PlanView) -> dict:
    return {
        "job_id": view.job["id"],
        "plan_id": view.plan_id,
        "plan_name": view.plan.get("name"),
        "revision": view.revision,
        "active": view.active,
        "total": len(view.order),
    }


def _selected(view: PlanView, sid: str, stage: str) -> tuple[str | None, dict | None]:
    vid = (view.plan.get("shots") or {}).get(sid, {}).get(stage)
    item = view.version(vid)
    return vid, item if isinstance(item, dict) else None


def subtitle_info(view: PlanView, sid: str, narration: str) -> dict:
    _, item = _selected(view, sid, "select")
    if not item or item.get("status") != "complete":
        return {"available": False}
    data = item.get("data")
    event = data.get("event") if isinstance(data, dict) else None
    cue = event.get("subtitle") if isinstance(event, dict) else None
    if not isinstance(cue, dict) or not all(key in cue for key in ("text", "start", "end")):
        return {"available": False}
    return {"available": True, "text": cue["text"], "start": cue["start"], "end": cue["end"],
            "stale": cue["text"] != narration}


def speech_info(view: PlanView, sid: str) -> dict:
    vid, item = _selected(view, sid, "tts")
    if not item or item.get("status") != "complete":
        return {"available": False}
    data = item.get("data")
    duration = data.get("duration") if isinstance(data, dict) else None
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return {"available": False}
    return {"available": True, "duration": duration, "version": short(vid)}


def generation_info(view: PlanView, sid: str) -> dict | None:
    """aigc 分镜当前选中的生成画面版本；非 aigc 分镜返回 None。"""
    if ((view.content(sid) or {}).get("source") or {}).get("kind") != "aigc":
        return None
    vid, item = _selected(view, sid, "generate")
    if not item:
        return {"available": False, "status": "missing"}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    info = {"available": item.get("status") == "complete", "status": item.get("status"), "version": short(vid)}
    for key in ("prompt", "seconds", "duration", "width", "height"):
        if data.get(key) is not None:
            info[key] = data[key]
    if item.get("error"):
        info["error"] = item["error"]
    return info


def _version_info(view: PlanView, sid: str, stage: str, keys: tuple[str, ...]) -> dict:
    vid, item = _selected(view, sid, stage)
    if not item:
        return {"available": False, "status": "missing"}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    info = {"available": item.get("status") == "complete", "status": item.get("status"), "version": short(vid)}
    info.update({key: data[key] for key in keys if data.get(key) is not None})
    if item.get("error"):
        info["error"] = item["error"]
    return info


def drawing_info(view: PlanView, sid: str) -> dict | None:
    """aigc_image 分镜当前选中的生成图片版本；其他来源返回 None。"""
    if ((view.content(sid) or {}).get("source") or {}).get("kind") != "aigc_image":
        return None
    return _version_info(view, sid, "draw", ("prompt", "width", "height"))


def music_info(view: PlanView, sid: str) -> dict | None:
    """背景音乐：提示点分镜给出提示点与生成版本，顺延分镜给出所属提示点；没有背景音乐返回 None。"""
    belong = (view.w.get("shot_music") or {}).get(sid) if view.active else None
    cue = (view.content(sid) or {}).get("music")
    if not cue and not belong:
        return None
    info = {"cue": cue}
    if belong and belong.get("from") and not belong.get("start"):
        info["continues"] = short(belong["from"])
    if cue and cue.get("kind") in ("generate", "cover"):
        info["generated"] = _version_info(view, sid, "music", ("duration", "prompt"))
    return info


def animation_source_url(view: PlanView, vid: str) -> str:
    return f"/api/studio/jobs/{view.job['id']}/versions/{vid}/animation.html"


def animation_info(view: PlanView, sid: str) -> dict | None:
    """animation 分镜当前选中的动画源码版本与录制（渲染）状态；非 animation 分镜返回 None。"""
    if ((view.content(sid) or {}).get("source") or {}).get("kind") != "animation":
        return None
    vid, item = _selected(view, sid, "animate")
    if not item:
        return {"available": False, "status": "missing"}
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    info = {"available": item.get("status") == "complete", "status": item.get("status"), "version": short(vid)}
    for key in ("origin", "chars", "brief", "seconds", "width", "height"):
        if data.get(key) is not None:
            info[key] = data[key]
    if info["available"] and "animation" in (item.get("files") or {}):
        info["source_url"] = animation_source_url(view, vid)
    if item.get("error"):
        info["error"] = item["error"]
    rid, render = _selected(view, sid, "render")
    if render and render.get("status") == "complete":
        recorded = render.get("data") if isinstance(render.get("data"), dict) else {}
        info["recording"] = {"version": short(rid), "frames": recorded.get("frames"), "fps": recorded.get("fps"),
                             "current": (render.get("inputs") or {}).get("animate") == vid}
    return info


def shot_detail(view: PlanView, sid: str, index: int) -> dict:
    content = view.content(sid)
    return {
        "index": index,
        "short_id": short(sid),
        "shot_id": sid,
        "text": content["text"],
        "query": content["query"],
        "visual": content.get("visual") or None,
        "estimated_seconds": content["estimated_seconds"],
        "granularity": content.get("granularity"),
        "recall_limit": content.get("recall_limit"),
        "recall": content.get("recall") or None,
        "source": content.get("source") or None,
        "manual_range": (view.plan.get("ranges") or {}).get(sid),
        "stages": {stage: view.stage_state(sid, stage) for stage in shot_stage_names(content)},
        "subtitle": subtitle_info(view, sid, content["text"]),
        "speech": speech_info(view, sid),
        "generation": generation_info(view, sid),
        "animation": animation_info(view, sid),
        "drawing": drawing_info(view, sid),
        "music": music_info(view, sid),
    }


def page_details(view: PlanView, offset: int, limit: int) -> list[dict]:
    order = view.order
    return [shot_detail(view, sid, offset + i + 1) for i, sid in enumerate(order[offset:offset + limit])]


MAX_FOCUS_SHOTS = 20


def resolve_refs(view: PlanView, refs: list[str]) -> list[dict]:
    """按提供顺序逐项解析引用；ok 项附完整 Shot_Detail，失败项只给状态与候选/原因。"""
    order = view.order
    details: dict[str, dict] = {}
    items: list[dict] = []
    for ref in refs:
        found = resolve_shot(order, ref)
        status = found["status"]
        if status == "ok":
            sid, index = found["shot_id"], found["index"]
            if sid not in details:
                details[sid] = shot_detail(view, sid, index)
            items.append({"ref": ref, "status": "ok", "index": index, "shot_id": sid, "detail": details[sid]})
        elif status == "ambiguous":
            items.append({"ref": ref, "status": "ambiguous",
                          "candidates": [{"index": c["index"], "shot_id": c["shot_id"]}
                                         for c in found["candidates"]]})
        else:
            items.append({"ref": ref, "status": "not_found",
                          "reason": found.get("reason") or "没有匹配的分镜"})
    return items


def details_focus(view: PlanView, items: list[dict] | None) -> dict:
    """页面定位：有解析成功的分镜时定位到这些分镜（去重、最多 20 个），否则定位到分镜工作台。"""
    focus = {"page": "studio", "job_id": view.job["id"], "plan_id": view.plan_id}
    shots: list[str] = []
    for item in items or []:
        if item["status"] == "ok" and item["shot_id"] not in shots:
            shots.append(item["shot_id"])
    if shots:
        focus["shots"] = shots[:MAX_FOCUS_SHOTS]
    else:
        focus["section"] = "storyboard"
    return focus


# ---------------------------------------------------------------- 渲染

NONE = "（无）"


def _quoted(value) -> str:
    """字符串原样放进「」；空值显示「（无）」。"""
    return f"「{value}」" if value else NONE


def _json(value) -> str:
    return NONE if value in (None, {}, []) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plain(value) -> str:
    return NONE if value is None else f"{value!r}" if isinstance(value, (int, float)) else str(value)


def _stage_cell(state: dict) -> str:
    mark = stage_mark(state)
    return f"{mark} {state['version']}" if state.get("version") else mark


def _stage_status(label: str, state: dict) -> str:
    text = f"{label} {state.get('status', 'missing')}"
    return text + (f" 版本 {state['version']}" if state.get("version") else "")


def render_detail(detail: dict) -> str:
    """单个分镜块；旁白、查询、画面意象、字幕文本原样输出，不截断。"""
    stages = detail["stages"]
    lines = [
        f"## #{detail['index']} {detail['short_id']}（完整ID {detail['shot_id']}）",
        f"旁白：「{detail['text']}」",
        f"查询：{_quoted(detail['query'])}",
        f"画面意象：{_quoted(detail['visual'])}",
        f"预估时长：{_plain(detail['estimated_seconds'])} 秒；召回粒度：{_plain(detail['granularity'])}；"
        f"召回数量：{_plain(detail['recall_limit'])}",
        f"召回设置：{_json(detail['recall'])}",
        f"画面来源：{_json(detail.get('source')) if detail.get('source') else '素材召回（默认）'}",
        f"人工区间：{_json(detail['manual_range'])}",
        "阶段：" + " | ".join(f"{STAGE_LABELS[s]} {_stage_cell(state)}" for s, state in stages.items()),
    ]
    sub = detail["subtitle"]
    if sub.get("available"):
        line = f"字幕：「{sub['text']}」 {sub['start']!r}–{sub['end']!r} 秒"
        if sub.get("stale"):
            line += "（字幕已过期：按旧旁白生成，与当前旁白不一致）"
        lines.append(line)
    else:
        lines.append(f"字幕：缺失（{_stage_status('选片', stages['select'])}）")
    speech = detail["speech"]
    if speech.get("available"):
        lines.append(f"配音时长：{speech['duration']!r} 秒（版本 {speech['version']}）")
    else:
        lines.append(f"配音时长：缺失（{_stage_status('配音', stages['tts'])}）")
    generation = detail.get("generation")
    if generation is not None:
        if generation.get("available"):
            lines.append(f"生成画面：版本 {generation['version']}，{generation.get('duration', 0):.2f} 秒"
                         f"（请求 {generation.get('seconds')!r} 秒），提示词「{generation.get('prompt') or ''}」")
        else:
            reason = f"：{generation['error']}" if generation.get("error") else ""
            lines.append(f"生成画面：{generation.get('status') or 'missing'}"
                         + (f" 版本 {generation['version']}" if generation.get("version") else "") + reason)
    drawing = detail.get("drawing")
    if drawing is not None:
        lines.append(f"生成图片：{'版本 ' + drawing['version'] if drawing.get('available') else drawing.get('status')}"
                     + (f"，{drawing.get('width')}×{drawing.get('height')}，提示词「{drawing.get('prompt') or ''}」"
                        if drawing.get("available") else (f"：{drawing['error']}" if drawing.get("error") else "")))
    music = detail.get("music")
    if music is not None:
        cue = music.get("cue")
        if cue:
            line = f"背景音乐提示点：{_json(cue)}"
            generated = music.get("generated")
            if generated:
                line += (f"；生成版本 {generated['version']}（{generated.get('duration', 0):.1f} 秒）"
                         if generated.get("available") else f"；生成状态 {generated.get('status')}")
            lines.append(line)
        elif music.get("continues"):
            lines.append(f"背景音乐：顺延分镜 {music['continues']} 的背景音乐")
    animation = detail.get("animation")
    if animation is not None:
        if animation.get("available"):
            origin = {"manual": "人工修改", "generated": "模型生成"}.get(animation.get("origin"), "未知来源")
            line = (f"动画源码：版本 {animation['version']}（{origin}，{animation.get('chars', 0)} 字符，"
                    f"{animation.get('seconds')!r} 秒，{animation.get('width')}×{animation.get('height')}）")
            if animation.get("source_url"):
                line += f"，沙箱预览 {animation['source_url']}"
            lines.append(line)
            recording = animation.get("recording")
            if recording:
                lines.append(f"动画录制：渲染版本 {recording['version']}，{recording.get('frames')} 帧 @ "
                             f"{recording.get('fps')} fps" + ("" if recording.get("current") else "（录制的是旧源码，需重新渲染）"))
            else:
                lines.append("动画录制：尚未渲染（构建或 run_node(stage=render) 后逐帧录制）")
        else:
            reason = f"：{animation['error']}" if animation.get("error") else ""
            lines.append(f"动画源码：{animation.get('status') or 'missing'}"
                         + (f" 版本 {animation['version']}" if animation.get("version") else "") + reason)
    return "\n".join(lines)


def _compose(head: str, blocks: list[str], tail: str) -> str:
    return "\n".join(part for part in (head, *blocks, tail) if part)


def fit_blocks(head: str, blocks: list[str], tail: Callable[[int], str], budget: int = MAX_TEXT) -> int:
    """可放入的完整块数 k：blocks 非空时 k ≥ 1，且 k > 1 时整段文本不超过 budget。"""
    if not blocks:
        return 0
    k = 1
    while k < len(blocks) and len(_compose(head, blocks[:k + 1], tail(k + 1))) <= budget:
        k += 1
    return k


def _title(header: dict) -> str:
    flag = "当前方案" if header["active"] else "非当前方案"
    return (f"任务 {header['job_id']} 方案 {header['plan_name']}({header['plan_id']})【{flag}】 "
            f"revision={header['revision']}")


def render_list(header: dict, details: list[dict], offset: int) -> tuple[str, int]:
    """列表模式全文与实际输出的分镜数（受 MAX_TEXT 约束，只在完整分镜边界截止）。"""
    title, total = _title(header), header["total"]
    if total == 0:
        return f"{title}\n当前方案还没有分镜。", 0
    if not details:
        return f"{title}\n分镜共 {total} 个\noffset={offset} 已超出分镜范围（共 {total} 个分镜）。", 0

    def head(k: int) -> str:
        return f"{title}\n分镜共 {total} 个；本次第 {offset + 1}–{offset + k} 个"

    def tail(k: int) -> str:
        return f"还有后续分镜：下一次请用 offset={offset + k} 读取。" if offset + k < total else ""

    blocks = [render_detail(d) for d in details]
    # 头部的范围随 k 变化；按最长形式预留，保证最终文本不超预算
    reserve = len(head(len(blocks))) - len(title)
    shown = fit_blocks(title, blocks, tail, MAX_TEXT - reserve)
    return _compose(head(shown), blocks[:shown], tail(shown)), shown


def _ref_block(item: dict) -> str:
    ref = item["ref"]
    if item["status"] == "ok":
        return f"{ref} → #{item['index']} {item['shot_id']}\n{render_detail(item['detail'])}"
    if item["status"] == "ambiguous":
        names = "、".join(f"#{c['index']} {c['shot_id']}" for c in item["candidates"])
        return f"{ref} → 有歧义，候选：{names}（请向用户确认）"
    reason = item.get("reason")
    return f"{ref} → 未找到" + (f"：{reason}" if reason else "")


def render_refs(header: dict, items: list[dict]) -> tuple[str, int]:
    """指定模式全文与实际渲染的结果项数；超预算时末尾列出未展开的引用。"""
    ok = sum(item["status"] == "ok" for item in items)
    head = f"{_title(header)}\n分镜共 {header['total']} 个；引用 {len(items)} 个，解析成功 {ok} 个"
    blocks = [_ref_block(item) for item in items]

    def tail(k: int) -> str:
        if k >= len(items):
            return ""
        return "以下引用因输出上限未展开，请分批再次调用：" + "、".join(item["ref"] for item in items[k:])

    rendered = fit_blocks(head, blocks, tail)
    return _compose(head, blocks[:rendered], tail(rendered)), rendered
