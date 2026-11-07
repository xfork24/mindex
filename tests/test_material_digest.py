"""素材摘要（Material Digest）纯函数核心的属性测试。

生成器产出"已 decorate 的快照字典"，与 ``Library.decorate`` 的输出字段一致（只用到 compose_digest
读取的键）。每个素材的名称与标签词都带素材专属标记 ``S{i}Z``，便于断言"范围外素材不出现在正文中"。
后续属性测试（Property 2–7）复用本文件的生成器。
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from studio.material_digest import (
    MIN_SOURCE_CHARS,
    SKIP_LABELS,
    STORY_ITEM_CHARS,
    VOCAB_LABELS,
    _storyline,
    allocate,
    compose_digest,
    parse_analysis,
    sample,
)

MAX_SOURCES = 4
KINDS = ("scene", "shot", "moment")
STATUSES = ("complete", "pending", "failed")


def source_id(i: int) -> str:
    return f"src-{i}"


def marker(i: int) -> str:
    """素材专属标记；以 Z 结尾，避免 S1Z 成为 S11Z 的子串。"""
    return f"S{i}Z"


@st.composite
def analysis_texts(draw, i: int) -> str:
    """按 ``Annotation.views()`` 格式生成 views.analysis：描述正文行 + 若干标签行（按"；"分词）。"""
    lines: list[str] = []
    if draw(st.booleans()):
        lines.append(f"描述{marker(i)}第{draw(st.integers(0, 9))}段")
    labels = draw(st.lists(st.sampled_from(VOCAB_LABELS + SKIP_LABELS), unique=True, max_size=4))
    for label in labels:
        words = draw(st.lists(st.integers(0, 5), min_size=1, max_size=3))
        lines.append(f"{label}：" + "；".join(f"词{w}{marker(i)}" for w in words))
    return "\n".join(lines)


@st.composite
def segments(draw, i: int, index: int) -> dict:
    """单个已 decorate 的片段快照。"""
    start = draw(st.floats(0, 600, allow_nan=False, allow_infinity=False))
    return {
        "id": f"seg-{i}-{index}",
        "source_id": source_id(i),
        "source_name": f"素材{marker(i)}",
        "source_duration": 600.0 + i,
        "start": start,
        "end": start + draw(st.floats(0.1, 10, allow_nan=False, allow_infinity=False)),
        "kind": draw(st.sampled_from(KINDS)),
        "annotation_status": draw(st.sampled_from(STATUSES)),
        "effective_hidden": draw(st.booleans()),
        "views": {"analysis": draw(analysis_texts(i)), "dialogue": f"对白{marker(i)}"},
    }


@st.composite
def snapshot_sets(draw, min_sources: int = 1, max_sources: int = MAX_SOURCES) -> list[dict]:
    """1–4 个素材、每素材 0–30 个片段的快照列表。"""
    n = draw(st.integers(min_sources, max_sources))
    result: list[dict] = []
    for i in range(n):
        count = draw(st.integers(0, 30))
        result.extend(draw(segments(i, j)) for j in range(count))
    return result


def scopes(snapshots: list[dict]):
    """素材范围：None、空集，或已有素材 id 的任意子集（可混入不存在的 id）。"""
    ids = sorted({s["source_id"] for s in snapshots})
    subset = st.sets(st.sampled_from(ids)) if ids else st.just(set())
    return st.one_of(
        st.none(),
        st.just(set()),
        st.tuples(subset, st.booleans()).map(lambda t: t[0] | ({"src-missing"} if t[1] else set())),
    )


def eligible(segment: dict, scope: set[str] | None) -> bool:
    """参考实现：complete、未隐藏、在范围内、analysis 非空。"""
    return (
        segment["annotation_status"] == "complete"
        and not segment["effective_hidden"]
        and (scope is None or segment["source_id"] in scope)
        and bool(segment["views"]["analysis"].strip())
    )


# Feature: studio-material-consistency, Property 1: 范围过滤
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_scope_filtering(data):
    snapshots = data.draw(snapshot_sets())
    scope = data.draw(scopes(snapshots))
    # 每个素材至少得到 MIN_SOURCE_CHARS 配额，保证纳入的素材都能渲染出正文。
    budget = data.draw(st.integers(MIN_SOURCE_CHARS * MAX_SOURCES, 20000))

    digest = compose_digest(snapshots, scope=scope, budget=budget, content_index_id="idx-1")

    expected = {s["source_id"] for s in snapshots if eligible(s, scope)}
    assert {s.source_id for s in digest.sources} == expected
    if scope == set():
        assert digest.empty
    for i in range(MAX_SOURCES):
        if source_id(i) not in expected:
            assert marker(i) not in digest.text
        else:
            assert f"素材{marker(i)}" in digest.text


@st.composite
def noise_segments(draw, scope: set[str] | None, index: int) -> dict:
    """不合格片段：已隐藏、未完成分析，或位于范围外（scope 为 None 时不存在"范围外"）。"""
    segment = draw(segments(draw(st.integers(0, MAX_SOURCES - 1)), index))
    segment["id"] = f"noise-{index}"
    kinds = ["hidden", "status"] + (["scope"] if scope is not None else [])
    kind = draw(st.sampled_from(kinds))
    if kind == "hidden":
        segment["effective_hidden"] = True
    elif kind == "status":
        segment["annotation_status"] = draw(st.sampled_from(("pending", "failed", "running")))
    else:
        segment["source_id"] = "src-noise"  # 生成器的 scope 从不包含该 id
    return segment


def rewrite_person(analysis: str, words: list[str]) -> str:
    """把"人物"标签行替换为新词；analysis 非空时再追加一行"人物"，不改变片段是否合格。"""
    line = "人物：" + "；".join(words)
    kept = [x for x in analysis.splitlines() if not x.strip().startswith("人物：")]
    if analysis.strip():
        kept.append(line)
    return "\n".join(kept)


def digest_view(digest) -> tuple:
    return digest.text, [s.model_dump() for s in digest.sources], digest.segment_count


# Feature: studio-material-consistency, Property 2: 不合格片段与非分析文字不影响 Digest
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_ineligible_and_non_analysis_text_ignored(data):
    snapshots = data.draw(snapshot_sets())
    scope = data.draw(scopes(snapshots))
    budget = data.draw(st.integers(MIN_SOURCE_CHARS * MAX_SOURCES, 20000))
    clean = [s for s in snapshots if eligible(s, scope)]
    noise = [data.draw(noise_segments(scope, j)) for j in range(data.draw(st.integers(0, 15)))]
    assert not any(eligible(s, scope) for s in noise)

    def compose(items):
        return compose_digest(items, scope=scope, budget=budget, content_index_id="idx-1")

    base = compose(clean)
    assert base.segment_count == len(clean)

    # 追加噪声片段、改写任意片段的对白：Digest 完全相同（含指纹）。
    dialogue = data.draw(st.text(max_size=20))
    noisy = [{**s, "views": {**s["views"], "dialogue": dialogue + "对白X"}} for s in clean + noise]
    mixed = data.draw(st.permutations(noisy))
    with_noise = compose(mixed)
    assert digest_view(with_noise) == digest_view(base)
    assert with_noise.fingerprint == base.fingerprint
    assert "对白" not in with_noise.text

    # 改写"人物"标签行：正文、素材与镜头数不变，人物词不出现在正文中。
    # 指纹覆盖纳入片段的完整 analysis（Property 4 要求对分析文字敏感），因此这里不比较指纹。
    person = data.draw(st.lists(st.sampled_from(("人物甲Q", "人物乙Q", "人物丙Q")), min_size=1, max_size=3))
    persons = [
        {**s, "views": {**s["views"], "analysis": rewrite_person(s["views"]["analysis"], person)}}
        for s in mixed
    ]
    with_person = compose(persons)
    assert digest_view(with_person) == digest_view(base)
    assert "人物" not in with_person.text


# Feature: studio-material-consistency, Property 3: 确定性与顺序无关
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_deterministic_and_order_independent(data):
    snapshots = data.draw(snapshot_sets())
    scope = data.draw(scopes(snapshots))
    # 任意预算（含 0 与小于 200×n 的情况）下确定性都应成立。
    budget = data.draw(st.integers(0, 20000))
    original = copy.deepcopy(snapshots)

    def compose(items):
        return compose_digest(items, scope=scope, budget=budget, content_index_id="idx-1")

    first = compose(snapshots)
    second = compose(snapshots)
    shuffled = compose(data.draw(st.permutations(snapshots)))

    for other in (second, shuffled):
        assert other.model_dump() == first.model_dump()
        assert [s.source_id for s in other.sources] == [s.source_id for s in first.sources]
    # compose_digest 不修改输入快照。
    assert snapshots == original


# Feature: studio-material-consistency, Property 4: 指纹对分析文字敏感
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_fingerprint_sensitive_to_analysis(data):
    snapshots = data.draw(snapshot_sets())
    scope = data.draw(scopes(snapshots))
    # 每个素材都能拿到最低配额，被改写片段所在的素材一定纳入 Digest。
    budget = data.draw(st.integers(MIN_SOURCE_CHARS * MAX_SOURCES, 20000))

    candidates = [k for k, s in enumerate(snapshots) if eligible(s, scope)]
    if not candidates:
        # 没有合格片段时补一个合格片段，并把它的素材放进范围。
        forced = data.draw(segments(0, 999))
        forced.update(annotation_status="complete", effective_hidden=False)
        if not forced["views"]["analysis"].strip():
            forced["views"]["analysis"] = f"描述{marker(0)}补充"
        snapshots = [*snapshots, forced]
        if scope is not None:
            scope = scope | {forced["source_id"]}
        candidates = [len(snapshots) - 1]
    target = data.draw(st.sampled_from(candidates))

    def compose(items):
        return compose_digest(items, scope=scope, budget=budget, content_index_id="idx-1")

    base = compose(snapshots)
    assert not base.empty

    old = snapshots[target]["views"]["analysis"]
    new = data.draw(
        st.one_of(
            analysis_texts(int(snapshots[target]["source_id"].split("-")[1])),
            st.text(min_size=1, max_size=40),
        ).filter(lambda t: t.strip() and t != old)
    )
    changed = copy.deepcopy(snapshots)
    changed[target]["views"]["analysis"] = new
    assert eligible(changed[target], scope)

    assert compose(changed).fingerprint != base.fingerprint


def split_blocks(text: str) -> list[str]:
    """按块首字符"【"切分正文；名称、画面词与故事线都不含"【"，每块都以它开头。"""
    starts = [k for k, ch in enumerate(text) if ch == "【"]
    return [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)][: len(starts)], strict=True)]


# Feature: studio-material-consistency, Property 5: 预算分配与总长度上界
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_budget_allocation_and_length_bound(data):
    # allocate：总和不超过预算；够分时每项 ≥ 200 且随镜头数单调；不够分时平均分。
    counts = data.draw(st.lists(st.integers(1, 500), min_size=1, max_size=8))
    n = len(counts)
    budget = data.draw(
        st.one_of(st.integers(1, MIN_SOURCE_CHARS * n), st.integers(MIN_SOURCE_CHARS * n, 100000))
    )
    quotas = allocate(counts, budget)
    assert len(quotas) == n
    assert sum(quotas) <= budget
    if budget >= MIN_SOURCE_CHARS * n:
        assert all(q >= MIN_SOURCE_CHARS for q in quotas)
        for a in range(n):
            for b in range(n):
                if counts[a] > counts[b]:
                    assert quotas[a] >= quotas[b]
    else:
        assert quotas == [budget // n] * n

    # compose_digest：任意预算（含 0 与极小值）下正文长度不超过预算，每素材块不超过其配额。
    snapshots = data.draw(snapshot_sets())
    scope = data.draw(scopes(snapshots))
    digest_budget = data.draw(
        st.one_of(st.integers(0, 10), st.integers(0, MIN_SOURCE_CHARS * MAX_SOURCES), st.integers(0, 20000))
    )
    digest = compose_digest(snapshots, scope=scope, budget=digest_budget, content_index_id="idx-1")
    assert len(digest.text) <= digest_budget
    if digest_budget == 0:
        assert digest.empty

    groups: dict[str, list[dict]] = {}
    for s in snapshots:
        if eligible(s, scope):
            groups.setdefault(s["source_id"], []).append(s)
    ordered = sorted(groups, key=lambda sid: (groups[sid][0]["source_name"], sid))
    quota_of = dict(zip(ordered, allocate([len(groups[sid]) for sid in ordered], digest_budget), strict=True))

    included = [s.source_id for s in digest.sources]
    assert included == [sid for sid in ordered if sid in included]
    # 配额为 0 的素材渲染不出正文，被丢弃。
    assert all(quota_of[sid] > 0 for sid in included)
    blocks = split_blocks(digest.text)
    assert len(blocks) == len(included)
    assert "".join(blocks) == digest.text
    for sid, block in zip(included, blocks, strict=True):
        assert len(block) <= quota_of[sid]


# 故事线描述池：含重复项、首尾空白，以及截断后才相同的超长描述（共享前 STORY_ITEM_CHARS-1 个字符）。
LONG_PREFIX = "长" * (STORY_ITEM_CHARS + 5)
STORY_BODIES = ("情节甲", "情节乙", "  情节甲  ", "情节丙", LONG_PREFIX + "A", LONG_PREFIX + "B", "")


@st.composite
def story_segments(draw) -> list[dict]:
    """单个素材的合格片段：kind 随机，开始时间取自小集合以制造 (start, id) 的并列。"""
    count = draw(st.integers(0, 20))
    result = []
    for j in range(count):
        body = draw(st.sampled_from(STORY_BODIES))
        lines = [body] if body else []
        if draw(st.booleans()):
            lines.append(f"场景：词{draw(st.integers(0, 3))}")
        if not lines:
            lines.append("对象：词0")  # 保持 analysis 非空（片段合格），但没有描述正文
        start = float(draw(st.integers(0, 5)))
        result.append(
            {
                "id": f"seg-{j}",
                "source_id": source_id(0),
                "source_name": f"素材{marker(0)}",
                "source_duration": 600.0,
                "start": start,
                "end": start + 1.0,
                "kind": draw(st.sampled_from(KINDS)),
                "annotation_status": "complete",
                "effective_hidden": False,
                "views": {"analysis": "\n".join(lines)},
            }
        )
    return result


def clip_reference(text: str) -> str:
    return text if len(text) <= STORY_ITEM_CHARS else text[: STORY_ITEM_CHARS - 1] + "…"


def is_subsequence(part: list[str], whole: list[str]) -> bool:
    it = iter(whole)
    return all(any(x == y for y in it) for x in part)


# Feature: studio-material-consistency, Property 6: 故事线排序、去重与等间距抽样
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_storyline_order_dedupe_and_sampling(data):
    # sample：恰好 k 个、严格递增下标构成的子序列，k ≥ 2 时含首尾。
    n = data.draw(st.integers(1, 40))
    items = [f"条目{j}" for j in range(n)]
    k = data.draw(st.integers(1, n))
    picked = sample(items, k)
    assert len(picked) == k
    positions = [items.index(x) for x in picked]
    assert positions == sorted(set(positions))
    if k >= 2:
        assert picked[0] == items[0] and picked[-1] == items[-1]

    # _storyline：scene 按 (start, id) 升序、截断后去重保留最早一条；无 scene 描述时回退到 shot。
    segs = data.draw(story_segments())
    story = _storyline(segs)

    def reference(kind: str) -> list[tuple[tuple, str]]:
        chosen = sorted((s for s in segs if s["kind"] == kind), key=lambda s: (s["start"], s["id"]))
        out: list[tuple[tuple, str]] = []
        for s in chosen:
            body = clip_reference(parse_analysis(s["views"]["analysis"])[0].strip())
            if body and body not in [b for _, b in out]:
                out.append(((s["start"], s["id"]), body))
        return out

    expected = reference("scene") or reference("shot")
    assert story == [body for _, body in expected]
    assert len(set(story)) == len(story)
    assert all(x == x.strip() and len(x) <= STORY_ITEM_CHARS for x in story)
    # 每条对应的最早来源片段按 (start, id) 严格递增。
    keys = [key for key, _ in expected]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)

    # compose_digest：写进正文的故事线是完整故事线的子序列（抽样保持顺序），且出现在正文中。
    budget = data.draw(st.integers(1, 3000))
    digest = compose_digest(segs, scope=None, budget=budget, content_index_id="idx-1")
    for source in digest.sources:
        assert is_subsequence(source.storyline, story)
        if source.storyline:
            assert "故事线：" + "；".join(source.storyline) + "\n" in digest.text


# 画面词池：超过 30 个不同词（检验截断），并由小池抽样制造频次并列（检验按词升序的次序）。
VOCAB_POOL = tuple(f"画面{j:02d}" for j in range(45)) + ("Ａ词", "a词", "B词", "游客", "夜景")


@st.composite
def vocab_lines(draw) -> str:
    """一条标签行：词带随机首尾空白、可混入空词；标签含参与词表与不参与词表两类。"""
    label = draw(st.sampled_from(VOCAB_LABELS + SKIP_LABELS))
    words = draw(st.lists(st.sampled_from(VOCAB_POOL), min_size=1, max_size=6))
    pads = st.sampled_from(("", " ", "  ", "\u3000"))
    tokens = [draw(pads) + w + draw(pads) for w in words]
    if draw(st.booleans()):
        tokens.insert(draw(st.integers(0, len(tokens))), draw(pads))  # 空词或纯空白词
    return draw(pads) + f"{label}：" + "；".join(tokens)


@st.composite
def vocab_segments(draw) -> list[dict]:
    """单个素材的合格片段（kind 随机），analysis 由描述正文与若干标签行组成。"""
    result = []
    for j in range(draw(st.integers(1, 25))):
        lines = [draw(vocab_lines()) for _ in range(draw(st.integers(0, 4)))]
        if draw(st.booleans()) or not lines:
            lines.insert(0, f"描述正文{j}")
        result.append(
            {
                "id": f"seg-{j}",
                "source_id": source_id(0),
                "source_name": f"素材{marker(0)}",
                "source_duration": 600.0,
                "start": float(j),
                "end": float(j) + 1.0,
                "kind": draw(st.sampled_from(KINDS)),
                "annotation_status": "complete",
                "effective_hidden": False,
                "views": {"analysis": "\n".join(lines)},
            }
        )
    return result


def vocabulary_reference(segs: list[dict]) -> list[str]:
    """独立参考实现：只看 shot/moment 的"场景/对象/动作"行，按"；"切词、去空白、跳过空词后计数。"""
    counts: dict[str, int] = {}
    for s in segs:
        if s["kind"] not in ("shot", "moment"):
            continue
        for raw in s["views"]["analysis"].split("\n"):
            line = raw.strip()
            for label in ("场景", "对象", "动作"):
                prefix = label + "："
                if line.startswith(prefix):
                    for word in line[len(prefix) :].split("；"):
                        word = word.strip()
                        if word:
                            counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts, key=lambda w: (-counts[w], w))
    return ranked[: min(30, len(ranked))]


# Feature: studio-material-consistency, Property 7: 画面词表与参考实现一致
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_vocabulary_matches_reference(data):
    segs = data.draw(vocab_segments())
    expected = vocabulary_reference(segs)

    # 词表与预算无关：任意能纳入该素材的预算下，DigestSource.vocabulary 都等于参考结果。
    budget = data.draw(st.one_of(st.integers(1, 60), st.integers(1, 20000)))
    digest = compose_digest(segs, scope=None, budget=budget, content_index_id="idx-1")
    assert len(digest.sources) == 1
    vocabulary = digest.sources[0].vocabulary
    assert vocabulary == expected
    assert len(vocabulary) <= 30 and len(set(vocabulary)) == len(vocabulary)
    assert all(w == w.strip() and w for w in vocabulary)
