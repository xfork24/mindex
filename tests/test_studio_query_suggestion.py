"""POST /api/studio/jobs/{job}/plans/{plan}/shots/{sid}/query-suggestion（需求 10.2–10.4、10.7、12.4）。

只返回建议召回词：不加锁、不落盘；404/422 时不调用拆分镜模型，模型出错或返回空串时返回 503。
全部在 conftest 的临时数据目录中运行。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from api import create_app
from conftest import FixtureStoryboarder, legacy_job
from models.adapters import ModelError
from studio import studio
from studio import workspace as ws


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def setup_workspace(config):
    """三镜方案：a（有 visual）、b（老分镜，无 visual）、c（有 visual）。"""
    job_id = legacy_job(config, "红色开场。蓝色过渡。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(
                id="a", text="红色开场。", query="红色", estimated_seconds=1, visual="红色 幕布 拉开"
            ),
            studio.Shot(id="b", text="蓝色过渡。", query="蓝色", estimated_seconds=1),
            studio.Shot(
                id="c", text="绿色结尾。", query="绿色", estimated_seconds=1, visual="绿色 草地 远景"
            ),
        ],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id, p["id"]


@pytest.fixture
def env(config):
    fake = FixtureStoryboarder(config)
    client = TestClient(create_app(config, storyboarder=fake))
    job_id, pid = setup_workspace(config)
    return config, client, fake, job_id, pid


def url(job_id, pid, sid):
    return f"/api/studio/jobs/{job_id}/plans/{pid}/shots/{sid}/query-suggestion"


def test_uses_request_visual_and_neighbor_context(env):
    _config, client, fake, job_id, pid = env
    response = client.post(url(job_id, pid, "b"), json={"visual": "  蓝色 天空 云朵  "})
    assert response.status_code == 200, response.text
    assert response.json() == {"query": "蓝色 天空 画面"}
    assert fake.suggestions == [
        {
            "text": "蓝色过渡。",
            "visual": "蓝色 天空 云朵",
            "context": ("红色开场。", "绿色结尾。"),
            "vocabulary": None,  # 临时数据目录没有生效索引 → 摘要为空 → 不传词表
        }
    ]


def test_omitted_visual_uses_saved_visual(env):
    _config, client, fake, job_id, pid = env
    response = client.post(url(job_id, pid, "a"), json={})
    assert response.status_code == 200, response.text
    assert response.json() == {"query": "红色 幕布 画面"}
    assert fake.suggestions[-1]["visual"] == "红色 幕布 拉开"
    assert fake.suggestions[-1]["context"] == ("", "蓝色过渡。")
    response = client.post(url(job_id, pid, "c"), json={"visual": None})
    assert response.status_code == 200
    assert fake.suggestions[-1]["visual"] == "绿色 草地 远景"
    assert fake.suggestions[-1]["context"] == ("蓝色过渡。", "")


def test_read_only_and_works_while_lock_held(env):
    config, client, _fake, job_id, pid = env
    directory = studio.job_dir(config, job_id)
    before = tree_hashes(directory)
    w = ws.load(config, job_id)
    versions_before = {sid: dict(s) for sid, s in ws.plan(w, pid)["shots"].items()}
    lock = FileLock(str(studio.lock_path(config)), timeout=0, thread_local=False)
    lock.acquire()
    try:
        first = client.post(url(job_id, pid, "a"), json={"visual": "红色 灯笼"})
        second = client.post(url(job_id, pid, "a"), json={"visual": "红色 灯笼"})
    finally:
        lock.release()
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()  # 相同输入逐字节相同
    assert tree_hashes(directory) == before
    fresh = ws.load(config, job_id)
    assert ws.plan(fresh, pid)["shots"] == versions_before


@pytest.mark.parametrize(
    "target, message",
    [
        ("job", "找不到创作任务"),
        ("plan", "找不到构建方案"),
        ("shot", "找不到分镜"),
        ("unmigrated", "找不到构建方案"),
    ],
)
def test_missing_objects_return_404_without_model_call(env, target, message):
    config, client, fake, job_id, pid = env
    if target == "job":
        path = url("20990101-000000-abcdef", pid, "a")
    elif target == "plan":
        path = url(job_id, "no-such-plan", "a")
    elif target == "shot":
        path = url(job_id, pid, "zzz")
    else:
        # 未接入版本工作台的新任务：没有 workspace 文件，也就没有方案。
        fresh_job = studio.create_job(config, "尚未迁移。")["id"]
        assert not ws.path(config, fresh_job).is_file()
        path = url(fresh_job, pid, "a")
    response = client.post(path, json={"visual": "红色 幕布"})
    assert response.status_code == 404
    assert message in response.json()["detail"]
    assert fake.suggestions == []


@pytest.mark.parametrize(
    "sid, body",
    [
        ("a", {"visual": "   \n\t"}),
        ("a", {"visual": ""}),
        ("b", {}),  # 未携带 visual，且该镜没有已保存的 visual
    ],
)
def test_blank_visual_returns_422_without_model_call(env, sid, body):
    _config, client, fake, job_id, pid = env
    response = client.post(url(job_id, pid, sid), json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == "请先填写画面意象，再重写召回词"
    assert fake.suggestions == []


def test_overlong_visual_returns_422_without_model_call(env):
    _config, client, fake, job_id, pid = env
    response = client.post(url(job_id, pid, "a"), json={"visual": "雾" * 121})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "120" in detail and "画面意象" in detail
    assert fake.suggestions == []
    # 恰好 120 字仍被接受。
    assert client.post(url(job_id, pid, "a"), json={"visual": "雾" * 120}).status_code == 200


class Failing(FixtureStoryboarder):
    def suggest_query(self, text, visual, context, vocabulary):
        super().suggest_query(text, visual, context, vocabulary)
        raise ModelError("拆分镜服务超时")


class Blank(FixtureStoryboarder):
    def suggest_query(self, text, visual, context, vocabulary):
        super().suggest_query(text, visual, context, vocabulary)
        return "   "


@pytest.mark.parametrize("kind, needle", [(Failing, "拆分镜服务超时"), (Blank, "空召回词")])
def test_model_failure_or_blank_query_returns_503(config, kind, needle):
    fake = kind(config)
    client = TestClient(create_app(config, storyboarder=fake))
    job_id, pid = setup_workspace(config)
    directory = studio.job_dir(config, job_id)
    before = tree_hashes(directory)
    response = client.post(url(job_id, pid, "a"), json={"visual": "红色 幕布"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail.startswith("召回词重写失败：") and needle in detail
    assert len(fake.suggestions) == 1
    assert tree_hashes(directory) == before
