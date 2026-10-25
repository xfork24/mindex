"""素材绑定的不可变外部字幕；上传只登记，开始对白任务才应用。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from charset_normalizer import from_bytes

from mindex.processing.dialogue import atomic_json, extract_subtitles
from mindex.shared.media import MediaError

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".smi", ".sami"}
SUBTITLE_MAX_BYTES = 20 * 1024 * 1024


def decode_subtitle(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8-sig") if not payload.startswith((b"\xff\xfe", b"\xfe\xff")) else payload.decode("utf-16")
        if "\x00" not in text:
            return text
    except UnicodeError:
        pass
    guessed = from_bytes(payload).best()
    if guessed and guessed.encoding in {"big5", "cp950", "gb18030", "gb2312", "gbk"}:
        text = str(guessed)
        if "\x00" not in text:
            return text
    encodings = ("utf-16",) if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else (
        "utf-8-sig", "gb18030", "big5",
    )
    for encoding in encodings:
        try:
            text = payload.decode(encoding)
            if "\x00" not in text:
                return text
        except UnicodeError:
            pass
    raise ValueError("无法识别字幕编码，请使用 UTF-8、UTF-16、GB18030 或 Big5 文本字幕")


def parse_subtitle_file(path: Path, suffix: str, duration: float, *, fps: float | None = None) -> list[dict]:
    text = decode_subtitle(path.read_bytes())
    # Normalize legacy Chinese encodings before ffmpeg's subtitle conversion.
    with tempfile.TemporaryDirectory(prefix="mindex-subtitle-") as directory:
        normalized = Path(directory) / ("subtitle" + suffix)
        normalized.write_text(text, encoding="utf-8")
        try:
            input_format = "microdvd" if suffix == ".sub" and re.match(r"\s*\{\d+\}\{\d+\}", text) else None
            rows = extract_subtitles(normalized, {"index": 0, "language": "und"}, duration,
                                     input_format=input_format, subtitle_fps=fps)
        except MediaError as exc:
            raise ValueError("字幕文件格式无效或不受支持，请检查字幕内容与扩展名") from exc
    if not rows:
        raise ValueError("字幕文件没有有效对白或时间戳超出素材范围")
    return rows


def upload_root(store, source_id):
    return store.root / "subtitles" / source_id / "uploads"


def register_subtitle(store, source_id: str, path: Path, name: str) -> dict:
    suffix = Path(name).suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        raise ValueError("不支持的字幕文件格式，支持 SRT、ASS、SSA、VTT、SUB、SMI、SAMI")
    source = store.one("SELECT media FROM sources WHERE id=?", (source_id,))
    if not source:
        raise ValueError("找不到素材")
    media = json.loads(source["media"])
    rows = parse_subtitle_file(path, suffix, media["duration"], fps=media["fps"])
    identifier = hashlib.sha256(path.read_bytes()).hexdigest()
    root = upload_root(store, source_id) / identifier
    manifest = root / "manifest.json"
    if manifest.is_file():
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        try:
            resolve_subtitle(store, source_id, identifier)
        except ValueError:
            # Re-uploading the same bytes repairs a missing/changed artifact
            # without changing the file identity frozen in existing jobs.
            path.replace(root / ("subtitle." + metadata["format"]))
        return metadata
    root.mkdir(parents=True, exist_ok=True)
    path.replace(root / ("subtitle" + suffix))
    result = {"id": identifier, "name": name, "format": suffix[1:], "utterances": len(rows)}
    atomic_json(manifest, result)
    return result


def uploaded_subtitles(store, source_id: str) -> list[dict]:
    paths = sorted(upload_root(store, source_id).glob("*/manifest.json"), key=lambda path: path.stat().st_mtime_ns)
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def resolve_subtitle(store, source_id: str, identifier: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[a-f0-9]{64}", identifier):
        raise ValueError("无效的字幕文件标识")
    root = upload_root(store, source_id) / identifier
    try:
        metadata = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        suffix = "." + metadata["format"]
        if suffix not in SUBTITLE_EXTENSIONS:
            raise ValueError("无效的字幕文件格式")
        path = root / ("subtitle" + suffix)
        if hashlib.sha256(path.read_bytes()).hexdigest() != identifier:
            raise ValueError("字幕文件内容已变化，请重新上传")
    except FileNotFoundError as exc:
        raise ValueError("找不到该素材上传的字幕文件，请重新上传") from exc
    return path, metadata
