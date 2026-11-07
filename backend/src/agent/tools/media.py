"""工具结果附带的媒体：面板据此显示缩略图网格、大图浏览与音视频播放。

条目形状：``{kind: image|audio|video, url, title, poster?, playlist?, collapsed}``。
``playlist=True`` 表示 ``url`` 是镜头播放列表（由 shot-player 播放），否则是可直接加载的文件。
只允许本站 ``/api/...`` 地址；默认展开图片与成片，音频与单镜视频折叠（避免一次加载大量视频）。
"""

from __future__ import annotations

import re

from agent.ids import short

SAFE_URL = re.compile(r"^/api/[^\s]*$")
VIDEO = (".mp4", ".mov", ".webm", ".mkv")
AUDIO = (".wav", ".mp3", ".m4a", ".ogg", ".flac")
IMAGE = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_ITEMS = 24


def safe(url: str | None) -> bool:
    return bool(url) and bool(SAFE_URL.match(url)) and "//" not in url and ".." not in url


def item(
    kind: str,
    url: str,
    title: str,
    *,
    poster: str | None = None,
    playlist: bool = False,
    collapsed: bool | None = None,
) -> dict | None:
    if not safe(url) or (poster is not None and not safe(poster)):
        return None
    value = {
        "kind": kind,
        "url": url,
        "title": title,
        "collapsed": collapsed if collapsed is not None else kind == "audio",
    }
    if poster:
        value["poster"] = poster
    if playlist:
        value["playlist"] = True
    return value


def clean(items) -> list[dict]:
    """去掉空值、非本站地址与重复地址，截断到上限。"""
    seen, result = set(), []
    for value in items:
        if not value or value["url"] in seen or not safe(value["url"]):
            continue
        if value.get("poster") and not safe(value["poster"]):
            continue
        seen.add(value["url"])
        result.append(value)
        if len(result) >= MAX_ITEMS:
            break
    return result


def version_media(version: dict | None, title: str, *, collapsed: bool = True) -> list[dict]:
    """按文件扩展名取出版本里的视频、音频与图片（URL 由工作台详情注入）。"""
    if not version or version.get("status") != "complete":
        return []
    found = []
    for name, meta in (version.get("files") or {}).items():
        url, path = meta.get("url"), (meta.get("path") or "").lower()
        if path.endswith(VIDEO):
            found.append(
                item(
                    "video",
                    url,
                    f"{title} · {name}" if name not in ("mp4", "video", "generated") else title,
                    collapsed=collapsed,
                )
            )
        elif path.endswith(AUDIO):
            found.append(item("audio", url, title, collapsed=True))
        elif path.endswith(IMAGE):
            found.append(item("image", url, title, collapsed=False))
    # 渲染版本同时有 video、preview 与处理后的音轨：只保留主视频（视频自带声音），避免重复播放器。
    found = [f for f in found if f]
    videos = [f for f in found if f["kind"] == "video"]
    if videos:
        found = [f for f in found if f["kind"] != "audio" and (f["kind"] != "video" or f is videos[0])]
    return found


def candidate_media(version: dict | None, label: str, limit: int = 6) -> list[dict]:
    """召回候选：缩略图网格，点击在大图浏览里播放该片段。"""
    if not version or version.get("status") != "complete":
        return []
    found = []
    for rank, candidate in enumerate((version.get("data") or {}).get("results", [])[:limit], 1):
        segment = candidate.get("id")
        if not segment:
            continue
        found.append(
            item(
                "video",
                f"/api/admin/segments/{segment}/playlist",
                f"{label} 候选 {rank}",
                poster=candidate.get("thumbnail_url") or f"/api/admin/segments/{segment}/thumbnail",
                playlist=True,
                collapsed=False,
            )
        )
    return [f for f in found if f]


def shot_media(view, sid: str) -> list[dict]:
    index = view.order.index(sid) + 1 if sid in view.order else "?"
    label = f"#{index} {short(sid)}"
    selected = view.plan["shots"].get(sid, {})
    return (
        version_media(view.version(selected.get("tts")), f"{label} 配音")
        + generated_media(view, sid)
        + version_media(view.version(selected.get("render")), f"{label} 渲染")
        + candidate_media(view.version(selected.get("recall")), label)
    )


def generated_media(view, sid: str) -> list[dict]:
    """aigc 分镜当前选中的生成画面（generated.mp4）；其他来源或未生成时为空。"""
    vid = view.plan["shots"].get(sid, {}).get("generate")
    if not vid:
        return []
    index = view.order.index(sid) + 1 if sid in view.order else "?"
    return version_media(view.version(vid), f"#{index} {short(sid)} 生成画面")


def animation_media(view, sid: str) -> list[dict]:
    """animation 分镜当前渲染版本里的动画录制（展开显示）；其他来源或未渲染时为空。"""
    content = view.version(view.plan["shots"].get(sid, {}).get("content")) or {}
    if ((content.get("data") or {}).get("source") or {}).get("kind") != "animation":
        return []
    index = view.order.index(sid) + 1 if sid in view.order else "?"
    render = view.version(view.plan["shots"].get(sid, {}).get("render"))
    return version_media(render, f"#{index} {short(sid)} 动画录制", collapsed=False)


def final_media(view, version_id: str | None = None) -> list[dict]:
    vid = version_id or view.plan.get("final")
    return version_media(view.version(vid), f"成片 {short(vid)}", collapsed=False) if vid else []


def segment_media(result: dict, title: str) -> dict | None:
    segment = result.get("segment_id") or result.get("id")
    return item(
        "video",
        f"/api/playlists/{segment}",
        title,
        poster=result.get("thumbnail_url") or f"/api/thumbnails/{segment}",
        playlist=True,
        collapsed=False,
    )
