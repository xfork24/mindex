"""版本签名与构建产物快照：冻结工作台的签名、预检与成片时间线结构。

重构工作台（拆包、阶段注册表）不得改变既有版本签名，也不得改变固定 fixture 的构建产物，
否则历史版本无法复用。快照存于 ``tests/fixtures/studio_signatures.json``，由重构前的代码生成：

    MINDEX_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_studio_signature_snapshot.py -q

只有确认签名变化是有意为之（例如递增提示词版本）时才重新生成。

确定性：版本号与方案号来自 ``uuid4``，测试期间替换为递增计数；``studio.now`` 固定；
分镜媒体文件的 mtime 固定（``ShotMedia.identity`` 含 mtime）；任务目录、data_dir 等
绝对路径不进入签名（``portable_parameters`` 相对化）。

平台绑定：fixture 素材由平台 ffmpeg 编码，其内容哈希（source_id → segment/索引指纹 →
recall 参数）与切片文件体积（``ShotMedia.identity`` → render 签名）随 ffmpeg 构建变化，
逐字节比对只在生成快照的基线平台（darwin）成立。其他平台跳过这些环境绑定键、把内容
寻址 id 规范化为占位符后比较其余内容，结构与稳定值的回归仍会被捕获。
"""

from __future__ import annotations

import itertools
import json
import os
import re
import sys
import uuid
from pathlib import Path

import pytest
from conftest import FixtureEmbedder, FixtureSpeaker
from test_studio_workspace import build, setup_workspace

from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.storage import Store
from mindex.shared.types import Filters
from mindex.studio import studio
from mindex.studio import workspace as ws

pytestmark = pytest.mark.slow  # 需要真实切片与索引：CI 快速回归跳过，每晚完整运行

SNAPSHOT = Path(__file__).parent / "fixtures" / "studio_signatures.json"
UPDATE = os.environ.get("MINDEX_UPDATE_SNAPSHOTS") == "1"
FIXED_MTIME = 1_700_000_000
VERSION_KEYS = {"anchor_ref", "tts_version", "recall_version"}
BASELINE_PLATFORM = "darwin"
# 环境绑定键：recall 参数含活动索引 index_id（素材清单指纹）；render payload 含
# ShotMedia.identity（切片体积）；select:b/c 的人工区间与锚点含 source/segment 哈希。
ENV_BOUND_KEYS = re.compile(r"^(recall|render|select:[bc])")
HEX_ID = re.compile(r"^[0-9a-f]{20,64}$")


def normalized_ids(value):
    """内容寻址 id（source/segment/resource 哈希）→ 按首次出现顺序的占位符。"""
    mapping = {}

    def walk(item):
        if isinstance(item, dict):
            return {key: walk(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [walk(entry) for entry in item]
        if isinstance(item, str) and HEX_ID.match(item):
            if item not in mapping:
                mapping[item] = f"<id{len(mapping)}>"
            return mapping[item]
        return item

    return walk(value)


def check_snapshot(name, value):
    value = json.loads(json.dumps(value, ensure_ascii=False))
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.is_file() else {}
    if UPDATE:
        data[name] = value
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        return
    assert name in data, f"快照 {name} 缺失：用 MINDEX_UPDATE_SNAPSHOTS=1 在重构前的代码上生成"
    expected = data[name]
    if sys.platform != BASELINE_PLATFORM:
        if name == "signatures":
            value = {key: entry for key, entry in value.items() if not ENV_BOUND_KEYS.match(key)}
            expected = {key: entry for key, entry in expected.items() if not ENV_BOUND_KEYS.match(key)}
            # preflight 的 revision/token 是覆盖方案内容的指纹（含内容寻址 id，跨平台必然不同）；
            # 非基线平台比较其余全部字段（含逐镜 version id——它们跨平台一致）。
            for entries in (value, expected):
                for key, entry in entries.items():
                    if isinstance(entry, dict):
                        entries[key] = {k: v for k, v in entry.items() if k not in ("revision", "token")}
        else:
            value, expected = normalized_ids(value), normalized_ids(expected)
        assert value == expected, f"快照 {name} 在非基线平台的规范化比对不一致"
        return
    if isinstance(value, dict):
        assert sorted(value) == sorted(expected)
        for key in value:
            assert value[key] == expected[key], key
    assert value == expected


@pytest.fixture
def deterministic(monkeypatch):
    """uuid4 换成递增计数（覆盖所有已导入的 mindex 模块），并固定时间戳。"""
    counter = itertools.count(1)

    def fake_uuid4():
        return uuid.UUID(int=next(counter))

    for name, module in list(sys.modules.items()):
        if name.startswith("mindex") and getattr(module, "uuid4", None) is uuid.uuid4:
            monkeypatch.setattr(module, "uuid4", fake_uuid4)
    monkeypatch.setattr(studio, "now", lambda: "2026-01-01T00:00:00")


@pytest.fixture
def prepared(config, video_file):
    # 快照生成时的缺省请求超时；超时进入阶段参数，固定为当时的值才能逐字节比对签名。
    # 调整缺省超时不影响历史版本复用（portable_parameters 按当前值看待 timeout，见下方测试）。
    for service in (config.studio.generator, config.studio.writer, config.studio.tts):
        service.timeout = 300
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 29.97
    Pipeline(config).prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    for directory, _, names in os.walk(config.data_dir):
        for name in names:
            if not name.startswith("metadata.sqlite3"):
                os.utime(Path(directory) / name, (FIXED_MTIME, FIXED_MTIME))
    return config, embedder


def test_stage_signatures_and_preflight_match_snapshot(prepared, deterministic):
    config, _ = prepared
    store = Store(config.data_dir)
    source_id = store.one("SELECT id FROM sources")["id"]
    segments = store.rows(
        "SELECT id,start,end FROM segments WHERE source_id=? AND kind='shot' ORDER BY start", (source_id,)
    )
    jid = studio.create_job(config, "从红色画面讲到蓝色天空。", options={"burn": False})["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    result = {}

    def record(key, stage, sid=None):
        spec = ws.specification(config, jid, w, p, stage, sid)
        result[key] = spec["signature"]
        return spec

    def select_version(stage, sid, spec, **kwargs):
        p["shots"][sid][stage] = ws.add_version(config, jid, w, stage, sid, **spec, **kwargs)

    record("script", "script")
    ws.add_script(config, jid, w, p, "红色首先出现。蓝色天空随后展开。绿色收尾。", "manual")
    record("split", "split")
    ws.set_board(config, jid, w, p, [
        studio.Shot(id="a", text="红色首先出现。", query="红色画面", estimated_seconds=1),
        studio.Shot(
            id="b", text="蓝色天空随后展开。", query="蓝色天空", estimated_seconds=2,
            granularity="shot", recall_limit=5,
            recall=studio.ShotRecall(filters=Filters(min_duration=0.5, source_ids=[source_id])),
        ),
        studio.Shot(id="c", text="绿色收尾。", query="绿色", estimated_seconds=1),
    ])
    p["board_script"] = p["script"]
    for sid in "abc":
        record(f"recall:{sid}", "recall", sid)
    # 任务素材范围进入召回参数（sources），只在有限制时加键。
    state = studio.load_job(config, jid)
    studio.save(studio.job_path(config, jid), {**state, "source_ids": [source_id]})
    record("recall:a:scoped", "recall", "a")
    studio.save(studio.job_path(config, jid), state)

    for sid, segment in zip("abc", segments, strict=False):
        candidate = {"segment_id": segment["id"], "source_id": source_id,
                     "start": segment["start"], "end": segment["end"]}
        select_version("recall", sid, ws.specification(config, jid, w, p, "recall", sid),
                       data={"results": [candidate] if sid == "c" else []})
        select_version("tts", sid, record(f"tts:{sid}", "tts", sid))
    # a 自动选片；b 人工区间（不依赖召回）；c 人工锚点。
    p["ranges"]["b"] = {"source_id": source_id, "start": segments[1]["start"], "end": segments[1]["end"],
                        "segment_ids": [segments[1]["id"]]}
    p["anchors"]["c"] = {"mode": "manual", "recall_version": p["shots"]["c"]["recall"],
                         "candidate_id": segments[2]["id"]}
    for sid, segment in zip("abc", segments, strict=False):
        event = {"video": {"source_id": source_id, "start": segment["start"], "end": segment["end"]}}
        select_version("select", sid, record(f"select:{sid}", "select", sid), data={"event": event})
    for sid in "abc":
        select_version("render", sid, record(f"render:{sid}", "render", sid))
    record("compose", "compose")
    result["preflight"] = ws.preflight(config, jid, w, p)
    result["preflight:force"] = ws.preflight(config, jid, w, p, force=True)
    config.studio.tts.voice = "另一个声音"
    result["conflicts"] = {
        f"{stage}:{sid}": ws.conflicts(config, jid, w, p, stage, sid, p["shots"][sid][stage])
        for sid in "abc" for stage in ws.STAGES
    }
    check_snapshot("signatures", result)


def structure(config, jid, w, p):
    """成片时间线的结构内容：去掉随机版本号、绝对路径与时间戳。"""
    final = ws.version(w, p["final"])
    timeline = json.loads(ws.file_path(config, jid, final, "timeline").read_text(encoding="utf-8"))
    graph, plan_snapshot = timeline["graph"], timeline["plan"]
    order = graph[plan_snapshot["board"]]["data"]["order"]

    def event(value):
        return {**value, "audio": {k: v for k, v in value["audio"].items() if k != "file"}}

    def portable(value):
        """去掉引用版本号的字段（版本号由 uuid4 生成），候选只保留身份与区间。"""
        if isinstance(value, dict):
            return {k: portable(v) for k, v in value.items() if k not in VERSION_KEYS}
        if isinstance(value, list):
            return [portable(v) for v in value]
        return value

    shots = []
    for sid in order:
        render = graph[plan_snapshot["shots"][sid]["render"]]
        select = graph[render["inputs"]["select"]]
        tts = graph[render["inputs"]["tts"]]
        recall = graph.get(select["inputs"].get("recall") or "")
        shots.append({
            "shot_id": sid,
            "content": graph[render["inputs"]["content"]]["data"],
            "recall": [c["segment_id"] for c in recall["data"]["results"]] if recall else None,
            "tts": tts["data"],
            "select": {
                **portable({k: v for k, v in select["data"].items() if k not in ("event", "anchor")}),
                "anchor": {k: select["data"]["anchor"].get(k) for k in ("segment_id", "source_id", "start",
                                                                        "end", "rank")},
                "event": event(select["data"]["event"]),
            },
            "render": {
                **{k: render["data"][k] for k in ("frames", "samples", "fps")},
                "event": event(render["data"]["event"]),
            },
        })
    return {
        "order": order,
        "shots": shots,
        "options": plan_snapshot["options"],
        "boundaries": timeline["boundaries"],
        **{k: timeline[k] for k in ("total_frames", "total_samples", "total_seconds", "burn")},
        "stages": sorted({item["stage"] for item in graph.values()}),
    }


def test_fixture_build_timeline_matches_snapshot(prepared, deterministic):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder, speaker=FixtureSpeaker(config))
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    check_snapshot("timeline", structure(config, jid, w, p))


def test_timeout_changes_keep_versions_reusable_and_layout(prepared, deterministic):
    """请求超时只影响等待：改超时后历史版本仍按签名复用，切分布局不变。"""
    config, _ = prepared
    layout = config.layout_key
    config.detector.timeout = 60
    assert config.layout_key == layout
    jid = studio.create_job(config, "红色开场。", options={"burn": False})["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    ws.add_script(config, jid, w, p, "红色开场。", "manual")
    ws.set_board(config, jid, w, p, [studio.Shot(id="a", text="红色开场。", query="红色", estimated_seconds=1)])
    p["board_script"] = p["script"]
    spec = ws.specification(config, jid, w, p, "tts", "a")
    vid = ws.add_version(config, jid, w, "tts", "a", **spec)
    p["shots"]["a"]["tts"] = vid
    config.studio.tts.timeout = 42
    changed = ws.specification(config, jid, w, p, "tts", "a")
    assert changed["signature"] != spec["signature"]
    assert ws.match(config, jid, w, p, "tts", "a", verify=False) == vid
    assert ws.conflicts(config, jid, w, p, "tts", "a", vid) == []
