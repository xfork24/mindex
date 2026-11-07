"""素材摘要（Material Digest）的示例测试：范围、隐藏、未分析、预算、指纹、空原因优先级与配置项。

纯函数部分直接给 compose_digest 传快照字典；build_digest 部分用临时数据目录里真实构建的生效索引
（Pipeline + IndexBuilder）与 Library 覆盖层。全部只读写 tmp_path，不触碰工作区 data/。
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from admin.curation import CurationPatch, Library
from conftest import FixtureAnalyzer, FixtureEmbedder
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from shared.config import Config, load_config
from shared.storage import Store
from studio.material_digest import EMPTY_BUDGET, EMPTY_INDEX, EMPTY_SEGMENTS, build_digest, compose_digest


def seg(
    sid: str,
    source: str,
    kind: str,
    start: float,
    analysis: str,
    *,
    status: str = "complete",
    hidden: bool = False,
    name: str | None = None,
    duration: float = 60.0,
) -> dict:
    return {
        "id": sid,
        "source_id": source,
        "source_name": name or f"素材{source}",
        "source_duration": duration,
        "kind": kind,
        "start": start,
        "end": start + 1,
        "annotation_status": status,
        "effective_hidden": hidden,
        "views": {"analysis": analysis, "dialogue": "对白不该出现"},
    }


def digest(snapshots, *, scope=None, budget=12000, index="idx-1"):
    return compose_digest(snapshots, scope=scope, budget=budget, content_index_id=index)


def base_snapshots() -> list[dict]:
    return [
        seg("a1", "A", "scene", 0, "港口清晨的渔船\n场景：港口"),
        seg("a2", "A", "shot", 0, "渔船靠岸\n场景：码头\n对象：渔船\n动作：靠岸"),
        seg("a3", "A", "moment", 1, "渔民收网\n对象：渔网\n动作：收网"),
        seg("b1", "B", "scene", 0, "山顶日出全景\n场景：山顶"),
        seg("b2", "B", "shot", 0, "云海翻涌\n场景：山顶\n对象：云海"),
    ]


# ---------- 范围、隐藏、未分析 ----------


def test_scope_keeps_only_sources_in_scope():
    result = digest(base_snapshots(), scope={"B"})
    assert [s.source_id for s in result.sources] == ["B"]
    assert result.source_count == 1 and result.segment_count == 2
    assert "素材A" not in result.text and "渔船" not in result.text and "港口" not in result.text
    assert "素材B" in result.text and "云海" in result.text


def test_scope_empty_set_returns_empty_with_no_segments_reason():
    result = digest(base_snapshots(), scope=set())
    assert result.empty and result.text == "" and result.source_count == 0 and result.segment_count == 0
    assert result.empty_reason == EMPTY_SEGMENTS


def test_hidden_segments_excluded_from_text_vocabulary_and_counts():
    snapshots = base_snapshots() + [seg("a4", "A", "shot", 2, "秘密灯塔\n对象：隐藏灯塔", hidden=True)]
    result = digest(snapshots)
    assert result.segment_count == 5
    assert "灯塔" not in result.text
    assert all("隐藏灯塔" not in s.vocabulary for s in result.sources)
    assert result == digest(base_snapshots())


def test_unanalyzed_segments_excluded():
    extra = [
        seg(f"x{i}", "A", "shot", 5 + i, f"未完成画面{i}\n对象：未完成词{i}", status=status)
        for i, status in enumerate(["pending", "processing", "failed", "not_processed"])
    ]
    result = digest(base_snapshots() + extra)
    assert result.segment_count == 5
    assert "未完成" not in result.text
    # 全部是未分析片段的素材整体省略，不计素材数。
    only_bad = digest([seg("c1", "C", "shot", 0, "画面\n对象：词", status="failed")] + base_snapshots())
    assert [s.source_id for s in only_bad.sources] == ["A", "B"]


def test_dialogue_and_person_labels_never_appear():
    result = digest([seg("a", "A", "shot", 0, "海边散步\n人物：张三\n场景：海边\n氛围：轻松")])
    assert "对白" not in result.text and "张三" not in result.text and "轻松" not in result.text
    assert result.sources[0].vocabulary == ["海边"]


# ---------- 故事线来源回退 ----------


def test_storyline_falls_back_to_shot_descriptions():
    result = digest(
        [
            seg("s2", "A", "shot", 2, "第二个镜头\n场景：街道"),
            seg("s1", "A", "shot", 0, "第一个镜头\n场景：街道"),
            seg("s3", "A", "shot", 4, "  第一个镜头  \n场景：街道"),  # 去空白后重复，只保留最早一条
        ]
    )
    assert result.sources[0].storyline == ["第一个镜头", "第二个镜头"]
    assert "故事线：第一个镜头；第二个镜头\n" in result.text


def test_only_moment_descriptions_gives_empty_storyline_but_keeps_vocabulary():
    result = digest([seg("m1", "A", "moment", 0, "细节\n对象：咖啡杯\n动作：端起", duration=12.4)])
    source = result.sources[0]
    assert source.storyline == []
    assert source.vocabulary == ["咖啡杯", "端起"]
    assert result.text == "【素材A】时长 12 秒\n画面词：咖啡杯、端起\n"
    assert result.source_count == 1 and result.segment_count == 1


# ---------- 预算 ----------


def test_budget_zero_returns_empty_with_budget_reason():
    result = digest(base_snapshots(), budget=0)
    assert result.empty and result.source_count == 0 and result.segment_count == 0
    assert result.empty_reason == EMPTY_BUDGET


def test_budget_within_limit_and_proportional_quota():
    # A 有 9 个片段，B 有 1 个；预算足够时 A 的块不短于 B，且总长不超过预算。
    many = [seg(f"a{i}", "A", "scene", i, f"港口第{i}个场景描述，渔船来来往往") for i in range(9)]
    few = [seg("b0", "B", "scene", 0, "山顶日出")]
    result = digest(many + few, budget=500)
    assert len(result.text) <= 500
    block_a, block_b = result.text.split("【素材B】")
    assert len(block_a) > len("【素材B】" + block_b)
    assert result.source_count == 2 and result.segment_count == 10


def test_budget_below_floor_splits_evenly():
    many = [seg(f"a{i}", "A", "scene", i, f"港口第{i}个场景描述，渔船来来往往") for i in range(9)]
    few = [seg("b0", "B", "scene", 0, "山顶日出的全景描述" * 5)]
    result = digest(many + few, budget=300)  # < 200 × 2，每素材 150
    assert len(result.text) <= 300
    head, tail = result.text.split("【素材B】")
    assert len(head) <= 150 and len("【素材B】" + tail) <= 150
    assert result.source_count == 2


def test_budget_so_small_every_quota_is_zero_returns_budget_reason():
    result = digest(base_snapshots(), budget=1)  # 2 个素材，⌊1/2⌋ = 0
    assert result.empty and result.empty_reason == EMPTY_BUDGET
    tiny = digest(base_snapshots(), budget=4)  # 每素材 2 字，只剩截断的头行
    assert len(tiny.text) <= 4 and tiny.source_count == 2


# ---------- 指纹 ----------


def test_fingerprint_stable_and_order_independent():
    first = digest(base_snapshots())
    again = digest(list(reversed(base_snapshots())))
    assert first.model_dump() == again.model_dump()
    assert first.fingerprint and len(first.fingerprint) == 24
    assert first.stats() == {
        "sources": 2,
        "segments": 5,
        "fingerprint": first.fingerprint,
        "content_index_id": "idx-1",
    }


def test_fingerprint_changes_when_analysis_changes():
    changed = base_snapshots()
    changed[2]["views"]["analysis"] = "渔民补网\n对象：渔网"
    assert digest(changed).fingerprint != digest(base_snapshots()).fingerprint
    assert digest(base_snapshots(), index="idx-2").fingerprint != digest(base_snapshots()).fingerprint


def test_compose_does_not_mutate_input():
    snapshots = base_snapshots()
    before = [dict(s, views=dict(s["views"])) for s in snapshots]
    digest(snapshots, scope={"A"}, budget=300)
    assert snapshots == before


# ---------- build_digest：真实临时索引与覆盖层 ----------


@pytest.fixture
def indexed(config, video_file):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    source_id = pipeline.prepare(video_file)
    pipeline.analyze(source_id)
    IndexBuilder(config, embedder).build(activate=True)
    return config, pipeline.store, source_id


@pytest.mark.slow
def test_build_digest_reads_active_index(indexed):
    config, store, source_id = indexed
    result = build_digest(config, store, {})
    assert not result.empty
    assert result.content_index_id == store.active_index()["id"]
    assert [s.source_id for s in result.sources] == [source_id]
    assert result.segment_count == len(store.snapshots_shared(result.content_index_id))
    assert "室内" in result.sources[0].vocabulary
    assert "平静" not in result.text and "近景" not in result.text  # 风格类标签不进摘要
    assert build_digest(config, store, {}).model_dump() == result.model_dump()


@pytest.mark.slow
def test_build_digest_respects_hidden_overlays(indexed):
    config, store, source_id = indexed
    before = build_digest(config, store, {})
    shot = next(s for s in store.snapshots_shared(before.content_index_id) if s["kind"] == "shot")
    Library(store).patch("segment", shot["id"], CurationPatch(hidden=True))
    after = build_digest(config, store, {})
    assert 0 < after.segment_count < before.segment_count
    assert after.fingerprint != before.fingerprint
    Library(store).patch("source", source_id, CurationPatch(hidden=True))
    hidden = build_digest(config, store, {})
    assert hidden.empty and hidden.empty_reason == EMPTY_SEGMENTS


@pytest.mark.slow
def test_build_digest_scope(indexed):
    config, store, source_id = indexed
    assert not build_digest(config, store, {"source_ids": [source_id]}).empty
    other = build_digest(config, store, {"source_ids": ["not-in-library"]})
    assert other.empty and other.empty_reason == EMPTY_SEGMENTS


@pytest.mark.slow
def test_empty_reason_priority_all_combinations(indexed, tmp_path):
    config, store, _ = indexed
    bare_config = Config(data_dir=tmp_path / "bare")
    bare_store = Store(bare_config.data_dir)
    assert bare_store.active_index() is None
    expected_reason = {
        # (预算为 0, 无生效索引, 无已分析镜头) → 期望原因
        (True, True, True): EMPTY_BUDGET,
        (True, True, False): EMPTY_BUDGET,
        (True, False, True): EMPTY_BUDGET,
        (True, False, False): EMPTY_BUDGET,
        (False, True, True): EMPTY_INDEX,
        (False, True, False): EMPTY_INDEX,
        (False, False, True): EMPTY_SEGMENTS,
        (False, False, False): None,
    }
    for (zero, no_index, no_segments), reason in expected_reason.items():
        cfg, st = (bare_config, bare_store) if no_index else (config, store)
        cfg.studio.material_digest_chars = 0 if zero else 12000
        state = {"source_ids": ["not-in-library"]} if no_segments else {}
        result = build_digest(cfg, st, state)
        assert result.empty_reason == reason, (zero, no_index, no_segments)
        assert result.empty == (reason is not None)
        if reason is not None:
            assert result.text == "" and result.source_count == 0 and result.segment_count == 0
            assert result.public()["empty_reason"] == reason


# ---------- 配置项 ----------


def test_config_default_is_12000():
    assert Config().studio.material_digest_chars == 12000


@pytest.mark.parametrize("value", [0, 1, 100000])
def test_config_accepts_bounds(value):
    assert (
        Config.model_validate({"studio": {"material_digest_chars": value}}).studio.material_digest_chars
        == value
    )


@pytest.mark.parametrize("value", [-1, 1.5, "12000", 100001, True])
def test_config_rejects_invalid_values(value, tmp_path):
    with pytest.raises(ValidationError) as caught:
        Config.model_validate({"studio": {"material_digest_chars": value}})
    assert "studio.material_digest_chars" in str(caught.value)
    # 通过 load_config 加载 YAML 时走同一条校验路径。
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump({"data_dir": str(tmp_path / "data"), "studio": {"material_digest_chars": value}})
    )
    with pytest.raises(ValidationError) as loaded:
        load_config(path)
    assert "studio.material_digest_chars" in str(loaded.value)


def test_config_range_in_error_message():
    with pytest.raises(ValidationError) as caught:
        Config.model_validate({"studio": {"material_digest_chars": 100001}})
    assert "100000" in str(caught.value)
    with pytest.raises(ValidationError) as negative:
        Config.model_validate({"studio": {"material_digest_chars": -5}})
    assert "0" in str(negative.value)
