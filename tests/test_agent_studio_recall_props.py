"""性质测试：分镜召回设置往返（Property 12，Req 14.1）。

两层：
- 纯函数层：任意合法 recall / granularity / recall_limit 经 ``plan_shots``（update 与 insert）得到的分镜
  通过 ``ShotBody`` 与 ``studio.Shot`` 校验，字段等于输入（recall 为 ShotRecall 规范形状）；clear 后为空。
- 端到端层：经 ``edit_shots`` 预览 + 执行写入真实创作接口，再从工作台读回；引用只取服务端能接受的
  真实数据（预先登记的演员、预先上传的查询图片/视频；任务不限素材范围）。
"""

from __future__ import annotations

import asyncio
import io

import pytest
from conftest import legacy_job
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.agent.tools.common import PlanView
from mindex.agent.tools.studio_write import (
    RecallFilterArgs,
    ShotFields,
    ShotOperation,
    ShotRecallArgs,
    plan_shots,
)
from mindex.api import create_app
from mindex.shared.types import Filters
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.studio_models import ShotBody

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-77f0c3e1cc"]
RECALL_KEYS = ("recall", "granularity", "recall_limit")
FIXTURE_CHECKS = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]


# ------------------------------------------------------------ 生成器
def labels() -> st.SearchStrategy[str | None]:
    return st.none() | st.text(max_size=20)


SOURCE_IDS = st.text(min_size=1, max_size=12)
PERSON_IDS = st.text(min_size=1, max_size=16)
MEDIA = st.tuples(st.sampled_from(["image", "video"]), st.text("0123456789abcdef", min_size=64, max_size=64))


@st.composite
def filters(draw, sources: st.SearchStrategy[str] = SOURCE_IDS) -> dict:
    """合法的 RecallFilterArgs：max_duration 为空或不小于 min_duration 且大于 0。"""
    minimum = draw(st.floats(min_value=0, max_value=600, allow_nan=False, allow_infinity=False))
    maximum = draw(st.none() | st.floats(min_value=0, max_value=600, allow_nan=False, allow_infinity=False)
                   .map(lambda delta: minimum + delta).filter(lambda value: value > 0))
    return {
        "source_id": draw(st.none() | sources),
        "source_ids": draw(st.lists(sources, max_size=4)),
        "min_duration": minimum,
        "max_duration": maximum,
        "scene": draw(labels()),
        "emotion": draw(labels()),
        "shot_size": draw(labels()),
    }


@st.composite
def recalls(draw, persons: st.SearchStrategy[str] = PERSON_IDS,
            media: st.SearchStrategy[tuple[str, str]] = MEDIA) -> dict:
    """合法的 ShotRecallArgs 参数：text 模式随机人物/关系/筛选；image/video 模式带对应类型的查询文件。

    ``media`` 生成 (kind, media_id)；缺省为任意 64 位 hex 与任意类型（纯函数层不校验文件存在）。
    """
    value = {"filters": draw(filters())}
    if draw(st.booleans()):
        value.update(
            mode=draw(st.sampled_from(["text", None])),  # None = 省略 mode（默认文字召回）
            person_ids=draw(st.lists(persons, max_size=5)),
            person_mode=draw(st.sampled_from(["auto", "onscreen", "mentioned"])),
            person_relation=draw(st.sampled_from(["all", "any"])),
        )
    else:
        kind, media_id = draw(media)
        value.update(mode=kind, media_id=media_id, media_kind=draw(st.sampled_from([kind, None])))
    return {key: item for key, item in value.items() if item is not None}


granularities = st.sampled_from(["shot", "moment", "scene"])
limits = st.integers(min_value=1, max_value=50)


def expected_recall(arguments: dict) -> dict:
    """独立于 normalize_recall 计算期望：ShotRecall 完整形状，image/video 缺省 media_kind 取 mode。"""
    data = {**arguments, "filters": Filters(**arguments.get("filters", {})).model_dump()}
    if data.get("mode", "text") != "text":
        data.setdefault("media_kind", data["mode"])
    return studio.ShotRecall.model_validate(data).model_dump()


# ------------------------------------------------------------ 夹具与助手
@pytest.fixture
def job(config):
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=SHOTS[0], text="红色开场。", query="红色", estimated_seconds=1.5),
        studio.Shot(id=SHOTS[1], text="蓝色中段。", query="蓝色", estimated_seconds=2.0, granularity="scene",
                    recall_limit=5),
        studio.Shot(id=SHOTS[2], text="绿色结尾。", query="绿色", estimated_seconds=1.0),
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


@pytest.fixture
def client(config, job):
    with TestClient(create_app(config)) as client:
        yield client


def workspace(client, job):
    return client.get(f"/api/studio/jobs/{job}/workspace").json()


def saved_shot(data, sid):
    p = data["plans"][data["active_plan"]]
    return data["versions"][p["shots"][sid]["content"]]["data"]


def edit_shots(client, arguments: dict):
    """与运行时确认流程一致：同一 ToolContext 先预览再执行。"""
    spec = build_registry().get("edit_shots")

    async def go():
        loop = MindexClient(client.app)
        try:
            ctx = ToolContext(client=loop, page={"page": "studio"})
            parsed = spec.parse(arguments)
            await spec.preview(parsed, ctx)
            return await spec.execute(parsed, ctx)
        finally:
            await loop.aclose()

    return asyncio.run(go())


def png(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, "PNG")
    return buffer.getvalue()


def query_media(client, job, name: str, data: bytes, content_type: str, kind: str) -> str:
    pid = workspace(client, job)["active_plan"]
    response = client.post(f"/api/studio/jobs/{job}/plans/{pid}/query-media",
                           files={"file": (name, data, content_type)}, data={"kind": kind})
    assert response.status_code == 201, response.text
    return response.json()["media_id"]


@pytest.fixture
def references(client, job, video_file):
    """服务端 validate_recall_references 能接受的真实引用：已登记的演员与已上传的查询文件。"""
    actors = [client.post("/api/persons/entities", json={"kind": "actor", "name": name}).json()["id"]
              for name in ("演员甲", "演员乙")]
    media = [("image", query_media(client, job, "红.png", png("red"), "image/png", "image")),
             ("image", query_media(client, job, "蓝.png", png("blue"), "image/png", "image")),
             ("video", query_media(client, job, "片段.mp4", video_file.read_bytes(), "video/mp4", "video"))]
    return actors, media


# ------------------------------------------------------------ 纯函数层
def test_recall_args_mirror_shot_recall():
    assert set(ShotRecallArgs.model_fields) == set(studio.ShotRecall.model_fields)
    assert set(RecallFilterArgs.model_fields) == set(Filters.model_fields)


# Feature: agent-capability-expansion, Property 12: 召回设置往返
@settings(max_examples=100, deadline=None, suppress_health_check=FIXTURE_CHECKS)
@given(recall=recalls(), granularity=granularities, limit=limits, target=st.integers(0, 2),
       insert=st.booleans())
def test_plan_shots_recall_round_trip(client, job, recall, granularity, limit, target, insert):
    """**Validates: Requirements 14.1**"""
    view = PlanView(workspace(client, job))
    original = view.shots()
    fields = ShotFields(recall=ShotRecallArgs.model_validate(recall), granularity=granularity, recall_limit=limit)
    if insert:
        operation = ShotOperation(op="insert", after=f"#{target + 1}",
                                  fields=fields.model_copy(update={"text": "新镜。", "query": "新镜"}))
        index = target + 1
    else:
        operation = ShotOperation(op="update", shot=f"#{target + 1}", fields=fields)
        index = target
    shots, _changes, _touched = plan_shots(view, [operation])
    shot = shots[index]
    assert shot["recall"] == expected_recall(recall)
    assert shot["granularity"] == granularity and shot["recall_limit"] == limit
    # 结果可通过请求体与存储模型校验，且模型往返后召回字段不变。
    body = ShotBody.model_validate(shot)
    assert body.recall.model_dump() == shot["recall"]
    if not insert:
        stored = studio.Shot.model_validate(shot).model_dump()
        assert {key: stored[key] for key in RECALL_KEYS} == {key: shot[key] for key in RECALL_KEYS}
    # 其余分镜不受影响。
    others = [s for i, s in enumerate(shots) if i != index]
    assert others == [s for i, s in enumerate(original) if insert or i != target]

    if insert:
        return
    # 同一批操作中随后 clear：三项恢复为跟随方案（None），仍可通过校验。
    clear = ShotOperation(op="update", shot=f"#{target + 1}", clear=list(RECALL_KEYS))
    cleared = plan_shots(view, [operation, clear])[0][index]
    assert all(cleared[key] is None for key in RECALL_KEYS)
    ShotBody.model_validate(cleared)
    assert {k: v for k, v in cleared.items() if k not in RECALL_KEYS} == \
        {k: v for k, v in original[target].items() if k not in RECALL_KEYS}


# ------------------------------------------------------------ 端到端层
# Feature: agent-capability-expansion, Property 12: 召回设置往返
@settings(max_examples=100, deadline=None, suppress_health_check=FIXTURE_CHECKS)
@given(data=st.data(), granularity=granularities, limit=limits, target=st.integers(0, 2))
def test_edit_shots_recall_round_trip_through_workspace(client, job, references, data, granularity, limit, target):
    """**Validates: Requirements 14.1**

    每个样例先写入再 clear，工作台回到等价状态，样例之间互不影响。
    """
    actors, media = references
    recall = data.draw(recalls(persons=st.sampled_from(actors), media=st.sampled_from(media)), label="recall")
    sid = SHOTS[target]
    before = saved_shot(workspace(client, job), sid)

    edit_shots(client, {"job_id": job, "operations": [{"op": "update", "shot": f"#{target + 1}", "fields": {
        "recall": recall, "granularity": granularity, "recall_limit": limit}}]})
    after = workspace(client, job)
    shot = saved_shot(after, sid)
    assert shot["recall"] == expected_recall(recall)
    assert shot["granularity"] == granularity and shot["recall_limit"] == limit
    assert {k: v for k, v in shot.items() if k not in RECALL_KEYS} == \
        {k: v for k, v in before.items() if k not in RECALL_KEYS}
    studio.Shot.model_validate(shot)

    edit_shots(client, {"job_id": job, "operations": [{"op": "update", "shot": f"#{target + 1}",
                                                       "clear": list(RECALL_KEYS)}]})
    cleared = saved_shot(workspace(client, job), sid)
    assert all(cleared.get(key) is None for key in RECALL_KEYS)
    assert {k: v for k, v in cleared.items() if k not in RECALL_KEYS} == \
        {k: v for k, v in before.items() if k not in RECALL_KEYS}
