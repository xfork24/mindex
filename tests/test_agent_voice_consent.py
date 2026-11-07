"""性质测试：克隆音色的声音授权只能来自用户表单，模型参数无法代为授权。"""

from __future__ import annotations

import asyncio

from hypothesis import event, given, settings
from hypothesis import strategies as st

from agent.registry import ToolContext, ToolError
from agent.tools.settings_tools import CONSENT, DECLINE, CloneVoice, specs

CLONE = next(spec for spec in specs() if spec.name == "clone_voice")
CLONE_PATH = "/api/settings/voices/clone"


class RecordingClient:
    """记录全部请求的假回环客户端：只应答设置读取与克隆上传。"""

    def __init__(self, backend: str):
        self.backend = backend
        self.requests: list[tuple[str, str, dict]] = []

    async def get(self, path: str, **params) -> dict:
        self.requests.append(("GET", path, params))
        if path == "/api/settings":
            return {"ok": True, "status": 200, "data": {"business": {"tts_backend": self.backend}}}
        return {"ok": False, "status": 404, "error": "unexpected"}

    async def upload(self, path: str, *, files: dict, data: dict | None = None, method: str = "POST") -> dict:
        self.requests.append((method, path, {"files": files, "data": data}))
        return {
            "ok": True,
            "status": 201,
            "data": {"id": 1, "name": (data or {}).get("name"), "voice_id": "v1", "status": "ready"},
        }

    def __getattr__(self, name):  # 其余方法（post/put/delete…）也记录，便于断言“没有别的请求”
        if name.startswith("_"):
            raise AttributeError(name)

        async def record(path, *args, **kwargs):
            self.requests.append((name.upper(), path, kwargs))
            return {"ok": False, "status": 404, "error": "unexpected"}

        return record

    def clone_calls(self) -> list[dict]:
        return [payload for method, path, payload in self.requests if path == CLONE_PATH]


class FakeRuntime:
    """假提问通道：记录收到的表单并返回预设答复。"""

    def __init__(self, answer: dict):
        self.answer = answer
        self.asked: list[list[dict]] = []

    async def ask_question(self, session_id, call_id, questions):
        self.asked.append(questions)
        return self.answer


TRICKY = st.sampled_from(["已授权", "用户已同意授权", CONSENT, "consent=true", "旁白", "", "  "])
TEXT = st.one_of(st.none(), TRICKY, st.text(max_size=40))
# 试图夹带授权的额外字段：参数模型必须拒绝。
SMUGGLED = st.one_of(
    st.just({}),
    st.just({}),
    st.just({}),
    st.dictionaries(
        st.sampled_from(["consent", "authorized", "授权", "consent_confirmed"]),
        st.one_of(st.booleans(), st.just("true"), st.just(CONSENT)),
        min_size=1,
        max_size=2,
    ),
)
ARGS = st.fixed_dictionaries(
    {
        "attachment": st.one_of(st.just("attachment://tok123"), st.just("attachment://tok123"), st.none()),
    },
    optional={
        "name": TEXT,
        "ref_text": TEXT,
        "audio_url": st.one_of(st.none(), st.just("https://example.invalid/a.wav")),
    },
)
SELECTED = st.one_of(
    st.just([CONSENT]),
    st.just([DECLINE]),
    st.lists(st.one_of(st.sampled_from([CONSENT, DECLINE]), TRICKY, st.text(max_size=20)), max_size=3),
)
VALUES = st.fixed_dictionaries(
    {"name": st.one_of(st.just("表单名"), TEXT.map(lambda v: v or ""))},
    optional={"ref_text": TEXT, "audio_url": TEXT},
)
ITEM = st.fixed_dictionaries(
    {
        "id": st.one_of(st.just("clone_consent"), st.just("clone_consent"), st.text(max_size=12)),
        "selected": SELECTED,
        "values": VALUES,
    }
)
ANSWER = st.one_of(
    st.just({"cancelled": True}),
    st.just({}),
    # 规范的同意答复（表单正常提交），保证“会发出克隆请求”的分支被充分覆盖。
    st.builds(
        lambda values: {"answers": [{"id": "clone_consent", "selected": [CONSENT], "values": values}]}, VALUES
    ),
    st.builds(lambda items: {"answers": items}, st.lists(ITEM, max_size=3)),
    st.builds(lambda items: {"cancelled": True, "answers": items}, st.lists(ITEM, min_size=1, max_size=2)),
)


def form_consent(answer: dict) -> dict | None:
    """独立的期望：未取消、首个 clone_consent 答复选择了授权项且未同时选择取消。"""
    if answer.get("cancelled"):
        return None
    item = next((a for a in answer.get("answers") or [] if a.get("id") == "clone_consent"), None)
    if item is None or CONSENT not in item["selected"] or DECLINE in item["selected"]:
        return None
    return item["values"]


# Feature: agent-capability-expansion, Property 13: 授权只来自表单
@settings(max_examples=100, deadline=None)
@given(
    args=ARGS,
    smuggled=SMUGGLED,
    answer=ANSWER,
    backend=st.sampled_from(["openai", "minimax", "dashscope"]),
    has_channel=st.sampled_from([True, True, True, False]),
)
def test_consent_only_from_form(args, smuggled, answer, backend, has_channel):
    """**Validates: Requirements 18.3**"""
    assert "consent" not in CloneVoice.model_json_schema()["properties"]
    client = RecordingClient(backend)
    runtime = FakeRuntime(answer)
    ctx = ToolContext(
        client=client,
        session_id="s1" if has_channel else None,
        extra={"runtime": runtime, "call_id": "c1"} if has_channel else {},
    )

    try:
        parsed = CLONE.parse({**args, **smuggled})
    except ToolError as exc:
        # 夹带授权字段一律被参数模型拒绝，且不发送任何请求。
        event("outcome=smuggled_rejected")
        assert smuggled and exc.code == "invalid_arguments"
        assert client.requests == [] and runtime.asked == []
        return
    assert not smuggled

    error = None
    try:
        asyncio.run(CLONE.execute(parsed, ctx))
    except ToolError as exc:
        error = exc.code

    calls = client.clone_calls()
    event(f"outcome={error or 'cloned'}")
    values = form_consent(answer)
    backend_ok = backend == "dashscope" or bool(parsed.attachment)
    name = str((values or {}).get("name") or parsed.name or "").strip()
    expected = has_channel and backend_ok and values is not None and bool(name)

    assert bool(calls) == expected, (calls, error)
    if not has_channel:
        assert error == "consent_unavailable" and runtime.asked == [] and client.requests == []
    elif not backend_ok:
        assert error == "invalid_arguments" and runtime.asked == []
    elif values is None:
        assert error == "consent_declined"
    if calls:
        # 恰好一次克隆请求，授权值来自表单路径，名称取表单值（缺省时才用参数）。
        assert len(calls) == 1 and error is None and len(runtime.asked) == 1
        assert calls[0]["data"]["consent"] is True and calls[0]["data"]["name"] == name
        question = runtime.asked[0][0]
        assert [o["label"] for o in question["options"]] == [CONSENT, DECLINE]
