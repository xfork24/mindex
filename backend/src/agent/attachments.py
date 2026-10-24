"""助手附件暂存（``attachment://<token>``）。

面板或 MCP 调用方先把文件上传到 ``POST /api/agent/attachments``，之后在消息与工具参数里只传引用；
回环客户端提交 multipart 时再按归属解析成文件。记录放在 ``data_dir/agent.sqlite3``（与会话同库，
不进业务备份），文件放在 ``data_dir/agent-attachments/<token>/<安全化文件名>``。

归属规则：
- ``session``：网页凭证上传，必须绑定会话，只能被该会话的工具调用解析；
- ``mcp``：持 MCP 令牌上传，只能被 MCP 工具调用解析。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import unicodedata
from pathlib import Path
from typing import Literal

from mindex.agent.session_store import Database, now_ms

PREFIX = "attachment://"
DIRNAME = "agent-attachments"
OWNERS = ("session", "mcp")
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
MAX_FILENAME = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_attachments (
  id TEXT PRIMARY KEY, session_id TEXT, owner TEXT NOT NULL, filename TEXT NOT NULL,
  content_type TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_attachments_session ON agent_attachments(session_id);
CREATE INDEX IF NOT EXISTS agent_attachments_expires ON agent_attachments(expires_at);
"""

# 文件名里不允许的字符：路径分隔符与 Windows 保留字符；控制字符另行剔除。
UNSAFE = re.compile(r'[/\\:*?"<>|]')


class AttachmentError(Exception):
    """附件引用无效、过期、越权或文件缺失。消息为中文，可直接转述给模型。"""


def token(ref: str) -> str:
    """接受 ``attachment://<token>`` 或裸 token，返回校验过的 token。"""
    if not isinstance(ref, str):
        raise AttachmentError("附件引用格式无效")
    value = ref[len(PREFIX):] if ref.startswith(PREFIX) else ref
    if not TOKEN_RE.fullmatch(value):
        raise AttachmentError("附件引用格式无效")
    return value


def safe_filename(name: str | None) -> str:
    """只保留文件名本身：去掉目录、控制字符与分隔符，保留扩展名；为空时回落为 file。"""
    raw = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C")
    raw = UNSAFE.sub("_", raw).strip().lstrip(".").strip()
    if not raw:
        return "file"
    if len(raw) > MAX_FILENAME:
        stem, dot, ext = raw.rpartition(".")
        if dot and stem and len(ext) <= 16:
            raw = f"{stem[:MAX_FILENAME - len(ext) - 1]}.{ext}"
        else:
            raw = raw[:MAX_FILENAME]
    return raw


class AttachmentStore:
    PREFIX = PREFIX

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / DIRNAME
        self.store = Database(self.data_dir / "agent.sqlite3")
        with self.store.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    # ------------------------------------------------------------ 写入
    def put(self, upload_path: Path, *, filename: str | None, content_type: str | None,
            owner: Literal["session", "mcp"], session_id: str | None, ttl_hours: int) -> dict:
        """把已写好的上传文件移入暂存目录并登记，返回公开元数据。

        ``upload_path`` 会被移动（跨文件系统时复制后删除）；空文件抛 ``ValueError``。
        """
        if owner not in OWNERS:
            raise ValueError(f"未知附件归属：{owner}")
        if owner == "session" and not session_id:
            raise ValueError("会话附件必须指定会话")
        if ttl_hours < 1:
            raise ValueError("附件有效期至少 1 小时")
        source = Path(upload_path)
        size, digest = _digest(source)
        if size == 0:
            raise ValueError("附件为空")
        tok = secrets.token_hex(16)
        name = safe_filename(filename)
        folder = self.root / tok
        folder.mkdir(parents=True, exist_ok=False)
        target = folder / name
        try:
            shutil.move(str(source), target)
            stamp = now_ms()
            self.store.execute(
                "INSERT INTO agent_attachments(id,session_id,owner,filename,content_type,bytes,sha256,path,"
                "created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tok, session_id, owner, name, (content_type or "application/octet-stream")[:200], size, digest,
                 target.relative_to(self.data_dir).as_posix(), stamp, stamp + ttl_hours * 3600 * 1000),
            )
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return self.get(tok)

    # ------------------------------------------------------------ 读取
    def _row(self, ref: str) -> dict:
        row = self.store.one("SELECT * FROM agent_attachments WHERE id=?", (token(ref),))
        if row is None:
            raise AttachmentError("找不到附件，可能已被删除或过期回收，请重新上传")
        return row

    def get(self, ref: str) -> dict:
        return _public(self._row(ref))

    def path(self, row: dict) -> Path:
        """记录里的相对路径解析为绝对路径；越出暂存目录的路径一律视为无效。"""
        target = (self.data_dir / row["path"]).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise AttachmentError("附件路径无效")
        return target

    def resolve(self, ref: str, *, session_id: str | None, owner: str) -> Path:
        """按调用方归属解析附件文件：归属、会话、有效期与文件都校验通过才返回路径。"""
        row = self._row(ref)
        if row["owner"] != owner or (owner == "session" and (not session_id or row["session_id"] != session_id)):
            raise AttachmentError("附件不属于当前会话或调用方，请在当前会话中重新上传")
        if row["expires_at"] <= now_ms():
            raise AttachmentError("附件已过期，请重新上传")
        target = self.path(row)
        if not target.is_file():
            raise AttachmentError("附件文件缺失，请重新上传")
        return target

    def resolver(self, *, session_id: str | None, owner: str):
        """回环客户端的附件解析器：``ref -> (文件名, 路径, 内容类型)``，归属与有效期校验同 ``resolve``。"""

        def resolve(ref: str) -> tuple[str, Path, str]:
            target = self.resolve(ref, session_id=session_id, owner=owner)
            meta = self.get(ref)
            return meta["filename"], target, meta["content_type"]

        return resolve

    def file(self, ref: str) -> tuple[dict, Path]:
        """未过期且文件存在的附件：返回 (公开元数据, 绝对路径)；不校验归属，归属由调用方判断。"""
        row = self._row(ref)
        if row["expires_at"] <= now_ms():
            raise AttachmentError("附件已过期，请重新上传")
        target = self.path(row)
        if not target.is_file():
            raise AttachmentError("附件文件缺失，请重新上传")
        return _public(row), target

    def expired(self, now: int | None = None) -> list[dict]:
        """已过期的附件（含绝对路径 ``file``），供清理规则使用。"""
        stamp = now_ms() if now is None else now
        rows = self.store.rows(
            "SELECT * FROM agent_attachments WHERE expires_at<=? ORDER BY expires_at", (stamp,))
        return [{**_public(row), "file": str(self.data_dir / row["path"])} for row in rows]

    def tokens(self) -> set[str]:
        """全部已登记的 token；清理规则据此识别没有记录的孤儿目录。"""
        return {row["id"] for row in self.store.rows("SELECT id FROM agent_attachments")}

    # ------------------------------------------------------------ 删除
    def delete(self, ref: str) -> None:
        row = self._row(ref)
        self.store.execute("DELETE FROM agent_attachments WHERE id=?", (row["id"],))
        self._remove_dir(row["id"])

    def delete_session(self, session_id: str) -> None:
        rows = self.store.rows("SELECT id FROM agent_attachments WHERE session_id=?", (session_id,))
        self.store.execute("DELETE FROM agent_attachments WHERE session_id=?", (session_id,))
        for row in rows:
            self._remove_dir(row["id"])

    def _remove_dir(self, tok: str) -> None:
        if TOKEN_RE.fullmatch(tok):
            shutil.rmtree(self.root / tok, ignore_errors=True)


def _public(row: dict) -> dict:
    return {
        "id": f"{PREFIX}{row['id']}", "filename": row["filename"], "content_type": row["content_type"],
        "bytes": row["bytes"], "sha256": row["sha256"], "owner": row["owner"], "session_id": row["session_id"],
        "created_at": row["created_at"], "expires_at": row["expires_at"],
    }


def _digest(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()
