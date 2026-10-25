"""参考文稿：方案级的改写/补充素材，内容寻址存于任务 ``inputs/<sha256>.txt``。

登记在 ``options.references``（``[{id, name, chars}]``，按 id 去重）；删除只移除登记，文件留在
``inputs/``（与查询媒体相同，随任务目录保留与备份）。正文统一为 UTF-8、``\\n`` 换行并去首尾空白，
id 是规范化正文 UTF-8 字节的 sha256；文案阶段执行前逐份复验摘要。
"""

from __future__ import annotations

import hashlib

from mindex.studio import studio
from mindex.studio.studio_artifacts import digest

from .versions import root

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".text"}
MISSING = "参考文稿缺失或已损坏，请重新上传"


def normalize(raw: bytes | str) -> str:
    """解码（UTF-8，容忍 BOM）并规范化换行与首尾空白；二进制或无法解码时报错。"""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise studio.StudioError("参考文稿必须是 UTF-8 编码的文本文件") from None
    else:
        text = raw.removeprefix("\ufeff")
    if "\x00" in text:
        raise studio.StudioError("参考文稿必须是 UTF-8 编码的文本文件")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise studio.StudioError("参考文稿不能为空")
    return text


def references(p) -> list[dict]:
    """方案登记的参考文稿；没有该键的旧方案为空。"""
    return list((p.get("options") or {}).get("references") or [])


def reference_ids(p) -> list[str]:
    return sorted(item["id"] for item in references(p))


def file_for(config, job_id, rid):
    return root(config, job_id) / "inputs" / f"{rid}.txt"


def register(config, job_id, p, name: str, text: str) -> dict:
    """写入 inputs/<sha>.txt 并登记到方案；同一正文只登记一次。返回登记项。"""
    text = normalize(text)
    data = text.encode("utf-8")
    rid = hashlib.sha256(data).hexdigest()
    existing = next((item for item in references(p) if item["id"] == rid), None)
    if existing:
        return existing
    limit = config.studio.max_reference_chars
    total = sum(item["chars"] for item in references(p)) + len(text)
    if total > limit:
        raise studio.StudioError(f"参考文稿总字数超过 {limit} 字上限（登记后共 {total} 字），请删减后再试")
    target = file_for(config, job_id, rid)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or digest(target) != rid:
        studio.save_bytes(target, data)
    name = " ".join((name or "").split())[:200] or "参考文稿"
    item = {"id": rid, "name": name, "chars": len(text)}
    p["options"] = {**p["options"], "references": [*references(p), item]}
    return item


def unregister(p, rid) -> None:
    items = references(p)
    if not any(item["id"] == rid for item in items):
        raise studio.EntityNotFound("找不到参考文稿")
    p["options"] = {**p["options"], "references": [item for item in items if item["id"] != rid]}


def load_texts(config, job_id, p) -> list[str]:
    """按登记顺序读取正文，并复验 sha256；缺失或被改动时拒绝执行。"""
    texts = []
    for item in references(p):
        path = file_for(config, job_id, item["id"])
        try:
            data = path.read_bytes()
        except OSError:
            raise studio.StudioError(MISSING) from None
        if hashlib.sha256(data).hexdigest() != item["id"]:
            raise studio.StudioError(MISSING)
        texts.append(data.decode("utf-8"))
    return texts
