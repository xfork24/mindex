"""get_shot_details 的纯函数性质测试：分镜详情、字幕/配音信息、引用解析、分页与渲染。

生成器构造与 ``GET /api/studio/jobs/{job_id}/workspace?compact=true`` 同形的 workspace 字典，
直接用 ``PlanView`` 包装，不需要真实任务或回环。
"""

from __future__ import annotations

import asyncio
import copy
import itertools
from typing import ClassVar
from uuid import uuid4

import pytest
from agent_helpers import new_session, result_text, results, send, wait_idle
from conftest import legacy_job
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mindex.agent.ids import short
from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.agent.tools.common import STAGES, PlanView
from mindex.agent.tools.shot_details import page_details, speech_info, subtitle_info
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import workspace as ws

HEX = "0123456789abcdef"
STATUSES = ("complete", "failed", "running", "pending")
DETAIL_KEYS = {
    "index", "short_id", "shot_id", "text", "query", "visual", "estimated_seconds", "granularity",
    "recall_limit", "recall", "source", "manual_range", "stages", "subtitle", "speech", "generation", "animation", "drawing", "music",
}

# 旁白/查询等自由文本：含换行、全角空格、连续空白、标点与 emoji
SPECIAL = st.sampled_from(["\n", "\u3000", "  ", "\t", "，", "。", "！", "「", "」", "😀", "🎬", "…"])
CHARS = st.one_of(st.characters(blacklist_categories=("Cs",)), SPECIAL)


def free_text(min_size: int, max_size: int):
    return st.lists(CHARS, min_size=min_size, max_size=max_size).map("".join)


hex_str = lambda n: st.text(HEX, min_size=n, max_size=n)
finite = st.floats(min_value=0, max_value=600, allow_nan=False, allow_infinity=False)


@st.composite
def shot_ids(draw, min_size: int = 0, max_size: int = 8) -> list[str]:
    """唯一的 ``shot-`` + 10 位十六进制 ID；部分 ID 共享 7–8 位前缀，以便产生 ambiguous 引用。"""
    n = draw(st.integers(min_size, max_size))
    prefixes = draw(st.lists(st.one_of(hex_str(7), hex_str(8)), min_size=1, max_size=3))
    ids: list[str] = []
    while len(ids) < n:
        prefix = draw(st.sampled_from(prefixes)) if draw(st.booleans()) else ""
        sid = "shot-" + prefix + draw(hex_str(10 - len(prefix)))
        if sid not in ids:
            ids.append(sid)
    return ids


@st.composite
def contents(draw) -> dict:
    """分镜内容版本 data（ShotBody 形状）；可选字段可缺省。"""
    body = {
        "text": draw(free_text(1, 200)),
        "query": draw(free_text(1, 400)),
        "estimated_seconds": draw(st.floats(min_value=0.1, max_value=60, allow_nan=False)),
    }
    if draw(st.booleans()):
        body["visual"] = draw(st.one_of(st.just(""), free_text(1, 120)))
    if draw(st.booleans()):
        body["granularity"] = draw(st.sampled_from(["shot", "scene", "segment"]))
    if draw(st.booleans()):
        body["recall_limit"] = draw(st.integers(1, 50))
    if draw(st.booleans()):
        body["recall"] = draw(st.one_of(
            st.just({}),
            st.fixed_dictionaries({}, optional={"sources": st.lists(hex_str(8), max_size=3),
                                                "persons": st.lists(st.text(max_size=6), max_size=2),
                                                "min_score": st.floats(0, 1, allow_nan=False)}),
        ))
    return body


@st.composite
def versions(draw, stage: str, sid: str, narration: str = "") -> dict | None:
    """某分镜某阶段的选用版本；None 表示该阶段没有版本（missing）。

    select 版本可带 ``data.event.subtitle``（文本可能与当前旁白不同），tts 版本可带 ``data.duration``。
    """
    if not draw(st.booleans()):
        return None
    data: dict = {}
    if stage == "select" and draw(st.booleans()):
        event: dict = {"start": 0.0}
        if draw(st.booleans()):
            text = narration if draw(st.booleans()) else draw(free_text(1, 200))
            start = draw(finite)
            event["subtitle"] = {"text": text, "start": start, "end": start + draw(finite)}
        data["event"] = event
    if stage == "tts" and draw(st.booleans()):
        data["duration"] = draw(finite)
    return {"stage": stage, "shot_id": sid, "status": draw(st.sampled_from(STATUSES)), "data": data}


@st.composite
def plans(draw, versions_out: dict, ids: list[str]) -> dict:
    """组装一个方案：board 版本 + 每个分镜的内容与各阶段版本 + 可选人工区间；版本写入 versions_out。"""

    def new_vid() -> str:
        while True:
            vid = draw(hex_str(32))
            if vid not in versions_out:
                return vid

    board = None
    if ids or draw(st.booleans()):
        board = new_vid()
        versions_out[board] = {"stage": "board", "status": "complete", "data": {"order": list(ids)}}
    shots: dict = {}
    ranges: dict = {}
    for sid in ids:
        body = draw(contents())
        cid = new_vid()
        versions_out[cid] = {"stage": "content", "shot_id": sid, "status": "complete", "data": body}
        entry = {"content": cid}
        for stage in STAGES:
            item = draw(versions(stage, sid, body["text"]))
            if item is not None:
                vid = new_vid()
                versions_out[vid] = item
                entry[stage] = vid
        shots[sid] = entry
        if draw(st.booleans()):
            start = draw(finite)
            ranges[sid] = {"source": draw(hex_str(8)), "start": start, "end": start + draw(finite)}
    plan = {"name": draw(st.text(min_size=1, max_size=10)), "shots": shots, "ranges": ranges}
    if board:
        plan["board"] = board
    return plan


@st.composite
def workspace(draw, min_shots: int = 0, max_shots: int = 8) -> dict:
    """完整 workspace 字典：激活方案 + 可选的第二个非激活方案、冲突表与 revision。"""
    ids = draw(shot_ids(min_shots, max_shots))
    all_versions: dict = {}
    plans_out = {"p1": draw(plans(all_versions, ids))}
    if draw(st.booleans()):
        plans_out["p2"] = draw(plans(all_versions, draw(shot_ids(0, 3))))
    conflicts: dict = {}
    for sid in ids:
        for stage in STAGES:
            if draw(st.integers(0, 4)) == 0:
                conflicts[f"{stage}:{sid}"] = draw(st.lists(st.sampled_from(["content", "tts", "recall"]),
                                                            min_size=1, max_size=2))
    return {"job": {"id": "job-" + draw(hex_str(8))}, "active_plan": "p1", "plans": plans_out,
            "versions": all_versions, "conflicts": conflicts, "revision": "rev-" + draw(hex_str(8))}


@st.composite
def refs(draw, order: list[str], max_size: int = 10) -> list[str]:
    """混合引用：完整 ID、随机长度前缀（随机大小写、可带 shot-）、#N（-2..total+3）与随机字符串。"""

    def one() -> str:
        kinds = ["index", "random"] + (["full", "prefix"] if order else [])
        kind = draw(st.sampled_from(kinds))
        if kind == "full":
            return draw(st.sampled_from(order))
        if kind == "prefix":
            bare = draw(st.sampled_from(order))[5:]
            cut = bare[:draw(st.integers(1, len(bare)))]
            cut = "".join(c.upper() if draw(st.booleans()) else c for c in cut)
            return ("shot-" if draw(st.booleans()) else "") + cut
        if kind == "index":
            return f"#{draw(st.integers(-2, len(order) + 3))}"
        return draw(st.text(min_size=1, max_size=12).filter(lambda s: s.strip()))

    return [one() for _ in range(draw(st.integers(1, max_size)))]


@st.composite
def view_with_plan(draw, **kwargs) -> PlanView:
    """任意 workspace 中任选一个方案构造 PlanView。"""
    w = draw(workspace(**kwargs))
    return PlanView(w, draw(st.sampled_from(sorted(w["plans"]))))


# Feature: agent-storyboard-detail-access, Property 1: 列表详情完整且与工作台数据一致
@settings(max_examples=100, deadline=None)
@given(view=view_with_plan())
def test_page_details_complete_and_consistent(view: PlanView):
    """**Validates: Requirements 1.1, 1.2, 1.3, 1.5, 3.6**"""
    order = view.order
    details = page_details(view, 0, len(order))
    rows = view.shot_rows()

    assert [d["shot_id"] for d in details] == order
    assert [d["index"] for d in details] == list(range(1, len(order) + 1))
    for detail, row, sid in zip(details, rows, order, strict=True):
        assert set(detail) == DETAIL_KEYS
        content = view.content(sid)
        assert detail["text"] == content["text"]
        assert detail["query"] == content["query"]
        assert detail["visual"] == (content.get("visual") or None)
        assert detail["estimated_seconds"] == content["estimated_seconds"]
        assert detail["granularity"] == content.get("granularity")
        assert detail["recall_limit"] == content.get("recall_limit")
        assert detail["recall"] == (content.get("recall") or None)
        assert (detail["generation"] is None) == ((content.get("source") or {}).get("kind") != "aigc")
        assert detail["manual_range"] == view.plan.get("ranges", {}).get(sid)
        assert set(detail["stages"]) == set(STAGES)
        assert detail["short_id"] == row["short_id"]
        assert detail["stages"] == row["stages"]


# Feature: agent-storyboard-detail-access, Property 3: 字幕信息可用性与过期标记
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_subtitle_info_availability_and_stale(data):
    """**Validates: Requirements 3.1, 3.3, 3.5**"""
    sid = "shot-" + data.draw(hex_str(10))
    narration = data.draw(free_text(1, 200))
    item = data.draw(versions("select", sid, narration))
    entry = {"content": "c" * 32}
    all_versions = {"b" * 32: {"stage": "board", "status": "complete", "data": {"order": [sid]}},
                    "c" * 32: {"stage": "content", "shot_id": sid, "status": "complete",
                               "data": {"text": narration, "query": "q", "estimated_seconds": 1.0}}}
    if item is not None:
        all_versions["s" * 32] = item
        entry["select"] = "s" * 32
    w = {"job": {"id": "job-1"}, "active_plan": "p1", "versions": all_versions, "conflicts": {},
         "plans": {"p1": {"name": "p", "board": "b" * 32, "shots": {sid: entry}, "ranges": {}}},
         "revision": "rev-1"}
    view = PlanView(w, "p1")

    info = subtitle_info(view, sid, narration)

    cue = ((item or {}).get("data") or {}).get("event", {}).get("subtitle")
    expected = item is not None and item["status"] == "complete" and cue is not None
    assert info["available"] is expected
    if expected:
        assert info["text"] == cue["text"]
        assert info["start"] == cue["start"]
        assert info["end"] == cue["end"]
        assert info["stale"] is (cue["text"] != narration)
    else:
        assert not {"text", "start", "end"} & set(info)


# Feature: agent-storyboard-detail-access, Property 4: 配音时长可用性
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_speech_info_availability(data):
    """**Validates: Requirements 3.2, 3.4**"""
    sid = "shot-" + data.draw(hex_str(10))
    item = data.draw(versions("tts", sid))
    tts_vid = data.draw(hex_str(32).filter(lambda v: v not in ("b" * 32, "c" * 32)))
    entry = {"content": "c" * 32}
    all_versions = {"b" * 32: {"stage": "board", "status": "complete", "data": {"order": [sid]}},
                    "c" * 32: {"stage": "content", "shot_id": sid, "status": "complete",
                               "data": {"text": "t", "query": "q", "estimated_seconds": 1.0}}}
    if item is not None:
        all_versions[tts_vid] = item
        entry["tts"] = tts_vid
    w = {"job": {"id": "job-1"}, "active_plan": "p1", "versions": all_versions, "conflicts": {},
         "plans": {"p1": {"name": "p", "board": "b" * 32, "shots": {sid: entry}, "ranges": {}}},
         "revision": "rev-1"}
    view = PlanView(w, "p1")

    info = speech_info(view, sid)

    duration = ((item or {}).get("data") or {}).get("duration")
    expected = item is not None and item["status"] == "complete" and isinstance(duration, (int, float))
    assert info["available"] is expected
    if expected:
        assert info["duration"] == duration
        assert info["version"] == short(tts_vid) == tts_vid[:8]
    else:
        assert "duration" not in info


class _WorkspaceClient:
    """只回放固定 workspace 的回环客户端替身：get_shot_details 只读取工作台一次。"""

    def __init__(self, data: dict):
        self.data = data

    async def get(self, path: str, **params):
        assert path == f"/api/studio/jobs/{self.data['job']['id']}/workspace"
        return {"ok": True, "status": 200, "data": self.data}


# Feature: agent-storyboard-detail-access, Property 5: 分页窗口与头部信息
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_list_mode_paging_window_and_header(data):
    """**Validates: Requirements 1.7, 4.6, 7.3, 7.4**"""
    import asyncio

    from mindex.agent.registry import ToolContext
    from mindex.agent.tools.studio_read import ShotDetailsArgs, get_shot_details

    w = data.draw(workspace(max_shots=6))
    plan_id = data.draw(st.sampled_from(sorted(w["plans"])))
    view = PlanView(w, plan_id)
    order = view.order
    offset = data.draw(st.integers(0, len(order) + 3))
    limit = data.draw(st.integers(1, 100))

    args = ShotDetailsArgs(offset=offset, limit=limit, job_id=w["job"]["id"], plan_id=plan_id)
    out = asyncio.run(get_shot_details(args, ToolContext(client=_WorkspaceClient(w))))
    value = out.value

    assert value["mode"] == "list"
    assert value["total"] == len(order)
    assert value["revision"] == view.revision
    assert value["active"] is (plan_id == w["active_plan"])
    ids = [shot["shot_id"] for shot in value["shots"]]
    window = order[offset:offset + limit]
    assert ids == window[:len(ids)]
    assert value["returned"] == len(value["shots"])
    assert value["has_more"] is (offset + value["returned"] < value["total"])
    if offset >= len(order):
        assert value["shots"] == []
        assert value["has_more"] is False


# Feature: agent-storyboard-detail-access, Property 6: 引用解析与 resolve_shot 一致
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_resolve_refs_consistent_with_resolve_shot(data):
    """**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**"""
    import re

    from mindex.agent.ids import resolve_shot
    from mindex.agent.tools.shot_details import resolve_refs

    view = data.draw(view_with_plan(max_shots=5))
    order = view.order
    ref_list = data.draw(refs(order))

    items = resolve_refs(view, ref_list)

    assert len(items) == len(ref_list)
    status_map = {"ok": "ok", "ambiguous": "ambiguous", "missing": "not_found"}
    for item, ref in zip(items, ref_list, strict=True):
        assert item["ref"] == ref
        found = resolve_shot(order, ref)
        assert item["status"] == status_map[found["status"]]
        if item["status"] == "ok":
            assert item["index"] == found["index"]
            assert item["shot_id"] == found["shot_id"]
            assert item["detail"]["shot_id"] == found["shot_id"]
            assert item["detail"]["index"] == found["index"]
        elif item["status"] == "ambiguous":
            assert "detail" not in item
            got = {(c["shot_id"], c["index"]) for c in item["candidates"]}
            assert got == {(c["shot_id"], c["index"]) for c in found["candidates"]}
        else:
            assert "detail" not in item
            match = re.fullmatch(r"#(\d{1,4})", ref.strip())
            if match and not 1 <= int(match.group(1)) <= len(order):
                assert str(len(order)) in item["reason"]


# Feature: agent-storyboard-detail-access, Property 7: 按引用读取与按列表读取结果相同
@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_ref_details_equal_list_details(data):
    """**Validates: Requirements 2.6**"""
    from mindex.agent.tools.shot_details import resolve_refs

    view = data.draw(view_with_plan(max_shots=5))
    order = view.order
    ref_list = data.draw(refs(order))

    listed = page_details(view, 0, len(order))
    items = resolve_refs(view, ref_list)

    by_shot: dict = {}
    for item in items:
        if item["status"] != "ok":
            continue
        assert item["detail"] == listed[item["index"] - 1]
        by_shot.setdefault(item["shot_id"], []).append(item["detail"])
    for same in by_shot.values():
        assert all(d == same[0] for d in same)


# Feature: agent-storyboard-detail-access, Property 9: 页面定位
# 本属性有意生成大方案与长引用列表（越过 20 个分镜的定位上限），数据量大是设计使然。
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow])
@given(data=st.data())
def test_details_focus_page_location(data):
    """**Validates: Requirements 7.1, 7.2**"""
    from mindex.agent.tools.shot_details import details_focus, resolve_refs

    # 大方案（最多 25 个分镜）+ 长引用列表（最多 50 条），可越过 20 个分镜的定位上限
    view = data.draw(st.one_of(view_with_plan(max_shots=5), view_with_plan(min_shots=18, max_shots=25)))
    order = view.order
    ref_list = data.draw(refs(order, max_size=50))
    if order and data.draw(st.booleans()):
        # 追加完整 ID（含重复），确保去重与上限都被覆盖
        ref_list += data.draw(st.lists(st.sampled_from(order), min_size=1, max_size=30))
    items = resolve_refs(view, ref_list)

    base = {"page": "studio", "job_id": view.job["id"], "plan_id": view.plan_id}

    listed = details_focus(view, None)
    assert listed == {**base, "section": "storyboard"}

    focus = details_focus(view, items)
    assert {k: focus[k] for k in base} == base
    ok_ids: list[str] = []
    for item in items:
        if item["status"] == "ok" and item["shot_id"] not in ok_ids:
            ok_ids.append(item["shot_id"])
    if ok_ids:
        assert focus["shots"] == ok_ids[:20]
        assert len(focus["shots"]) <= 20
        assert "section" not in focus
    else:
        assert focus["section"] == "storyboard"
        assert "shots" not in focus


# Feature: agent-storyboard-detail-access, Property 2: 渲染文本逐字保留并完整呈现各字段
# 每个分镜都携带多段长自由文本，最小样例天然较大，故关闭 large_base_example 健康检查
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.large_base_example])
@given(view=view_with_plan(min_shots=1, max_shots=3))
def test_render_detail_verbatim_and_complete(view: PlanView):
    """**Validates: Requirements 1.3, 1.4, 3.7**"""
    import json

    from mindex.agent.tools.shot_details import render_detail

    def compact(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    for detail in page_details(view, 0, len(view.order)):
        out = render_detail(detail)

        assert f"「{detail['text']}」" in out
        assert f"「{detail['query']}」" in out
        assert f"画面意象：「{detail['visual']}」" in out if detail["visual"] else "画面意象：（无）" in out
        assert repr(detail["estimated_seconds"]) in out
        if detail["granularity"] is None:
            assert "召回粒度：（无）" in out
        else:
            assert f"召回粒度：{detail['granularity']}" in out
        if detail["recall_limit"] is None:
            assert "召回数量：（无）" in out
        else:
            assert f"召回数量：{detail['recall_limit']!r}" in out
        if detail["recall"]:
            assert f"召回设置：{compact(detail['recall'])}" in out
        else:
            assert "召回设置：（无）" in out
        if detail["manual_range"]:
            assert f"人工区间：{compact(detail['manual_range'])}" in out
        else:
            assert "人工区间：（无）" in out

        stages = detail["stages"]
        sub = detail["subtitle"]
        if sub["available"]:
            assert f"「{sub['text']}」" in out
            assert repr(sub["start"]) in out and repr(sub["end"]) in out
            if sub["stale"]:
                assert "字幕已过期" in out
        else:
            assert "字幕：缺失" in out
            assert f"选片 {stages['select'].get('status', 'missing')}" in out
        speech = detail["speech"]
        if speech["available"]:
            assert f"配音时长：{speech['duration']!r} 秒" in out
        else:
            assert "配音时长：缺失" in out
            assert f"配音 {stages['tts'].get('status', 'missing')}" in out


# 渲染预算测试用的轻量文本：不含 "#"、"=" 与换行，保证分镜标题与 offset= 提示只来自渲染结构
BUDGET_ALPHABET = "镜头旁白查询画面意象城市夜景海浪山川abcXYZ0123456789，。！「」 \u3000😀🎬…"


@st.composite
def budget_workspace(draw) -> dict:
    """1–100 个分镜、文本接近长度上限（旁白 200、查询 400、画面意象 120）的 workspace，程序化组装。"""
    count = draw(st.integers(1, 100))
    seeds = draw(st.lists(st.text(BUDGET_ALPHABET, min_size=1, max_size=20), min_size=1, max_size=4))
    near_max = draw(st.booleans())

    def text_of(i: int, max_len: int) -> str:
        seed = seeds[i % len(seeds)]
        length = max_len - (i % 7 if near_max else (i * 37) % max_len)
        return (seed * (max_len // len(seed) + 1))[:max(length, 1)]

    order = [f"shot-{i:010x}" for i in range(count)]
    versions_out: dict = {"b" * 32: {"stage": "board", "status": "complete", "data": {"order": order}}}
    shots: dict = {}
    for i, sid in enumerate(order):
        cid = f"{i:032x}"
        body = {"text": text_of(i, 200), "query": text_of(i + 1, 400), "visual": text_of(i + 2, 120),
                "estimated_seconds": 1.5 + i}
        versions_out[cid] = {"stage": "content", "shot_id": sid, "status": "complete", "data": body}
        entry = {"content": cid}
        if i % 2 == 0:
            tid = f"{i:031x}t"
            versions_out[tid] = {"stage": "tts", "shot_id": sid, "status": "complete",
                                 "data": {"duration": 2.25 + i}}
            entry["tts"] = tid
        if i % 3 == 0:
            sel = f"{i:031x}s"
            cue_text = body["text"] if i % 2 else text_of(i + 3, 200)
            versions_out[sel] = {"stage": "select", "shot_id": sid, "status": "complete",
                                 "data": {"event": {"start": 0.0,
                                                    "subtitle": {"text": cue_text, "start": 0.5, "end": 3.5}}}}
            entry["select"] = sel
        shots[sid] = entry
    plan = {"name": "预算方案", "board": "b" * 32, "shots": shots, "ranges": {}}
    return {"job": {"id": "job-budget01"}, "active_plan": "p1", "plans": {"p1": plan},
            "versions": versions_out, "conflicts": {}, "revision": "rev-budget01"}


# Feature: agent-storyboard-detail-access, Property 10: 渲染预算与结构化结果一一对应
# 每个样例最多 100 个接近长度上限的分镜，最小样例天然较大，故关闭 large_base_example / too_slow 健康检查
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.large_base_example, HealthCheck.too_slow])
@given(data=st.data())
def test_list_render_budget_matches_structured(data):
    """**Validates: Requirements 7.6, 7.7, 1.4**"""
    import asyncio
    import re

    from mindex.agent.registry import MAX_TEXT, ToolContext
    from mindex.agent.tools.studio_read import ShotDetailsArgs, get_shot_details

    w = data.draw(budget_workspace())
    total = len(w["plans"]["p1"]["shots"])
    offset = data.draw(st.integers(0, total - 1))
    limit = data.draw(st.integers(1, 100))

    args = ShotDetailsArgs(offset=offset, limit=limit, job_id=w["job"]["id"], plan_id="p1")
    out = asyncio.run(get_shot_details(args, ToolContext(client=_WorkspaceClient(w))))
    value, text = out.value, out.text

    assert len(text) <= MAX_TEXT
    assert value["returned"] >= 1  # 窗口非空时至少输出一个分镜
    assert value["returned"] == len(value["shots"])
    assert len(re.findall(r"^## #", text, flags=re.MULTILINE)) == value["returned"]
    for shot in value["shots"]:
        assert shot["text"] in text
        assert shot["query"] in text
        assert shot["visual"] in text
    if value["has_more"]:
        assert value["next_offset"] == offset + value["returned"]
        assert f"offset={value['next_offset']}" in text
    else:
        assert value["next_offset"] is None
        assert offset + value["returned"] == total


# ---------------------------------------------------------------- 参数校验

BLANK = st.text(alphabet=" \t\n\u3000", max_size=4)
GOOD_REF = st.text(min_size=1, max_size=12).filter(lambda s: s.strip())
GOOD_OFFSET = st.integers(min_value=0, max_value=10_000)
GOOD_LIMIT = st.integers(min_value=1, max_value=100)
NON_INT = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False).filter(lambda x: x != int(x)),
    st.booleans(), st.text(max_size=5),
)
BAD_OFFSET = st.one_of(st.integers(max_value=-1), NON_INT)
BAD_LIMIT = st.one_of(st.integers(max_value=0), st.integers(min_value=101), NON_INT)


@st.composite
def illegal_shot_args(draw) -> dict:
    kind = draw(st.sampled_from(["refs_empty", "refs_long", "refs_blank", "offset", "limit", "mixed"]))
    if kind == "refs_empty":
        return {"refs": []}
    if kind == "refs_long":
        return {"refs": draw(st.lists(GOOD_REF, min_size=51, max_size=60))}
    if kind == "refs_blank":
        items = draw(st.lists(GOOD_REF, max_size=5))
        position = draw(st.integers(min_value=0, max_value=len(items)))
        return {"refs": items[:position] + [draw(BLANK)] + items[position:]}
    if kind == "offset":
        args = {"offset": draw(BAD_OFFSET)}
        if draw(st.booleans()):
            args["limit"] = draw(GOOD_LIMIT)
        return args
    if kind == "limit":
        args = {"limit": draw(BAD_LIMIT)}
        if draw(st.booleans()):
            args["offset"] = draw(GOOD_OFFSET)
        return args
    args = {"refs": draw(st.lists(GOOD_REF, min_size=1, max_size=5))}
    which = draw(st.sampled_from(["offset", "limit", "both"]))
    if which in ("offset", "both"):
        args["offset"] = draw(GOOD_OFFSET)
    if which in ("limit", "both"):
        args["limit"] = draw(GOOD_LIMIT)
    return args


@st.composite
def legal_shot_args(draw) -> dict:
    if draw(st.booleans()):
        return {"refs": draw(st.lists(GOOD_REF, min_size=1, max_size=50))}
    args = {}
    if draw(st.booleans()):
        args["offset"] = draw(GOOD_OFFSET)
    if draw(st.booleans()):
        args["limit"] = draw(GOOD_LIMIT)
    return args


# Feature: agent-storyboard-detail-access, Property 8: 非法参数一律拒绝
# **Validates: Requirements 2.7, 7.5**
@settings(max_examples=100, deadline=None)
@given(bad=illegal_shot_args(), good=legal_shot_args())
def test_invalid_shot_detail_arguments_rejected(bad: dict, good: dict):
    spec = build_registry().get("get_shot_details")
    with pytest.raises(ToolError) as info:
        spec.parse(bad)
    assert info.value.code == "invalid_arguments"

    parsed = spec.parse(good)
    assert parsed.refs == good.get("refs")
    assert parsed.offset == good.get("offset")
    assert parsed.limit == good.get("limit")


# ---------------------------------------------------------------- 示例与集成测试（真实应用 + 进程内回环）

BOARD_SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-925281d6cc"]
BOARD_TEXTS = ["红色开场。", "蓝色中段。", "绿色结尾。"]
SUBTITLE = {"text": "红色开场。", "start": 0.0, "end": 1.5}
DURATION = 1.52
OTHER_PLAN_NAME = "备选方案"


def _shots(texts):
    return [studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.5)
            for sid, text in zip(BOARD_SHOTS, texts)]


@pytest.fixture
def board(config):
    """三个分镜（#2/#3 共享前缀 925281d）；#1 带完整选片字幕与配音时长，#2 配音失败；另有一个非激活方案。"""
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, _shots(BOARD_TEXTS))
    p["board_script"] = p["script"]
    first, second = BOARD_SHOTS[0], BOARD_SHOTS[1]
    p["shots"][first]["tts"] = ws.add_version(config, job_id, w, "tts", first, data={"duration": DURATION})
    p["shots"][first]["select"] = ws.add_version(
        config, job_id, w, "select", first, data={"event": {"start": 0.0, "subtitle": dict(SUBTITLE)}})
    p["shots"][second]["tts"] = ws.add_version(config, job_id, w, "tts", second, status="failed",
                                               error="配音失败")
    other = copy.deepcopy(p)
    other.update(id=uuid4().hex[:12], name=OTHER_PLAN_NAME, created_at=studio.now())
    ws.set_board(config, job_id, w, other, _shots(["备选一。", "备选二。"]))
    w["plans"][other["id"]] = other
    ws.persist(config, job_id, w)
    return job_id


@pytest.fixture
def app(config):
    return create_app(config)


def run_detail(app, arguments, page=None, client_cls=MindexClient):
    spec = build_registry().get("get_shot_details")

    async def go():
        client = client_cls(app)
        try:
            return await spec.execute(spec.parse(arguments), ToolContext(client=client, page=page or {}))
        finally:
            await client.aclose()

    return asyncio.run(go())


def workspace_state(app, job_id):
    data = TestClient(app).get(f"/api/studio/jobs/{job_id}/workspace").json()
    p = data["plans"][data["active_plan"]]
    order = data["versions"][p["board"]]["data"]["order"]
    return data["revision"], len(order), {sid: dict(p["shots"][sid]) for sid in order}


def other_plan_id(app, job_id):
    data = TestClient(app).get(f"/api/studio/jobs/{job_id}/workspace").json()
    return next(pid for pid, p in data["plans"].items() if p["name"] == OTHER_PLAN_NAME), data["active_plan"]


class RecordingClient(MindexClient):
    """真实回环客户端，额外记录每次请求的方法与路径。"""

    calls: ClassVar[list] = []

    async def request(self, method, path, **kwargs):
        RecordingClient.calls.append((method, path))
        return await super().request(method, path, **kwargs)


def test_list_mode_reads_all_shots(app, board):
    """Validates: Requirements 1.1, 1.2, 1.7"""
    out = run_detail(app, {"job_id": board})
    value, text = out.value, out.render()
    assert value["mode"] == "list" and value["total"] == 3 and value["returned"] == 3
    assert value["has_more"] is False and value["active"] is True
    assert [s["shot_id"] for s in value["shots"]] == BOARD_SHOTS
    assert [s["text"] for s in value["shots"]] == BOARD_TEXTS
    assert value["revision"] == TestClient(app).get(f"/api/studio/jobs/{board}/workspace").json()["revision"]
    for i, sid in enumerate(BOARD_SHOTS, 1):
        assert f"## #{i} {short(sid)}（完整ID {sid}）" in text
    assert out.focus == {"page": "studio", "job_id": board, "plan_id": value["plan_id"], "section": "storyboard"}


def test_refs_mode_ok_ambiguous_not_found(app, board):
    """Validates: Requirements 2.5"""
    out = run_detail(app, {"job_id": board, "refs": ["#2", "a1daad59", "925281d", "#9"]})
    value, text = out.value, out.render()
    assert [r["status"] for r in value["results"]] == ["ok", "ok", "ambiguous", "not_found"]
    assert value["returned"] == 2
    ok2, ok1, amb, missing = value["results"]
    assert ok2["shot_id"] == BOARD_SHOTS[1] and ok2["detail"]["text"] == "蓝色中段。"
    assert ok1["shot_id"] == BOARD_SHOTS[0] and ok1["detail"]["index"] == 1
    assert {c["shot_id"] for c in amb["candidates"]} == set(BOARD_SHOTS[1:]) and "detail" not in amb
    assert "detail" not in missing and "3" in missing["reason"]
    assert "请向用户确认" in text and "#9 → 未找到" in text
    assert out.focus["shots"] == [BOARD_SHOTS[1], BOARD_SHOTS[0]]

    failed = run_detail(app, {"job_id": board, "refs": ["#9", "ffff"]})
    assert failed.value["returned"] == 0
    assert [r["status"] for r in failed.value["results"]] == ["not_found", "not_found"]
    assert failed.focus["section"] == "storyboard"


def test_subtitle_and_speech_values(app, config, board):
    """Validates: Requirements 3.1, 3.2, 3.5"""
    out = run_detail(app, {"job_id": board, "refs": ["#1", "#2"]})
    first, second = (r["detail"] for r in out.value["results"])
    text = out.render()
    assert first["subtitle"] == {"available": True, **SUBTITLE, "stale": False}
    assert first["speech"]["available"] is True and first["speech"]["duration"] == DURATION
    tts_vid = TestClient(app).get(f"/api/studio/jobs/{board}/workspace").json()
    tts_vid = tts_vid["plans"][tts_vid["active_plan"]]["shots"][BOARD_SHOTS[0]]["tts"]
    assert first["speech"]["version"] == short(tts_vid)
    assert f"字幕：「{SUBTITLE['text']}」 {SUBTITLE['start']!r}–{SUBTITLE['end']!r} 秒" in text
    assert f"配音时长：{DURATION!r} 秒（版本 {short(tts_vid)}）" in text
    assert "字幕已过期" not in text
    assert second["subtitle"] == {"available": False} and second["speech"] == {"available": False}
    assert second["stages"]["tts"]["status"] == "failed"
    assert "配音时长：缺失（配音 failed" in text and "字幕：缺失（选片 missing）" in text

    # 修改旁白后，字幕仍是按旧旁白生成的：标记过期
    w = ws.load(config, board)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, board, w, p, _shots(["红色开场，已改写。", *BOARD_TEXTS[1:]]))
    ws.persist(config, board, w)
    stale = run_detail(app, {"job_id": board, "refs": ["#1"]})
    detail = stale.value["results"][0]["detail"]
    assert detail["text"] == "红色开场，已改写。"
    assert detail["subtitle"]["stale"] is True and detail["subtitle"]["text"] == SUBTITLE["text"]
    assert "字幕已过期" in stale.render()


def test_job_and_plan_location(app, board):
    """Validates: Requirements 4.1, 4.2, 4.4, 4.5"""
    other, active = other_plan_id(app, board)

    # 页面同任务且含 plan_id → 使用页面方案
    same = run_detail(app, {}, page={"page": "studio", "job_id": board, "plan_id": other})
    assert same.value["job_id"] == board and same.value["plan_id"] == other
    assert same.value["active"] is False and same.value["total"] == 2
    assert [s["text"] for s in same.value["shots"]] == ["备选一。", "备选二。"]

    # 页面是其他任务 → 使用目标任务的激活方案（不沿用页面 plan_id）
    elsewhere = run_detail(app, {"job_id": board}, page={"page": "studio", "job_id": "other-job", "plan_id": other})
    assert elsewhere.value["plan_id"] == active and elsewhere.value["active"] is True

    # 页面无 plan → 激活方案
    no_plan = run_detail(app, {}, page={"page": "studio", "job_id": board})
    assert no_plan.value["plan_id"] == active and no_plan.value["total"] == 3

    # 显式 plan_id 优先
    explicit = run_detail(app, {"job_id": board, "plan_id": other})
    assert explicit.value["plan_id"] == other

    with pytest.raises(ToolError) as info:
        run_detail(app, {"job_id": board, "plan_id": "nope"})
    assert info.value.code == "missing_plan"
    message = str(info.value)
    assert "nope" in message and f"{OTHER_PLAN_NAME}({other})" in message and active in message

    with pytest.raises(ToolError) as info:
        run_detail(app, {"job_id": "20990101-000000-abcdef"})
    assert info.value.code == "http_404" and "HTTP 404" in str(info.value)


def test_request_counts(app, board):
    """Validates: Requirements 4.3, 5.5"""
    RecordingClient.calls = []
    with pytest.raises(ToolError) as info:
        run_detail(app, {}, page={"page": "studio"}, client_cls=RecordingClient)
    assert info.value.code == "missing_job" and "list_jobs" in str(info.value)
    assert RecordingClient.calls == []

    for bad in ({"job_id": board, "refs": []}, {"job_id": board, "refs": ["#1"], "offset": 0},
                {"job_id": board, "limit": 0}):
        with pytest.raises(ToolError) as info:
            run_detail(app, bad, client_cls=RecordingClient)
        assert info.value.code == "invalid_arguments"
    assert RecordingClient.calls == []

    run_detail(app, {"job_id": board}, client_cls=RecordingClient)
    run_detail(app, {"job_id": board, "refs": ["#1"]}, client_cls=RecordingClient)
    assert RecordingClient.calls == [("GET", f"/api/studio/jobs/{board}/workspace")] * 2


def test_read_only_invariance(app, board):
    """Validates: Requirements 4.7, 5.6"""
    before = workspace_state(app, board)
    for arguments in ({"job_id": board}, {"job_id": board, "refs": ["#1", "925281d", "#9"]},
                      {"job_id": board, "offset": 1, "limit": 1}):
        out = run_detail(app, arguments)
        assert out.mutation is None
    for arguments in ({"job_id": board, "plan_id": "nope"}, {"job_id": "20990101-000000-abcdef"}):
        with pytest.raises(ToolError):
            run_detail(app, arguments)
    assert workspace_state(app, board) == before


def test_runtime_executes_without_approval(config, board):
    """Validates: Requirements 5.2"""
    llm = ScriptedLlm([[call("get_shot_details", call_id="d1", refs=["#1"])], [say("已读取")]])
    with TestClient(create_app(config, agent_llm=llm)) as client:
        sid = new_session(client)
        send(client, sid, "看看 #1 的旁白", context={"page": "studio", "job_id": board})
        events = wait_idle(client, sid)
    types = [e["type"] for e in events]
    assert "plugin:mindex/approval-asked" not in types
    assert "plugin:mindex/mutation" not in types
    text = result_text(results(events)[0])
    assert "旁白：「红色开场。」" in text and BOARD_SHOTS[0] in text


@st.composite
def split_case(draw) -> tuple[PlanView, str, str, list[str]]:
    """1–4 个分镜的最小 workspace，其中一个为拆分目标；目标旁白长度 L ≥ 2，取 1..L−1 个不同切点。

    旁白使用 free_text（含空白、标点与 emoji）；ShotFields 不 strip 旁白，切出的各段均非空，
    因此按顺序拼接严格等于原旁白（不只是忽略空白意义下相等）。内容 data 与真实 workspace 一样带 ``id``。
    """
    ids = draw(shot_ids(1, 4))
    target = draw(st.sampled_from(ids))
    narration = draw(free_text(2, 200))
    cuts = sorted(draw(st.sets(st.integers(1, len(narration) - 1), min_size=1, max_size=len(narration) - 1)))
    bounds = [0, *cuts, len(narration)]
    segments = [narration[a:b] for a, b in itertools.pairwise(bounds)]
    versions_out: dict = {"board": {"stage": "board", "status": "complete", "data": {"order": list(ids)}}}
    shots: dict = {}
    for n, sid in enumerate(ids):
        body = {"id": sid, "text": narration if sid == target else f"第{n}镜旁白。", "query": f"查询{n}",
                "estimated_seconds": 3.0}
        if draw(st.booleans()):
            body["visual"] = f"画面{n}"
        versions_out[f"c{n}"] = {"stage": "content", "shot_id": sid, "status": "complete", "data": body}
        shots[sid] = {"content": f"c{n}"}
    w = {"job": {"id": "job-1"}, "active_plan": "p1", "versions": versions_out, "conflicts": {},
         "plans": {"p1": {"name": "方案", "board": "board", "shots": shots, "ranges": {}}}, "revision": "rev-1"}
    return PlanView(w, "p1"), target, narration, segments


# Feature: agent-storyboard-detail-access, Property 11: 按完整旁白切分的 split 往返
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(split_case())
def test_split_by_full_narration_round_trip(case):
    """**Validates: Requirements 6.4**"""
    from mindex.agent.tools.studio_write import ShotFields, ShotOperation, plan_shots

    view, target, narration, segments = case
    assert all(segments) and "".join(segments) == narration
    before = view.shots()
    index = view.order.index(target)

    op = ShotOperation(op="split", shot=target, parts=[ShotFields(text=s) for s in segments])
    shots, _changes, _touched = plan_shots(view, [op])

    assert len(shots) == len(before) + len(segments) - 1
    new = shots[index:index + len(segments)]
    assert [s["text"] for s in new] == segments
    assert "".join(s["text"] for s in new) == narration
    assert new[0]["id"] == target and all("id" not in s for s in new[1:])
    # 其他分镜不变（顺序与内容）
    assert shots[:index] == before[:index]
    assert shots[index + len(segments):] == before[index + 1:]
    assert view.shots() == before  # 不修改输入


def squash(text: str) -> str:
    """与 studio_write.plan_shots 的 split 校验一致：按 str.split() 去除全部空白后拼接。"""
    return "".join(text.split())


@st.composite
def mismatched_split_case(draw) -> tuple[PlanView, str, list[str]]:
    """在 split_case 的基础上构造 ≥2 段、拼接后（忽略空白）≠ 原旁白的 parts。

    扰动方式：任意自由文本、删去一段、重复一段、在某段插入一个非空白字符；最终用 filter 保证确实不一致。
    """
    view, target, narration, segments = draw(split_case())
    non_space = st.characters(blacklist_categories=("Cs", "Zs", "Zl", "Zp", "Cc"))

    def perturb(kind: str) -> list[str]:
        parts = list(segments)
        if kind == "free":
            return draw(st.lists(free_text(1, 200), min_size=2, max_size=5))
        if kind == "drop" and len(parts) >= 3:
            parts.pop(draw(st.integers(0, len(parts) - 1)))
        elif kind == "dup":
            i = draw(st.integers(0, len(parts) - 1))
            parts.insert(i, parts[i])
        else:  # insert：各段长度 < 原旁白 ≤ 200，插入一个字符后仍 ≤ 200
            i = draw(st.integers(0, len(parts) - 1))
            pos = draw(st.integers(0, len(parts[i])))
            parts[i] = parts[i][:pos] + draw(non_space) + parts[i][pos:]
        return parts

    parts = draw(st.sampled_from(["free", "drop", "dup", "insert"]).map(perturb)
                 .filter(lambda p: len(p) >= 2 and squash("".join(p)) != squash(narration)))
    return view, target, parts


# Feature: agent-storyboard-detail-access, Property 12: 旁白不一致的 split 被拒绝
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
@given(mismatched_split_case())
def test_split_with_mismatched_narration_rejected(case):
    """**Validates: Requirements 6.5**"""
    from mindex.agent.tools.studio_write import ShotFields, ShotOperation, plan_shots

    view, target, parts = case
    before = copy.deepcopy(view.w)
    op = ShotOperation(op="split", shot=target, parts=[ShotFields(text=p) for p in parts])
    with pytest.raises(ToolError) as info:
        plan_shots(view, [op])
    assert info.value.code == "invalid_operation"
    assert view.w == before  # 不修改输入
