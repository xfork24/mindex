"""人物审核、发布、检索组合与演员表导入工具：经进程内回环调用真实 /api/persons 接口。"""

from __future__ import annotations

import json

import pytest
from test_agent_persons import REF, call, evidence, seed_shots
from test_person_identify import photo, seed_library

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolError
from mindex.agent.tools import build_registry
from mindex.shared.storage import Store, dumps

TOOLS = {"list_review_shots": "read", "review_shot": "write", "publish_persons": "danger",
         "list_search_releases": "read", "change_search_release": "danger", "import_cast": "write",
         "confirm_cast_import": "write"}


@pytest.fixture
def library(config, monkeypatch):
    return seed_library(config, monkeypatch)


def test_specs_risk_and_domain():
    registry = build_registry()
    for name, risk in TOOLS.items():
        spec = registry.get(name)
        assert spec.domain == "persons" and spec.risk == risk
        assert (spec.preview is not None) == (risk != "read")
    # 选择必须带角色：参数模型层面即拒绝。
    with pytest.raises(ToolError, match="role_id"):
        registry.get("review_shot").parse({"source_id": "s", "shot_id": "x", "selections": [{"actor_id": "a"}]})


def test_list_and_review_shot(library, config):
    client, ids, _refs = library
    store = seed_shots(config, 2)
    evidence(store, "ev0", "shot0", ids["alice"], ids["young"], status="candidate")
    evidence(store, "ev1", "shot1", ids["bob"], ids["villain"], status="candidate")

    listed = call(client, "list_review_shots", {"status": "candidate"})
    assert {s["shot_id"] for s in listed.value["items"]} == {"shot0", "shot1"}
    assert "演员甲·青年叶淑柔" in listed.text and listed.media
    # 只给角色：唯一出演演员自动补全。
    only = call(client, "list_review_shots", {"role_id": ids["young"]})
    assert [s["shot_id"] for s in only.value["items"]] == ["shot0"]

    # 演员与角色关系不符：本地预检拒绝，不发请求。
    wrong = {"source_id": "source", "shot_id": "shot0",
             "selections": [{"actor_id": ids["alice"], "role_id": ids["villain"]}]}
    for preview in (True, False):
        with pytest.raises(ToolError, match="演员与作品角色关系不符") as excinfo:
            call(client, "review_shot", wrong, preview=preview)
        assert excinfo.value.code == "invalid_selection"

    good = {"source_id": "source", "shot_id": "shot0",
            "selections": [{"actor_id": ids["alice"], "role_id": ids["young"]}]}
    preview = call(client, "review_shot", good, preview=True)
    assert preview.changes[0]["before"].startswith("待确认") and "演员甲·青年叶淑柔" in preview.changes[0]["after"]
    out = call(client, "review_shot", good)
    assert out.mutation["domain"] == "persons" and "publish_persons" in out.text
    accepted = call(client, "list_review_shots", {"status": "accepted"})
    assert [s["shot_id"] for s in accepted.value["items"]] == ["shot0"]

    exclude = {"source_id": "source", "shot_id": "shot1", "selections": []}
    assert any("整镜排除" in w for w in call(client, "review_shot", exclude, preview=True).warnings)
    call(client, "review_shot", exclude)
    rejected = call(client, "list_review_shots", {"status": "rejected"})
    assert [s["shot_id"] for s in rejected.value["items"]] == ["shot1"]


def test_review_shot_relays_server_rejection(library, config):
    """预检之后角色关系被并发修改：服务端 422 的中文原因原样转述。"""
    client, ids, _refs = library
    seed_shots(config, 1)
    store = Store(config.data_dir)

    class Racing(MindexClient):
        async def post(self, path, body=None, **params):
            row = store.one("SELECT data FROM person_entities WHERE id=?", (ids["young"],))
            data = json.loads(row["data"]) | {"actor_ids": [ids["bob"]]}
            store.execute("UPDATE person_entities SET data=? WHERE id=?", (dumps(data), ids["young"]))
            return await super().post(path, body, **params)

    with pytest.raises(ToolError, match="演员与作品角色关系不符") as excinfo:
        call(client, "review_shot", {"source_id": "source", "shot_id": "shot0",
                                     "selections": [{"actor_id": ids["alice"], "role_id": ids["young"]}]},
             client_class=Racing)
    assert excinfo.value.code == "http_422"


def seed_index(config, identifier):
    store = Store(config.data_dir)
    spec = {"format": 2, "mode": "auto", "space": {"model": "fixture"}, "preprocessing": config.video.model_dump()}
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES(?,?,'ready')", (identifier, dumps(spec)))
    store.activate(identifier)
    return store


def test_publish_and_change_release(library, config):
    client, _ids, _refs = library
    preview = call(client, "publish_persons", {}, preview=True)
    assert any("没有活动内容索引" in w for w in preview.warnings)
    with pytest.raises(ToolError, match="请先发布内容索引") as excinfo:
        call(client, "publish_persons", {})
    assert excinfo.value.code == "http_422"

    store = seed_index(config, "content1")
    first = store.active_index()["search_release_id"]
    preview = call(client, "publish_persons", {}, preview=True)
    assert any("切换当前生效的检索组合" in w for w in preview.warnings)
    published = call(client, "publish_persons", {}).value
    second = published["release_id"]
    assert second != first and store.active_index()["search_release_id"] == second

    listed = call(client, "list_search_releases", {})
    assert {r["id"] for r in listed.value["releases"]} == {first, second}
    assert next(r for r in listed.value["releases"] if r["active"])["id"] == second

    # 当前生效组合受保护：预览即拒绝；直接执行时转述服务端原因。
    with pytest.raises(ToolError, match="当前生效的检索组合") as excinfo:
        call(client, "change_search_release", {"release_id": second, "action": "delete"}, preview=True)
    assert excinfo.value.code == "release_active"
    with pytest.raises(ToolError, match="当前生效的检索组合") as excinfo:
        call(client, "change_search_release", {"release_id": second, "action": "delete"})
    assert excinfo.value.code == "http_422"

    preview = call(client, "change_search_release", {"release_id": first[:8], "action": "activate"}, preview=True)
    assert preview.changes[0]["after"] == first[:8]
    out = call(client, "change_search_release", {"release_id": first, "action": "activate"})
    assert out.mutation["domain"] == "settings" and store.active_index()["search_release_id"] == first
    call(client, "change_search_release", {"release_id": second, "action": "delete"})
    remaining = call(client, "list_search_releases", {}).value["releases"]
    assert [r["id"] for r in remaining] == [first]


def test_import_and_confirm_cast(library, monkeypatch):
    client, ids, _refs = library

    def parsed(_config, image):
        return {"entries": [
            {"image_id": image["id"], "actor_name": "演员甲", "foreign_name": "", "role_name": "青年叶淑柔",
             "actor_id": None, "role_id": None, "bbox": None, "single_person": False},
            {"image_id": image["id"], "actor_name": "演员丁", "foreign_name": "Ding", "role_name": "侦探",
             "actor_id": None, "role_id": None, "bbox": None, "single_person": False},
        ], "warnings": ["条目 3 为合照，未拆分"]}

    monkeypatch.setattr("mindex.persons.person_api.parse_cast", parsed)
    image = ("cast.png", photo("teal"), "image/png")
    out = call(client, "import_cast", {"attachments": [REF]}, attachment=image)
    draft = out.value["import_id"]
    assert len(out.value["entries"]) == 2 and "合照" in out.text and out.media
    assert out.media[0]["url"].endswith("?thumbnail=true")
    # 附件不可解析：不发送请求。
    with pytest.raises(ToolError) as excinfo:
        call(client, "import_cast", {"attachments": [REF]})
    assert excinfo.value.code == "attachment_invalid"

    work = next(s for s in client.get("/api/persons").json()["sources"] if s["id"] == "source")["work_id"]
    with pytest.raises(ToolError, match="不是作品"):
        call(client, "confirm_cast_import", {"import_id": draft, "work_id": ids["alice"]}, preview=True)
    preview = call(client, "confirm_cast_import", {"import_id": draft, "work_id": work}, preview=True)
    assert "复用「演员甲」" in preview.changes[0]["after"] and "复用「青年叶淑柔」" in preview.changes[0]["after"]
    assert "新建「演员丁」" in preview.changes[1]["after"] and "新建「侦探」" in preview.changes[1]["after"]

    result = call(client, "confirm_cast_import", {"import_id": draft, "work_id": work, "exclude": [1]})
    assert len(result.value["confirmed"]) == 1 and "演员丁·侦探" in result.text
    entities = client.get("/api/persons").json()["entities"]
    assert any(e["name"] == "演员丁" and e["aliases"] == ["Ding"] for e in entities)
    with pytest.raises(ToolError, match="演员表已确认") as excinfo:
        call(client, "confirm_cast_import", {"import_id": draft, "work_id": work})
    assert excinfo.value.code == "import_confirmed"
