"""分镜画面来源：读取分镜 content 的来源设置，定位并复验来源文件（任务 ``inputs/<sha256>.<suffix>``）。

来源为 None 的分镜等价素材召回（recall）。clip/image 来源的文件与 aigc 来源的参考图复用查询文件通道上传，
执行前按文件名中的 sha256 复验摘要，文件缺失或被改动时阻塞/失败，不用错误的画面继续。
"""

from __future__ import annotations

from pathlib import Path

from studio import studio
from studio.artifacts import digest
from studio.workspace.versions import root, version

MISSING = "画面文件缺失或已损坏，请重新上传"
REFERENCE_MISSING = "参考图片缺失或已损坏，请重新上传"
# 有来源文件（media_id）的来源；aigc 的参考图（image_id）是可选的，由 generate 阶段另行校验。
FILE_KINDS = ("clip", "image")
VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".avi")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
AUDIO_SUFFIXES = (".mp3", ".wav", ".flac", ".m4a", ".aac")
KIND_SUFFIXES = {
    "clip": VIDEO_SUFFIXES,
    "image": IMAGE_SUFFIXES,
    "video": VIDEO_SUFFIXES,
    "audio": AUDIO_SUFFIXES,
}


def content_source(content) -> dict | None:
    """content 版本数据中的来源设置；None 表示素材召回。"""
    value = (content or {}).get("source")
    if not isinstance(value, dict) or value.get("kind", "recall") == "recall":
        return None
    return value


def kind(content) -> str:
    value = content_source(content)
    return value["kind"] if value else "recall"


def plan_source(w, p, sid) -> dict | None:
    selected = p["shots"].get(sid)
    if not selected or selected.get("content") not in w["versions"]:
        return None
    return content_source(version(w, selected["content"])["data"])


def find_input(config, job_id, media_id, suffixes=None) -> Path | None:
    """已上传且摘要一致的输入文件；``suffixes`` 限定类型。找不到返回 None。"""
    directory = root(config, job_id) / "inputs"
    for path in sorted(directory.glob(media_id + ".*")):
        if path.suffix == ".partial" or not path.is_file():
            continue
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        if digest(path) == media_id:
            return path
    return None


def present(config, job_id, media_id, source_kind) -> bool:
    """PATCH 级校验：文件已上传且类型匹配（不复验摘要，执行前另有复验）。"""
    directory = root(config, job_id) / "inputs"
    return any(
        path.is_file() and path.suffix.lower() in KIND_SUFFIXES[source_kind]
        for path in directory.glob(media_id + ".*")
    )


def source_file(config, job_id, source) -> Path:
    """来源文件（复验摘要）；缺失或被改动时报中文错误。"""
    found = find_input(config, job_id, source["media_id"], KIND_SUFFIXES[source["kind"]])
    if found is None:
        raise studio.StudioError(MISSING)
    return found


def reference_image(config, job_id, source, key="image_id") -> Path | None:
    """aigc 来源的首帧（或 ``key`` 指定的尾帧/主体参考）图片（复验摘要）；未设置返回 None，
    缺失或被改动时报中文错误。
    """
    if not source.get(key):
        return None
    found = find_input(config, job_id, source[key], IMAGE_SUFFIXES)
    if found is None:
        raise studio.StudioError(REFERENCE_MISSING)
    return found


def reference_file(config, job_id, kind, media_id) -> Path:
    """多模态参考素材 / 翻唱参考音频（复验摘要）；缺失或被改动时报中文错误。"""
    found = find_input(config, job_id, media_id, KIND_SUFFIXES[kind])
    if found is None:
        raise studio.StudioError(REFERENCE_MISSING if kind == "image" else "参考素材缺失或已损坏，请重新上传")
    return found


def source_references(source) -> list[tuple[str, str]]:
    """aigc 来源用到的全部参考文件 (类型, media_id)：首帧、尾帧与多模态参考。"""
    found = []
    if source.get("image_id"):
        found.append(("image", source["image_id"]))
    if source.get("last_image_id"):
        found.append(("image", source["last_image_id"]))
    for item in source.get("references") or []:
        found.append((item["kind"], item["media_id"]))
    if source.get("reference_id"):
        found.append(("image", source["reference_id"]))
    return found


def missing_reference(config, job_id, source) -> str | None:
    """PATCH/预检级校验：任一参考文件未上传或类型不符时返回中文原因。"""
    for kind, media_id in source_references(source):
        if not present(config, job_id, media_id, kind):
            return REFERENCE_MISSING if kind == "image" else "参考素材缺失或已损坏，请重新上传"
    return None


def video_file(config, job_id, w, video) -> Path:
    """时间线文件区间（FileVideo）对应的文件：inputs 文件复验摘要；版本产物经 ``file_path`` 复验。"""
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from studio.workspace.versions import file_path

    ref = video.ref
    if ref.input:
        media_id = ref.input.split(".")[0]
        target = root(config, job_id) / "inputs" / ref.input
        if not target.is_file() or digest(target) != media_id:
            raise studio.StudioError(MISSING)
        return target
    return file_path(config, job_id, version(w, ref.version), ref.file)


def identity(source) -> dict:
    """来源在签名中的身份：只含决定画面的字段（clip 起点、image 运动参数）。"""
    if source["kind"] == "clip":
        return {"kind": "clip", "media_id": source["media_id"], "start": source.get("start") or 0}
    return {
        "kind": "image",
        "media_id": source["media_id"],
        "motion": source.get("motion", "zoom-in"),
        "strength": source.get("strength", 0.3),
    }
