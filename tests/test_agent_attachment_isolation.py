"""附件隔离的性质测试：AttachmentStore.resolve 与回环 upload 的归属、会话、有效期校验。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mindex.agent import attachments as att
from mindex.agent.attachments import AttachmentError, AttachmentStore
from mindex.agent.loopback import MindexClient

T0 = 1_700_000_000_000
HOUR = 3600 * 1000

# (名称, owner, session_id, ttl 小时, 是否删掉文件)
SPECS = [
    ("a_short", "session", "A", 1, False),
    ("a_long", "session", "A", 24, False),
    ("b_short", "session", "B", 1, False),
    ("mcp_short", "mcp", None, 1, False),
    ("mcp_long", "mcp", None, 24, False),
    ("a_missing", "session", "A", 24, True),
]


@pytest.fixture(scope="module")
def world():
    root = Path(tempfile.mkdtemp(prefix="mindex-att-iso-"))
    original = att.now_ms
    att.now_ms = lambda: T0
    try:
        store = AttachmentStore(root / "data")
        rows = {}
        for name, owner, sid, ttl, missing in SPECS:
            incoming = root / "incoming" / f"{name}.bin"
            incoming.parent.mkdir(parents=True, exist_ok=True)
            incoming.write_bytes(f"content-{name}".encode())
            meta = store.put(incoming, filename=f"{name}.bin", content_type="application/octet-stream",
                             owner=owner, session_id=sid, ttl_hours=ttl)
            if missing:
                store.path(store._row(meta["id"])).unlink()
            rows[meta["id"]] = {"owner": owner, "session_id": sid, "expires_at": meta["expires_at"],
                                "missing": missing, "content": f"content-{name}".encode()}
    finally:
        att.now_ms = original
    yield store, rows
    shutil.rmtree(root, ignore_errors=True)


def expected_ok(rows: dict, ref: str, owner: str, session_id: str | None, now: int) -> bool:
    row = rows.get(ref)
    return (row is not None
            and row["owner"] == owner
            and (owner != "session" or row["session_id"] == session_id)
            and now < row["expires_at"]
            and not row["missing"])


KNOWN = st.sampled_from(range(len(SPECS))).map(lambda i: ("known", i))
# 已登记引用加权出现，保证成功分支也被充分覆盖。
REF_KINDS = st.one_of(
    KNOWN, KNOWN, KNOWN, KNOWN,
    st.text(alphabet="0123456789abcdef", min_size=32, max_size=32).map(lambda t: ("ref", f"attachment://{t}")),
    st.text(max_size=40).map(lambda t: ("ref", t)),
    st.text(max_size=40).map(lambda t: ("ref", f"attachment://{t}")),
)
OFFSETS = st.one_of(
    st.integers(min_value=0, max_value=48 * HOUR),
    st.sampled_from([HOUR - 1, HOUR, HOUR + 1, 24 * HOUR - 1, 24 * HOUR]),
)
CALLERS = st.tuples(st.sampled_from(["session", "mcp"]), st.sampled_from(["A", "B", None]))


def pick(rows: dict, kind) -> str:
    tag, value = kind
    return list(rows)[value] if tag == "known" else value


# Feature: agent-capability-expansion, Property 5: 附件隔离
@settings(max_examples=100, deadline=None)
@given(kind=REF_KINDS, caller=CALLERS, offset=OFFSETS)
def test_resolve_isolation(world, kind, caller, offset):
    """**Validates: Requirements 3.4, 3.5**"""
    store, rows = world
    ref = pick(rows, kind)
    owner, sid = caller
    now = T0 + offset
    original = att.now_ms
    att.now_ms = lambda: now
    try:
        if expected_ok(rows, ref, owner, sid, now):
            path = store.resolve(ref, session_id=sid, owner=owner)
            assert path.read_bytes() == rows[ref]["content"]
        else:
            with pytest.raises(AttachmentError):
                store.resolve(ref, session_id=sid, owner=owner)
    finally:
        att.now_ms = original


# Feature: agent-capability-expansion, Property 5: 附件隔离
@settings(max_examples=100, deadline=None)
@given(kind=REF_KINDS, caller=CALLERS, offset=OFFSETS)
def test_loopback_upload_isolation(world, kind, caller, offset):
    """**Validates: Requirements 3.4, 3.5**"""
    store, rows = world
    ref = pick(rows, kind)
    owner, sid = caller
    now = T0 + offset
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"received": True})

    async def run():
        client = MindexClient(base_url="http://mindex.test")
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mindex.test")
        scoped = client.with_resolvers(attachment=store.resolver(session_id=sid, owner=owner))
        try:
            return await scoped.upload("/api/target", files={"file": ref}, data={"kind": "image"})
        finally:
            await client.aclose()

    original = att.now_ms
    att.now_ms = lambda: now
    try:
        result = asyncio.run(run())
    finally:
        att.now_ms = original

    if expected_ok(rows, ref, owner, sid, now):
        assert result["ok"] is True
        assert len(seen) == 1
        body = seen[0].read()
        assert rows[ref]["content"] in body
    else:
        assert result == {"ok": False, "status": 400, "code": "attachment_invalid", "error": result["error"]}
        assert seen == []
