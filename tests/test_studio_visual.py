"""分镜画面意象（visual）字段的属性测试。

覆盖 design.md Correctness Properties 中与 visual 相关的属性（15–19）。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from conftest import legacy_job
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mindex.studio import studio_workspace as ws
from mindex.studio.studio import VISUAL_LIMIT, Shot, clip_visual, visual_error

# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------

# 首尾空白：普通空格、制表、换行、全角空格等 str.strip() 会去掉的字符。
WHITESPACE = st.text(alphabet=" \t\n\r\u3000\u00a0", max_size=5)

# 正文字符：ASCII、汉字、emoji（非 BMP），以及少量内部空白。
BODY_CHARS = st.one_of(
    st.characters(min_codepoint=0x21, max_codepoint=0x7E),
    st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
    st.characters(min_codepoint=0x1F300, max_codepoint=0x1FAFF),
    st.sampled_from([" ", "，", "。"]),
)


@st.composite
def visual_inputs(draw) -> str:
    """任意 visual 输入：纯空白、短文本、恰在上限附近或远超上限的文本，前后可带空白。"""
    length = draw(
        st.one_of(
            st.just(0),
            st.integers(min_value=1, max_value=VISUAL_LIMIT - 1),
            st.integers(min_value=VISUAL_LIMIT - 1, max_value=VISUAL_LIMIT + 1),
            st.integers(min_value=VISUAL_LIMIT + 2, max_value=VISUAL_LIMIT * 3),
        )
    )
    body = draw(st.text(alphabet=BODY_CHARS, min_size=length, max_size=length))
    return draw(WHITESPACE) + body + draw(WHITESPACE)


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


# Feature: studio-material-consistency, Property 15: 模型输出 visual 的截断
@settings(max_examples=100, deadline=None)
@given(value=visual_inputs())
def test_clip_visual_truncates_model_output(value: str) -> None:
    """**Validates: Requirements 7.2**"""
    stripped = value.strip()
    result, clipped = clip_visual(value)

    if not stripped:
        assert result is None
        assert clipped is False
        return

    assert result is not None
    assert stripped.startswith(result)
    assert len(result) == min(VISUAL_LIMIT, len(stripped))
    assert clipped is (len(stripped) > VISUAL_LIMIT)


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------


@st.composite
def submitted_shots(draw) -> list:
    """人工/智能助手提交的分镜列表：每镜为字典或带 .visual 属性的对象，visual 可省略、为 None 或任意文本。"""
    shots = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        visual = draw(st.one_of(st.none(), visual_inputs()))
        form = draw(st.sampled_from(["dict", "dict-missing", "object"]))
        if form == "dict":
            shots.append({"text": "旁白", "visual": visual})
        elif form == "dict-missing":
            shots.append({"text": "旁白"})
        else:
            shots.append(SimpleNamespace(text="旁白", visual=visual))
    return shots


def _visual_of(shot) -> str | None:
    return shot.get("visual") if isinstance(shot, dict) else shot.visual


# Feature: studio-material-consistency, Property 16: 人工提交的超长 visual 被拒绝
@settings(max_examples=100, deadline=None)
@given(shots=submitted_shots())
def test_visual_error_rejects_overlong_submission(shots: list) -> None:
    """**Validates: Requirements 7.4, 8.10**"""
    lengths = [len((_visual_of(s) or "").strip()) for s in shots]
    overlong = [i for i, n in enumerate(lengths, 1) if n > VISUAL_LIMIT]

    error = visual_error(shots)

    if not overlong:
        assert error is None
        return

    assert error is not None
    first = overlong[0]
    # 中文消息，指出第一个超长分镜的序号与当前字数。
    assert error.startswith(f"第 {first} 个分镜")
    assert "画面意象" in error
    assert f"{VISUAL_LIMIT} 字上限" in error
    assert f"当前 {lengths[first - 1]} 字" in error


# ---------------------------------------------------------------------------
# Property 17
# ---------------------------------------------------------------------------


@st.composite
def valid_visuals(draw) -> str:
    """去空白后长度为 1–120 的 visual（含边界 1 与 120），前后可带任意空白。"""
    length = draw(
        st.one_of(
            st.integers(min_value=1, max_value=VISUAL_LIMIT),
            st.sampled_from([1, VISUAL_LIMIT - 1, VISUAL_LIMIT]),
        )
    )
    body = draw(
        st.text(alphabet=BODY_CHARS, min_size=length, max_size=length).filter(
            lambda s: s.strip() == s and s
        )
    )
    return draw(WHITESPACE) + body + draw(WHITESPACE)


@st.composite
def shot_fields(draw) -> dict:
    """合法的分镜其余字段（id/text/query/estimated_seconds 及可选召回覆盖）。"""
    fields = {
        "id": draw(st.from_regex(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")),
        "text": draw(st.text(alphabet=BODY_CHARS, min_size=1, max_size=200)),
        "query": draw(st.text(alphabet=BODY_CHARS, min_size=1, max_size=400)),
        "estimated_seconds": draw(
            st.floats(min_value=0.1, max_value=60, allow_nan=False, allow_infinity=False)
        ),
    }
    if draw(st.booleans()):
        fields["recall_limit"] = draw(st.integers(min_value=1, max_value=50))
    return fields


# Feature: studio-material-consistency, Property 17: visual 往返
@settings(max_examples=100, deadline=None)
@given(fields=shot_fields(), visual=valid_visuals())
def test_visual_round_trip(fields: dict, visual: str) -> None:
    """**Validates: Requirements 7.5, 7.6**"""
    expected = visual.strip()
    shot = Shot(**fields, visual=visual)
    dumped = shot.model_dump()

    assert dumped["visual"] == expected
    assert Shot.model_validate(dumped).visual == expected
    # 落盘路径：JSON 序列化后再读回，visual 逐字符一致。
    restored = Shot.model_validate_json(shot.model_dump_json())
    assert restored.visual == expected
    assert restored.model_dump() == dumped


# ---------------------------------------------------------------------------
# Property 18
# ---------------------------------------------------------------------------

_OMIT = object()


# Feature: studio-material-consistency, Property 18: 空 visual 不改变老分镜的 dump
@settings(max_examples=100, deadline=None)
@given(
    fields=shot_fields(),
    visual=st.one_of(st.just(_OMIT), st.none(), WHITESPACE),
)
def test_empty_visual_keeps_legacy_dump(fields: dict, visual: object) -> None:
    """**Validates: Requirements 7.3, 7.7**"""
    legacy = Shot.model_validate(fields).model_dump()
    data = dict(fields) if visual is _OMIT else {**fields, "visual": visual}

    shot = Shot.model_validate(data)
    dumped = shot.model_dump()

    assert shot.visual is None
    assert "visual" not in dumped
    assert dumped == legacy
    # JSON 落盘形式同样不含 visual 键。
    assert "visual" not in shot.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Property 19
# ---------------------------------------------------------------------------

SIGNED_STAGES = ("recall", "tts", "select")
ANY_VISUAL = st.one_of(st.none(), WHITESPACE, valid_visuals())


@pytest.fixture
def base_workspace(config):
    """一次性创建旧形态任务并接入工作台；各个用例在其深拷贝上设置分镜。"""
    job_id = legacy_job(config, "红色开场。蓝色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    return job_id, w


def _signatures(config, job_id, base, fields, visual):
    w = copy.deepcopy(base)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [Shot(**fields, visual=visual)])
    sid = fields["id"]
    return {
        stage: ws.specification(config, job_id, w, p, stage, sid)["signature"]
        for stage in SIGNED_STAGES
    }


# Feature: studio-material-consistency, Property 19: visual 不进签名
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(fields=shot_fields(), a=ANY_VISUAL, b=ANY_VISUAL)
def test_visual_not_in_signatures(config, base_workspace, fields: dict, a, b) -> None:
    """**Validates: Requirements 7.9, 7.10**"""
    job_id, base = base_workspace
    first = _signatures(config, job_id, base, fields, a)
    second = _signatures(config, job_id, base, fields, b)
    assert first == second
