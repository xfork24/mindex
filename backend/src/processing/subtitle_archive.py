"""Explicit preparation-time archive of embedded subtitle dependencies."""

from __future__ import annotations

import json

from shared import checkpoints, files, lock_backend
from shared.media import MediaError, atomic_media, command, content_hash


def archive_path(store, source_id):
    return store.root / "subtitle-archives" / source_id / "manifest.json"


def load_archive(store, source_id):
    path = archive_path(store, source_id)
    payload = checkpoints.read_text(store, path)
    if payload is None:
        raise MediaError("Subtitle archive missing; run explicit preparation/migration")
    data = json.loads(payload)
    for relative, digest in data["files"].items():
        with store.materialize_media(relative) as path:
            if content_hash(path) != digest:
                raise MediaError("字幕归档摘要校验失败")
    return data


def archive_subtitles(store, source):
    manifest = archive_path(store, source["id"])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with lock_backend.resource(store, "subtitle-archive:" + source["id"], timeout=None):
        if checkpoints.read_text(store, manifest) is not None:
            return load_archive(store, source["id"])
        with store.materialize_media(source["path"]) as original:
            return _archive(store, source, manifest, original)


def _archive(store, source, manifest, original):
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from processing.dialogue import extract_subtitles, subtitle_tracks

    tracks = subtitle_tracks(original)
    metadata = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(original)]))
    data = {
        "version": 1,
        "tracks": tracks,
        "streams": metadata.get("streams", []),
        "utterances": {},
        "files": {},
    }
    for stream in data["streams"]:
        if stream.get("codec_type") == "attachment":
            target = manifest.parent / f"attachment-{stream['index']}.bin"
            command(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    f"-dump_attachment:{stream['index']}",
                    str(target),
                    "-i",
                    str(original),
                    "-t",
                    "0",
                    "-f",
                    "null",
                    "-",
                ]
            )
            data["files"][str(target.relative_to(store.root))] = content_hash(target)
    for track in tracks:
        extension = ".mp4" if track["codec"] == "mov_text" else ".mkv"
        raw = manifest.parent / f"track-{track['index']}{extension}"
        atomic_media(
            [
                "-copyts",
                "-i",
                str(original),
                "-map",
                f"0:{track['index']}",
                *(["-map", "0:t?"] if extension == ".mkv" else []),
                "-c",
                "copy",
            ],
            raw,
        )
        data["files"][str(raw.relative_to(store.root))] = content_hash(raw)
    for track in tracks:
        if track["text"]:
            data["utterances"][str(track["index"])] = extract_subtitles(
                original, track, json.loads(source["media"])["duration"]
            )
    for relative in data["files"]:
        files.publish(store, store.root / relative)
    checkpoints.save_text(store, manifest, json.dumps(data, ensure_ascii=False))
    return data
