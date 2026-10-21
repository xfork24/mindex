"""内容拒绝只跳过当前片段，检查点、场景、发布和 API 保持可用。"""


import httpx
import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.admin.source_tasks import SourceTasks, WorkOptions
from mindex.api import create_app
from mindex.models import adapters
from mindex.models.adapters import AnalysisContentRejected, ModelError, QwenAnalyzer
from mindex.processing.channel_index import snapshots_for
from mindex.processing.indexing import IndexBuilder, check_index
from mindex.processing.pipeline import Pipeline
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.shared.types import Annotation

REASON = "系统检测到输入或生成内容可能包含不安全或敏感内容，请您避免输入易产生敏感内容的提示语，感谢您的配合。"


@pytest.mark.parametrize("status,body,rejected", [
    (400, {"error": {"code": "1301", "message": REASON}}, True),
    (400, {"error": {"message": REASON}}, True),
    (403, {"error": {"code": "content_policy_violation"}}, True),
    (400, {"error": {"message": "invalid max_tokens"}}, False),
    (401, {"error": {"message": "invalid API key"}}, False),
    (403, {"error": {"message": "access denied"}}, False),
    (400, {"error": {"message": "sensitive parameter is not supported"}}, False),
])
def test_explicit_content_error_is_not_retried(config, monkeypatch, status, body, rejected):
    calls = []

    def wire(method, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(adapters.httpx, "request", wire)
    monkeypatch.setattr(adapters.time, "sleep", lambda _: pytest.fail("不应重试内容或永久错误"))
    with pytest.raises(AnalysisContentRejected if rejected else ModelError) as found:
        QwenAnalyzer(config)._chat([], "测试指令")
    assert isinstance(found.value, AnalysisContentRejected) == rejected
    assert len(calls) == 1


@pytest.mark.parametrize("message,finish", [
    ({"content": None}, "content_filter"),
    ({"content": None}, "sensitive"),
    ({"refusal": "Cannot process this content", "content": None}, "stop"),
])
def test_chat_refusal_skips_without_json_retry(config, monkeypatch, message, finish):
    monkeypatch.setattr(adapters, "request", lambda *a, **k: {"choices": [{"message": message, "finish_reason": finish}]})
    monkeypatch.setattr(adapters.time, "sleep", lambda _: pytest.fail("拒绝不能通过修改提示词重试"))
    with pytest.raises(AnalysisContentRejected):
        QwenAnalyzer(config)._chat([], "测试指令")


def test_task_continues_after_rejection_and_publishes_without_rejected_text(config, video_file, monkeypatch):
    from test_source_work import finished

    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    rejected = pipeline.store.segments(sid, "shot")[1]
    rejected_paths = {pipeline.store.resolve_media(s["path"]) for s in pipeline.store.segments(sid)
                      if s["start"] == rejected["start"] and s["end"] == rejected["end"]}
    calls, groups = [], []

    class Analyzer(FixtureAnalyzer):
        def analyze(self, path, dialogue):
            calls.append(path)
            if path in rejected_paths:
                raise AnalysisContentRejected(REASON)
            return super().analyze(path, dialogue)

        def group_scenes(self, units):
            groups.append(units)
            assert all(u["end"] <= rejected["start"] or u["start"] >= rejected["end"] for u in units)
            return super().group_scenes(units)

    monkeypatch.setattr("mindex.processing.pipeline.make_analyzer", lambda _: Analyzer(embedder))
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store, embedder)
    task = finished(manager, manager.start(sid, "analysis", WorkOptions()))
    assert task["status"] == "complete" and task["publication"]["status"] == "complete", task
    assert task["progress"]["processing_progress"]["skipped"] == 2  # shot + same-boundary moment
    assert len(groups) == 2  # no scene joins across the skipped interval
    profile = pipeline.annotation_profile(sid)
    saved = pipeline.store.json_value("annotations", "segment_id", rejected["id"], profile)
    assert saved["status"] == "skipped" and saved["reason"] == REASON and "annotation" not in saved
    assert any(p in rejected_paths for p in calls)
    assert any(p != pipeline.store.resolve_media(rejected["path"]) for p in calls)
    before = len(calls)
    resumed = finished(manager, manager.start(sid, "analysis", WorkOptions()))
    assert resumed["status"] == "complete" and len(calls) == before
    client = TestClient(create_app(config, embedder))
    detail = client.get(f"/api/admin/segments/{rejected['id']}/details").json()["llm"]
    assert detail["status"] == "skipped" and detail["reason"] == REASON
    assert "已跳过" in detail["message"]
    work = client.get(f"/api/admin/sources/{sid}").json()["work"]["analysis"]
    assert work["status"] == "complete" and work["result"]["skipped"] == 2
    assert [(r["reason"], r["count"]) for r in work["result"]["skipped_reasons"]] == [(REASON, 2)]
    assert work["result"]["analyzed"] + work["result"]["skipped"] == work["result"]["total"]
    assert work["result"]["analyzed"] + 2 == work["result"]["total"]
    snapshots = snapshots_for(pipeline, sid, ["shot", "moment"])
    for s in snapshots:
        if s["annotation_status"] == "skipped":
            assert not s["views"].get("analysis") and s["annotation"] is None
            assert s["channel_metadata"]["analysis"]["status"] == "skipped"
    assert not check_index(pipeline.store, pipeline.store.active_index()["id"])
    assert all(s["id"] != rejected["id"] for s in pipeline.snapshots(sid))


@pytest.mark.parametrize("reject_stage", ["all", "scene", "group"])
def test_all_rejected_scene_or_group_still_completes_and_reuses(config, video_file, reject_stage):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    calls = []

    class Analyzer:
        def analyze(self, path, dialogue):
            calls.append(path)
            segment = next(s for s in pipeline.store.segments() if pipeline.store.resolve_media(s["path"]) == path)
            if reject_stage == "all" or reject_stage == "scene" and segment["kind"] == "scene":
                raise AnalysisContentRejected(REASON)
            return Annotation(description="工程测试画面")

        def group_scenes(self, units):
            calls.append("group")
            if reject_stage == "group":
                raise AnalysisContentRejected(REASON)
            return [list(range(len(units)))]

    pipeline._analyzer = Analyzer()
    pipeline.analyze(sid)
    plan = pipeline.store.json_value("scene_plans", "source_id", sid, pipeline.annotation_profile(sid))
    assert plan == ([] if reject_stage in {"all", "group"} else [[0, 10]])
    before = len(calls)
    pipeline.analyze(sid)
    assert len(calls) == before
    version = IndexBuilder(config, embedder).build(source_id=sid, activate=True)
    assert not check_index(pipeline.store, version)


def test_ordinary_model_failure_is_not_saved_as_skip(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)

    class Failed:
        def analyze(self, path, dialogue):
            raise ModelError("invalid API key")

    pipeline._analyzer = Failed()
    with pytest.raises(ModelError, match="invalid API key"):
        pipeline.analyze(sid)
    assert not pipeline.store.rows("SELECT * FROM annotations")


def test_publishing_skip_removes_previous_analysis_channel(config, video_file):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    builder = IndexBuilder(config, embedder)
    previous = builder.build(source_id=sid, activate=True)
    segment = pipeline.store.segments(sid, "moment")[0]
    saved = {"status": "skipped", "reason": REASON, "dialogue": []}
    pipeline.store.put_json("annotations", "segment_id", segment["id"], "new-profile", saved)
    published = builder.build(source_id=sid, channels=["analysis"], activate=True)
    snapshot = next(s for s in pipeline.store.snapshots(published) if s["id"] == segment["id"])
    assert snapshot["annotation_status"] == "skipped" and "analysis" not in snapshot["indexed_views"]
    assert "video" in snapshot["indexed_views"]
    assert "analysis" in next(s for s in pipeline.store.snapshots(previous) if s["id"] == segment["id"])["indexed_views"]
    assert not check_index(pipeline.store, published)
