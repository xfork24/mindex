"""人物名称解析、文档提及识别与出镜区间查询辅助。"""

from __future__ import annotations

import re

KIND_LABELS = {"actor": "演员", "work": "作品", "role": "角色"}


def _occurrences(name, text):
    # Avoid matching Ann inside Anna; Chinese registered names may form phrases.
    pattern = re.escape(name)
    if re.fullmatch(r"[\w\s.-]+", name, flags=re.ASCII):
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    return list(re.finditer(pattern, text, flags=re.IGNORECASE))


def _fuzzy_union(pairs, base):
    """Distinct full role names sharing a typed suffix form a union; duplicate names stay ambiguous."""
    names = [name.casefold() for _, name in pairs]
    return (
        len(names) > 1
        and len(set(names)) == len(names)
        and all(name.endswith(base.casefold()) for name in names)
    )


_DOCUMENT = r"(?:记录|档案|资料|信件|书信|日记|传记|报道|报告|照片|画像)"


_REFERENCE = re.compile(r"(?:关于|有关)[^，,。.;；！？!?：:\n]{1,40}?" + _DOCUMENT)


_POSSESSIVE_DOCUMENT = re.compile(r"的(?:相关)?" + _DOCUMENT)


AUTO_PERSON_QUERY_VERSION = "document-reference-v1"


def has_document_reference(text):
    """Whether auto name interpretation can differ from the legacy co-appearance rule."""
    return bool(_REFERENCE.search(text) or _POSSESSIVE_DOCUMENT.search(text))


def _document_reference(text, start, end):
    # Only clear documentary references are relaxed. Separate visible occurrences of
    # the same person, coordinated subjects and explicit selections remain strict.
    return bool(
        _POSSESSIVE_DOCUMENT.match(text, end)
        or any(m.start() <= start and end <= m.end() for m in _REFERENCE.finditer(text))
    )


def resolve_names(query, entities):
    """Resolve typed names to identities; returns (ids, recognized, remaining, variant_groups).

    Roles also match by significant suffix so 叶淑柔 recalls a role named 老年叶淑柔;
    such variants form one union group (any member counts), never an ambiguity error.
    Explicit person_ids selections stay precise singletons.
    """

    lookup = {e["id"]: e for e in entities}
    explicit = list(dict.fromkeys(query.person_ids))
    selected_ids = set(explicit)
    for identifier in explicit:
        if identifier not in lookup or lookup[identifier]["kind"] == "work":
            raise ValueError("请选择已发布人物版本中的演员或角色")
    matches = []
    for e in entities:
        if e["kind"] == "work":
            continue
        for name in [e["name"], *e.get("aliases", [])]:
            for m in _occurrences(name, query.text):
                matches.append((m.start(), m.end(), e["id"], name))
    # Suffix recall: a role whose full name is absent can still match its base name.
    for e in entities:
        if e["kind"] != "role":
            continue
        for name in [e["name"], *e.get("aliases", [])]:
            if _occurrences(name, query.text):
                continue
            shortest = max(2, (len(name) + 1) // 2)
            for length in range(len(name) - 1, shortest - 1, -1):
                found = _occurrences(name[-length:], query.text)
                if found:
                    m = found[0]
                    matches.append((m.start(), m.end(), e["id"], name[-length:]))
                    break
    # Longest registered name wins over contained names (老年叶淑柔 vs 叶淑柔).
    matches = [
        m
        for m in matches
        if not any(a <= m[0] and b >= m[1] and b - a > m[1] - m[0] for a, b, _, _ in matches)
    ]
    groups = {}
    for a, b, identifier, name in matches:
        groups.setdefault((a, b), []).append((identifier, name))
    recognized = []
    variant_groups = []
    onscreen_spans = []
    for (a, b), group in sorted(groups.items()):
        options = list(dict.fromkeys(i for i, _ in group))
        selected = [i for i in options if i in explicit]
        union = (
            len(options) > 1
            and all(lookup[i]["kind"] == "role" for i in options)
            and _fuzzy_union([(i, lookup[i]["name"]) for i in options], query.text[a:b])
            and not selected
        )
        if len(options) > 1 and len(selected) != 1 and not union:
            raise ValueError(
                "姓名有歧义，请选择具体身份（演员与角色是不同身份）："
                + "、".join(
                    f"{KIND_LABELS.get(lookup[i]['kind'], lookup[i]['kind'])} {lookup[i]['name']} ({i})"
                    for i in options
                )
            )
        members = selected or options
        reference = (
            query.person_mode == "auto"
            and not selected_ids.intersection(members)
            and _document_reference(query.text, a, b)
        )
        for identifier in members:
            if not reference and identifier not in explicit:
                explicit.append(identifier)
            recognized.append(
                {
                    "id": identifier,
                    "name": lookup[identifier]["name"],
                    "kind": lookup[identifier]["kind"],
                    "span": [a, b],
                    **({"usage": "reference"} if reference else {}),
                }
            )
        if not reference:
            variant_groups.append(members)
            onscreen_spans.append((a, b))
    remaining = query.text
    for a, b in sorted(onscreen_spans, reverse=True):
        remaining = remaining[:a] + " " + remaining[b:]
    from_text = {i for g in variant_groups for i in g}
    singletons = [[i] for i in explicit if i not in from_text]
    return explicit, recognized, remaining.strip(" ，,。的与和及、"), [*singletons, *variant_groups]


def overlaps_for(segment, evidence, identifiers, relation):
    """identifiers are ids or variant groups (lists of ids); any member hits its group."""

    groups = [
        [
            e
            for e in evidence
            if any(
                i in (e["actor_id"], e["role_id"]) for i in (group if isinstance(group, list) else [group])
            )
            and e["source_id"] == segment["source_id"]
            and e["start"] < segment["end"]
            and e["end"] > segment["start"]
        ]
        for group in identifiers
    ]
    if not groups:
        return []
    if relation == "any":
        return list({e["id"]: e for g in groups for e in g}.values())
    if any(not g for g in groups):
        return []
    # Endpoint sweep: every group (selected identity or fuzzy variant union) overlaps at one instant.
    points = sorted({max(segment["start"], e["start"]) for g in groups for e in g})
    for t in points:
        if all(any(e["start"] <= t < min(e["end"], segment["end"]) for e in g) for g in groups):
            return list({e["id"]: e for g in groups for e in g}.values())
    return []


def coverage_missing(snapshots, coverage):
    """A partially processed film still reports missing coverage outside processed intervals."""
    by_source = {}
    for c in coverage:
        by_source.setdefault(c["source_id"], []).append((c["data"]["start"], c["data"]["end"]))
    missing = set()
    for s in snapshots:
        end = s["start"]
        for a, b in sorted(by_source.get(s["source_id"], [])):
            if a > end + 1e-6:
                break
            end = max(end, b)
            if end >= s["end"] - 1e-6:
                break
        if end < s["end"] - 1e-6:
            missing.add(s["source_id"])
    return sorted(missing)
