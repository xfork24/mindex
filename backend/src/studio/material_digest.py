"""可用素材摘要（Material Digest）：文案、拆分镜与重写召回词共用的只读参考资料。

摘要只取召回实际比对的 ``views.analysis`` 文字，使文案、画面意象与召回词的用词贴近素材画面词表。
结构：``compose_digest`` 等为纯函数（便于属性测试）；``build_digest`` 是只读 I/O 外壳；
``safe_digest`` 供 script/split 阶段与重写召回词使用，出错时退化为空摘要。

全程只读：只读取生效内容索引快照与覆盖层，不修改快照（decorate 返回新字典）、任务或任何产物。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable

from mindex.shared.analysis_text import select_analysis
from mindex.shared.config import Config, fingerprint
from mindex.shared.types import DigestSource, MaterialDigest

log = logging.getLogger(__name__)

DIGEST_VERSION = 2
VOCABULARY_SIZE = 30  # 每素材画面词上限（需求 2.4）
MIN_SOURCE_CHARS = 200  # 每素材最低配额（需求 2.5）
STORY_ITEM_CHARS = 60  # 单条故事线描述上限（按 Unicode 字符计，超出截断并以"…"结尾）
MERGED_VOCABULARY_SIZE = 120  # 传给拆分镜/重写召回词的合并词表上限
VOCAB_LABELS = ("场景", "对象", "动作")  # views.analysis 中参与词表的标签行
SKIP_LABELS = ("人物", "人物情绪", "氛围", "景别", "角度", "镜头运动")  # 不进故事线与词表
LABELS = VOCAB_LABELS + SKIP_LABELS

EMPTY_BUDGET = "预算为 0"
EMPTY_INDEX = "无生效索引"
EMPTY_SEGMENTS = "无已分析镜头"

STORY_KINDS = ("scene", "shot")  # 故事线来源优先级：scene 描述，没有时回退到 shot 描述
VOCAB_KINDS = ("shot", "moment")


def parse_analysis(text: str, language: str = "zh") -> tuple[str, dict[str, list[str]]]:
    """把 views.analysis 拆成"描述正文"与标签行。

    格式来自 ``Annotation.views()``：以"动作：/场景：/人物：/对象：/人物情绪：/氛围：/景别：/角度：/镜头运动："
    开头的行是标签行（按"；"切词、去空白、跳过空词），其余行是描述正文（换行压成空格）。
    """
    body: list[str] = []
    labels: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        label = next((name for name in LABELS if stripped.startswith(name + "：")), None)
        if label is None:
            body.append(stripped)
            continue
        words = [w.strip() for w in stripped[len(label) + 1 :].split("；")]
        labels.setdefault(label, []).extend(select_analysis(w, language) for w in words if w)
    return select_analysis(" ".join(body), language), labels


def allocate(counts: list[int], budget: int) -> list[int]:
    """按镜头数比例分配各素材的字符配额（需求 2.5/2.6），总和不超过 budget。

    budget ≥ 200·n 时每素材先得 200，余量按镜头数比例向下取整分配；否则每素材 ⌊budget/n⌋。
    """
    n = len(counts)
    if n == 0 or budget <= 0:
        return [0] * n
    floor_total = MIN_SOURCE_CHARS * n
    if budget < floor_total:
        return [budget // n] * n
    total = sum(counts)
    spare = budget - floor_total
    if total <= 0:
        return [MIN_SOURCE_CHARS + spare // n] * n
    return [MIN_SOURCE_CHARS + spare * c // total for c in counts]


def sample(items: list[str], k: int) -> list[str]:
    """等间距抽样 k 条：k ≥ 2 时取下标 round(i·(n-1)/(k-1))，包含首尾。

    步长 (n-1)/(k-1) ≥ 1，因此下标严格递增，结果是 items 的子序列。
    """
    n = len(items)
    if k <= 0 or n == 0:
        return []
    if k >= n:
        return list(items)
    if k == 1:
        return [items[0]]
    return [items[round(i * (n - 1) / (k - 1))] for i in range(k)]


def _clip_story(text: str) -> str:
    if len(text) <= STORY_ITEM_CHARS:
        return text
    return text[: STORY_ITEM_CHARS - 1] + "…"


def _storyline(segments: list[dict], language: str = "zh") -> list[str]:
    """scene 描述按 (start, id) 升序、截断到 STORY_ITEM_CHARS 后去重（保留最早一条）；
    没有 scene 描述时改用 shot（需求 2.2/2.3）。"""
    for kind in STORY_KINDS:
        picked = sorted((s for s in segments if s["kind"] == kind), key=lambda s: (s["start"], s["id"]))
        seen: set[str] = set()
        story: list[str] = []
        for segment in picked:
            # 先截断再去重：截断后相同的条目也只保留最早一条，保证故事线条目两两不同。
            body = _clip_story(parse_analysis(segment["views"]["analysis"], language)[0].strip())
            if body and body not in seen:
                seen.add(body)
                story.append(body)
        if story:
            return story
    return []


def _vocabulary(segments: list[dict], language: str = "zh") -> list[str]:
    """shot/moment 的场景、对象、动作词合并计数，按（频次降序，词升序）取前 30 个（需求 2.4）。"""
    counter: Counter[str] = Counter()
    for segment in segments:
        if segment["kind"] not in VOCAB_KINDS:
            continue
        labels = parse_analysis(segment["views"]["analysis"], language)[1]
        for label in VOCAB_LABELS:
            counter.update(labels.get(label, []))
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:VOCABULARY_SIZE]]


def _render(
    name: str, duration: float, vocabulary: list[str], story: list[str], quota: int, language: str = "zh"
) -> tuple[str, list[str]]:
    """按配额渲染一个素材块，返回（块正文，实际写入的故事线条目）；块长度不超过 quota。"""
    header = (f"[{name}] Duration: {round(duration)} seconds\n" if language == "en"
              else f"【{name}】时长 {round(duration)} 秒\n")
    if len(header) >= quota:
        return header[:quota], []
    block = header
    words = list(vocabulary)
    while words:
        line = (("Visual vocabulary: " + ", ".join(words)) if language == "en" else "画面词：" + "、".join(words)) + "\n"
        if len(block) + len(line) <= quota:
            block += line
            break
        words.pop()
    for k in range(len(story), 0, -1):
        picked = sample(story, k)
        line = (("Storyline: " + "; ".join(picked)) if language == "en" else "故事线：" + "；".join(picked)) + "\n"
        if len(block) + len(line) <= quota:
            return block + line, picked
    return block, []


def _eligible(segment: dict, scope: set[str] | None) -> bool:
    if segment.get("annotation_status") != "complete" or segment.get("effective_hidden"):
        return False
    if scope is not None and segment["source_id"] not in scope:
        return False
    return bool(str((segment.get("views") or {}).get("analysis") or "").strip())


def compose_digest(
    snapshots: Iterable[dict], *, scope: set[str] | None, budget: int, content_index_id: str | None, language: str = "zh"
) -> MaterialDigest:
    """纯函数核心：snapshots 为已 decorate 的快照字典（含 effective_hidden 与生效显示名）。

    不修改输入。相同输入（与快照顺序无关）得到逐字节相同的 Digest 与指纹。

    极小预算的约定：配额向下取整为 0 的素材渲染不出任何正文，这类素材不进入 Digest（也不计入
    素材数、镜头数与指纹），以保持"非空 Digest 必有正文"的不变式；全部素材都放不下时返回空 Digest，
    为空原因记为"预算为 0"（有效配额为 0）。
    """
    if budget <= 0:
        return MaterialDigest.empty_of(EMPTY_BUDGET, content_index_id)
    groups: dict[str, list[dict]] = {}
    for segment in snapshots:
        if _eligible(segment, scope):
            groups.setdefault(segment["source_id"], []).append(segment)
    if not groups:
        return MaterialDigest.empty_of(EMPTY_SEGMENTS, content_index_id)

    def source_name(items: list[dict]) -> str:
        # 同一素材的显示名来自覆盖层，各片段一致；取最小值使结果与片段顺序无关。
        return min(str(s.get("source_name") or "") for s in items)

    ordered = sorted(groups.items(), key=lambda item: (source_name(item[1]), item[0]))
    counts = [len(items) for _, items in ordered]
    quotas = allocate(counts, budget)

    blocks: list[str] = []
    sources: list[DigestSource] = []
    included: list[tuple[str, str]] = []
    for (source_id, items), quota in zip(ordered, quotas, strict=True):
        name = source_name(items)
        duration = max(float(s.get("source_duration") or s.get("end") or 0) for s in items)
        vocabulary = _vocabulary(items, language)
        block, story = _render(name, duration, vocabulary, _storyline(items, language), quota, language)
        if not block:
            continue
        blocks.append(block)
        sources.append(
            DigestSource(
                source_id=source_id,
                name=name,
                duration=duration,
                segments=len(items),
                storyline=story,
                vocabulary=vocabulary,
            )
        )
        included.extend((s["id"], s["views"]["analysis"]) for s in items)
    if not sources:
        return MaterialDigest.empty_of(EMPTY_BUDGET, content_index_id)

    text = "".join(blocks)
    digest_fingerprint = fingerprint(
        {
            "v": DIGEST_VERSION,
            "index": content_index_id,
            "budget": budget,
            "segments": sorted(included),
            "text": text,
        }
    )
    return MaterialDigest(
        text=text,
        sources=sources,
        source_count=len(sources),
        segment_count=sum(s.segments for s in sources),
        fingerprint=digest_fingerprint,
        content_index_id=content_index_id,
    )


def merged_vocabulary(digest: MaterialDigest | None) -> list[str] | None:
    """按素材顺序拼接各素材词表、去重并截断到 MERGED_VOCABULARY_SIZE；空 Digest 返回 None。"""
    if digest is None or digest.empty:
        return None
    merged: list[str] = []
    seen: set[str] = set()
    for source in digest.sources:
        for word in source.vocabulary:
            if word not in seen:
                seen.add(word)
                merged.append(word)
                if len(merged) >= MERGED_VOCABULARY_SIZE:
                    return merged
    return merged or None


def build_digest(config: Config, store=None, state: dict | None = None, *, language: str | None = None,
                 filters=None) -> MaterialDigest:
    """I/O 外壳：读取生效索引快照、覆盖层、任务范围与预算，调用 compose_digest。只读。

    空原因优先级：预算为 0 → 无生效索引 → 无已分析镜头（需求 3.6）。
    """
    from mindex.admin.curation import Library
    from mindex.shared.storage import Store
    from mindex.shared.types import Filters

    store = store or Store.open(config)
    state = state or {}
    scope = Filters(source_ids=state.get("source_ids") or [],
                    source_group_ids=state.get("source_group_ids") or []).effective_sources(store)
    if filters is not None:
        additional = filters.effective_sources(store)
        if additional is not None:
            scope = additional if scope is None else scope & additional
    budget = config.studio.material_digest_chars
    if budget == 0:
        return MaterialDigest.empty_of(EMPTY_BUDGET)
    store = store or Store.open(config)
    active = store.active_index()
    if active is None:
        return MaterialDigest.empty_of(EMPTY_INDEX)
    library = Library(store)
    sources, segments = library.overlays()
    decorated = [library.decorate(s, sources, segments) for s in store.snapshots_shared(active["id"])]
    if filters is not None:
        def matches(segment):
            duration = segment["end"] - segment["start"]
            annotation = segment.get("annotation") or {}
            return (duration >= filters.min_duration
                    and (filters.max_duration is None or duration <= filters.max_duration)
                    and all(not value or any(value.casefold() in item.casefold()
                                             for item in annotation.get(field) or [])
                            for field, value in (("scene", filters.scene), ("emotions", filters.emotion),
                                                 ("shot_size", filters.shot_size))))

        decorated = [segment for segment in decorated if matches(segment)]
    selected_language = language or state.get("language", "zh")
    if selected_language == "en":
        from mindex.shared.analysis_translations import english_analysis_view, load_translations

        translations = load_translations(store.root)
        if translations:
            decorated = [{**segment, "views": {**segment.get("views", {}),
                          "analysis": english_analysis_view(segment.get("views", {}).get("analysis", ""), translations)}}
                         for segment in decorated]
    return compose_digest(
        decorated,
        scope=scope,
        budget=budget,
        content_index_id=active["id"],
        language=selected_language,
    )


def safe_digest(
    config: Config, job_id: str, *, store=None, on_error: Callable[[str], None] | None = None, filters=None
) -> MaterialDigest:
    """script/split 阶段与重写召回词共用：范围失效报错，基础设施异常退化为空 Digest。

    出错时记一条中文日志，并把同一条中文诊断交给 on_error（例如写入版本调试记录）。
    回退的空 Digest 为空原因取"无已分析镜头"：调用方只看 ``empty``，该原因不会展示给用户
    （预览接口直接调用 build_digest，出错照常上抛）。
    """
    from mindex.shared.storage import Store
    from mindex.shared.types import Filters
    from mindex.studio.studio import load_job

    state = load_job(config, job_id)
    store = store or Store.open(config)
    # 用户范围错误不能作为基础设施故障吞掉，否则会掩盖已删除的组。
    Filters(source_ids=state.get("source_ids") or [],
            source_group_ids=state.get("source_group_ids") or []).effective_sources(store)
    if filters is not None:
        filters.effective_sources(store)
    try:
        return build_digest(config, store, state, filters=filters)
    except Exception as exc:  # noqa: BLE001 - 摘要只是参考资料，任何错误都不应中断主流程
        message = f"素材摘要生成失败，已按无摘要继续：{exc}"
        log.warning(message)
        if on_error is not None:
            on_error(message)
        return MaterialDigest.empty_of(EMPTY_SEGMENTS)


def scope_parameters(config, job_id, store) -> dict:
    """只为显式限定素材的任务追加动态范围签名，保留旧任务签名。"""
    from mindex.shared.media_groups import Groups
    from mindex.studio.studio import load_job

    state = load_job(config, job_id)
    if not state.get("source_ids") and not state.get("source_group_ids"):
        return {}
    return {"material_scope": {"source_ids": sorted(state.get("source_ids") or []),
                               "groups": Groups(store).state("source", state.get("source_group_ids") or [])}}
