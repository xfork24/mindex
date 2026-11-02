"""媒体组迁移、事务管理、动态范围、召回签名、备份合并与 Agent/MCP。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from mindex.agent.loopback import MindexClient
from mindex.agent.mcp_server import exported
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.maintenance.backup_db import filter_database, merge_database, snapshot, validate_database
from mindex.maintenance.backup_files import referenced_files
from mindex.search.search import SearchEngine
from mindex.shared.media_groups import Groups
from mindex.shared.storage import Store, dumps
from mindex.shared.types import Filters
from mindex.studio import music_library, studio
from mindex.studio import workspace as ws


@pytest.fixture
def data(config):
    store = Store(config.data_dir)
    for source_id in ("a", "b", "c"):
        store.execute(
            "INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)",
            (
                source_id,
                f"素材 {source_id}",
                f"sources/{source_id}.mp4",
                dumps({"duration": 5, "width": 160, "height": 120, "fps": 25}),
                "layout",
            ),
        )
    for i, track_id in enumerate(("1" * 64, "2" * 64, "3" * 64)):
        store.execute(
            "INSERT INTO music_tracks(id,suffix,title,source,license,attribution,duration,created_at,updated_at,"
            "expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                track_id,
                ".wav",
                f"音乐 {i}",
                "自制",
                "CC0",
                "作者",
                5,
                "2026-01-01",
                "2026-01-01",
                "2000-01-01" if i == 2 else None,
            ),
        )
    return config, store, Groups(store)


def test_crud_atomic_membership_and_cleanup(data):
    _, store, groups = data
    first = groups.save("source", "  城市  ", "原文 description")
    second = groups.save("source", "夜景")
    assert first["name"] == "城市"
    with pytest.raises(ValueError, match="已存在"):
        groups.save("source", "城市 ")
    music = groups.save("music", "城市")
    assert groups.change_members(first["id"], ["a", "a", "b"])["member_count"] == 2
    assert groups.change_members(second["id"], ["b", "c"])["member_count"] == 2
    assert groups.resolve("source", [first["id"], second["id"]]) == {"a", "b", "c"}
    assert Filters(source_ids=["b"], source_group_ids=[first["id"], second["id"]]).effective_sources(
        store
    ) == {"b"}
    with pytest.raises(ValueError, match="类型不符"):
        groups.change_members(first["id"], ["c", "1" * 64])
    assert groups.resolve("source", [first["id"]]) == {"a", "b"}  # 整批回滚
    with pytest.raises(ValueError, match="类型不符"):
        groups.set_groups("source", "a", [second["id"], music["id"]])
    assert [g["id"] for g in groups.for_member("source", "a")] == [first["id"]]
    groups.set_groups("source", "a", [first["id"], second["id"], second["id"]])
    assert len(groups.for_member("source", "a")) == 2
    assert groups.members(first["id"], page_size=1)["member_count"] == 2
    assert len(groups.members(first["id"], page=2, page_size=1)["items"]) == 1
    before = groups.state("source", [first["id"]])
    store.execute("DELETE FROM sources WHERE id=?", ("b",))
    assert groups.state("source", [first["id"]]) != before
    groups.delete(first["id"])
    assert store.one("SELECT id FROM sources WHERE id=?", ("a",))
    assert groups.state("source", [first["id"]]) == [{"id": first["id"], "invalid": "missing"}]
    with pytest.raises(ValueError, match="重新选择"):
        groups.resolve("source", [first["id"]])


def test_search_snapshots_never_expand_empty_or_hidden(data):
    config, store, groups = data
    group = groups.save("source", "范围")["id"]
    engine = SearchEngine(config)
    snapshots = [
        {
            "id": f"shot-{sid}",
            "source_id": sid,
            "kind": "shot",
            "start": 0,
            "end": 3,
            "annotation": {},
            "views": {},
            "name": sid,
        }
        for sid in ("a", "b", "c")
    ]
    engine.store.snapshots_shared = lambda _: snapshots
    filters = Filters(source_group_ids=[group])
    assert engine._snapshots({"id": "index"}, "shot", filters) == []
    groups.change_members(group, ["a", "b"])
    assert {s["source_id"] for s in engine._snapshots({"id": "index"}, "shot", filters)} == {"a", "b"}
    store.execute("INSERT INTO source_curation(id,hidden) VALUES(?,1)", ("a",))
    assert {s["source_id"] for s in engine._snapshots({"id": "index"}, "shot", filters)} == {"b"}
    assert (
        engine._snapshots({"id": "index"}, "shot", Filters(source_ids=["c"], source_group_ids=[group])) == []
    )
    groups.delete(group)
    with pytest.raises(ValueError, match="已删除"):
        engine._snapshots({"id": "index"}, "shot", filters)


def test_music_union_search_recommend_expiry_and_empty(data):
    _, store, groups = data
    g1 = groups.save("music", "安静")["id"]
    g2 = groups.save("music", "热闹")["id"]
    assert music_library.list_tracks(store, music_group_ids=[g1]) == []
    groups.change_members(g1, ["1" * 64, "3" * 64])
    groups.change_members(g2, ["2" * 64])
    assert len(music_library.list_tracks(store, music_group_ids=[g1, g2])) == 3
    assert music_library.list_tracks(store, music_group_ids=[g1], query="音乐 1") == []
    assert [t["id"] for t in music_library.recommend(store, "安静的夜晚", music_group_ids=[g1])["items"]] == [
        "1" * 64
    ]
    groups.change_members(g1, ["1" * 64, "3" * 64], remove=True)
    assert music_library.recommend(store, "夜晚", music_group_ids=[g1])["items"] == []
    assert len(music_library.list_tracks(store)) == 3
    groups.delete(g1)
    with pytest.raises(ValueError, match="重新选择"):
        music_library.recommend(store, "夜晚", music_group_ids=[g1])


def test_job_scope_signature_and_deleted_group_remain_reviewable(data):
    config, store, groups = data
    group = groups.save("source", "范围")["id"]
    unrelated = groups.save("source", "无关")["id"]
    groups.change_members(group, ["a"])
    job = studio.create_job(config, "城市夜景", options={"source_group_ids": [group]})
    w = ws.load(config, job["id"])
    p = ws.plan(w, w["active_plan"])
    ws.add_script(config, job["id"], w, p, "城市夜景。", "manual")
    ws.set_board(
        config,
        job["id"],
        w,
        p,
        [studio.Shot(id="shot", text="城市夜景。", query="城市", estimated_seconds=2)],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job["id"], w)
    frozen_workspace = (studio.job_dir(config, job["id"]) / "workspace.json").read_bytes()
    first = ws.specification(config, job["id"], w, p, "recall", "shot")
    groups.change_members(unrelated, ["c"])
    assert ws.specification(config, job["id"], w, p, "recall", "shot")["signature"] == first["signature"]
    groups.change_members(group, ["b"])
    assert studio.material_scope(studio.load_job(config, job["id"]), store) == {"a", "b"}
    assert ws.specification(config, job["id"], w, p, "recall", "shot")["signature"] != first["signature"]
    assert studio.material_scope({**job, "source_ids": ["b", "c"]}, store) == {"b"}
    groups.delete(group)
    assert (studio.job_dir(config, job["id"]) / "workspace.json").read_bytes() == frozen_workspace
    assert studio.load_job(config, job["id"])["source_group_ids"] == [group]
    detail = ws.detail(config, job["id"], w, store=store)
    assert detail["material_scope"]["error"] and detail["material_scope"]["source_ids"] == []
    assert detail["job"]["source_group_ids"] == [group]
    with pytest.raises(studio.StudioError, match="重新选择"):
        studio.material_scope(job, store)
    with TestClient(create_app(config)) as client:
        assert (
            client.put(
                f"/api/studio/jobs/{job['id']}/material-scope",
                json={"source_ids": ["c"], "source_group_ids": []},
            ).status_code
            == 200
        )
        assert client.get(f"/api/studio/jobs/{job['id']}/material-scope").json()["source_ids"] == ["c"]


def test_rest_and_agent_mcp_crud(data):
    config, _, _ = data
    app = create_app(config)
    registry = build_registry()
    names = {
        "list_groups",
        "get_group",
        "create_group",
        "update_group",
        "delete_group",
        "add_group_members",
        "remove_group_members",
        "set_member_groups",
        "resolve_material_scope",
        "set_job_material_scope",
    }
    assert names <= {spec.name for spec in exported(registry)}

    async def call(name, args):
        client = MindexClient(app)
        try:
            spec = registry.get(name)
            return await spec.execute(spec.input_model.model_validate(args), ToolContext(client))
        finally:
            await client.aclose()

    group = asyncio.run(call("create_group", {"type": "source", "name": "城市"})).value
    gid = group["id"]
    asyncio.run(call("add_group_members", {"group_id": gid, "member_ids": ["a", "b"]}))
    assert asyncio.run(call("get_group", {"group_id": gid})).value["member_count"] == 2
    assert asyncio.run(
        call("resolve_material_scope", {"source_group_ids": [gid], "source_ids": ["b", "c"]})
    ).value["source_ids"] == ["b"]
    asyncio.run(call("update_group", {"group_id": gid, "description": "保留原文"}))
    with TestClient(app) as client:
        assert (
            client.get("/api/admin/groups", params={"type": "source"}).json()["items"][0]["description"]
            == "保留原文"
        )
        assert client.get("/api/admin/sources", params={"source_group_ids": gid}).json()["total"] == 2
        assert (
            client.put("/api/admin/group-memberships/source/c", json={"group_ids": [gid]}).status_code == 200
        )
        assert client.get(f"/api/admin/groups/{gid}", params={"page_size": 1}).json()["member_count"] == 3
        assert (
            client.patch(f"/api/admin/groups/{gid}", json={"name": "  新名称  "}).json()["name"] == "新名称"
        )
    asyncio.run(call("remove_group_members", {"group_id": gid, "member_ids": ["a", "b", "c"]}))
    with pytest.raises(ToolError, match="没有成员"):
        asyncio.run(call("resolve_material_scope", {"source_group_ids": [gid]}))
    asyncio.run(call("set_member_groups", {"type": "source", "member_id": "a", "group_ids": [gid]}))
    asyncio.run(call("delete_group", {"group_id": gid}))
    with pytest.raises(ToolError, match="已删除"):
        asyncio.run(call("get_group", {"group_id": gid}))


def test_backup_partial_empty_groups_and_name_mapping(data, tmp_path):
    config, store, groups = data
    first = groups.save("source", "共享", "说明")["id"]
    empty = groups.save("source", "空组")["id"]
    omitted = groups.save("source", "未导出")["id"]
    music = groups.save("music", "音乐")["id"]
    groups.change_members(first, ["a", "b"])
    groups.change_members(omitted, ["c"])
    groups.change_members(music, ["1" * 64])
    # 全量副本保留空组；部分副本仅保留导出成员涉及的组。
    full = tmp_path / "full"
    snapshot(store.path, full / "metadata.sqlite3")
    assert Groups(Store(full)).get(empty)["member_count"] == 0
    incoming = tmp_path / "incoming"
    snapshot(store.path, incoming / "metadata.sqlite3")
    filter_database(incoming / "metadata.sqlite3", {"a"})
    inc = Store(incoming)
    assert {g["id"] for g in Groups(inc).list("source") if not g.get("system")} == {first}
    assert Groups(inc).resolve("source", [first]) == {"a"}
    assert Groups(inc).get(music)["member_count"] == 1
    target = Store(tmp_path / "target")
    existing = Groups(target).save("source", "共享", "说明")["id"]
    row = inc.one("SELECT * FROM sources WHERE id='a'")
    target.execute(
        "INSERT INTO sources(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")",
        tuple(row.values()),
    )
    Groups(target).change_members(existing, ["a"])
    maps = merge_database(target.root, incoming, restore_config=False)
    assert maps["media_groups"][first] == existing
    assert Groups(target).resolve("source", [existing]) == {"a"}
    validate_database(target.root)
    # 同名但成员不同不能合并，否则会悄悄改变导入任务的动态范围。
    distinct = Store(tmp_path / "distinct")
    empty_target = Groups(distinct).save("source", "共享", "说明")["id"]
    mapped = merge_database(distinct.root, incoming, restore_config=False)
    assert mapped["media_groups"][first] == first
    assert Groups(distinct).resolve("source", [first]) == {"a"}
    assert Groups(distinct).resolve("source", [empty_target]) == set()
    # 带确定性名称后缀的组再次恢复仍然幂等。
    assert merge_database(distinct.root, incoming, restore_config=False)["media_groups"][first] == first
    media = config.data_dir / "music" / ("1" * 64 + ".wav")
    media.parent.mkdir()
    media.write_bytes(b"test audio")
    assert media.relative_to(config.data_dir).as_posix() in referenced_files(config.data_dir, store.path)
    with target.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.slow
def test_every_search_mode_stays_inside_groups(config, video_file, tmp_path):
    from conftest import FixtureEmbedder
    from PIL import Image

    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline
    from mindex.shared.media import cut
    from mindex.shared.types import TextSearch

    pipeline = Pipeline(config)
    first = pipeline.prepare(video_file)
    short = tmp_path / "second.mp4"
    cut(video_file, 1, 3, short)
    second = pipeline.prepare(short)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    store = pipeline.store
    groups = Groups(store)
    group = groups.save("source", "仅第二份素材")["id"]
    empty = groups.save("source", "空")["id"]
    groups.change_members(group, [second])
    filters = Filters(source_group_ids=[group])
    engine = SearchEngine(config, embedder)
    image = tmp_path / "query.png"
    Image.new("RGB", (160, 120), "red").save(image)
    seed = store.one("SELECT id FROM segments WHERE source_id=? AND kind='shot'", (first,))["id"]
    for result in (
        engine.text(TextSearch(text="红色", granularity="shot", filters=filters)),
        engine.image(image, "shot", filters=filters),
        engine.video(video_file, "shot", filters=filters),
        engine.similar(seed, "shot", filters=filters),
    ):
        assert result["results"]
        assert {s["source_id"] for s in result["results"]} == {second}
    with TestClient(create_app(config, embedder)) as client:
        for mode, path, mime in [("image", image, "image/png"), ("video", video_file, "video/mp4")]:
            response = client.post(
                f"/api/search/{mode}",
                files={"file": (path.name, path.read_bytes(), mime)},
                data={"granularity": "shot", "filters": dumps({"source_group_ids": [empty]})},
            )
            assert response.status_code == 200, response.text
            assert response.json()["results"] == []
        assert (
            client.post(
                "/api/search/similar",
                json={"segment_id": seed, "granularity": "shot", "filters": {"source_group_ids": [empty]}},
            ).json()["results"]
            == []
        )


def test_mcp_group_lifecycle_and_music_filter(data):
    import json

    from mcp.shared.memory import create_connected_server_and_client_session

    from mindex.agent.mcp_server import server_for_app

    config, _, _ = data
    app = create_app(config)
    server = server_for_app(app, app.state.runtime_config, app.state.store, policy="all")

    # 曲库工具有专用可读输出，直接用 MCP 文本验证范围，避免把可读文本误当成 JSON。
    async def lifecycle():
        async with create_connected_server_and_client_session(server) as client:

            async def call(name, args):
                result = await client.call_tool(name, args)
                assert not result.isError, result
                return "".join(block.text for block in result.content)

            group = json.loads(await call("create_group", {"type": "music", "name": "MCP 音乐组"}))
            gid = group["id"]
            await call("add_group_members", {"group_id": gid, "member_ids": ["1" * 64]})
            listed = await call("list_music_tracks", {"music_group_ids": [gid]})
            assert "1" * 64 in listed and "2" * 64 not in listed
            await call("update_group", {"group_id": gid, "description": "说明"})
            await call("set_member_groups", {"type": "music", "member_id": "2" * 64, "group_ids": [gid]})
            assert json.loads(await call("get_group", {"group_id": gid}))["member_count"] == 2
            await call("remove_group_members", {"group_id": gid, "member_ids": ["1" * 64]})
            await call("delete_group", {"group_id": gid})
            deleted = await client.call_tool("get_group", {"group_id": gid})
            assert deleted.isError

    asyncio.run(lifecycle())


def test_legacy_unversioned_snapshot_keeps_existing_groups(data):
    _, store, groups = data
    group = groups.save("source", "旧快照组")["id"]
    groups.change_members(group, ["a"])
    with store.connect() as db:
        db.execute("PRAGMA user_version=0")
    reopened = Store(store.root)
    assert Groups(reopened).resolve("source", [group]) == {"a"}


def test_automatic_uncategorized_groups(data):
    from mindex.shared.media_groups import UNGROUPED

    _, store, groups = data
    source_default, music_default = UNGROUPED['source'], UNGROUPED['music']
    assert groups.get(source_default)['system']
    assert groups.resolve('source', [source_default]) == {'a', 'b', 'c'}
    assert groups.get(music_default)['member_count'] == 3
    assert [row['id'] for row in groups.members(source_default, 2, 1)['items']] == ['b']
    first = groups.save('source', '分类一')['id']
    second = groups.save('source', '分类二')['id']
    before = groups.state('source', [source_default])
    groups.change_members(first, ['a'])
    groups.change_members(second, ['a'])
    assert groups.resolve('source', [source_default]) == {'b', 'c'}
    assert before != groups.state('source', [source_default])
    groups.change_members(first, ['a'], remove=True)
    assert groups.resolve('source', [source_default]) == {'b', 'c'}
    groups.delete(second)
    assert groups.for_member('source', 'a')[0]['id'] == source_default
    groups.set_groups('source', 'a', [first, source_default])
    assert groups.for_member('source', 'a')[0]['id'] == first
    groups.set_groups('source', 'a', [source_default])
    assert groups.for_member('source', 'a')[0]['id'] == source_default
    music_group = groups.save('music', '音乐分类')['id']
    groups.change_members(music_group, ['1' * 64])
    assert groups.resolve('music', [music_default]) == {'2' * 64, '3' * 64}
    assert groups.resolve('music', [music_default, music_group]) == {'1' * 64, '2' * 64, '3' * 64}
    with pytest.raises(ValueError, match='类型'):
        groups.resolve('source', [music_default])
    for action in (lambda: groups.delete(source_default),
                   lambda: groups.save('source', '改名', group_id=source_default),
                   lambda: groups.change_members(source_default, ['a'])):
        with pytest.raises(ValueError, match='系统自动管理'):
            action()
    # 新导入的素材也即时归入，备份不需要额外保存派生成员。
    store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                  ('d', 'd', 'sources/d.mp4', '{}', 'layout'))
    assert 'd' in groups.resolve('source', [source_default])


def test_uncategorized_reserved_name_migration(data):
    from mindex.shared.storage import _metadata_v10

    _, store, groups = data
    group_id = groups.save('source', '历史分组')['id']
    groups.change_members(group_id, ['a'])
    store.execute("UPDATE media_groups SET name='未分类' WHERE id=?", (group_id,))
    with store.connect() as db:
        _metadata_v10(db)
    assert groups.get(group_id)['name'] == '未分类（自定义）'
    assert groups.resolve('source', [group_id]) == {'a'}
    assert len([group for group in groups.list('source') if group['name'] == '未分类']) == 1


@pytest.mark.parametrize("kind", ["source", "music"])
def test_filter_catalogue_members_are_bulk_loaded_and_current(data, monkeypatch, kind):
    config, store, groups = data
    first = groups.save(kind, "目录一")["id"]
    empty = groups.save(kind, "目录空组")["id"]
    member = "a" if kind == "source" else "1" * 64
    groups.change_members(first, [member])
    connect = store.connect
    calls = []

    def counted():
        calls.append(True)
        return connect()

    monkeypatch.setattr(store, "connect", counted)
    rows = {row["id"]: row for row in groups.list(kind, include_members=True)}
    assert len(calls) == 1  # 组数增长不增加连接数，也不读取源媒体的完整元信息。
    assert rows[first]["member_ids"] == [member]
    assert rows[empty]["member_ids"] == []
    assert member not in rows[f"ungrouped-{kind}"]["member_ids"]
    assert all(row["member_count"] == len(row["member_ids"]) for row in rows.values())
    assert all("member_ids" not in row for row in groups.list(kind))

    client = TestClient(create_app(config))
    url = f"/api/admin/groups?type={kind}&include_members=true"
    assert client.get(url).status_code == 200
    groups.change_members(first, [member], remove=True)
    fresh = {row["id"]: row for row in client.get(url).json()["items"]}
    assert fresh[first]["member_ids"] == []
    assert member in fresh[f"ungrouped-{kind}"]["member_ids"]
