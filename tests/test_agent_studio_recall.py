"""创作写工具扩展：分镜召回字段（recall / granularity / recall_limit / clear）、
rename_job、attach_query_media。

经进程内回环调用真实创作接口；附件经真实附件存储解析（owner=mcp）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.attachments import AttachmentStore
from agent.loopback import MindexClient
from agent.registry import ToolContext, ToolError
from agent.tools import build_registry
from agent.tools.common import PlanView
from agent.tools.studio_write import RECALL_REBUILD, ShotFields, ShotOperation, ShotRecallArgs, plan_shots
from api import create_app
from conftest import legacy_job
from studio import studio
from studio import workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-77f0c3e1cc"]
PERSON_RECALL = {
    "mode": "text",
    "person_ids": ["p1"],
    "person_mode": "onscreen",
    "person_relation": "any",
    "filters": {
        "source_id": None,
        "source_ids": [],
        "min_duration": 2.0,
        "max_duration": None,
        "scene": None,
        "emotion": None,
        "shot_size": None,
    },
    "media_id": None,
    "media_kind": None,
}


@pytest.fixture
def job(config):
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(id=SHOTS[0], text="红色开场。", query="红色", estimated_seconds=1.5),
            studio.Shot(
                id=SHOTS[1],
                text="蓝色中段。",
                query="蓝色",
                estimated_seconds=2.0,
                granularity="scene",
                recall_limit=5,
            ),
            studio.Shot(id=SHOTS[2], text="绿色结尾。", query="绿色", estimated_seconds=1.0),
        ],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


@pytest.fixture
def client(config, job):
    with TestClient(create_app(config)) as client:
        yield client


def workspace(client, job):
    return client.get(f"/api/studio/jobs/{job}/workspace").json()


def content(data, sid):
    p = data["plans"][data["active_plan"]]
    return data["versions"][p["shots"][sid]["content"]]["data"]


def run(client, config, name, arguments, *, execute=True, between=None):
    """预览 + 执行（共享同一 ToolContext，与运行时确认流程一致）；返回 (preview, output, 请求列表)。

    ``between`` 在预览之后、执行之前调用，用来模拟确认期间工作台被并发修改。
    """
    spec = build_registry().get(name)
    sent: list[tuple[str, str]] = []

    async def record(request):
        sent.append((request.method, request.url.path))

    async def go():
        store = AttachmentStore(config.data_dir)
        loop = MindexClient(client.app, resolve_attachment=store.resolver(session_id=None, owner="mcp"))
        loop.http.event_hooks = {"request": [record], "response": []}
        try:
            ctx = ToolContext(client=loop, page={"page": "studio"})
            parsed = spec.parse(arguments)
            preview = await spec.preview(parsed, ctx)
            if between is not None:
                between()
            return preview, (await spec.execute(parsed, ctx) if execute else None)
        finally:
            await loop.aclose()

    preview, output = asyncio.run(go())
    return preview, output, sent


def attachment(client, data: bytes, name: str, content_type: str) -> str:
    response = client.post("/api/agent/attachments", files={"file": (name, data, content_type)})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buffer, "PNG")
    return buffer.getvalue()


def test_specs_risk_and_domain():
    registry = build_registry()
    for name in ("rename_job", "attach_query_media", "edit_shots"):
        spec = registry.get(name)
        assert spec.domain == "studio" and spec.risk == "write" and spec.preview is not None
    schema = registry.get("edit_shots").parameters()
    assert "clear" in str(schema) and "recall_limit" in str(schema)
    assert "consent" not in str(registry.get("attach_query_media").parameters())


def test_plan_shots_sets_and_clears_recall_fields(client, job):
    view = PlanView(workspace(client, job))
    recall = ShotRecallArgs(
        person_ids=["p1"], person_mode="onscreen", person_relation="any", filters={"min_duration": 2}
    )
    update = ShotOperation(
        op="update", shot="#1", fields=ShotFields(recall=recall, granularity="shot", recall_limit=12)
    )
    clear = ShotOperation(op="update", shot="#2", clear=["granularity", "recall_limit", "visual"])
    shots, changes, touched = plan_shots(view, [update, clear])
    assert shots[0]["recall"] == PERSON_RECALL
    assert shots[0]["granularity"] == "shot" and shots[0]["recall_limit"] == 12
    assert shots[1]["granularity"] is None and shots[1]["recall_limit"] is None and "visual" not in shots[1]
    assert shots[2] == view.shots()[2]
    labels = [c["label"] for c in changes]
    assert any("召回设置" in label for label in labels) and any("召回粒度" in label for label in labels)
    recall_change = next(c for c in changes if "召回设置" in c["label"])
    assert recall_change["before"] == "文字召回（默认）" and "人物 p1" in recall_change["after"]
    assert next(c for c in changes if "#2" in c["label"] and "召回数量" in c["label"])["after"] == "跟随方案"
    assert touched == [SHOTS[0], SHOTS[1]]

    # clear recall → None；image 召回缺省 media_kind 取 mode。
    reset = ShotOperation(op="update", shot="#1", clear=["recall"])
    assert plan_shots(view, [update, reset])[0][0]["recall"] is None
    image = ShotOperation(
        op="update", shot="#1", fields=ShotFields(recall=ShotRecallArgs(mode="image", media_id="a" * 64))
    )
    assert plan_shots(view, [image])[0][0]["recall"]["media_kind"] == "image"


def test_plan_shots_rejects_invalid_recall_operations(client, job):
    view = PlanView(workspace(client, job))
    both = ShotOperation(op="update", shot="#1", fields=ShotFields(recall_limit=3), clear=["recall_limit"])
    with pytest.raises(ToolError) as exc:
        plan_shots(view, [both])
    assert exc.value.code == "invalid_operation"
    insert = ShotOperation(op="insert", fields=ShotFields(text="新", query="新"), clear=["recall"])
    with pytest.raises(ToolError, match="clear 只能用于 update"):
        plan_shots(view, [insert])
    bad = ShotOperation(op="update", shot="#1", fields=ShotFields(recall=ShotRecallArgs(mode="image")))
    with pytest.raises(ToolError) as exc:
        plan_shots(view, [bad])
    assert exc.value.code == "invalid_recall" and "查询文件" in str(exc.value)


def test_edit_shots_recall_preview_warns_and_persists(client, config, job):
    args = {
        "job_id": job,
        "operations": [
            {"op": "update", "shot": "#3", "fields": {"granularity": "moment", "recall_limit": 9}}
        ],
    }
    preview, _output, _ = run(client, config, "edit_shots", args)
    assert (
        preview.warnings and "#3" in preview.warnings[0] and "召回、选片、渲染及成片" in preview.warnings[0]
    )
    saved = content(workspace(client, job), SHOTS[2])
    assert saved["granularity"] == "moment" and saved["recall_limit"] == 9
    # 只改旁白不提示召回重建。
    text_only = {
        "job_id": job,
        "operations": [{"op": "update", "shot": "#1", "fields": {"query": "红色 幕布"}}],
    }
    preview, _, _ = run(client, config, "edit_shots", text_only, execute=False)
    assert preview.warnings == []


def test_edit_shots_relays_server_recall_rejection(client, config, job):
    before = workspace(client, job)["revision"]
    args = {
        "job_id": job,
        "operations": [
            {"op": "update", "shot": "#1", "fields": {"recall": {"person_ids": ["no-such-person"]}}}
        ],
    }
    with pytest.raises(ToolError) as exc:
        run(client, config, "edit_shots", args)
    assert "找不到对应人物" in str(exc.value)
    assert workspace(client, job)["revision"] == before


def test_rename_job(client, config, job):
    preview, output, sent = run(client, config, "rename_job", {"job_id": job, "title": "  秋天短片  "})
    assert preview.changes[0]["after"] == "秋天短片" and preview.changes[0]["before"] not in (
        None,
        "（未命名）",
    )
    assert output.value == {"job_id": job, "title": "秋天短片"} and output.mutation["job_id"] == job
    assert ("PATCH", f"/api/studio/jobs/{job}/title") in sent
    assert client.get(f"/api/studio/jobs/{job}").json()["job"]["title"] == "秋天短片"


def test_attach_query_media_uploads_then_sets_recall(client, config, job):
    ref = attachment(client, png(), "红色.png", "image/png")
    data = workspace(client, job)
    pid = data["active_plan"]
    preview, output, sent = run(
        client, config, "attach_query_media", {"job_id": job, "shot": "#2", "attachment": ref}
    )
    assert "图片召回" in preview.summary and "一次确认" in preview.summary
    assert preview.changes[0]["label"] == "上传查询文件" and "红色.png" in preview.changes[0]["after"]
    assert "红色.png" in preview.changes[1]["after"] and preview.warnings
    writes = [entry for entry in sent if entry[0] != "GET"]
    assert writes == [
        ("POST", f"/api/studio/jobs/{job}/plans/{pid}/query-media"),
        ("PATCH", f"/api/studio/jobs/{job}/plans/{pid}"),
    ]
    media_id = output.value["media_id"]
    after = workspace(client, job)
    recall = content(after, SHOTS[1])["recall"]
    assert recall["mode"] == "image" and recall["media_kind"] == "image" and recall["media_id"] == media_id
    assert content(after, SHOTS[1])["granularity"] == "scene"  # 其余召回覆盖保持不变
    assert content(after, SHOTS[0]) == content(data, SHOTS[0])


def test_attach_query_media_errors(client, config, job):
    before = workspace(client, job)["revision"]
    with pytest.raises(ToolError) as exc:
        run(
            client,
            config,
            "attach_query_media",
            {"job_id": job, "shot": "#1", "attachment": "attachment://" + "0" * 32},
        )
    assert exc.value.code == "attachment_invalid"
    # 类型无法推断时要求显式 kind；显式 kind 与文件不符时转述上传端点的 422。
    ref = attachment(client, b"not an image", "a.bin", "application/octet-stream")
    with pytest.raises(ToolError) as exc:
        run(client, config, "attach_query_media", {"job_id": job, "shot": "#1", "attachment": ref})
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(ToolError) as exc:
        run(
            client,
            config,
            "attach_query_media",
            {"job_id": job, "shot": "#1", "attachment": ref, "kind": "image"},
        )
    assert exc.value.code == "http_422" and "查询文件无法解码" in str(exc.value)
    assert workspace(client, job)["revision"] == before


# ------------------------------------------------------------ 16.3 集成：服务端拒绝原因的转述
def edit(client, config, job, operations, **kwargs):
    return run(client, config, "edit_shots", {"job_id": job, "operations": operations}, **kwargs)


def set_recall(client, config, job, shot, recall):
    edit(client, config, job, [{"op": "update", "shot": shot, "fields": {"recall": recall}}])


def patches(sent) -> list:
    return [entry for entry in sent if entry[0] == "PATCH"]


def rejected(client, config, job, recall, shot="#1") -> ToolError:
    """经 edit_shots 提交召回设置，断言被拒且工作台 revision 不变；返回 ToolError。"""
    before = workspace(client, job)["revision"]
    with pytest.raises(ToolError) as exc:
        edit(client, config, job, [{"op": "update", "shot": shot, "fields": {"recall": recall}}])
    assert workspace(client, job)["revision"] == before
    return exc.value


def actor(client, name="演员甲") -> str:
    response = client.post("/api/persons/entities", json={"kind": "actor", "name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def limit_scope(config, job, source_ids):
    """把任务限定到 source_ids（创建后只读的任务素材范围；直接写 job.json，免去登记真实素材）。"""
    path = studio.job_path(config, job)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["source_ids"] = source_ids
    studio.save(path, state)


def recall_signature(config, job, sid) -> str:
    w = ws.load(config, job)
    return ws.specification(config, job, w, ws.plan(w, w["active_plan"]), "recall", sid)["signature"]


def test_edit_shots_relays_person_rejections(client, config, job):
    work = client.post("/api/persons/entities", json={"kind": "work", "name": "某部作品"}).json()["id"]
    error = rejected(client, config, job, {"person_ids": [work]})
    assert error.code == "http_422" and "人物召回不能选择作品，请选择演员或角色" in str(error)
    # 有效演员与不存在实体混在一起：整份请求被拒。
    ok = actor(client)
    error = rejected(client, config, job, {"person_ids": [ok, "ghost"], "person_relation": "any"})
    assert error.code == "http_422" and "找不到对应人物" in str(error)
    # 图片/视频召回不支持人物：本地校验即拒绝，不发 PATCH。
    before = workspace(client, job)["revision"]
    with pytest.raises(ToolError) as exc:
        edit(
            client,
            config,
            job,
            [
                {
                    "op": "update",
                    "shot": "#1",
                    "fields": {"recall": {"mode": "image", "media_id": "a" * 64, "person_ids": [ok]}},
                }
            ],
        )
    assert exc.value.code == "invalid_recall" and "图片/视频召回不支持人物设置" in str(exc.value)
    assert workspace(client, job)["revision"] == before
    # onscreen / mentioned 模式下已登记演员被接受并原样保存。
    for mode in ("onscreen", "mentioned"):
        set_recall(client, config, job, "#1", {"person_ids": [ok], "person_mode": mode})
        saved = content(workspace(client, job), SHOTS[0])["recall"]
        assert saved["person_ids"] == [ok] and saved["person_mode"] == mode


def test_edit_shots_relays_source_scope_rejections(client, config, job):
    limit_scope(config, job, ["src-a"])
    error = rejected(client, config, job, {"filters": {"source_ids": ["src-a", "src-b"]}})
    assert error.code == "http_422" and "素材超出本任务允许的素材范围：src-b" in str(error)
    # 旧版单素材字段同样受范围约束；不存在的素材 ID 也在范围之外。
    error = rejected(client, config, job, {"filters": {"source_id": "ghost-source"}})
    assert "素材超出本任务允许的素材范围：ghost-source" in str(error)
    # 范围之内被接受。
    set_recall(client, config, job, "#1", {"filters": {"source_ids": ["src-a"]}})
    assert content(workspace(client, job), SHOTS[0])["recall"]["filters"]["source_ids"] == ["src-a"]


def test_edit_shots_relays_query_media_rejections(client, config, job):
    # 查询文件不存在：服务端「查询文件缺失」。
    error = rejected(client, config, job, {"mode": "image", "media_id": "f" * 64})
    assert error.code == "http_422" and "查询文件缺失，请先上传查询图片或视频" in str(error)
    # media_kind 与 mode 不符：本地校验拒绝，不发 PATCH。
    before = workspace(client, job)["revision"]
    with pytest.raises(ToolError) as exc:
        edit(
            client,
            config,
            job,
            [
                {
                    "op": "update",
                    "shot": "#1",
                    "fields": {"recall": {"mode": "image", "media_id": "f" * 64, "media_kind": "video"}},
                }
            ],
        )
    assert exc.value.code == "invalid_recall" and "对应类型的查询文件" in str(exc.value)
    assert workspace(client, job)["revision"] == before


def test_attach_query_media_relays_upload_rejections(client, config, job, video_file):
    before = workspace(client, job)["revision"]

    def attach(ref, **extra):
        with pytest.raises(ToolError) as exc:
            run(
                client,
                config,
                "attach_query_media",
                {"job_id": job, "shot": "#1", "attachment": ref, **extra},
            )
        return exc.value

    video = attachment(client, video_file.read_bytes(), "片段.mp4", "video/mp4")
    # 显式 kind 与文件类型不符：视频当图片上传，解码失败。
    error = attach(video, kind="image")
    assert error.code == "http_422" and "查询文件无法解码" in str(error)
    # 视频超过 max_query_seconds。
    config.search.max_query_seconds = 0.5
    error = attach(video)
    assert error.code == "http_422" and "查询视频不能超过 0.5 秒" in str(error)
    config.search.max_query_seconds = 30
    # 图片超过上传大小上限（临时调到 1 MiB；随机噪声 PNG 约 3 MiB）。
    noise = io.BytesIO()
    Image.frombytes("RGB", (1024, 1024), os.urandom(1024 * 1024 * 3)).save(noise, "PNG")
    assert len(noise.getvalue()) > 1024 * 1024
    big = attachment(client, noise.getvalue(), "大图.png", "image/png")
    config.search.max_image_upload_mb = 1
    error = attach(big)
    assert error.code == "http_413" and "上传文件过大" in str(error)
    assert workspace(client, job)["revision"] == before
    # 被拒的上传不在 inputs/ 留文件。
    inputs = ws.root(config, job) / "inputs"
    assert not inputs.exists() or not any(inputs.iterdir())


# ------------------------------------------------------------ 16.3 集成：attach_query_media 完整链路
def test_attach_query_media_video_path(client, config, job, video_file):
    ref = attachment(client, video_file.read_bytes(), "片段.mp4", "video/mp4")
    preview, output, sent = run(
        client, config, "attach_query_media", {"job_id": job, "shot": "#3", "attachment": ref}
    )
    assert "上传视频查询文件" in preview.summary and "视频召回" in preview.summary
    assert "视频" in preview.changes[0]["after"] and preview.changes[1]["after"].startswith("视频召回")
    assert len(patches(sent)) == 1
    media_id = output.value["media_id"]
    assert output.value["kind"] == "video" and output.value["shot_id"] == SHOTS[2]
    recall = content(workspace(client, job), SHOTS[2])["recall"]
    assert recall["mode"] == "video" and recall["media_kind"] == "video" and recall["media_id"] == media_id
    assert (ws.root(config, job) / "inputs" / f"{media_id}.mp4").is_file()


def test_attach_query_media_drops_persons_keeps_filters(client, config, job):
    person = actor(client)
    filters = {"source_ids": ["src-a"], "min_duration": 2, "max_duration": 8, "scene": "室内"}
    set_recall(
        client, config, job, "#1", {"person_ids": [person], "person_mode": "onscreen", "filters": filters}
    )
    ref = attachment(client, png(), "红色.png", "image/png")
    preview, output, _ = run(
        client, config, "attach_query_media", {"job_id": job, "shot": "#1", "attachment": ref}
    )
    assert any("原有人物条件会被移除" in warning for warning in preview.warnings)
    change = preview.changes[1]
    assert f"人物 {person}" in change["before"]
    assert "人物" not in change["after"]
    for part in ("限定素材 src-a", "时长 ≥ 2 秒", "时长 ≤ 8 秒", "场景 室内"):
        assert part in change["after"]
    recall = content(workspace(client, job), SHOTS[0])["recall"]
    assert recall["mode"] == "image" and recall["media_id"] == output.value["media_id"]
    assert (
        recall["person_ids"] == [] and recall["person_mode"] == "auto" and recall["person_relation"] == "all"
    )
    assert recall["filters"]["source_ids"] == ["src-a"] and recall["filters"]["scene"] == "室内"
    assert recall["filters"]["min_duration"] == 2 and recall["filters"]["max_duration"] == 8
    # 无人物条件的分镜不出现该提示。
    preview, _, _ = run(
        client, config, "attach_query_media", {"job_id": job, "shot": "#2", "attachment": ref}, execute=False
    )
    assert not any("人物" in warning for warning in preview.warnings)


def test_attach_query_media_patch_conflict_reports_media_id(client, config, job):
    ref = attachment(client, png(), "红色.png", "image/png")

    def concurrent_edit():
        # 确认期间用户在页面上改了别的分镜：revision 变化。
        w = ws.load(config, job)
        p = ws.plan(w, w["active_plan"])
        order = w["versions"][p["board"]]["data"]["order"]
        shots = [studio.Shot(**w["versions"][p["shots"][sid]["content"]]["data"]) for sid in order]
        shots[2] = shots[2].model_copy(update={"query": "绿色 草地"})
        ws.set_board(config, job, w, p, shots)
        p["board_script"] = p["script"]
        ws.persist(config, job, w)

    with pytest.raises(ToolError) as exc:
        run(
            client,
            config,
            "attach_query_media",
            {"job_id": job, "shot": "#1", "attachment": ref},
            between=concurrent_edit,
        )
    media_id = exc.value.value["media_id"]
    assert exc.value.code == "http_422" and "方案已变化" in str(exc.value)
    assert f"media_id={media_id}" in str(exc.value) and "edit_shots" in str(exc.value)
    # 查询文件已落盘，召回未改；用户的并发修改保留。
    assert (ws.root(config, job) / "inputs" / f"{media_id}.png").is_file()
    after = workspace(client, job)
    assert content(after, SHOTS[0])["recall"] is None and content(after, SHOTS[2])["query"] == "绿色 草地"
    # 按提示用 edit_shots 引用已上传的 media_id 即可完成。
    set_recall(client, config, job, "#1", {"mode": "image", "media_id": media_id})
    assert content(workspace(client, job), SHOTS[0])["recall"]["media_id"] == media_id


def test_attach_query_media_dedupes_same_file(client, config, job):
    data = png()
    first = run(
        client,
        config,
        "attach_query_media",
        {"job_id": job, "shot": "#1", "attachment": attachment(client, data, "a.png", "image/png")},
    )[1]
    second = run(
        client,
        config,
        "attach_query_media",
        {"job_id": job, "shot": "#3", "attachment": attachment(client, data, "b.png", "image/png")},
    )[1]
    media_id = first.value["media_id"]
    assert second.value["media_id"] == media_id
    assert len(list((ws.root(config, job) / "inputs").glob(media_id + ".*"))) == 1
    after = workspace(client, job)
    assert (
        content(after, SHOTS[0])["recall"]["media_id"]
        == content(after, SHOTS[2])["recall"]["media_id"]
        == media_id
    )


# ------------------------------------------------------------ 16.3 集成：clear 语义
def test_clear_recall_restores_default_text_recall(client, config, job):
    default = recall_signature(config, job, SHOTS[0])
    set_recall(client, config, job, "#1", {"filters": {"min_duration": 2}})
    assert recall_signature(config, job, SHOTS[0]) != default
    preview, _, _ = edit(client, config, job, [{"op": "update", "shot": "#1", "clear": ["recall"]}])
    change = preview.changes[0]
    assert "恢复" in change["label"] and "召回设置" in change["label"]
    assert "时长 ≥ 2 秒" in change["before"] and change["after"] == "文字召回（默认）"
    assert preview.warnings and RECALL_REBUILD in preview.warnings[0]
    assert content(workspace(client, job), SHOTS[0])["recall"] is None
    # 恢复默认后召回签名与从未设置时逐字节一致。
    assert recall_signature(config, job, SHOTS[0]) == default


def test_clear_granularity_and_limit_follow_defaults(client, config, job):
    original = recall_signature(config, job, SHOTS[1])
    preview, _, _ = edit(
        client, config, job, [{"op": "update", "shot": "#2", "clear": ["granularity", "recall_limit"]}]
    )
    changes = {c["label"].rsplit("的", 1)[1]: c for c in preview.changes}
    assert changes["召回粒度"]["before"] == "场景" and changes["召回粒度"]["after"] == "跟随服务配置"
    assert changes["召回数量"]["before"] == 5 and changes["召回数量"]["after"] == "跟随方案"
    assert preview.warnings and "#2" in preview.warnings[0]
    saved = content(workspace(client, job), SHOTS[1])
    assert saved["granularity"] is None and saved["recall_limit"] is None
    # 召回节点签名变化 → 下次构建需重建召回；改回原值后签名复原。
    cleared = recall_signature(config, job, SHOTS[1])
    assert cleared != original
    edit(
        client,
        config,
        job,
        [{"op": "update", "shot": "#2", "fields": {"granularity": "scene", "recall_limit": 5}}],
    )
    assert recall_signature(config, job, SHOTS[1]) == original
    # 对未设置覆盖的分镜 clear：值不变，签名不变，不提示重建。
    third = recall_signature(config, job, SHOTS[2])
    preview, _, _ = edit(client, config, job, [{"op": "update", "shot": "#3", "clear": ["granularity"]}])
    assert preview.warnings == [] and recall_signature(config, job, SHOTS[2]) == third


def test_clear_visual_removes_key(client, config, job):
    edit(client, config, job, [{"op": "update", "shot": "#1", "fields": {"visual": "红色幕布缓缓拉开"}}])
    assert content(workspace(client, job), SHOTS[0])["visual"] == "红色幕布缓缓拉开"
    preview, _, _ = edit(client, config, job, [{"op": "update", "shot": "#1", "clear": ["visual"]}])
    assert (
        preview.changes[0]["label"].startswith("清空") and preview.changes[0]["before"] == "红色幕布缓缓拉开"
    )
    assert preview.changes[0]["after"] is None and preview.warnings == []
    assert "visual" not in content(workspace(client, job), SHOTS[0])


def test_split_inherits_unset_recall_fields(client, config, job):
    set_recall(client, config, job, "#2", {"filters": {"min_duration": 1}})
    edit(
        client,
        config,
        job,
        [
            {
                "op": "split",
                "shot": "#2",
                "parts": [
                    {"text": "蓝色", "recall_limit": 8},
                    {"text": "中段。", "recall": {"filters": {"max_duration": 6}}},
                ],
            }
        ],
    )
    data = workspace(client, job)
    order = PlanView(data).order
    assert len(order) == 4 and order[1] == SHOTS[1]
    first, second = content(data, order[1]), content(data, order[2])
    assert first["granularity"] == "scene" and first["recall_limit"] == 8
    assert first["recall"]["filters"]["min_duration"] == 1
    assert second["granularity"] == "scene" and second["recall_limit"] == 5
    # recall 整体替换：第二段只有 max_duration，不保留原镜的 min_duration。
    assert (
        second["recall"]["filters"]["max_duration"] == 6 and second["recall"]["filters"]["min_duration"] == 0
    )


def test_merge_inherits_recall_when_previous_is_default(client, config, job):
    set_recall(client, config, job, "#2", {"filters": {"min_duration": 3}})
    edit(client, config, job, [{"op": "merge", "shots": ["#1", "#2"]}])
    data = workspace(client, job)
    assert PlanView(data).order == [SHOTS[0], SHOTS[2]]
    merged = content(data, SHOTS[0])
    assert merged["text"] == "红色开场。蓝色中段。" and merged["recall"]["filters"]["min_duration"] == 3
    # 前一镜已有非默认召回：保留前一镜。
    set_recall(client, config, job, "#2", {"filters": {"max_duration": 9}})
    shots, _, _ = plan_shots(
        PlanView(workspace(client, job)), [ShotOperation(op="merge", shots=["#1", "#2"])]
    )
    assert (
        shots[0]["recall"]["filters"]["min_duration"] == 3
        and shots[0]["recall"]["filters"]["max_duration"] is None
    )
