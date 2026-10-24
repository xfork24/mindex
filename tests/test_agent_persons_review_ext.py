"""人物审核与发布工具的补充测试：更多审核规则分支、发布与检索组合前置条件、多附件演员表导入与照片框质检。

全部经进程内回环调用真实 /api/persons 接口；人物服务使用 test_person_identify 的确定性桩。
"""

from __future__ import annotations

import asyncio
import io
from typing import ClassVar

import pytest
from PIL import Image
from test_agent_persons import evidence, seed_shots
from test_person_identify import StubPersonClient, face, photo, seed_library

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.agent.tools.persons_review import CAST_HINTS
from mindex.persons.person_models import Entity
from mindex.persons.persons import Persons
from mindex.shared.processing_lock import ProcessingLock
from mindex.shared.storage import Store, dumps


@pytest.fixture
def library(config, monkeypatch):
    return seed_library(config, monkeypatch)


def run(client, name, arguments, *, preview=False, files=None, client_class=MindexClient):
    """执行（或只预览）一个工具；files 把附件引用映射为 (文件名, 内容, 类型)，按引用分别解析。"""
    spec = build_registry().get(name)

    async def go():
        resolver = (lambda ref: files[ref]) if files else None
        loop = client_class(client.app, resolve_attachment=resolver)
        try:
            ctx = ToolContext(client=loop, page={"page": "persons"})
            args = spec.parse(arguments)
            return await (spec.preview(args, ctx) if preview else spec.execute(args, ctx))
        finally:
            await loop.aclose()

    return asyncio.run(go())


def rejected(client, name, arguments, *, code, match, preview=(True, False), **kwargs):
    """预览与执行都以同一错误拒绝。"""
    for flag in preview:
        with pytest.raises(ToolError, match=match) as excinfo:
            run(client, name, arguments, preview=flag, **kwargs)
        assert excinfo.value.code == code, (flag, excinfo.value)


class Recording(MindexClient):
    posts: ClassVar[list] = []

    async def post(self, path, body=None, **params):
        Recording.posts.append((path, body))
        return await super().post(path, body, **params)


# ---------- 审核规则 ----------

def test_review_shot_rule_branches(library, config):
    client, ids, _refs = library
    store = seed_shots(config, 1)
    persons = Persons(store)
    other = persons.save(Entity(kind="work", name="另一部作品"))
    cameo = persons.save(Entity(kind="role", name="客串", work_ids=[other["id"]], actor_ids=[ids["alice"]]))

    def body(actor, role, source="source"):
        return {"source_id": source, "shot_id": "shot0", "selections": [{"actor_id": actor, "role_id": role}]}

    rejected(client, "review_shot", body(ids["young"], ids["young"]), code="invalid_selection",
             match="角色「青年叶淑柔」不是演员")
    rejected(client, "review_shot", body(ids["alice"], ids["alice"]), code="invalid_selection",
             match="演员「演员甲」不是角色：出镜审核必须指定角色")
    # 角色不属于素材作品（演员确实出演）：只报作品不符。
    with pytest.raises(ToolError, match="角色「客串」不属于该素材的作品") as excinfo:
        run(client, "review_shot", body(ids["alice"], cameo["id"]))
    assert "出演演员中没有" not in str(excinfo.value) and excinfo.value.code == "invalid_selection"
    # 演员不出演该角色：点名并列出实际出演演员。
    with pytest.raises(ToolError, match="出演演员中没有演员「演员甲」") as excinfo:
        run(client, "review_shot", body(ids["alice"], ids["villain"]), preview=True)
    assert "出演演员：演员乙" in str(excinfo.value) and "不属于该素材的作品" not in str(excinfo.value)
    rejected(client, "review_shot", body("nobody", ids["young"]), code="missing_person", match="找不到人物")
    rejected(client, "review_shot", body(ids["alice"], ids["young"], source="ghost"), code="missing_source",
             match="找不到素材 ghost")
    # 以上全部在本地预检拒绝：没有写入任何证据。
    assert store.one("SELECT COUNT(*) AS n FROM person_evidence")["n"] == 0


def test_review_shot_deduplicates_selections(library, config):
    client, ids, _refs = library
    store = seed_shots(config, 1)
    pair = {"actor_id": ids["alice"], "role_id": ids["young"]}
    other = {"actor_id": ids["alice"], "role_id": ids["old"]}
    args = {"source_id": "source", "shot_id": "shot0", "selections": [pair, pair, other, pair]}

    preview = run(client, "review_shot", args, preview=True)
    assert preview.changes[0]["before"] == "尚无人物处理结果"
    assert preview.changes[0]["after"] == "已接受：演员甲·青年叶淑柔、演员甲·老年叶淑柔"

    Recording.posts = []
    out = run(client, "review_shot", args, client_class=Recording)
    [(_path, sent)] = [p for p in Recording.posts if p[0] == "/api/persons/review/shot"]
    assert sent["selections"] == [pair, other]
    assert out.text.count("演员甲·青年叶淑柔") == 1
    rows = store.rows("SELECT actor_id,role_id FROM person_evidence WHERE shot_id='shot0' AND status='accepted'")
    assert sorted((r["actor_id"], r["role_id"]) for r in rows) == sorted(
        [(ids["alice"], ids["young"]), (ids["alice"], ids["old"])])
    accepted = run(client, "list_review_shots", {"status": "accepted"}).value["items"]
    assert [s["shot_id"] for s in accepted] == ["shot0"] and len(accepted[0]["confirmed_people"]) == 2


def test_list_review_shots_identity_filters(library, config):
    client, ids, _refs = library
    store = seed_shots(config, 3)
    evidence(store, "ev0", "shot0", ids["alice"], ids["young"], status="candidate")
    evidence(store, "ev1", "shot1", ids["bob"], ids["villain"], status="candidate")
    evidence(store, "ev2", "shot2", ids["alice"], ids["old"], status="candidate")

    # 只给演员且只饰演一个角色：自动补全角色。
    only = run(client, "list_review_shots", {"actor_id": ids["bob"]})
    assert [s["shot_id"] for s in only.value["items"]] == ["shot1"]
    # 只给演员但饰演多个角色：要求补 role_id 并列出候选角色。
    with pytest.raises(ToolError, match="饰演 2 个角色，请同时给出 role_id") as excinfo:
        run(client, "list_review_shots", {"actor_id": ids["alice"]})
    assert excinfo.value.code == "invalid_arguments"
    assert ids["young"] in str(excinfo.value) and ids["old"] in str(excinfo.value)
    both = run(client, "list_review_shots", {"actor_id": ids["alice"], "role_id": ids["old"]})
    assert [s["shot_id"] for s in both.value["items"]] == ["shot2"]

    # 只给角色但有多名出演演员：要求补 actor_id 并列出演员。
    work = Persons(store).work_for("source")
    duo = Persons(store).save(Entity(kind="role", name="双生", work_ids=[work], actor_ids=[ids["alice"], ids["bob"]]))
    with pytest.raises(ToolError, match="有 2 名出演演员，请同时给出 actor_id") as excinfo:
        run(client, "list_review_shots", {"role_id": duo["id"]})
    assert f"演员甲({ids['alice']})" in str(excinfo.value) and f"演员乙({ids['bob']})" in str(excinfo.value)
    assert run(client, "list_review_shots", {"role_id": duo["id"], "actor_id": ids["bob"]}).value["items"] == []

    # 身份类型不对。
    with pytest.raises(ToolError, match="不是角色") as excinfo:
        run(client, "list_review_shots", {"role_id": ids["alice"]})
    assert excinfo.value.code == "invalid_arguments"
    with pytest.raises(ToolError, match="不是演员"):
        run(client, "list_review_shots", {"actor_id": ids["young"]})

    # no_candidate 与身份筛选互斥：参数模型层面拒绝。
    spec = build_registry().get("list_review_shots")
    for extra in ({"actor_id": ids["bob"]}, {"role_id": ids["villain"]}):
        with pytest.raises(ToolError, match="no_candidate"):
            spec.parse({"no_candidate": True, **extra})
    alone = run(client, "list_review_shots", {"no_candidate": True})
    assert all(not (s["counts"] or {}).get("candidate") for s in alone.value["items"])


# ---------- 发布前置条件 ----------

def seed_version(store, config, identifier, status="ready"):
    spec = {"format": 2, "mode": "auto", "space": {"model": "fixture"}, "preprocessing": config.video.model_dump()}
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)", (identifier, dumps(spec), status))


def test_publish_preconditions(library, config):
    client, ids, _refs = library
    store = seed_shots(config, 1)
    seed_version(store, config, "abcdef01aaaa")
    seed_version(store, config, "abcdef02bbbb", status="building")
    seed_version(store, config, "fedcba99cccc")
    store.activate("abcdef01aaaa")
    before = store.active_index()["search_release_id"]

    # content_id 前缀解析：过短、歧义、不存在，预览与执行都拒绝。
    rejected(client, "publish_persons", {"content_id": "abc"}, code="invalid_arguments", match="至少 6 位")
    rejected(client, "publish_persons", {"content_id": "abcdef"}, code="ambiguous_index", match="有歧义")
    rejected(client, "publish_persons", {"content_id": "zzzzzz"}, code="missing_index", match="索引版本不存在")

    # 指定未就绪的内容索引：预览提示，执行转述服务端原因，活动组合不变。
    preview = run(client, "publish_persons", {"content_id": "abcdef02"}, preview=True)
    assert any("状态为 building，服务端会拒绝发布" in w for w in preview.warnings)
    with pytest.raises(ToolError, match="内容索引尚未就绪") as excinfo:
        run(client, "publish_persons", {"content_id": "abcdef02"})
    assert excinfo.value.code == "http_422"
    assert store.active_index()["search_release_id"] == before

    # 仍有待确认分镜：预览提示数量。
    evidence(store, "ev0", "shot0", ids["alice"], ids["young"], status="candidate")
    preview = run(client, "publish_persons", {}, preview=True)
    assert any("仍有 1 个待确认分镜" in w for w in preview.warnings)
    assert not any("会同时把活动内容索引切换为" in w for w in preview.warnings)

    # 处理锁被占用：服务端 409 原因原样转述，活动组合不变。
    lock = ProcessingLock(config, source_id="source", deleting=True)
    with lock:
        with pytest.raises(ToolError, match="处理锁被占用，请稍后重试人物发布操作") as excinfo:
            run(client, "publish_persons", {})
    assert excinfo.value.code == "http_409"
    assert store.active_index()["search_release_id"] == before

    # 指定另一个就绪的内容索引：预览说明会切换活动内容索引，执行后生效。
    preview = run(client, "publish_persons", {"content_id": "fedcba99"}, preview=True)
    assert any("会同时把活动内容索引切换为" in w for w in preview.warnings)
    assert preview.changes[1]["after"].startswith("fedcba99")
    out = run(client, "publish_persons", {"content_id": "fedcba99"})
    assert out.value["content_id"] == "fedcba99cccc" and store.active_index()["id"] == "fedcba99cccc"


# ---------- 检索组合 ----------

def test_change_release_resolution_and_content_switch(library, config):
    client, _ids, _refs = library
    store = Store(config.data_dir)
    seed_version(store, config, "content1")
    seed_version(store, config, "content2")
    seed_version(store, config, "content3", status="building")
    store.activate("content1")
    first = store.active_index()["search_release_id"]
    switch, broken = "abcdef11" + "0" * 24, "abcdef22" + "0" * 24
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES(?,?,NULL)", (switch, "content2"))
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES(?,?,NULL)", (broken, "content3"))

    for action in ("activate", "delete"):
        rejected(client, "change_search_release", {"release_id": "abcde", "action": action},
                 code="invalid_arguments", match="至少 6 位")
        rejected(client, "change_search_release", {"release_id": "abcdef", "action": action},
                 code="ambiguous_release", match="有歧义")
        rejected(client, "change_search_release", {"release_id": "zzzzzz", "action": action},
                 code="missing_release", match="检索组合不存在")

    # 目标内容索引未就绪：预览提示，执行转述服务端拒绝，活动组合不变。
    preview = run(client, "change_search_release", {"release_id": "abcdef22", "action": "activate"}, preview=True)
    assert any("该内容索引未就绪，服务端会拒绝恢复" in w for w in preview.warnings)
    with pytest.raises(ToolError, match="检索组合不存在或内容索引未就绪") as excinfo:
        run(client, "change_search_release", {"release_id": "abcdef22", "action": "activate"})
    assert excinfo.value.code == "http_422"
    assert store.active_index()["search_release_id"] == first and store.active_index()["id"] == "content1"

    # 切换到另一个内容索引的组合：预览说明同时切换内容索引；执行后两者都生效。
    preview = run(client, "change_search_release", {"release_id": "ABCDEF11", "action": "activate"}, preview=True)
    assert any("会同时把活动内容索引切换为 content2（状态 ready）" in w for w in preview.warnings)
    assert not any("未就绪" in w for w in preview.warnings)
    assert preview.changes[1] == {"label": "内容索引", "before": "content1", "after": "content2"}
    run(client, "change_search_release", {"release_id": "abcdef11", "action": "activate"})
    active = store.active_index()
    assert active["id"] == "content2" and active["search_release_id"] == switch

    # 新的生效组合经前缀同样受保护；原组合可删除。
    rejected(client, "change_search_release", {"release_id": "abcdef11", "action": "delete"},
             code="release_active", match="当前生效的检索组合", preview=(True,))
    run(client, "change_search_release", {"release_id": first, "action": "delete"})
    ids = {r["id"] for r in run(client, "list_search_releases", {}).value["releases"]}
    assert ids == {switch, broken}


# ---------- 演员表导入 ----------

COLORS = {(0, 128, 128): "teal", (255, 165, 0): "orange", (128, 0, 128): "purple"}


def cast_parser(entries_by_color):
    """确定性拆分：按原图颜色返回条目，验证条目与原图编号的对应关系。"""

    def parsed(_config, image):
        with Image.open(io.BytesIO(image["raw"])) as decoded:
            color = COLORS[decoded.convert("RGB").getpixel((0, 0))]
        entries = [{"image_id": image["id"], "foreign_name": "", "actor_id": None, "role_id": None, "bbox": None,
                    "single_person": False, **entry} for entry in entries_by_color.get(color, [])]
        return {"entries": entries, "warnings": [f"{color} 已拆分"]}

    return parsed


def attach(client, name, content, ctype):
    response = client.post("/api/agent/attachments", files={"file": (name, content, ctype)})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def stash(client, *items):
    """上传真实附件（预览读取元数据），并返回按引用分别解析的映射。"""
    files = {}
    for name, content, ctype in items:
        files[attach(client, name, content, ctype)] = (name, content, ctype)
    return list(files), files


def test_import_cast_multiple_attachments(library, monkeypatch):
    client, _ids, _refs = library
    monkeypatch.setattr("mindex.persons.person_api.parse_cast", cast_parser({
        "teal": [{"actor_name": "演员丁", "foreign_name": "Ding", "role_name": "侦探"}],
        "orange": [{"actor_name": "演员戊", "role_name": "刺客", "bbox": [10, 10, 90, 70]},
                   {"actor_name": "演员己", "role_name": ""}],
    }))
    refs, files = stash(client, ("cast1.png", photo("teal"), "image/png"),
                        ("cast2.png", photo("orange"), "image/png"))
    note_ref, note_files = stash(client, ("notes.txt", b"not an image", "text/plain"))

    # 预览：逐张列出附件；非图片附件给出警告。
    preview = run(client, "import_cast", {"attachments": [refs[0], *note_ref]}, preview=True)
    assert [c["label"] for c in preview.changes] == ["截图 1", "截图 2"]
    assert preview.changes[0]["after"].startswith("cast1.png / image/png")
    assert any("截图 2 类型为 text/plain" in w for w in preview.warnings)
    assert not any("截图 1 类型" in w for w in preview.warnings)
    clean = run(client, "import_cast", {"attachments": refs}, preview=True)
    assert not any("类型为" in w for w in clean.warnings)

    out = run(client, "import_cast", {"attachments": refs}, files=files)
    value = out.value
    assert len(value["images"]) == 2 and len(out.media) == 2
    assert [m["url"] for m in out.media] == [f"{im['url']}?thumbnail=true" for im in value["images"]]
    assert [m["title"] for m in out.media] == ["演员表原图 1", "演员表原图 2"]
    first, second = (im["id"] for im in value["images"])
    assert [e["image_id"] for e in value["entries"]] == [first, second, second]
    assert "#1 演员丁（Ding） → 侦探 | 原图 1 | 无照片框" in out.text
    assert "#2 演员戊 → 刺客 | 原图 2 | 照片框 [10" in out.text
    assert "#3 演员己 → 无角色 | 原图 2 | 无照片框" in out.text
    assert "teal 已拆分" in out.text and "orange 已拆分" in out.text
    assert "3 个条目" in out.text and "2 张原图" in out.text
    # 每张缩略图都可读取。
    for media in out.media:
        assert client.get(media["url"]).status_code == 200

    # 非图片附件执行时由服务端以 422 拒绝（不是 500），不创建草稿。
    drafts = len(client.get("/api/persons/imports").json())
    with pytest.raises(ToolError, match="图片无法解码") as excinfo:
        run(client, "import_cast", {"attachments": note_ref}, files=note_files)
    assert excinfo.value.code == "http_422"
    assert len(client.get("/api/persons/imports").json()) == drafts

    # 确认：带照片框的条目经人物服务质检后生成参考图。
    StubPersonClient.observations = [face([1, 0, 0])]
    work = next(s for s in client.get("/api/persons").json()["sources"] if s["id"] == "source")["work_id"]
    preview = run(client, "confirm_cast_import", {"import_id": value["import_id"], "work_id": work}, preview=True)
    assert preview.changes[1]["after"].endswith("原图 2 裁剪参考图")
    assert preview.changes[0]["after"].endswith("无参考图") and preview.changes[2]["after"].endswith("无参考图")
    assert any("调用人物服务质检" in w for w in preview.warnings)
    before = len(client.get("/api/persons").json()["references"])
    StubPersonClient.seen = []
    result = run(client, "confirm_cast_import", {"import_id": value["import_id"], "work_id": work})
    confirmed = result.value["confirmed"]
    assert len(confirmed) == 3 and sum(1 for c in confirmed if c.get("reference_id")) == 1
    assert "演员戊·刺客（参考图" in result.text
    # 人物服务收到的是照片框裁剪后的图。
    assert ("image", "PNG", (80, 60)) in StubPersonClient.seen
    catalogue = client.get("/api/persons").json()
    assert len(catalogue["references"]) == before + 1
    actor = next(e for e in catalogue["entities"] if e["kind"] == "actor" and e["name"] == "演员戊")
    assert any(r["actor_id"] == actor["id"] for r in catalogue["references"])


def make_draft(client, monkeypatch, color, entries):
    monkeypatch.setattr("mindex.persons.person_api.parse_cast", cast_parser({color: entries}))
    refs, files = stash(client, (f"{color}.png", photo(color), "image/png"))
    return run(client, "import_cast", {"attachments": refs}, files=files).value


def test_confirm_cast_quality_check_rejects_whole_batch(library, monkeypatch):
    client, _ids, _refs = library
    draft = make_draft(client, monkeypatch, "purple", [])
    image = draft["images"][0]["id"]
    work = next(s for s in client.get("/api/persons").json()["sources"] if s["id"] == "source")["work_id"]
    entries = [{"image_id": image, "actor_name": "演员丁", "role_name": "侦探"},
               {"image_id": image, "actor_name": "演员戊", "role_name": "刺客", "bbox": [0, 0, 80, 60]}]
    baseline = client.get("/api/persons").json()

    def unchanged():
        now = client.get("/api/persons").json()
        assert len(now["entities"]) == len(baseline["entities"])
        assert len(now["references"]) == len(baseline["references"])
        assert client.get(f"/api/persons/imports/{draft['import_id']}").json()["status"] == "draft"

    # 照片框里没有清晰人脸：整批 422，点名条目并附处理建议；没有任何写入。
    StubPersonClient.observations = []
    with pytest.raises(ToolError) as excinfo:
        run(client, "confirm_cast_import", {"import_id": draft["import_id"], "work_id": work, "entries": entries})
    message = str(excinfo.value)
    assert excinfo.value.code == "http_422"
    assert "条目 2（演员戊）" in message and "参考照片需要恰好一张清晰人脸" in message
    assert CAST_HINTS[422] in message
    unchanged()

    # 人物服务不可用：503 转述原因与建议，同样没有写入。
    StubPersonClient.fail = "identity"
    with pytest.raises(ToolError) as excinfo:
        run(client, "confirm_cast_import", {"import_id": draft["import_id"], "work_id": work, "entries": entries})
    assert excinfo.value.code == "http_503"
    assert "人物服务不可用" in str(excinfo.value) and CAST_HINTS[503] in str(excinfo.value)
    unchanged()

    # 调整照片框且服务恢复：整批导入成功。
    StubPersonClient.fail, StubPersonClient.observations = None, [face([0, 1, 0])]
    entries[1]["bbox"] = [20, 20, 120, 100]
    result = run(client, "confirm_cast_import",
                 {"import_id": draft["import_id"], "work_id": work, "entries": entries})
    assert [bool(c.get("reference_id")) for c in result.value["confirmed"]] == [False, True]
    assert client.get(f"/api/persons/imports/{draft['import_id']}").json()["status"] != "draft"


def test_confirm_cast_ambiguous_actor_name(library, config, monkeypatch):
    client, _ids, _refs = library
    Persons(Store(config.data_dir)).save(Entity(kind="actor", name="演员甲"))
    draft = make_draft(client, monkeypatch, "teal", [{"actor_name": "演员甲", "role_name": "青年叶淑柔"}])
    work = next(s for s in client.get("/api/persons").json()["sources"] if s["id"] == "source")["work_id"]
    args = {"import_id": draft["import_id"], "work_id": work}

    preview = run(client, "confirm_cast_import", args, preview=True)
    assert "「演员甲」有 2 个同名演员" in preview.changes[0]["after"]
    assert any("演员「演员甲」有歧义，服务端会拒绝：请给该条目指定 actor_id" in w for w in preview.warnings)
    with pytest.raises(ToolError, match="演员姓名有歧义") as excinfo:
        run(client, "confirm_cast_import", args)
    assert excinfo.value.code == "http_422"
    assert client.get(f"/api/persons/imports/{draft['import_id']}").json()["status"] == "draft"
