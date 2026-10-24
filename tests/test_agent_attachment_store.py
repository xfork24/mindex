"""助手附件暂存 AttachmentStore 的单元测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mindex.agent import attachments as att
from mindex.agent.attachments import AttachmentError, AttachmentStore, safe_filename, token
from mindex.shared.config import AgentConfig


def upload(tmp_path: Path, data: bytes = b"hello", name: str = "up.bin") -> Path:
    path = tmp_path / "incoming" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def store(tmp_path):
    return AttachmentStore(tmp_path / "data")


def put(store, tmp_path, *, owner="session", session_id="session-a", data=b"hello", filename="a.png", ttl=1):
    return store.put(upload(tmp_path, data), filename=filename, content_type="image/png",
                     owner=owner, session_id=session_id, ttl_hours=ttl)


def test_put_records_metadata_and_moves_file(store, tmp_path):
    source = upload(tmp_path, b"abc")
    meta = store.put(source, filename="clip.mp4", content_type="video/mp4", owner="session",
                     session_id="session-a", ttl_hours=2)
    assert meta["id"].startswith("attachment://")
    assert meta["filename"] == "clip.mp4"
    assert meta["content_type"] == "video/mp4"
    assert meta["bytes"] == 3
    assert meta["sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert meta["owner"] == "session" and meta["session_id"] == "session-a"
    assert meta["expires_at"] - meta["created_at"] == 2 * 3600 * 1000
    assert not source.exists()
    path = store.resolve(meta["id"], session_id="session-a", owner="session")
    assert path.read_bytes() == b"abc"
    assert path.parent.parent == (store.root).resolve()
    assert store.get(meta["id"]) == meta
    # 裸 token 也能解析
    assert store.get(token(meta["id"]))["id"] == meta["id"]


def test_put_rejects_empty_and_invalid(store, tmp_path):
    with pytest.raises(ValueError, match="附件为空"):
        put(store, tmp_path, data=b"")
    with pytest.raises(ValueError):
        put(store, tmp_path, session_id=None)
    with pytest.raises(ValueError):
        put(store, tmp_path, owner="web")
    assert store.tokens() == set()
    assert not store.root.exists() or not any(store.root.iterdir())


def test_resolve_enforces_owner_session_and_expiry(store, tmp_path, monkeypatch):
    session_ref = put(store, tmp_path)["id"]
    mcp_ref = put(store, tmp_path, owner="mcp", session_id=None)["id"]

    assert store.resolve(mcp_ref, session_id=None, owner="mcp").is_file()
    for ref, sid, owner in [
        (session_ref, "session-b", "session"),
        (session_ref, None, "session"),
        (session_ref, None, "mcp"),
        (mcp_ref, "session-a", "session"),
    ]:
        with pytest.raises(AttachmentError):
            store.resolve(ref, session_id=sid, owner=owner)

    for bad in ["attachment://nope", "attachment://" + "0" * 32, "../etc/passwd", ""]:
        with pytest.raises(AttachmentError):
            store.resolve(bad, session_id="session-a", owner="session")

    expires = store.get(session_ref)["expires_at"]
    monkeypatch.setattr(att, "now_ms", lambda: expires)
    with pytest.raises(AttachmentError, match="过期"):
        store.resolve(session_ref, session_id="session-a", owner="session")


def test_resolve_missing_file(store, tmp_path):
    ref = put(store, tmp_path)["id"]
    store.resolve(ref, session_id="session-a", owner="session").unlink()
    with pytest.raises(AttachmentError, match="缺失"):
        store.resolve(ref, session_id="session-a", owner="session")


def test_delete_and_delete_session(store, tmp_path):
    a1 = put(store, tmp_path)["id"]
    a2 = put(store, tmp_path)["id"]
    b1 = put(store, tmp_path, session_id="session-b")["id"]
    m1 = put(store, tmp_path, owner="mcp", session_id=None)["id"]

    store.delete(a1)
    assert not (store.root / token(a1)).exists()
    with pytest.raises(AttachmentError):
        store.get(a1)
    with pytest.raises(AttachmentError):
        store.delete(a1)

    store.delete_session("session-a")
    assert not (store.root / token(a2)).exists()
    assert store.tokens() == {token(b1), token(m1)}
    assert store.resolve(b1, session_id="session-b", owner="session").is_file()


def test_expired_lists_only_past_records(store, tmp_path):
    first = put(store, tmp_path, ttl=1)
    second = put(store, tmp_path, ttl=5)
    assert store.expired(first["created_at"]) == []
    listed = store.expired(first["expires_at"])
    assert [item["id"] for item in listed] == [first["id"]]
    assert Path(listed[0]["file"]).is_file()
    assert {item["id"] for item in store.expired(second["expires_at"] + 1)} == {first["id"], second["id"]}


def test_store_shares_agent_database(tmp_path):
    from mindex.agent.session_store import SessionStore

    data = tmp_path / "data"
    sessions = SessionStore(data)
    store = AttachmentStore(data)
    sid = sessions.create()["id"]
    ref = store.put(upload(tmp_path), filename="x.txt", content_type="text/plain", owner="session",
                    session_id=sid, ttl_hours=1)["id"]
    assert (data / "agent.sqlite3").is_file()
    assert AttachmentStore(data).get(ref)["session_id"] == sid


@pytest.mark.parametrize(("raw", "expected"), [
    ("photo.jpg", "photo.jpg"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\win\\evil.exe", "evil.exe"),
    ("C:\\Users\\a\\b.srt", "b.srt"),
    ("..", "file"),
    ("", "file"),
    (None, "file"),
    (".hidden.png", "hidden.png"),
    ("a\x00b\nc.wav", "abc.wav"),
    ('we?ird:na*me".mp3', "we_ird_na_me_.mp3"),
    ("中文 名称.png", "中文 名称.png"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_truncates_keeping_extension(store, tmp_path):
    name = safe_filename("x" * 300 + ".mp4")
    assert len(name) == att.MAX_FILENAME and name.endswith(".mp4")
    meta = put(store, tmp_path, filename="../" * 5 + "evil.png")
    path = store.resolve(meta["id"], session_id="session-a", owner="session")
    assert path.name == "evil.png" and path.is_relative_to(store.root.resolve())


def test_config_defaults_and_bounds():
    cfg = AgentConfig()
    assert (cfg.attachment_ttl_hours, cfg.attachment_max_mb, cfg.max_images) == (168, 500, 8)
    AgentConfig(attachment_max_mb=20480, max_images=0)
    for bad in [{"attachment_ttl_hours": 0}, {"attachment_max_mb": 0}, {"attachment_max_mb": 20481},
                {"max_images": 33}, {"max_images": -1}]:
        with pytest.raises(ValueError):
            AgentConfig(**bad)
