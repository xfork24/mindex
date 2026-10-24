"""人物领域工具：经进程内回环调用真实 /api/persons 接口；人物服务使用确定性桩。"""

from __future__ import annotations

import asyncio

import pytest
from test_person_identify import StubPersonClient, face, photo, seed_library

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.shared.storage import Store

REF = "attachment://" + "a" * 32


@pytest.fixture
def library(config, monkeypatch):
    return seed_library(config, monkeypatch)


def call(client, name, arguments, *, preview=False, attachment=None, client_class=MindexClient):
    """执行（或只预览）一个工具；attachment 为附件引用解析出的 (文件名, 内容, 类型)。"""
    spec = build_registry().get(name)

    async def go():
        loop = client_class(client.app, resolve_attachment=(lambda ref: attachment) if attachment else None)
        try:
            ctx = ToolContext(client=loop, page={"page": "persons"})
            args = spec.parse(arguments)
            return await (spec.preview(args, ctx) if preview else spec.execute(args, ctx))
        finally:
            await loop.aclose()

    return asyncio.run(go())


def test_specs_risk_and_domain():
    registry = build_registry()
    risks = {name: registry.get(name).risk for name in (
        "get_person", "save_person", "delete_person", "assign_source_work", "add_person_reference",
        "remove_person_reference", "identify_person")}
    assert risks == {"get_person": "read", "save_person": "write", "delete_person": "danger",
                     "assign_source_work": "write", "add_person_reference": "write",
                     "remove_person_reference": "danger", "identify_person": "read"}
    for name in risks:
        spec = registry.get(name)
        assert spec.domain == "persons"
        assert (spec.preview is not None) == (spec.risk != "read")
    # 不开放删除演员表导入图片（物理删除）。
    assert not any("import" in s.name and "delete" in s.name for s in registry.all())


def test_get_and_save_person(library):
    client, ids, _refs = library
    out = call(client, "get_person", {"person_id": ids["alice"]})
    assert {r["id"] for r in out.value["roles"]} == {ids["young"], ids["old"]}
    assert out.value["linked_shots"]["total"] == 0 and out.media

    preview = call(client, "save_person", {"person_id": ids["young"], "aliases": ["小叶"]}, preview=True)
    assert preview.changes == [{"label": "别名", "before": "无", "after": "小叶"}]
    out = call(client, "save_person", {"person_id": ids["young"], "aliases": ["小叶"]})
    assert out.value["entity"]["aliases"] == ["小叶"] and out.value["entity"]["actor_ids"] == [ids["alice"]]
    assert out.mutation["domain"] == "persons"

    created = call(client, "save_person", {"kind": "actor", "name": "演员丙"}).value["entity"]
    assert created["kind"] == "actor" and created["name"] == "演员丙"
    with pytest.raises(ToolError, match="角色至少归属一部作品"):
        call(client, "save_person", {"kind": "role", "name": "无作品角色"})


def test_delete_person_rules(library):
    client, ids, _refs = library
    work = call(client, "get_person", {"person_id": ids["young"]}).value["entity"]["work_ids"][0]
    with pytest.raises(ToolError, match="不可删除") as excinfo:
        call(client, "delete_person", {"person_id": work}, preview=True)
    assert excinfo.value.code == "work_not_deletable"
    # 执行路径同样在发请求前拒绝，作品仍在。
    with pytest.raises(ToolError, match="不可删除") as excinfo:
        call(client, "delete_person", {"person_id": work})
    assert excinfo.value.code == "work_not_deletable"
    assert any(e["id"] == work for e in client.get("/api/persons").json()["entities"])
    preview = call(client, "delete_person", {"person_id": ids["villain"]}, preview=True)
    assert any("参考图" in w for w in preview.warnings)
    out = call(client, "delete_person", {"person_id": ids["villain"]})
    assert out.value["removed_references"] == 1
    with pytest.raises(ToolError, match="找不到"):
        call(client, "get_person", {"person_id": ids["villain"]})


def seed_shots(config, count):
    store = Store(config.data_dir)
    for n in range(count):
        store.execute(
            "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"shot{n}", "source", "shot", n, n + 1, "source.mp4", "source.mp4", "gen", None, "fixture"),
        )
    return store


def evidence(store, identifier, shot, actor_id, role_id, status="accepted"):
    store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (identifier, "source", shot, actor_id, role_id, None, 0, 1, status, "{}"),
    )


def test_delete_person_with_linked_shots_relays_examples(library, config):
    client, ids, _refs = library
    store = seed_shots(config, 7)
    for n in range(7):
        evidence(store, f"ev{n}", f"shot{n}", ids["alice"], ids["young"])

    detail = call(client, "get_person", {"person_id": ids["young"]})
    assert detail.value["linked_shots"]["total"] == 7 and len(detail.value["linked_shots"]["items"]) == 5
    assert "关联镜头（已接受或待核对）：7 个" in detail.text

    for preview in (True, False):
        with pytest.raises(ToolError) as excinfo:
            call(client, "delete_person", {"person_id": ids["alice"]}, preview=preview)
        error = excinfo.value
        assert error.code == "person_has_linked_shots"
        message = str(error)
        assert "仍有 7 个未排除的关联镜头" in message and "影片 0.0–1.0s（分镜 shot0）" in message
        # 最多转述 5 个示例，并给出处理办法。
        assert message.count("（分镜 shot") == 5 and "shot5" not in message and "处理与审核" in message
        assert error.value["linked_shots"]["total"] == 7
    # 已排除的证据不再拦截：角色变为可删除。
    store.execute("UPDATE person_evidence SET status='rejected'")
    assert call(client, "delete_person", {"person_id": ids["young"]}).value["deleted"] == ids["young"]


def test_delete_person_relays_server_rejection_after_concurrent_link(library, config):
    """预检之后、删除请求之前出现新的关联镜头：服务端 422 的中文原因（含示例）原样转述。"""
    client, ids, _refs = library
    store = seed_shots(config, 1)

    class Racing(MindexClient):
        async def delete(self, path):
            evidence(store, "late", "shot0", ids["bob"], ids["villain"])
            return await super().delete(path)

    call(client, "delete_person", {"person_id": ids["villain"]}, preview=True)
    with pytest.raises(ToolError) as excinfo:
        call(client, "delete_person", {"person_id": ids["villain"]}, client_class=Racing)
    assert excinfo.value.code == "http_422"
    assert "该身份仍有关联出镜证据：1 个未排除分镜" in str(excinfo.value)
    assert "影片 0.000–1.000 秒（分镜 shot0）" in str(excinfo.value)
    assert any(e["id"] == ids["villain"] for e in client.get("/api/persons").json()["entities"])


def test_remove_reference_and_assign_work(library):
    client, ids, refs = library
    preview = call(client, "remove_person_reference", {"reference_id": refs["bob"]["id"]}, preview=True)
    assert "演员乙" in preview.summary
    call(client, "remove_person_reference", {"reference_id": refs["bob"]["id"]})
    assert all(r["id"] != refs["bob"]["id"] for r in client.get("/api/persons").json()["references"])

    work = call(client, "save_person", {"kind": "work", "name": "续集"}).value["entity"]
    preview = call(client, "assign_source_work", {"source_id": "source", "work_id": work["id"]}, preview=True)
    assert preview.changes[0]["after"] == "续集" and preview.warnings
    call(client, "assign_source_work", {"source_id": "source", "work_id": work["id"]})
    source = next(s for s in client.get("/api/persons").json()["sources"] if s["id"] == "source")
    assert source["work_id"] == work["id"]
    with pytest.raises(ToolError, match="不是作品"):
        call(client, "assign_source_work", {"source_id": "source", "work_id": ids["alice"]}, preview=True)


def test_add_reference_from_attachment_and_quality_failure(library):
    client, ids, _refs = library
    image = ("ref.png", photo("purple"), "image/png")
    StubPersonClient.observations = [face([1, 0, 0])]
    out = call(client, "add_person_reference", {"role_id": ids["young"], "attachment": REF}, attachment=image)
    assert out.value["reference"]["role_id"] == ids["young"] and out.media
    # 质检不合格（没有人脸）：服务端中文原因原样转述并附处理建议。
    StubPersonClient.observations = []
    with pytest.raises(ToolError, match="恰好一张清晰人脸") as excinfo:
        call(client, "add_person_reference", {"actor_id": ids["bob"], "attachment": REF},
             attachment=("x.png", photo("navy"), "image/png"))
    assert excinfo.value.code == "http_422"
    # 附件引用无法解析：不发送请求，转为 attachment_invalid。
    with pytest.raises(ToolError) as excinfo:
        call(client, "add_person_reference", {"actor_id": ids["bob"], "attachment": REF})
    assert excinfo.value.code == "attachment_invalid"


def test_reference_quality_failures_relayed(library, config):
    """不同质检原因（多张人脸、清晰度低于门槛、人物服务不可用）都原样转述并附处理建议。"""
    client, ids, _refs = library
    before = len(client.get("/api/persons").json()["references"])

    def add(color):
        return call(client, "add_person_reference", {"actor_id": ids["bob"], "attachment": REF},
                    attachment=(f"{color}.png", photo(color), "image/png"))

    StubPersonClient.observations = [face([1, 0, 0]), face([0, 1, 0], box=(50, 10, 80, 40))]
    with pytest.raises(ToolError) as excinfo:
        add("olive")
    message = str(excinfo.value)
    assert excinfo.value.code == "http_422" and "恰好一张清晰人脸，且属于画面主体" in message
    assert "请换一张单人、正脸清晰的图片" in message

    config.person = config.person.model_copy(update={"min_reference_quality": 0.5})
    StubPersonClient.observations = [face([1, 0, 0], quality=0.2)]
    with pytest.raises(ToolError, match=r"清晰度 0\.20 低于门槛 0\.50") as excinfo:
        add("maroon")
    assert excinfo.value.code == "http_422"

    StubPersonClient.fail = "identity"
    with pytest.raises(ToolError, match="人物服务不可用：connection refused") as excinfo:
        add("silver")
    assert excinfo.value.code == "http_503" and "设置页检查人物模型" in str(excinfo.value)
    # 质检不合格的图片不会留下参考图记录。
    assert len(client.get("/api/persons").json()["references"]) == before


def test_identify_person(library):
    client, ids, _refs = library
    StubPersonClient.observations = [face([1, 0, 0])]
    out = call(client, "identify_person", {"attachment": REF, "top_k": 2},
               attachment=("q.png", photo(), "image/png"))
    assert out.value["faces"][0]["accepted_role"]["id"] == ids["young"]
    assert len(out.value["faces"][0]["roles"]) == 2
    assert "青年叶淑柔" in out.text and out.value["images"][0] == REF
    assert out.media[0]["url"] == f"/api/agent/attachments/{'a' * 32}/content"
    StubPersonClient.fail = "identity"
    with pytest.raises(ToolError, match="人物服务不可用") as excinfo:
        call(client, "identify_person", {"attachment": REF}, attachment=("q.png", photo(), "image/png"))
    assert excinfo.value.code == "http_503"
