"""GET /api/studio/jobs/{job}/plans/{plan}/diagnostics（需求 15.1）。

只读诊断：分镜硬/软检查与覆盖、字幕环境、已选节点状态与变化原因、各阶段当前输入。
全部在 conftest 的临时数据目录中运行。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from api import create_app
from conftest import FixtureSpeaker, legacy_job
from studio import studio
from studio import workspace as ws
from test_studio_workspace import wait_build


def tree_hashes(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


SHOTS = [
    studio.Shot(id="a", text="红色开场。", query="红色", estimated_seconds=1, visual="红色 幕布 拉开"),
    studio.Shot(id="b", text="蓝色过渡。", query="蓝色", estimated_seconds=1),
    studio.Shot(id="c", text="绿色结尾。", query="绿色", estimated_seconds=1, visual="绿色 草地 远景"),
]


@pytest.fixture
def env(config):
    job_id = legacy_job(config, "红色开场。蓝色过渡。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, SHOTS)
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return config, TestClient(create_app(config)), job_id, p["id"]


def url(job_id, pid):
    return f"/api/studio/jobs/{job_id}/plans/{pid}/diagnostics"


def test_structure_and_storyboard_checks(env):
    config, client, job_id, pid = env
    response = client.get(url(job_id, pid))
    assert response.status_code == 200, response.text
    body = response.json()
    w = ws.load(config, job_id)
    p = ws.plan(w, pid)
    assert body["plan_id"] == pid and body["revision"] == ws.revision(p)
    assert body["active"] is True and body["running_build"] is None
    assert body["storyboard"]["hard"] is None
    assert body["storyboard"]["coverage"] is None
    assert body["storyboard"]["soft"] == [
        {"index": 2, "shot_id": "b", "kinds": ["missing_visual"], "reasons": ["缺少画面意象 visual"]}
    ]
    assert body["subtitle"] == {"burn": False, "filter": None, "font": None}
    assert body["nodes"] == []  # 还没有任何已选的阶段版本
    assert set(body["inputs_by_stage"]) == set(ws.stages.names(scope="shot"))
    assert body["inputs_by_stage"]["generate"] == {}  # 没有 aigc 分镜：来源专属阶段不给出 inputs
    for stage in ws.STAGES:
        for sid in ("a", "b", "c"):
            expected = ws.specification(config, job_id, w, p, stage, sid)["inputs"]
            assert body["inputs_by_stage"][stage][sid] == expected
    assert body["inputs_by_stage"]["recall"]["a"] == {"content": p["shots"]["a"]["content"]}
    assert {n["key"] for n in body["preflight"]["nodes"]} >= {"recall:a", "tts:b", "compose"}


def test_changed_narration_marks_tts_stale(env):
    config, client, job_id, pid = env
    w = ws.load(config, job_id)
    p = ws.plan(w, pid)
    spec = ws.specification(config, job_id, w, p, "tts", "a")
    vid = ws.add_version(
        config,
        job_id,
        w,
        "tts",
        "a",
        inputs=spec["inputs"],
        parameters=spec["parameters"],
        signature=spec["signature"],
    )
    p["shots"]["a"]["tts"] = vid
    ws.persist(config, job_id, w)
    [node] = client.get(url(job_id, pid)).json()["nodes"]
    assert node == {
        "key": "tts:a",
        "stage": "tts",
        "shot_id": "a",
        "version": vid,
        "state": "ok",
        "reasons": [],
    }

    edited = [SHOTS[0].model_copy(update={"text": "红色的开场。"}), *SHOTS[1:]]
    ws.set_board(config, job_id, w, p, edited)
    ws.persist(config, job_id, w)
    body = client.get(url(job_id, pid)).json()
    [node] = body["nodes"]
    assert node["state"] == "stale" and "旁白已变化" in node["reasons"]
    assert body["storyboard"]["coverage"] == "分镜文本未按顺序完整覆盖原文，请核对遗漏、重复或改写内容"


def test_missing_query_media_is_hard_error(env):
    config, client, job_id, pid = env
    w = ws.load(config, job_id)
    p = ws.plan(w, pid)
    recall = studio.ShotRecall(mode="image", media_id="0" * 64, media_kind="image")
    ws.set_board(config, job_id, w, p, [SHOTS[0], SHOTS[1].model_copy(update={"recall": recall}), SHOTS[2]])
    ws.persist(config, job_id, w)
    hard = client.get(url(job_id, pid)).json()["storyboard"]["hard"]
    assert hard == "第 2 个分镜：查询文件缺失，请先上传查询图片或视频"


def test_read_only_while_lock_held(env):
    config, client, job_id, pid = env
    directory = studio.job_dir(config, job_id)
    before = tree_hashes(directory)
    lock = FileLock(str(studio.lock_path(config)), timeout=0, thread_local=False)
    lock.acquire()
    try:
        first = client.get(url(job_id, pid))
        second = client.get(url(job_id, pid))
    finally:
        lock.release()
    assert first.status_code == 200 and first.json() == second.json()
    assert tree_hashes(directory) == before


def test_missing_objects_return_404(env):
    config, client, job_id, _pid = env
    assert client.get(url("20990101-000000-abcdef", "x")).json()["detail"] == "找不到创作任务"
    response = client.get(url(job_id, "no-such-plan"))
    assert response.status_code == 404 and response.json()["detail"] == "找不到构建方案"
    fresh = studio.create_job(config, "未迁移任务。")["id"]
    assert client.get(url(fresh, "x")).status_code == 404


def test_burned_subtitles_report_font_check(env, tmp_path):
    config, client, job_id, pid = env
    w = ws.load(config, job_id)
    ws.plan(w, pid)["options"]["burn"] = True
    ws.persist(config, job_id, w)
    config.studio.subtitle.font_file = str(tmp_path / "missing.ttf")
    subtitle = client.get(url(job_id, pid)).json()["subtitle"]
    assert subtitle["burn"] is True and isinstance(subtitle["filter"], bool)
    assert subtitle["font"] == f"字幕字体文件不存在：{tmp_path / 'missing.ttf'}"


# ------------------------------------------------------------ 任务 18.4 补充
def set_burn(config, job_id, pid):
    w = ws.load(config, job_id)
    ws.plan(w, pid)["options"]["burn"] = True
    ws.persist(config, job_id, w)


def test_font_ok_when_libass_and_cjk_font_available(env, tmp_path):
    """环境具备 libass 与可用 CJK 字体时 font == "ok"；否则跳过（由下一个用例覆盖失败分支）。"""
    from shared.media import MediaError
    from studio.render import check_font

    config, client, job_id, pid = env
    config.studio.subtitle.font_file = None
    probe = tmp_path / "probe.srt"
    probe.write_text("1\n00:00:00,000 --> 00:00:00,200\n字幕测试 Aa\n", encoding="utf-8")
    try:
        check_font(probe, config.studio.subtitle)
    except MediaError as exc:
        pytest.skip(f"当前环境无法烧录 CJK 字幕：{exc}")
    set_burn(config, job_id, pid)
    assert client.get(url(job_id, pid)).json()["subtitle"] == {"burn": True, "filter": True, "font": "ok"}


def test_missing_subtitles_filter_reported(env, monkeypatch):
    from studio import render as studio_render

    config, client, job_id, pid = env
    config.studio.subtitle.font_file = None
    monkeypatch.setattr(studio_render, "has_subtitles_filter", lambda: False)
    set_burn(config, job_id, pid)
    subtitle = client.get(url(job_id, pid)).json()["subtitle"]
    assert subtitle["burn"] is True and subtitle["filter"] is False
    assert "未编译 subtitles 滤镜" in subtitle["font"] and "burn=false" in subtitle["font"]


def replace_shot(config, job_id, pid, index, **update):
    w = ws.load(config, job_id)
    p = ws.plan(w, pid)
    shots = list(SHOTS)
    shots[index] = shots[index].model_copy(update=update)
    ws.set_board(config, job_id, w, p, shots)
    ws.persist(config, job_id, w)


def test_more_hard_storyboard_errors(env):
    config, client, job_id, pid = env

    def hard():
        response = client.get(url(job_id, pid))
        assert response.status_code == 200, response.text
        return response.json()["storyboard"]["hard"]

    # 分镜数超上限（例如配置被调低）。
    original = config.studio.max_shots
    config.studio.max_shots = 2
    try:
        assert hard() == "分镜数量 3 超过上限 2"
    finally:
        config.studio.max_shots = original

    # 画面意象超长：模型层会拒绝，这里直接改写已保存的 content 数据模拟旧数据。
    w = ws.load(config, job_id)
    p = ws.plan(w, pid)
    w["versions"][p["shots"]["b"]["content"]]["data"]["visual"] = "长" * 121
    ws.persist(config, job_id, w)
    assert hard() == "第 2 个分镜的画面意象超过 120 字上限（当前 121 字）"

    # 人物不存在。
    replace_shot(config, job_id, pid, 1, recall=studio.ShotRecall(person_ids=["ghost"]))
    assert hard() == "第 2 个分镜：找不到对应人物、作品或角色"

    # 选择了作品。
    work = client.post("/api/persons/entities", json={"kind": "work", "name": "某部作品"})
    assert work.status_code == 201, work.text
    replace_shot(config, job_id, pid, 1, recall=studio.ShotRecall(person_ids=[work.json()["id"]]))
    assert hard() == "第 2 个分镜：人物召回不能选择作品，请选择演员或角色"

    # 素材超出任务范围。
    from test_agent_studio_recall import limit_scope

    limit_scope(config, job_id, ["src-a"])
    replace_shot(config, job_id, pid, 2, recall=studio.ShotRecall(filters={"source_ids": ["src-b"]}))
    assert hard() == "第 3 个分镜：素材超出本任务允许的素材范围：src-b"
    replace_shot(config, job_id, pid, 2, recall=studio.ShotRecall(filters={"source_ids": ["src-a"]}))
    assert hard() is None


def test_node_states_missing_failed_corrupt(env):
    config, client, job_id, pid = env
    w = ws.load(config, job_id)
    p = ws.plan(w, pid)
    # a：真实配音版本，随后删除音频文件 → corrupt。
    vid = ws.execute(config, job_id, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    item = ws.version(w, vid)
    for name in item["files"]:
        ws.file_path(config, job_id, item, name).unlink()
    # b：失败版本被选中（只可能来自旧数据或手工修改）→ failed，原因取版本错误。
    failed = ws.add_version(config, job_id, w, "tts", "b", status="failed", error="TTS 服务不可用")
    p["shots"]["b"]["tts"] = failed
    # c：已选版本记录不存在 → missing。
    p["shots"]["c"]["tts"] = "f" * 32
    ws.persist(config, job_id, w)

    response = client.get(url(job_id, pid))
    assert response.status_code == 200, response.text
    nodes = {n["key"]: n for n in response.json()["nodes"]}
    assert set(nodes) == {"tts:a", "tts:b", "tts:c"}
    assert (nodes["tts:a"]["state"], nodes["tts:a"]["reasons"]) == ("corrupt", ["版本文件缺失或损坏"])
    assert (nodes["tts:b"]["state"], nodes["tts:b"]["reasons"]) == ("failed", ["TTS 服务不可用"])
    assert (nodes["tts:c"]["state"], nodes["tts:c"]["reasons"]) == ("missing", ["已选版本记录缺失"])
    assert nodes["tts:c"]["version"] == "f" * 32


def test_inputs_by_stage_submit_column_and_running_build(env):
    config, _client, job_id, pid = env
    entered, release = threading.Event(), threading.Event()

    class Blocking(FixtureSpeaker):
        def speak(self, text, voice=None):
            entered.set()
            assert release.wait(20)
            return super().speak(text, voice)

    client = TestClient(create_app(config, speaker=Blocking(config)))
    body = client.get(url(job_id, pid)).json()
    response = client.post(
        f"/api/studio/jobs/{job_id}/plans/{pid}/execute-column",
        json={"revision": body["revision"], "stage": "tts", "inputs_by_shot": body["inputs_by_stage"]["tts"]},
    )
    assert response.status_code == 202, response.text
    bid = response.json()["id"]
    try:
        assert entered.wait(10)
        running = client.get(url(job_id, pid))
        assert running.status_code == 200 and running.json()["running_build"] == bid
    finally:
        release.set()
    done = wait_build(client, job_id)["builds"][-1]
    assert done["id"] == bid and done["status"] == "complete"
    assert [n["key"] for n in done["nodes"]] == ["tts:a", "tts:b", "tts:c"]
    after = client.get(url(job_id, pid)).json()
    assert after["running_build"] is None
    assert {n["key"]: n["state"] for n in after["nodes"]} == {"tts:a": "ok", "tts:b": "ok", "tts:c": "ok"}
