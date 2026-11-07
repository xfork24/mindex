"""拆分镜校验、重试与提示词的属性测试（Hypothesis）。

共享生成器放在文件顶部，供各条 Correctness Property 复用。
"""

from __future__ import annotations

import json
from itertools import pairwise
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from models import adapters, storyboard
from models.adapters import ModelError
from models.storyboard import QwenStoryboarder, choose_attempt, hard_check, soft_issues
from shared.types import ShotDraft

# ---- 共享生成器 ----

# 非空白字符：英文、汉字、数字与常见标点（不含任何空白，便于控制"去空白后覆盖"）。
GLYPHS = st.sampled_from(list("abcdefgXYZ0123456789,.!?—'山水夜市面条灯塔。，！？"))
WHITESPACE = st.text(alphabet=" \t\n\u3000", max_size=3)
MAX_PIECES = 8


@st.composite
def originals(draw, min_chars: int = 1, max_chars: int = 60) -> str:
    """原文：若干非空白词元用随机空白连接，首尾也可带空白。"""
    words = draw(st.lists(st.text(alphabet=GLYPHS, min_size=1, max_size=8), min_size=1, max_size=10))
    seps = [draw(st.text(alphabet=" \t\n", min_size=1, max_size=2)) for _ in words[1:]]
    body = words[0] + "".join(sep + word for sep, word in zip(seps, words[1:], strict=True))
    assume(min_chars <= len("".join(body.split())) <= max_chars)
    return draw(WHITESPACE) + body + draw(WHITESPACE)


@st.composite
def partitions(draw, text: str, max_pieces: int = MAX_PIECES) -> list[str]:
    """把原文去空白后的字符串切成连续的非空段，每段两端随机补空白（段间空白可任意增减）。"""
    compact = "".join(text.split())
    k = draw(st.integers(1, min(max_pieces, len(compact))))
    cuts = (
        sorted(draw(st.sets(st.integers(1, len(compact) - 1), min_size=k - 1, max_size=k - 1)))
        if k > 1
        else []
    )
    bounds = [0, *cuts, len(compact)]
    pieces = [compact[a:b] for a, b in pairwise(bounds)]
    return [draw(WHITESPACE) + piece + draw(WHITESPACE) for piece in pieces]


def drafts_of(texts: list[str], visuals: list[str] | None = None) -> list[ShotDraft]:
    """用给定 text（与可选 visual）构造合法的 ShotDraft 列表。"""
    visuals = visuals or ["画面"] * len(texts)
    return [
        ShotDraft(visual=visual, text=text, query="画面 测试", estimated_seconds=1.5)
        for visual, text in zip(visuals, texts, strict=True)
    ]


@st.composite
def covering_splits(draw, max_pieces: int = MAX_PIECES) -> tuple[str, list[str]]:
    """(原文, 覆盖原文的切分)。"""
    text = draw(originals())
    return text, draw(partitions(text, max_pieces))


def _compact(value: str) -> str:
    return "".join(value.split())


# ---- Property 10 ----

MUTATIONS = ("omit", "duplicate", "swap", "rewrite", "empty", "overflow")


# Feature: studio-material-consistency, Property 10: 硬校验接受覆盖原文的切分并拒绝其他
@settings(max_examples=100, deadline=None)
@given(data=st.data(), split=covering_splits(), mutation=st.sampled_from(MUTATIONS))
def test_hard_check_accepts_coverage_and_rejects_others(data, split, mutation):
    """**Validates: Requirements 5.11, 6.1**"""
    original, pieces = split
    n = len(pieces)
    max_shots = data.draw(st.integers(n, n + 5), label="max_shots")

    # 覆盖原文、段数在 1..max_shots 之间：通过。
    assert hard_check(original, drafts_of(pieces), max_shots) is None

    bad = list(pieces)
    limit = max_shots
    if mutation == "omit":
        assume(n >= 2)
        del bad[data.draw(st.integers(0, n - 1), label="omit")]
    elif mutation == "duplicate":
        i = data.draw(st.integers(0, n - 1), label="dup")
        bad.insert(i, bad[i])
    elif mutation == "swap":
        assume(n >= 2)
        i = data.draw(st.integers(0, n - 2), label="swap")
        bad[i], bad[i + 1] = bad[i + 1], bad[i]
        # 两段互为同一串的幂（如 "a"/"aa"）时调换不改变拼接结果，不算调换顺序。
        assume(_compact("".join(bad)) != _compact(original))
    elif mutation == "rewrite":
        i = data.draw(st.integers(0, n - 1), label="piece")
        piece = bad[i]
        positions = [j for j, ch in enumerate(piece) if not ch.isspace()]
        j = data.draw(st.sampled_from(positions), label="char")
        new = data.draw(GLYPHS.filter(lambda ch: ch != piece[j]), label="new")
        bad[i] = piece[:j] + new + piece[j + 1 :]
    elif mutation == "empty":
        bad = []
    else:  # overflow：段数超过上限
        assume(n >= 2)
        limit = data.draw(st.integers(1, n - 1), label="limit")

    error = hard_check(original, drafts_of(bad), limit)
    assert error, f"{mutation} 应被拒绝"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in error), "错误原因应为中文"


# ---- Property 11 ----

# 独立参考实现所用的受限字符集：生成器只产出这些字符，参考实现按字符逐个判定，不复用被测正则。
LATIN = "abcXYZ019"
HAN = "山水夜市面条灯塔村"
TERMINALS = ".!?。！？…"
CLOSERS = "\"'”’」』）)]】"
SOFT_TOKENS = st.one_of(
    st.text(alphabet=LATIN, min_size=1, max_size=5),  # 英文词
    st.text(alphabet=HAN, min_size=1, max_size=4),  # 汉字串
    st.text(alphabet=LATIN + HAN + ",—", min_size=1, max_size=5),  # 混合词元
    st.sampled_from([",", "—", "，", "、"]),  # 不计数的纯标点
)


@st.composite
def soft_texts(draw) -> str:
    """分镜 text：若干词元，可带句末标点、收尾引号/括号与尾部空白。"""
    tokens = draw(st.lists(SOFT_TOKENS, min_size=1, max_size=8))
    tail = draw(st.sampled_from(["", *TERMINALS])) if draw(st.booleans()) else ""
    closers = draw(st.text(alphabet=CLOSERS, max_size=2)) if tail else ""
    return draw(WHITESPACE) + " ".join(tokens) + tail + closers + draw(st.text(alphabet=" \t", max_size=2))


VISUALS = st.one_of(st.just(""), WHITESPACE, st.text(alphabet=HAN + LATIN + " ", min_size=1, max_size=10))


def _ref_short(text: str) -> bool:
    words = sum(1 for token in text.split() if any(ch in LATIN for ch in token))
    han = sum(1 for ch in text if ch in HAN)
    return words < 3 and han < 5


def _ref_ends(text: str) -> bool:
    body = text.rstrip()
    while body and body[-1] in CLOSERS:
        body = body[:-1]
    return bool(body) and body[-1] in TERMINALS


def _ref_issues(texts: list[str], visuals: list[str]) -> dict[int, set[str]]:
    expected: dict[int, set[str]] = {}
    for i, text in enumerate(texts):
        whole = _ref_ends(text) and (i == 0 or _ref_ends(texts[i - 1]))
        kinds = set()
        if _ref_short(text) and not whole:
            kinds.add("short")
        if not visuals[i].strip():
            kinds.add("missing_visual")
        if kinds:
            expected[i + 1] = kinds
    return expected


# Feature: studio-material-consistency, Property 11: 软校验按镜计数
@settings(max_examples=100, deadline=None)
@given(shots=st.lists(st.tuples(soft_texts(), VISUALS), min_size=1, max_size=MAX_PIECES))
def test_soft_issues_count_per_shot(shots):
    """**Validates: Requirements 6.2**"""
    texts = [text for text, _ in shots]
    visuals = [visual for _, visual in shots]
    assume(all(text.strip() for text in texts))
    issues = soft_issues(drafts_of(texts, visuals))
    expected = _ref_issues(texts, visuals)

    indexes = [issue.index for issue in issues]
    # 每个分镜最多计一次，序号从 1 开始、按顺序排列。
    assert indexes == sorted(set(indexes))
    assert set(indexes) == set(expected)
    for issue in issues:
        kinds = set(issue.kinds or (issue.kind,))
        assert kinds == expected[issue.index]
        assert issue.kind in kinds
        # 过短优先作为主要原因。
        assert issue.kind == ("short" if "short" in kinds else "missing_visual")


# ---- Property 12 ----

# 固定原文：8 镜 × 3 个英文词，每镜都不"过短"，软问题只来自缺 visual，便于精确控制问题数。
RETRY_WORDS = [f"w{i}" for i in range(24)]
RETRY_TEXT = " ".join(RETRY_WORDS) + "."
RETRY_PIECES = [" ".join(RETRY_WORDS[i : i + 3]) for i in range(0, 24, 3)]
RETRY_PIECES[-1] += "."
GARBAGE = "这不是 JSON 输出"

# 每次尝试的结果：("garbage"|"omit", None) 为硬校验失败；("soft", m) 为通过硬校验且有 m 个软问题。
OUTCOMES = st.one_of(
    st.tuples(st.sampled_from(["garbage", "omit"]), st.none()),
    st.tuples(st.just("soft"), st.integers(0, len(RETRY_PIECES))),
)


def _shots_json(pieces: list[str], missing: int, tag: int) -> str:
    """前 ``missing`` 镜缺 visual；``tag`` 写进 query，用来识别返回的是哪一次尝试。"""
    shots = [
        {
            "visual": "" if i < missing else f"画面{i}",
            "text": piece,
            "query": f"尝试{tag} 画面",
            "estimated_seconds": 1.5,
        }
        for i, piece in enumerate(pieces)
    ]
    return json.dumps({"shots": shots}, ensure_ascii=False)


def _attempt_output(kind: str, missing: int | None, tag: int) -> tuple[str, str | None]:
    """返回 (模型输出, 预期硬校验错误)。"""
    if kind == "garbage":
        try:
            adapters.parse_json(GARBAGE)
        except ModelError as exc:
            return GARBAGE, str(exc)
        raise AssertionError("GARBAGE 应无法解析")
    if kind == "omit":
        pieces = RETRY_PIECES[:-1]
        return _shots_json(pieces, 0, tag), hard_check(RETRY_TEXT, drafts_of(pieces), 1024)
    return _shots_json(RETRY_PIECES, missing or 0, tag), None


# Feature: studio-material-consistency, Property 12: 重试与最佳尝试选择
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data(), retries=st.integers(0, 3))
def test_split_retry_and_best_attempt(config, data, retries):
    """**Validates: Requirements 6.3, 6.4, 6.5, 6.6**"""
    outcomes = data.draw(st.lists(OUTCOMES, min_size=retries + 1, max_size=retries + 1), label="outcomes")
    cfg = config.model_copy(deep=True)
    cfg.studio.generator = cfg.studio.generator.model_copy(update={"split_retries": retries})
    cfg.studio.max_shots = 1024

    outputs = [_attempt_output(kind, m, k) for k, (kind, m) in enumerate(outcomes)]
    replies = iter(outputs)
    requests: list[list[dict]] = []

    def reply(method, url, **kwargs):
        requests.append(kwargs["json"]["messages"][1]["content"])
        content, _ = next(replies)
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}

    first_clean = next((k for k, (kind, m) in enumerate(outcomes) if kind == "soft" and m == 0), None)
    counts = [m if kind == "soft" else None for kind, m in outcomes]
    best = choose_attempt(counts)

    with patch.object(adapters, "request", reply):
        if first_clean is None and best is None:
            with pytest.raises(ModelError) as caught:
                QwenStoryboarder(cfg).split(RETRY_TEXT)
            result = None
        else:
            result = QwenStoryboarder(cfg).split(RETRY_TEXT)

    # 调用次数：第一次 0 问题的尝试（从 1 计），否则用满 retries+1 次。
    expected_calls = first_clean + 1 if first_clean is not None else retries + 1
    assert len(requests) == expected_calls

    # 首次请求不带反馈；之后每次请求的最后一段反馈包含上一次的硬校验错误或全部问题分镜序号。
    assert len(requests[0]) == 1
    for k in range(1, expected_calls):
        assert len(requests[k]) == 2
        hint = requests[k][-1]["text"]
        kind, m = outcomes[k - 1]
        error = outputs[k - 1][1]
        if kind == "soft":
            for index in range(1, m + 1):
                assert f"第 {index} 个分镜" in hint
        else:
            assert error and error in hint

    if result is None:
        last_error = next(outputs[k][1] for k in reversed(range(len(outcomes))) if outcomes[k][0] != "soft")
        assert "分镜输出多次未通过校验" in str(caught.value)
        assert last_error in str(caught.value)
        return

    chosen = first_clean if first_clean is not None else best
    # 预期最佳：m 最小、相同时最早（独立于 choose_attempt 再算一次）。
    passed = [(m, k) for k, (kind, m) in enumerate(outcomes) if kind == "soft"]
    assert chosen == min(passed)[1]
    assert {draft.query for draft in result} == {f"尝试{chosen} 画面"}
    assert [draft.text for draft in result] == RETRY_PIECES


# Feature: studio-material-consistency, Property 14: 召回词规则是公共常量
@settings(max_examples=100, deadline=None)
@given(
    max_shots=st.integers(min_value=1, max_value=200),
    vocabulary=st.booleans(),
    marker=st.text(min_size=1, max_size=40),
)
def test_query_rules_is_shared_constant(max_shots: int, vocabulary: bool, marker: str) -> None:
    """**Validates: Requirements 9.5**"""
    original = storyboard.QUERY_RULES
    prompts = (
        storyboard.split_instructions(max_shots, vocabulary=vocabulary),
        storyboard.query_instructions(vocabulary=vocabulary),
    )
    for prompt in prompts:
        assert original in prompt
        assert (storyboard.VOCABULARY_RULE in prompt) is vocabulary

    # 替换公共常量后，两处提示词同步变化：都含新值、都不再含旧值。
    replacement = "〔召回词规则替换标记〕" + marker
    with patch.object(storyboard, "QUERY_RULES", replacement):
        changed = (
            storyboard.split_instructions(max_shots, vocabulary=vocabulary),
            storyboard.query_instructions(vocabulary=vocabulary),
        )
    for prompt in changed:
        assert replacement in prompt
        assert original not in prompt
        assert (storyboard.VOCABULARY_RULE in prompt) is vocabulary
    assert storyboard.QUERY_RULES == original


# ---- Property 13 ----

# 固定原文：一镜即可覆盖，返回的单镜 visual 非空、不过短，split 第一次尝试即返回。
VOCAB_TEXT = "alpha beta gamma."
VOCAB_SPLIT_REPLY = json.dumps(
    {"shots": [{"visual": "测试画面", "text": VOCAB_TEXT, "query": "测试 画面", "estimated_seconds": 1.5}]},
    ensure_ascii=False,
)
VOCAB_QUERY_REPLY = json.dumps({"query": "夜市 摊位"}, ensure_ascii=False)
VOCAB_WORDS = st.one_of(st.just(""), st.text(alphabet=HAN + LATIN + "画面词表", min_size=1, max_size=6))
VOCABULARIES = st.one_of(st.none(), st.lists(VOCAB_WORDS, max_size=8))


def _capture(config, call, reply_content: str) -> tuple[str, list[dict]]:
    """执行一次模型调用，返回 (system, user content)。"""
    requests: list[list[dict]] = []

    def reply(method, url, **kwargs):
        requests.append(kwargs["json"]["messages"])
        return {"choices": [{"message": {"content": reply_content}, "finish_reason": "stop"}]}

    with patch.object(adapters, "request", reply):
        call(QwenStoryboarder(config))
    assert len(requests) == 1
    system, user = requests[0]
    return system["content"], user["content"]


# Feature: studio-material-consistency, Property 13: 词表只在提供时进入请求
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(vocabulary=VOCABULARIES)
def test_vocabulary_only_enters_request_when_provided(config, vocabulary):
    """**Validates: Requirements 6.7, 9.3, 9.7**"""
    words = [word for word in (vocabulary or []) if word]
    segment = storyboard.VOCABULARY_HEADER + "、".join(words)
    calls = (
        (lambda s, v: s.split(VOCAB_TEXT, vocabulary=v), VOCAB_SPLIT_REPLY),
        (
            lambda s, v: s.suggest_query(
                "and quiet lakeside villages",
                "湖边安静的村落",
                ("From misty mountain trails", "to bustling night markets"),
                v,
            ),
            VOCAB_QUERY_REPLY,
        ),
    )
    for call, content in calls:
        system, user = _capture(config, lambda s, call=call: call(s, vocabulary), content)
        base_system, base_user = _capture(config, lambda s, call=call: call(s, None), content)
        assert storyboard.VOCABULARY_RULE not in base_system
        assert len(base_user) == 1
        texts = [part["text"] for part in user]
        if words:
            # 非空：独立的词表段（依次包含每个词），system 附加词表规则，其余文本与无词表时相同。
            assert storyboard.VOCABULARY_RULE in system
            assert texts == [base_user[0]["text"], segment]
            cursor = len(storyboard.VOCABULARY_HEADER)
            for word in words:  # 每个词按顺序出现在词表段中
                cursor = segment.index(word, cursor) + len(word)
            assert system.replace(storyboard.VOCABULARY_RULE, "", 1) == base_system
        else:
            # None、空列表或只有空字符串：没有词表段、没有词表规则，请求与 None 时完全相同。
            assert storyboard.VOCABULARY_RULE not in system
            assert not any(storyboard.VOCABULARY_HEADER in text for text in texts)
            assert (system, user) == (base_system, base_user)
