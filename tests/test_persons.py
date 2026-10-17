from __future__ import annotations

import base64
import io
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mindex.adapters import ModelError
from mindex.api import create_app
from mindex.config import Config, fingerprint
from mindex.person_processing import (
    PersonOptions,
    PersonProcessor,
    ShotTracker,
    attribute_frames,
    face_match,
    reference_subject,
)
from mindex.persons import (
    CastConfirm,
    Entity,
    Persons,
    Review,
    init_person_schema,
    overlaps_for,
    resolve_names,
)
from mindex.storage import Store, dumps
from mindex.types import TextSearch


def photo(color="red", size=(100, 120)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture
def catalogue(config):
    store = Store(config.data_dir)
    path = store.root / "source.mp4"
    path.write_bytes(b"fixture only")
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        ("source", "影片", "source.mp4", dumps({"duration": 3}), "layout"),
    )
    for n in range(3):
        store.execute(
            "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"shot{n}", "source", "shot", n, n + 1, "source.mp4", "source.mp4", "gen", None, "fixture"),
        )
    persons = Persons(store)
    work = persons.work_for("source")
    actor = persons.save(Entity(kind="actor", name="演员甲", aliases=["Alice"]))
    return persons, work, actor


def test_actor_work_role_many_to_many_and_retired_identity_rejected(catalogue):
    p, work, a = catalogue
    other = p.save(Entity(kind="actor", name="演员甲"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[a["id"], other["id"]]))
    with pytest.raises(ValueError):
        Entity(kind="stage", name="老年叶淑柔", role_id=role["id"])
    assert len(p.entities("actor")) == 2
    assert "stage_id" not in p.add_reference(other["id"], photo())
    one = p.add_reference(a["id"], photo())
    assert p.add_reference(a["id"], photo())["id"] == one["id"]
    p.add_reference(a["id"], photo("blue"))
    assert len(p.references()) == 3
    foreign = p.save(Entity(kind="work", name="另一作品"))
    assert (
        p.save(Entity(kind="role", name="叶淑柔", work_ids=[foreign["id"]], actor_ids=[a["id"]]))["id"]
        != role["id"]
    )


def test_cross_film_role_multiple_works_import_merge_and_review(catalogue, config):
    p, work, a = catalogue
    with pytest.raises(ValueError, match="至少归属一部作品"):
        p.save(Entity(kind="role", name="无主角色", work_ids=[]))
    sequel = p.save(Entity(kind="work", name="续作"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[a["id"]]))
    # 扩展到第二部作品不更换身份；旧格式单 work_id 行读取时迁移为列表。
    updated = p.save(
        Entity.model_validate({k: v for k, v in role.items() if k != "id"}).model_copy(
            update={"work_ids": [work, sequel["id"]]}
        ),
        role["id"],
    )
    assert updated["id"] == role["id"] and updated["work_ids"] == [work, sequel["id"]]
    legacy = {k: v for k, v in updated.items() if k != "id"} | {"work_id": sequel["id"]}
    legacy.pop("work_ids", None)
    p.store.execute("UPDATE person_entities SET data=? WHERE id=?", (dumps(legacy), role["id"]))
    assert p.entity(role["id"])["work_ids"] == [sequel["id"]]
    p.save(
        Entity.model_validate({k: v for k, v in p.entity(role["id"]).items() if k != "id"}).model_copy(
            update={"work_ids": [work, sequel["id"]]}
        ),
        role["id"],
    )
    # 续作素材中审核同一角色成功；处理时跨电影角色在其任一作品内可解析。
    p.store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        ("sequel", "续作正片", "sequel.mp4", dumps({"duration": 3}), "layout"),
    )
    (p.store.root / "sequel.mp4").write_bytes(b"fixture only")
    p.assign_work("sequel", sequel["id"])
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("ev2", "sequel", "shot0", a["id"], None, None, 0, 1, "candidate", dumps({"work_id": sequel["id"]})),
    )
    assert (
        p.review("ev2", Review(actor_id=a["id"], role_id=role["id"], reason="续作同角色"))["status"]
        == "accepted"
    )
    refs = [{**obs(), "actor_id": a["id"]}]
    shot = {"id": "s", "source_id": "sequel", "cache_id": "c", "start": 0, "end": 1, "space": {"model": "f"}}
    rows = attribute_frames(
        [{"timestamp": 0, "observations": [obs()]}],
        refs,
        config.person,
        shot,
        sequel["id"],
        [{"id": role["id"], "work_ids": [work, sequel["id"]], "actor_ids": [a["id"]]}],
        Tracker,
    )
    assert rows[0]["role_id"] == role["id"]
    # 演员表导入关联已有跨电影角色时并入该作品，而不是报错拒绝。
    draft = p.create_import([photo("gold")])
    p.confirm_import(
        draft["id"],
        CastConfirm(
            work_id=sequel["id"],
            entries=[
                {
                    "image_id": draft["images"][0]["id"],
                    "actor_name": "演员甲",
                    "actor_id": a["id"],
                    "role_id": role["id"],
                    "bbox": [10, 10, 90, 110],
                    "single_person": True,
                }
            ],
        ),
    )
    assert p.entity(role["id"])["work_ids"] == [work, sequel["id"]]
    # 移除仍有已接受出镜证据的作品被拒绝。
    with pytest.raises(ValueError, match="出镜证据"):
        p.save(
            Entity.model_validate({k: v for k, v in p.entity(role["id"]).items() if k != "id"}).model_copy(
                update={"work_ids": [work]}
            ),
            role["id"],
        )


def test_cast_original_coordinates_eight_entries_manual_pairing_transaction(catalogue):
    p, work, actor = catalogue
    draft = p.create_import([photo(size=(850, 260)), photo("blue", (300, 700))])
    entries = [
        {
            "image_id": draft["images"][0]["id"],
            "actor_name": f"演员{i}",
            "role_name": "叶淑柔" if i == 0 else f"角色{i}",
            "bbox": [i * 100, 10, i * 100 + 80, 200],
            "single_person": True,
        }
        for i in range(8)
    ]
    # Duplicate thumbnail and representative-work entries are removed by the reviewer, not auto-imported.
    bad = [
        *entries,
        {
            "image_id": draft["images"][1]["id"],
            "actor_name": "错误框",
            "bbox": [0, 0, 900, 900],
            "single_person": True,
        },
    ]
    with pytest.raises(ValueError, match="超出"):
        p.confirm_import(draft["id"], CastConfirm(work_id=work, entries=bad))
    assert len(p.entities("actor")) == 1 and not p.references()
    result = p.confirm_import(draft["id"], CastConfirm(work_id=work, entries=entries))
    assert len(result) == 8 and len(p.references()) == 8
    with Image.open(p.store.resolve_media(p.references()[0]["path"])) as image:
        assert image.size == (80, 190)
    old_role = result[0]["role_id"]
    second = p.create_import([photo("green")])
    result2 = p.confirm_import(
        second["id"],
        CastConfirm(
            work_id=work,
            entries=[
                {
                    "image_id": second["images"][0]["id"],
                    # 同名演员自动复用，不重复创建；不同时期直接写成角色名。
                    "actor_name": "演员甲",
                    "role_name": "老年叶淑柔",
                    "bbox": [1, 1, 80, 100],
                    "single_person": True,
                }
            ],
        ),
    )
    assert result2[0]["actor_id"] == actor["id"]
    assert len(p.entities("actor")) == 9  # 复用已有演员甲，仅新增 8 个条目演员
    assert result2[0]["role_id"] != old_role
    aged = p.entity(result2[0]["role_id"])
    assert aged["name"] == "老年叶淑柔" and actor["id"] in aged["actor_ids"]
    # 再次导入同名同作品角色时复用身份并追加演员。
    third = p.create_import([photo("purple")])
    result3 = p.confirm_import(
        third["id"],
        CastConfirm(
            work_id=work,
            entries=[
                {
                    "image_id": third["images"][0]["id"],
                    "actor_name": "演员甲",
                    "role_name": "老年叶淑柔",
                    "bbox": [1, 1, 80, 100],
                    "single_person": True,
                }
            ],
        ),
    )
    assert result3[0]["role_id"] == result2[0]["role_id"]
    roles = [r for r in p.entities("role") if r["name"] == "老年叶淑柔"]
    assert len(roles) == 1 and set(roles[0]["actor_ids"]) == {actor["id"]}
    with pytest.raises(ValueError, match="已确认"):
        p.confirm_import(draft["id"], CastConfirm(work_id=work, entries=entries))
    group = p.create_import([photo()])
    with pytest.raises(ValueError, match="仅包含一个"):
        p.confirm_import(
            group["id"],
            CastConfirm(
                work_id=work,
                entries=[
                    {"image_id": group["images"][0]["id"], "actor_name": "多人合照", "bbox": [0, 0, 50, 50]}
                ],
            ),
        )


def test_import_api_failure_draft_and_reference_repair(catalogue, config, monkeypatch):
    p, _work, actor = catalogue
    client = TestClient(create_app(config))
    response = client.post("/api/persons/imports", files=[("files", ("cast.png", photo(), "image/png"))])
    assert response.status_code == 201
    draft = response.json()

    def offline(*args):
        raise ModelError("offline")

    monkeypatch.setattr("mindex.person_api.parse_cast", offline)
    parsed = client.post(f"/api/persons/imports/{draft['id']}/parse").json()
    assert parsed["status"] == "draft" and parsed["warnings"] and parsed["entries"] == []
    assert len(p.entities("actor")) == 1
    uploaded = client.post(
        "/api/persons/references",
        data={"actor_id": actor["id"]},
        files={"file": ("a.png", photo(), "image/png")},
    ).json()
    other = p.save(Entity(kind="actor", name="同名"))
    moved = client.put("/api/persons/references/" + uploaded["id"], json={"actor_id": other["id"]}).json()
    assert moved["actor_id"] == other["id"]
    assert client.get(moved["url"]).status_code == 200
    assert client.delete("/api/persons/references/" + moved["id"]).status_code == 200
    assert p.references() == []
    assert client.get("/persons").status_code == 200


def test_reference_pair_and_actor_metadata_saved_together(catalogue, config):
    p, _work, actor = catalogue
    reference = p.add_reference(actor["id"], photo())
    other = p.save(Entity(kind="actor", name="演员乙"))
    client = TestClient(create_app(config))
    path = "/api/persons/references/" + reference["id"]
    response = client.put(
        path,
        json={
            "actor_id": other["id"],
            "entity": {
                "kind": "actor",
                "name": "演员乙改名",
                "aliases": ["小乙"],
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["actor_id"] == other["id"]
    assert p.entity(other["id"])["name"] == "演员乙改名"
    assert p.entity(other["id"])["aliases"] == ["小乙"]
    assert p.entity(actor["id"])["name"] == "演员甲"
    assert client.get(response.json()["url"]).status_code == 200
    assert "stage_id" not in response.json()
    failed = client.put(
        path, json={"actor_id": actor["id"], "entity": {"kind": "work", "name": "不合法配对"}}
    )
    assert failed.status_code == 422
    assert p.reference(reference["id"])["actor_id"] == other["id"]
    assert p.entity(actor["id"])["name"] == "演员甲"
    assert client.put(path, json={"actor_id": actor["id"], "stage_id": "retired"}).status_code == 422
    assert (
        client.post(
            "/api/persons/references/from-source",
            json={
                "actor_id": actor["id"],
                "source_id": "source",
                "timestamp": 0,
                "stage_id": "retired",
            },
        ).status_code
        == 422
    )
    page = client.get("/persons").text
    assert "角色阶段" not in page and "capture-stage" not in page and "reference-stage" not in page
    assert 'id="entity-dialog"' in page and 'id="reference-form"' not in page


def test_role_reference_is_stored_separately_and_drives_face_matching(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="老年叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    paired = p.add_reference(actor["id"], photo(), role_id=role["id"])
    assert paired["role_id"] == role["id"] and paired["actor_ids"] == [actor["id"]]
    assert "stage_id" not in paired
    # 角色照片与演员照片是两条独立配对：同一字节各自去重，互不覆盖。
    assert p.add_reference(actor["id"], photo(), role_id=role["id"])["id"] == paired["id"]
    actor_pair = p.add_reference(actor["id"], photo())
    assert actor_pair["id"] != paired["id"] and actor_pair["role_id"] is None
    with pytest.raises(ValueError, match="出演演员"):
        outsider = p.save(Entity(kind="actor", name="演员丙"))
        p.add_reference(outsider["id"], photo("gold"), role_id=role["id"])
    # 演员没有任何演员级照片时，角色照片同样驱动人脸匹配。
    client = TestClient(create_app(config))
    assert client.delete(f"/api/persons/references/{actor_pair['id']}").status_code == 200
    report = PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    assert report["reference_errors"] == []
    evidence = [e for e in p.evidence() if e["actor_id"] == actor["id"]]
    assert evidence and evidence[0]["role_id"] == role["id"]


def test_role_reference_api_upload_reassign_and_remove(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    other_role = p.save(
        Entity(kind="role", name="少女叶淑柔", work_ids=[work], actor_ids=[actor["id"]])
    )
    client = TestClient(create_app(config))
    created = client.post(
        "/api/persons/references",
        files={"file": ("role.png", photo("green"), "image/png")},
        data={"role_id": role["id"]},
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["role_id"] == role["id"] and payload["actor_id"] == actor["id"]
    assert "stage_id" not in payload
    path = "/api/persons/references/" + payload["id"]
    moved = client.put(path, json={"role_id": other_role["id"]})
    assert moved.status_code == 200 and moved.json()["role_id"] == other_role["id"]
    mismatch = client.put(
        path, json={"role_id": other_role["id"], "entity": {"kind": "actor", "name": "不应写入"}}
    )
    assert mismatch.status_code == 422
    renamed = client.put(
        path,
        json={
            "role_id": other_role["id"],
            "entity": {
                "kind": "role",
                "name": "少女叶淑柔",
                "work_ids": [work],
                "actor_ids": [actor["id"]],
            },
        },
    )
    assert renamed.status_code == 200
    assert p.entity(other_role["id"])["name"] == "少女叶淑柔"
    assert p.entity(actor["id"])["name"] == "演员甲"
    assert client.put(path, json={}).status_code == 422
    assert client.put(path, json={"actor_id": actor["id"], "role_id": other_role["id"]}).status_code == 200
    assert client.put(path, json={"role_id": "missing"}).status_code == 422
    assert (
        client.post(
            "/api/persons/references",
            files={"file": ("x.png", photo("teal"), "image/png")},
        ).status_code
        == 422
    )
    assert client.get(moved.json()["url"]).status_code == 200
    assert client.delete(path).status_code == 200
    assert p.references() == []


def test_role_reference_keeps_the_face_actor_and_competes_with_other_performers(catalogue, config):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"], other["id"]]))
    client = TestClient(create_app(config))
    url = "/api/persons/references"
    ambiguous = client.post(url, files={"file": ("face.png", photo())}, data={"role_id": role["id"]})
    assert ambiguous.status_code == 422
    created = client.post(
        url, files={"file": ("face.png", photo())}, data={"role_id": role["id"], "actor_id": actor["id"]}
    )
    assert created.status_code == 201, created.text
    assert created.json()["actor_ids"] == [actor["id"]]
    PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    assert {e["actor_id"] for e in p.evidence()} == {actor["id"]}
    refs = [dict(obs(), actor_id=a, role_id=role["id"]) for a in role["actor_ids"]]
    assert face_match(obs(), refs, config.person)[2] is False
    # Updating role membership cannot transfer an existing face to a different performer.
    p.save(Entity(kind="role", name=role["name"], work_ids=[work], actor_ids=[other["id"]]), role["id"])
    report = PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    assert report["reference_errors"] == [
        {"reference_id": created.json()["id"], "error": "参考图演员已不属于该角色，请重新配对"}
    ]
    assert all(e["actor_id"] is None for e in p.evidence())


def test_role_reference_edit_validates_final_membership_and_protects_accepted_evidence(catalogue, config):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    sequel = p.save(Entity(kind="work", name="续作"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    ref = p.add_reference(actor["id"], photo(), role_id=role["id"])
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("accepted", "source", "shot0", actor["id"], role["id"], None, 0, 1, "accepted", "{}"),
    )
    client = TestClient(create_app(config))
    path = "/api/persons/references/" + ref["id"]
    entity = {"kind": "role", "name": "新角色名", "work_ids": [sequel["id"]], "actor_ids": [other["id"]]}
    body = {"role_id": role["id"], "actor_id": other["id"], "entity": entity}
    assert client.put(path, json=body).status_code == 422
    assert p.entity(role["id"]) == role
    assert p.reference(ref["id"])["actor_id"] == actor["id"]
    entity["work_ids"] = [work]
    # Removing the reference actor without re-pairing must fail atomically.
    assert client.put(path, json={"role_id": role["id"], "entity": entity}).status_code == 422
    assert p.entity(role["id"]) == role
    # The newly selected performer is validated against the submitted membership, not the old one.
    response = client.put(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["actor_id"] == other["id"]
    assert p.entity(role["id"])["actor_ids"] == [other["id"]]


@pytest.mark.parametrize("role_scoped", [False, True])
def test_reference_can_be_recreated_after_reassignment(catalogue, role_scoped):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    role_id = None
    if role_scoped:
        role_id = p.save(
            Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"], other["id"]])
        )["id"]
    ref = p.add_reference(actor["id"], photo(), role_id=role_id)
    moved = p.update_reference(ref["id"], other["id"], role_id=role_id)
    recreated = p.add_reference(actor["id"], photo(), role_id=role_id)
    assert recreated["id"] != moved["id"]
    assert recreated["actor_id"] == actor["id"]
    assert p.add_reference(actor["id"], photo(), role_id=role_id)["id"] == recreated["id"]


def test_catalogue_page_has_filters_and_no_work_card(config):
    page = TestClient(create_app(config)).get("/persons").text
    for element in ('id="filter-work"', 'id="filter-actor"', 'id="filter-role"'):
        assert element in page
    assert '<option value="work">' not in page
    assert 'id="entity-dialog"' in page and 'id="reference-form"' not in page


def test_persons_ui_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("人物页面交互回归检查需要 Node.js")
    result = subprocess.run(
        [node, "tests/persons_ui.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_frame_capture_targets_a_role_with_a_drawn_box(catalogue, config, monkeypatch):
    """从原片选取参考：截图始终配给角色，可带手动画框（框选错误身份）。"""
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    monkeypatch.setattr("mindex.person_api.command", lambda args: photo("orange", (320, 240)))
    client = TestClient(create_app(config))
    created = client.post(
        "/api/persons/references/from-source",
        json={
            "source_id": "source",
            "timestamp": 1.5,
            "actor_id": actor["id"],
            "role_id": role["id"],
            "bbox": [10, 20, 110, 140],
        },
    )
    assert created.status_code == 201, created.text
    stored = p.references()[-1]
    assert stored["role_id"] == role["id"] and stored["actor_id"] == actor["id"]
    assert stored["data"]["bbox"] == [10, 20, 110, 140]
    with Image.open(p.store.resolve_media(stored["path"])) as cropped:
        assert cropped.size == (100, 120)  # 框选区域外的画面不会进参考图
    assert [r["id"] for r in p.references() if r["role_id"] == role["id"]] == [stored["id"]]
    # 角色没有演员时无法配对，演员不属于该角色同样拒绝。
    orphan = p.save(Entity(kind="role", name="无主角色", work_ids=[work]))
    assert (
        client.post(
            "/api/persons/references/from-source",
            json={"source_id": "source", "timestamp": 0, "role_id": orphan["id"]},
        ).status_code
        == 422
    )
    foreign = p.save(Entity(kind="work", name="其他作品"))
    foreign_role = p.save(Entity(kind="role", name="外片角色", work_ids=[foreign["id"]], actor_ids=[actor["id"]]))
    monkeypatch.setattr("mindex.person_api.command", lambda args: pytest.fail("无效配对不应调用 ffmpeg"))
    assert client.post(
        "/api/persons/references/from-source",
        json={"source_id": "source", "timestamp": 0, "role_id": foreign_role["id"]},
    ).status_code == 422


def test_listing_reports_pending_review_per_shot(catalogue, config):
    """处理与发布面板按分镜显示待核对量，不能把逐帧行数当工作量。"""
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    p.add_reference(actor["id"], photo())
    p.add_reference(other["id"], photo("blue"))
    p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    client = TestClient(create_app(config))
    review = client.get("/api/persons").json()["review_shots"]["source"]
    assert set(review) == {"accepted", "candidate", "rejected"}
    assert review["candidate"] == 3 and review["accepted"] == 0 and review["rejected"] == 0
    rows = p.store.one(
        "SELECT COUNT(*) c FROM person_evidence WHERE source_id=? AND status='candidate'", ("source",)
    )["c"]
    assert rows >= review["candidate"]


def test_reference_quality_threshold_default_zero_matches_everything(catalogue, config):
    """min_reference_quality 默认 0：低质量参考图也参与匹配；帧质量门槛保持独立。"""
    p, _work, actor = catalogue
    ref = p.add_reference(actor["id"], photo())
    client = FakeClient()
    space = client.identity()
    # 预置低质量（0.07）参考特征缓存，处理器不会再调模型。
    key = fingerprint(["reference-v1", ref["digest"], space])
    p.store.execute(
        "INSERT OR REPLACE INTO person_features VALUES(?,?,?,?,?)",
        (
            key,
            None,
            dumps(space),
            "reference",
            dumps({"space": space, "observations": [dict(obs(), face_quality=0.07)]}),
        ),
    )
    report = PersonProcessor(config, client, Tracker).run("source", PersonOptions())
    assert report["reference_errors"] == []
    attributed = [e for e in p.evidence() if e["actor_id"] == actor["id"]]
    assert attributed, "默认门槛 0 时低质量参考图也应当参与匹配"

    # 门槛抬高到 0.5：该参考图被挡下并显式报错，不再归到任何演员。
    config.person = config.person.model_copy(update={"min_reference_quality": 0.5})
    report = PersonProcessor(config, client, Tracker).run("source", PersonOptions())
    assert len(report["reference_errors"]) == 1
    assert report["reference_errors"][0]["reference_id"] == ref["id"]
    assert "清晰度 0.07" in report["reference_errors"][0]["error"]
    assert not [e for e in p.evidence() if e["actor_id"] == actor["id"]]

    # 帧侧门槛不受影响：模糊帧依旧不做自动确认；默认门槛下低质量参考图仍可命中。
    refs = [dict(obs(), face_quality=0.07, actor_id=actor["id"])]
    assert face_match(dict(obs(), face_quality=0.1), refs, config.person)[2] is False
    assert face_match(obs(), refs, config.person.model_copy(update={"min_reference_quality": 0.0}))[0] == actor["id"]


def test_reference_subject_accepts_edge_bodies_but_rejects_groups_and_background_faces():
    """人体检测把边缘路人/肩膀也算一项；参考图只认清晰人脸主体。"""
    face = {"face_feature": [1.0], "face_bbox": [60, 40, 140, 120], "face_quality": 0.9}
    body = {"body_bbox": [40, 20, 160, 230], "body_feature": [0.5]}

    def with_body(**extra):
        return {"body_bbox": None, "face_bbox": None, "face_feature": None, "face_quality": 0.0, **extra}

    # 单人像 + 右上角一块无人脸的肩膀：应当可用。
    shoulder = with_body(body_bbox=[103, 0, 164, 116])
    assert reference_subject([{**face, **body}, shoulder]) is not None
    # 背景远处的人体：同样只算一张清晰人脸，可用。
    distant = with_body(body_bbox=[38, 355, 95, 455])
    assert reference_subject([{**face, **body}, distant]) is not None
    # 无人脸检测时也要能拿到人脸。
    assert reference_subject([with_body(face_feature=[1.0], face_bbox=[60, 40, 140, 120])]) is not None
    # 群照：两张清晰人脸依旧拒绝。
    other = with_body(face_feature=[0.9], face_bbox=[300, 40, 380, 120], body_bbox=[280, 20, 400, 230])
    assert reference_subject([{**face, **body}, other]) is None
    # 主体背对镜头：唯一的人脸属于背景那个更小的人体，应拒绝而不是认错人。
    turned = with_body(body_bbox=[200, 20, 380, 230])
    assert reference_subject([{**face, **body}, turned]) is None
    # 没有清晰人脸：拒绝。
    assert reference_subject([body, shoulder]) is None
    assert reference_subject([]) is None


def test_task_report_counts_shots_not_frame_rows(catalogue, config):
    """人工确认以分镜为单位：报告必须报分镜数，逐帧观测行数会放大两个数量级。"""
    p, _work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    # 两张参考图同一张脸：margin 归零，所有观测都只能落成待确认。
    p.add_reference(actor["id"], photo())
    p.add_reference(other["id"], photo("blue"))
    report = PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    rows = p.store.one(
        "SELECT COUNT(*) c FROM person_evidence WHERE source_id=? AND status='candidate'", ("source",)
    )["c"]
    shots = p.store.one(
        "SELECT COUNT(DISTINCT shot_id) c FROM person_evidence WHERE source_id=? AND status='candidate'",
        ("source",),
    )["c"]
    assert report["candidate_shots"] == shots == 3
    assert report["accepted_shots"] == 0
    assert report["candidates"] == rows >= report["candidate_shots"]
    # 「已确认优先于待确认」的归类规则本身由 review_shots/review_summary 覆盖：
    # test_review_shot_status_is_classified_per_shot_not_per_frame 里的 shot0 同时有已接受与候选帧。


def test_task_report_shot_counts_include_preserved_human_review(catalogue, config):
    from mindex.persons import ShotReview

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    processor = PersonProcessor(config, FakeClient(), Tracker)
    processor.run("source", PersonOptions())
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[{"actor_id": actor["id"]}]))
    report = processor.run("source", PersonOptions(start=0, end=1))
    assert report["accepted_shots"] == 1
    assert report["candidate_shots"] == 0
    assert p.review_shots(shot_id="shot0")["items"][0]["counts"]["accepted"] == 1


def test_old_reference_table_gains_role_id_column_without_losing_rows(config):
    """旧库的 person_references 没有 role_id：首次启动必须能补列，且不丢任何配对。"""
    store = Store(config.data_dir)
    actor = Persons(store).save(Entity(kind="actor", name="演员甲"))
    kept = Persons(store).add_reference(actor["id"], photo())
    with store.connect() as db:
        db.execute("DROP INDEX IF EXISTS person_references_role")
        db.execute("ALTER TABLE person_references DROP COLUMN role_id")
    with store.connect() as db:
        init_person_schema(db)  # 旧库首次启动：补列 + 建索引
        init_person_schema(db)  # 迁移必须幂等
        assert "role_id" in {row["name"] for row in db.execute("PRAGMA table_info(person_references)")}
        assert "person_references_role" in {row["name"] for row in db.execute("PRAGMA index_list(person_references)")}
        assert [r["id"] for r in db.execute("SELECT id FROM person_references")] == [kept["id"]]
    assert Persons(store).reference(kept["id"])["role_id"] is None


def test_legacy_stage_migrates_to_role_without_losing_references_or_published_history(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    legacy = {
        "kind": "stage",
        "name": "老年叶淑柔",
        "aliases": ["老叶"],
        "role_id": role["id"],
        "actor_ids": [actor["id"]],
        "age": "老年",
        "styling": "白发",
        "costume": "灰衣",
    }
    p.store.execute("INSERT INTO person_entities VALUES(?, 'stage', ?)", ("old-stage", dumps(legacy)))
    reference = p.add_reference(actor["id"], photo())
    p.store.execute("UPDATE person_references SET stage_id='old-stage' WHERE id=?", (reference["id"],))
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "old-evidence",
            "source",
            "shot0",
            actor["id"],
            role["id"],
            "old-stage",
            0,
            1,
            "accepted",
            dumps({"reviewed": True}),
        ),
    )
    content = publish_content(p, config)
    release = p.publish()
    frozen = p.store.one("SELECT data FROM person_versions WHERE id=?", (release["person_version"],))["data"]
    p.store.execute("DELETE FROM settings WHERE key='migration.person-roles-v2'")
    migrated = Persons(Store(config.data_dir))
    aged = migrated.entity("old-stage")
    assert aged["kind"] == "role" and aged["work_ids"] == [work] and aged["name"] == "老年叶淑柔"
    assert aged["actor_ids"] == [actor["id"]] and "age" not in aged and "role_id" not in aged
    assert migrated.reference(reference["id"])["actor_id"] == actor["id"]
    assert migrated.add_reference(actor["id"], photo())["id"] == reference["id"]
    evidence = migrated.evidence()[0]
    assert evidence["role_id"] == "old-stage" and evidence["data"]["reviewed"]
    assert "stage_id" not in evidence
    _, published = migrated.published(content)
    assert next(e for e in published["entities"] if e["id"] == "old-stage")["kind"] == "role"
    assert published["evidence"][0]["role_id"] == "old-stage" and "stage_id" not in published["evidence"][0]
    assert (
        migrated.store.one("SELECT data FROM person_versions WHERE id=?", (release["person_version"],))[
            "data"
        ]
        == frozen
    )
    assert Persons(Store(config.data_dir)).entity("old-stage") == aged
    assert resolve_names(TextSearch(text="老年叶淑柔"), migrated.entities())[0] == ["old-stage"]


def test_import_confirm_requires_work_and_clear_rejection_message(catalogue):
    p, _work, _a = catalogue
    draft = p.create_import([photo("silver")])
    with pytest.raises(ValueError, match="请选择导入作品"):
        p.confirm_import(
            draft["id"],
            CastConfirm(work_id="", entries=[{"image_id": draft["images"][0]["id"], "actor_name": "演员甲"}]),
        )


def test_name_resolution_alias_ambiguity_longest_and_intersection(catalogue):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员甲"))
    with pytest.raises(ValueError, match="歧义"):
        resolve_names(TextSearch(text="演员甲哭泣"), p.entities())
    ids, _recognized, rest, _groups = resolve_names(
        TextSearch(text="演员甲哭泣", person_ids=[actor["id"]]), p.entities()
    )
    assert ids == [actor["id"]] and rest == "哭泣"
    assert resolve_names(TextSearch(text="Alice"), p.entities())[0] == [actor["id"]]
    p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    aged = p.save(Entity(kind="role", name="老年叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    assert resolve_names(TextSearch(text="老年叶淑柔"), p.entities())[0] == [aged["id"]]
    segment = {"source_id": "source", "start": 0, "end": 5}
    evidence = [
        {
            "id": "a",
            "source_id": "source",
            "actor_id": actor["id"],
            "role_id": None,
            "start": 0,
            "end": 2,
        },
        {
            "id": "b",
            "source_id": "source",
            "actor_id": other["id"],
            "role_id": None,
            "start": 2,
            "end": 4,
        },
    ]
    assert not overlaps_for(segment, evidence, [actor["id"], other["id"]], "all")
    assert len(overlaps_for(segment, evidence, [actor["id"], other["id"]], "any")) == 2
    evidence[1]["start"] = 1.9
    assert overlaps_for(segment, evidence, [actor["id"], other["id"]], "all")


def test_role_suffix_fuzzy_recall_variants_union_not_ambiguity(catalogue):
    p, work, actor = catalogue
    base = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    aged = p.save(Entity(kind="role", name="老年叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    # 输入基础名：两个角色联合召回，不报歧义；组内任一命中即算出镜。
    ids, recognized, rest, groups = resolve_names(TextSearch(text="叶淑柔在花园"), p.entities())
    assert set(ids) == {base["id"], aged["id"]} and rest == "在花园"
    assert [g for g in groups if set(g) == {base["id"], aged["id"]}]
    assert {r["kind"] for r in recognized} == {"role"}
    # 输入全名：最长名优先，只召回该角色，不扩散到基础角色。
    ids, _recognized, _rest, groups = resolve_names(TextSearch(text="老年叶淑柔"), p.entities())
    assert ids == [aged["id"]] and groups == [[aged["id"]]]
    # 只有长名角色登记时，基础名仍可后缀召回。
    p.store.execute("DELETE FROM person_entities WHERE id=?", (base["id"],))
    ids, _recognized, _rest, _groups = resolve_names(TextSearch(text="叶淑柔"), p.entities())
    assert ids == [aged["id"]]
    # 演员与角色同名仍必须显式选择；变体联合不吞掉跨身份歧义。
    p.save(Entity(kind="actor", name="叶淑柔"))
    with pytest.raises(ValueError, match="歧义"):
        resolve_names(TextSearch(text="叶淑柔"), p.entities())


def obs(face=True, box=None):
    return {
        "body_bbox": box or [0, 0, 100, 120],
        "face_bbox": [10, 10, 40, 40] if face else None,
        "face_feature": [1, 0] if face else None,
        "body_feature": [1, 0],
        "face_quality": 0.95,
        "det_score": 0.99,
    }


class Tracker:
    def __init__(self, fps):
        pass

    def update(self, observations):
        return {i: str(i) for i, o in enumerate(observations)}


def test_anchor_back_view_crossing_gaps_cuts_and_body_candidate(catalogue, config):
    _p, work, actor = catalogue
    refs = [{**obs(), "actor_id": actor["id"]}]
    shot = {
        "id": "s",
        "source_id": "source",
        "cache_id": "c",
        "start": 0,
        "end": 2,
        "space": {"model": "fixture"},
    }
    frames = [
        {"timestamp": 0, "observations": [obs()]},
        {"timestamp": 0.2, "observations": [obs(False)]},
        {"timestamp": 0.4, "observations": [obs(False), obs(False, [10, 0, 110, 120])]},
        {"timestamp": 0.6, "observations": [obs(False)]},
        {"timestamp": 0.8, "observations": []},
        {"timestamp": 1, "observations": [obs(False)]},
    ]
    rows = attribute_frames(frames, refs, config.person, shot, work, [], Tracker)
    assert [r["data"]["evidence_type"] for r in rows[:2]] == ["face", "track"]
    assert all(r["status"] == "candidate" for r in rows[2:])
    assert rows[0]["end"] == 0.2
    samples = [{"work_id": work, "feature": [1, 0], "actor_id": actor["id"], "role_id": "r"}]
    rows = attribute_frames(
        [{"timestamp": 0, "observations": [obs(False)]}],
        refs,
        config.person,
        shot,
        work,
        [],
        Tracker,
        samples,
    )
    assert rows[0]["data"]["evidence_type"] == "body_candidate" and rows[0]["status"] == "candidate"
    assert rows[0]["actor_id"] == actor["id"]
    assert (
        attribute_frames(
            [{"timestamp": 0, "observations": [obs(False)]}],
            refs,
            config.person,
            shot,
            "other-work",
            [],
            Tracker,
            samples,
        )[0]["actor_id"]
        is None
    )
    # Score gap compares distinct people, never two references of the same actor.
    assert face_match(obs(), refs * 3, config.person)[2]
    assert not face_match(obs(), refs + [dict(refs[0], actor_id="other")], config.person)[2]
    assert not face_match(dict(obs(), face_quality=0.1), refs, config.person)[2]


def test_real_bytetrack_body_ids_survive_motion_and_reset():
    tracker = ShotTracker(5)
    assert tracker.update([obs()])[0] == tracker.update([obs(False, [1, 0, 101, 120])])[0]
    assert tracker.update([]) == {}
    assert ShotTracker(5).update([obs()])


class FakeClient:
    def __init__(self):
        self.space = {"model": "cheetah_l", "weights": "fixture1"}
        self.images = self.videos = 0
        self.fail_at = None

    def identity(self):
        return self.space.copy()

    def image(self, path):
        self.images += 1
        return {"space": self.identity(), "observations": [obs()]}

    def video(self, path, start, end, fps):
        self.videos += 1
        if self.fail_at == self.videos:
            raise ModelError("simulated interruption")
        return {
            "space": self.identity(),
            "frames": [
                {
                    "timestamp": start,
                    "observations": [obs()],
                    "image_base64": base64.b64encode(photo()).decode(),
                }
            ],
        }


def test_checkpoint_append_rename_no_video_reinference_space_isolation(catalogue, config):
    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    client = FakeClient()
    processor = PersonProcessor(config, client, Tracker)
    client.fail_at = 2
    with pytest.raises(ModelError, match="interruption"):
        processor.run("source", PersonOptions())
    assert len(p.store.rows("SELECT * FROM person_coverage")) == 1
    client.fail_at = None
    report = processor.run("source", PersonOptions())
    assert report["reused_shots"] == 1 and report["extracted_shots"] == 2
    assert client.images == 1
    videos = client.videos
    p.add_reference(actor["id"], photo("green"))
    report = processor.run("source", PersonOptions())
    assert report["reused_shots"] == 3 and client.videos == videos and client.images == 2
    p.save(Entity(kind="actor", name="新名字"), actor["id"])
    processor.run("source", PersonOptions())
    assert client.videos == videos and client.images == 2
    # Manual rejection remains stable when adding new reference images.
    evidence = p.evidence()[0]
    p.review(evidence["id"], Review(accept=False, reason="不是此人"))
    processor.run("source", PersonOptions())
    assert (
        p.store.one("SELECT status FROM person_evidence WHERE id=?", (evidence["id"],))["status"]
        == "rejected"
    )
    client.space["weights"] = "fixture2"
    processor.run("source", PersonOptions())
    assert client.videos == videos + 3 and client.images == 4
    assert (
        len({row["space"] for row in p.store.rows("SELECT space FROM person_features WHERE kind='shot'")})
        == 2
    )


def test_service_contract_gpu_lock_auth_and_upload(config, monkeypatch, tmp_path):
    from filelock import FileLock, Timeout

    from mindex.person_server import create_person_app

    monkeypatch.setenv("MINDEX_GPU_LOCK", str(tmp_path / "gpu.lock"))
    monkeypatch.setenv("MINDEX_MODEL_API_KEY", "test-secret")

    class Engine(FakeClient):
        def image(self, path):
            return super().image(path)

        def video(self, path, start, end, fps):
            return super().video(path, start, end, fps)

    with TestClient(create_person_app(config, Engine)) as client:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer test-secret"}).json()["ready"]
        with pytest.raises(Timeout), FileLock(str(tmp_path / "gpu.lock"), timeout=0):
            pass
        response = client.post(
            "/extract/video",
            headers={"Authorization": "Bearer test-secret"},
            data={"start": "0", "end": "1", "fps": "5"},
            files={"file": ("v.mp4", b"fixture", "video/mp4")},
        )
        assert response.status_code == 200 and response.json()["frames"][0]["timestamp"] == 0
        assert (
            client.post(
                "/extract/video",
                headers={"Authorization": "Bearer test-secret"},
                data={"start": "0", "end": "1", "fps": "6"},
                files={"file": ("v.mp4", b"fixture")},
            ).status_code
            == 422
        )


def publish_content(p, config, identifier="content1", dialogues=None):
    from mindex.indexing import VectorIndex
    from mindex.types import Annotation

    spec = {
        "format": 2,
        "mode": "auto",
        "space": {"model": "fixture"},
        "preprocessing": config.video.model_dump(),
    }
    p.store.execute(
        "INSERT INTO index_versions(id,spec,status) VALUES(?,?,'ready')", (identifier, dumps(spec))
    )
    for shot in p.store.segments("source"):
        snapshot = {
            **shot,
            "source_name": "影片",
            "source_path": "source.mp4",
            "source_duration": 3,
            "annotation": Annotation(description="两人聊天").model_dump(),
            "dialogue": [],
            "views": {"dialogue": dialogues[shot["id"]] if dialogues else "演员甲快回来"},
            "indexed_views": ["video", "dialogue"],
        }
        p.store.execute(
            "INSERT INTO index_members VALUES(?,?,?,1)", (identifier, shot["id"], dumps(snapshot))
        )
    VectorIndex(p.store.root, identifier, create=True)
    p.store.activate(identifier)
    return identifier


def test_mentioned_role_variants_union_and_latin_name_boundaries(catalogue, config):
    from mindex.search import SearchEngine

    p, work, actor = catalogue
    latin = p.save(Entity(kind="actor", name="Ann"))
    base = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    aged = p.save(Entity(kind="role", name="老年叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    publish_content(
        p, config, dialogues={"shot0": "Anna walked", "shot1": "Ann walked", "shot2": "老年叶淑柔说话"}
    )

    class Offline:
        def identity(self):
            raise ModelError("offline")

    engine = SearchEngine(config, Offline())
    result = engine.text(TextSearch(text="Ann", person_mode="mentioned", granularity="shot"))
    assert result["eligible_segments"] == 1
    assert [hit["id"] for hit in result["results"]] == ["shot1"]
    result = engine.text(TextSearch(text="叶淑柔", person_mode="mentioned", granularity="shot"))
    assert result["eligible_segments"] == 1
    result = engine.text(
        TextSearch(
            person_ids=[base["id"], aged["id"], latin["id"]], person_mode="mentioned", granularity="shot"
        )
    )
    assert result["eligible_segments"] == 0


def test_strict_no_dialogue_leak_pretruncate_filter_offline_name_and_rollback(catalogue, config, monkeypatch):
    from mindex.curation import CurationPatch, Library
    from mindex.indexing import VectorIndex
    from mindex.search import SearchEngine

    p, _work, actor = catalogue
    version = publish_content(p, config)
    engine = SearchEngine(config)
    # Registered name with no published person processing must never degrade to mention mode.
    assert engine.text(TextSearch(text="演员甲", granularity="shot"))["results"] == []
    p.add_reference(actor["id"], photo())
    PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    rows = p.evidence()
    for r in rows[1:]:
        p.review(r["id"], Review(accept=False, reason="画面未出镜，仅在对白中提到"))
    release1 = p.publish()

    class Offline:
        def identity(self):
            raise ModelError("WeMM offline")

        def text(self, *args, **kwargs):
            raise AssertionError("name only must not embed")

    engine = SearchEngine(config, Offline())
    result = engine.text(TextSearch(text="演员甲", granularity="shot"))
    assert [r["id"] for r in result["results"]] == ["shot0"]
    assert result["person_version"] == release1["person_version"]
    assert result["results"][0]["person_evidence"][0]["actor_name"] == "演员甲"
    mentioned = engine.text(TextSearch(text="演员甲", person_mode="mentioned", granularity="shot"))
    assert len(mentioned["results"]) == 3
    assert all(m["channel"] == "dialogue" for r in mentioned["results"] for m in r["matches"])

    # Ensure onscreen filtering reaches vector retrieval before its candidate limit.
    class Embedder:
        def identity(self):
            return {"model": "fixture"}

        def text(self, texts, role="query"):
            assert texts == ["哭泣"]
            return [[1, 0]]

    seen = []

    def query_vectors(self, vector, view, ids, count):
        seen.append(ids)
        return [{"id": i, "distance": 0.1} for i in ids[:count]]

    monkeypatch.setattr(VectorIndex, "query", query_vectors)
    config.search.candidates = 1
    result = SearchEngine(config, Embedder()).text(TextSearch(text="演员甲哭泣", granularity="shot"))
    assert result["results"][0]["id"] == "shot0" and all(ids == ["shot0"] for ids in seen)
    # User corrections do not mutate a published version.
    p.review(rows[0]["id"], Review(accept=False, reason="误识别"))
    assert engine.text(TextSearch(text="演员甲", granularity="shot"))["results"]
    version2 = publish_content(p, config, "content2")
    release2 = p.publish()
    assert not engine.text(TextSearch(text="演员甲", granularity="shot"))["results"]
    p.store.activate(version)
    assert engine.text(TextSearch(text="演员甲", granularity="shot"))["results"]
    p.store.activate_release(release2["release_id"])
    assert p.store.active_index()["id"] == version2
    p.store.activate_release(release1["release_id"])
    Library(p.store).patch("segment", "shot0", CurationPatch(hidden=True))
    assert not engine.text(TextSearch(text="演员甲", granularity="shot"))["results"]
    p.store.execute("UPDATE index_versions SET status='building' WHERE id=?", (version2,))
    with pytest.raises(ValueError):
        p.store.activate_release(release2["release_id"])
    assert p.store.active_index()["id"] == version


def test_purge_keeps_global_references_and_removes_all_source_evidence(catalogue, config):
    from mindex.purge import purge_source

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    publish_content(p, config)
    p.publish()
    purge_source(p.store, "source")
    assert p.entities("actor") and p.references()
    for table in ["person_sources", "person_features", "person_evidence", "person_coverage", "person_tasks"]:
        assert p.store.rows(f"SELECT * FROM {table} WHERE source_id=?", ("source",)) == []
    assert all(not json.loads(r["data"])["evidence"] for r in p.store.rows("SELECT * FROM person_versions"))
    assert not (p.store.root / "persons" / "frames" / "source").exists()


def test_local_sampling_retains_real_pts_short_shots_and_batched_contract(config, video_file, monkeypatch):
    from mindex.person_frames import sample_frames
    from mindex.person_processing import PersonClient

    frames = list(sample_frames(video_file, 0, 1, 5))
    assert frames and len(frames) <= 5 and frames[0][0] == 0
    assert all(b[0] - a[0] >= 0.2 - 1e-8 for a, b in itertools.pairwise(frames))
    assert len(list(sample_frames(video_file, 1, 1.02, 5))) == 1
    client = PersonClient(config)
    batches = []

    def request(method, path, **kwargs):
        assert method == "POST" and path == "/extract/frames"
        batches.append(kwargs["files"])
        assert len(kwargs["files"]) <= 16
        return {"space": {"model": "cheetah_l"}, "observations": [[obs()] for _ in kwargs["files"]]}

    monkeypatch.setattr(client, "request", request)
    result = client.video(video_file, 0, 4, 5)
    assert [f["timestamp"] for f in result["frames"]] == [t for t, _ in sample_frames(video_file, 0, 4, 5)]
    assert len(batches) == 2


def test_partial_coverage_is_reported_and_review_roles_are_work_scoped(catalogue):
    from mindex.persons import coverage_missing

    p, work, actor = catalogue
    snapshots = [{"source_id": "source", "start": 0, "end": 100}]
    assert coverage_missing(snapshots, [{"source_id": "source", "data": {"start": 0, "end": 10}}]) == [
        "source"
    ]
    assert (
        coverage_missing(
            snapshots,
            [
                {"source_id": "source", "data": {"start": 0, "end": 40}},
                {"source_id": "source", "data": {"start": 40, "end": 100}},
            ],
        )
        == []
    )
    other_work = p.save(Entity(kind="work", name="另一个作品"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[other_work["id"]], actor_ids=[actor["id"]]))
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("e", "source", "shot0", actor["id"], None, None, 0, 1, "candidate", dumps({"work_id": work})),
    )
    with pytest.raises(ValueError, match="关系不符"):
        p.review("e", Review(actor_id=actor["id"], role_id=role["id"], reason="错误角色"))


def test_tasks_return_ids_retry_and_restart_checkpoints(catalogue, config, monkeypatch):
    from filelock import FileLock

    from mindex.person_processing import PersonTasks
    from mindex.runtime_settings import RuntimeConfig

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    processor = PersonProcessor(config, FakeClient(), Tracker)
    monkeypatch.setattr("mindex.person_processing.PersonProcessor", lambda cfg: processor)
    manager = PersonTasks(RuntimeConfig(config, p.store), p.store)
    task = manager.start("source", PersonOptions())
    assert task["id"]
    thread = manager.threads.get(task["id"])
    if thread:
        thread.join(10)
    assert manager.get(task["id"])["status"] == "complete"
    assert manager.get(task["id"])["result"]["accepted"] == 3
    assert "config" not in manager.get(task["id"])
    with FileLock(str(p.store.root / "pipeline.lock"), timeout=0), pytest.raises(Exception) as exc:
        manager.start("source", PersonOptions())
    assert exc.value.status_code == 409
    p.store.execute("UPDATE person_tasks SET status='running' WHERE id=?", (task["id"],))
    restarted = PersonTasks(RuntimeConfig(config, p.store), p.store)
    assert restarted.get(task["id"])["status"] == "interrupted"
    restarted.retry(task["id"])
    thread = restarted.threads.get(task["id"])
    if thread:
        thread.join(10)
    assert restarted.get(task["id"])["result"]["reused_shots"] == 3


def test_manual_anchor_only_extends_reliable_track_and_rejection_stops_it(catalogue, config):
    from mindex.config import fingerprint

    _p, work, actor = catalogue
    shot = {"id": "s", "source_id": "source", "cache_id": "cache", "end": 1, "space": {}}
    frames = [
        {"timestamp": 0, "observations": [obs(False)]},
        {"timestamp": 0.2, "observations": [obs(False)]},
        {"timestamp": 0.4, "observations": [obs(False)]},
        {"timestamp": 0.6, "observations": [obs(False)]},
    ]
    manual = {
        fingerprint(["cache", 0, 0]): {
            "actor_id": actor["id"],
            "role_id": "role",
            "status": "accepted",
        },
        fingerprint(["cache", 0.4, 0]): {
            "actor_id": actor["id"],
            "role_id": None,
            "status": "rejected",
        },
    }
    rows = attribute_frames(frames, [], config.person, shot, work, [], Tracker, manual_anchors=manual)
    assert rows[0]["data"]["evidence_type"] == "manual"
    assert rows[1]["status"] == "accepted" and rows[1]["role_id"] == "role"
    assert rows[2]["status"] == "rejected"
    assert rows[3]["status"] == "candidate"


def test_person_selection_without_text_and_evidence_paging(catalogue, config):
    from mindex.search import SearchEngine

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    publish_content(p, config)
    p.publish()
    assert (
        len(SearchEngine(config).text(TextSearch(person_ids=[actor["id"]], granularity="shot"))["results"])
        == 3
    )
    client = TestClient(create_app(config))
    first = client.get("/api/persons/evidence?limit=1").json()
    second = client.get("/api/persons/evidence?limit=1&offset=1").json()
    assert first["total"] == 3 and len(first["items"]) == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert "body_feature" not in first["items"][0]["data"]
    assert client.get("/api/persons/evidence/" + first["items"][0]["id"]).status_code == 200


def test_shot_review_marks_persons_and_rejects_unmarked_rows(catalogue):
    from mindex.persons import ShotReview

    p, work, a = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[a["id"]]))
    auto = {
        "auto-accept": ("shot0", a["id"], None, 0.0, 0.2, "accepted"),
        "unknown": ("shot0", None, None, 0.2, 0.4, "candidate"),
        "other-cand": ("shot0", other["id"], None, 0.4, 0.6, "candidate"),
        "next-shot": ("shot1", a["id"], None, 1.2, 1.4, "accepted"),
    }
    for key, (shot_id, actor_id, role_id, start, end, status) in auto.items():
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                "source",
                shot_id,
                actor_id,
                role_id,
                None,
                start,
                end,
                status,
                dumps({"image_path": f"persons/frames/{key}.png", "work_id": work}),
            ),
        )
    result = p.shot_review(
        ShotReview(
            source_id="source",
            shot_id="shot0",
            selections=[{"actor_id": a["id"], "role_id": role["id"]}, {"actor_id": other["id"]}],
        )
    )
    assert result["shot_id"] == "shot0" and len(result["accepted"]) == 2
    rows = {r["id"]: r for r in p.evidence("source") if r["shot_id"] == "shot0"}
    # 勾选的每个人生成一条覆盖整个分镜的 manual 证据；未勾选的自动行全部驳回留档。
    manual = {
        r["actor_id"]: r
        for r in rows.values()
        if r["data"].get("evidence_type") == "manual" and r["status"] == "accepted"
    }
    assert set(manual) == {a["id"], other["id"]} and manual[a["id"]]["role_id"] == role["id"]
    for r in manual.values():
        assert r["status"] == "accepted" and (r["start"], r["end"]) == (0.0, 1.0)
        assert r["data"]["reviewed"] and r["data"]["image_path"]
    assert all(
        rows[k]["status"] == "rejected" and rows[k]["data"]["reviewed"] for k in auto if k != "next-shot"
    )
    assert p.store.one("SELECT status FROM person_evidence WHERE id='next-shot'")["status"] == "accepted"
    # 幂等重审：减少勾选后旧 manual 行随之驳回，不产生重复。
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[{"actor_id": other["id"]}]))
    rows = {r["id"]: r for r in p.evidence("source") if r["shot_id"] == "shot0"}
    manual = [
        r for r in rows.values() if r["data"].get("evidence_type") == "manual" and r["status"] == "accepted"
    ]
    assert len(manual) == 1 and manual[0]["actor_id"] == other["id"]
    assert rows["auto-accept"]["status"] == "rejected"
    # 角色不属于该作品的关系校验仍然生效。
    foreign = p.save(Entity(kind="work", name="另一部"))
    bad = p.save(Entity(kind="role", name="外片角色", work_ids=[foreign["id"]], actor_ids=[a["id"]]))
    with pytest.raises(ValueError, match="关系不符"):
        p.shot_review(
            ShotReview(
                source_id="source",
                shot_id="shot0",
                selections=[{"actor_id": a["id"], "role_id": bad["id"]}],
            )
        )
    # 空选 = 排除整个分镜。
    p.shot_review(ShotReview(source_id="source", shot_id="shot1", selections=[]))
    assert all(r["status"] == "rejected" for r in p.evidence("source") if r["shot_id"] == "shot1")


def test_review_shots_listing_groups_by_shot_filters_and_pages(catalogue):
    p, work, a = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    auto = [
        ("shot0", a["id"], 0.0, 0.2, "accepted"),
        ("shot0", None, 0.2, 0.4, "candidate"),
        ("shot0", other["id"], 0.4, 0.6, "candidate"),
        ("shot1", a["id"], 1.0, 1.2, "accepted"),
    ]
    for n, (shot_id, actor_id, start, end, status) in enumerate(auto):
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"e{n}",
                "source",
                shot_id,
                actor_id,
                None,
                None,
                start,
                end,
                status,
                dumps({"image_path": f"persons/frames/e{n}.png", "work_id": work, "body_feature": [1]}),
            ),
        )
    data = p.review_shots()
    assert data["total"] == 2
    item = data["items"][0]
    assert (item["shot_id"], item["start"], item["end"]) == ("shot0", 0.0, 1.0)
    assert item["counts"] == {"accepted": 1, "candidate": 2, "rejected": 0}
    assert len(item["evidence"]) == 3
    assert all("body_feature" not in r["data"] for r in item["evidence"])
    assert item["image_url"] == "/api/persons/evidence/e0/image"
    # 分镜级状态：shot0 有一帧被确认就整体算已接受，不再算待确认。
    assert [i["shot_id"] for i in p.review_shots(status="accepted")["items"]] == ["shot0", "shot1"]
    assert p.review_shots(status="candidate")["items"] == []
    assert p.review_shots(status="candidate")["total"] == 0
    # 卡内始终是该分镜的完整证据，逐帧计数不被状态过滤裁剪。
    shot0 = next(i for i in p.review_shots(status="accepted")["items"] if i["shot_id"] == "shot0")
    assert shot0["counts"]["accepted"] == 1
    assert p.review_shots(source_id="source", limit=1, offset=1)["items"][0]["shot_id"] == "shot1"


def test_review_shot_status_is_classified_per_shot_not_per_frame(catalogue):
    """任一帧已确认 → 分镜已接受；无已确认帧但有候选 → 待确认；整镜无人 → 已排除。"""
    p, work, a = catalogue
    rows = [
        ("shot0", "accepted", 0.0),  # 已确认一帧即可整镜算已接受
        ("shot0", "candidate", 0.2),
        ("shot1", "candidate", 1.0),  # 无任何已确认帧 → 待确认
        ("shot1", "candidate", 1.2),
        ("shot2", "rejected", 2.0),  # 整镜被排除
    ]
    for n, (shot_id, status, start) in enumerate(rows):
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"r{n}",
                "source",
                shot_id,
                a["id"],
                None,
                None,
                start,
                start + 0.2,
                status,
                dumps({"work_id": work}),
            ),
        )
    ids = lambda status: [i["shot_id"] for i in p.review_shots(status=status)["items"]]
    assert ids("accepted") == ["shot0"]
    assert ids("candidate") == ["shot1"]
    assert ids("rejected") == ["shot2"]
    assert p.review_shots()["total"] == 3
    # 已接受的镜头仍要能整镜核对结清，卡内证据不因状态过滤而缺失。
    assert len(p.review_shots(status="accepted")["items"][0]["evidence"]) == 2
    # 处理面板显示的待核对量必须与审核列表同一套分类，否则会误导工作量。
    assert p.review_summary() == {"source": {"accepted": 1, "candidate": 1, "rejected": 1}}
    # Replaced segment layouts can leave evidence for shots that are no longer reviewable.
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("stale", "source", "removed-shot", a["id"], None, None, 10, 11, "candidate", "{}"),
    )
    assert p.review_shots()["total"] == 3
    assert p.review_summary() == {"source": {"accepted": 1, "candidate": 1, "rejected": 1}}


def test_task_delete_only_failed_or_interrupted(catalogue, config, monkeypatch):
    from mindex.person_processing import PersonTasks
    from mindex.runtime_settings import RuntimeConfig

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    processor = PersonProcessor(config, FakeClient(), Tracker)
    monkeypatch.setattr("mindex.person_processing.PersonProcessor", lambda cfg: processor)
    manager = PersonTasks(RuntimeConfig(config, p.store), p.store)
    task = manager.start("source", PersonOptions())
    thread = manager.threads.get(task["id"])
    if thread:
        thread.join(10)
    with pytest.raises(Exception) as exc:
        manager.delete(task["id"])
    assert exc.value.status_code == 409
    p.store.execute("UPDATE person_tasks SET status='failed',error='fixture' WHERE id=?", (task["id"],))
    assert manager.delete(task["id"]) == {"removed": task["id"]}
    with pytest.raises(Exception) as exc:
        manager.get(task["id"])
    assert exc.value.status_code == 404


def test_shot_review_and_task_delete_api(catalogue, config):
    p, work, a = catalogue
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[a["id"]]))
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("e0", "source", "shot0", None, None, None, 0.0, 0.4, "candidate", dumps({"work_id": work})),
    )
    client = TestClient(create_app(config))
    listing = client.get("/api/persons/review/shots?source_id=source").json()
    assert listing["total"] == 1 and listing["items"][0]["shot_id"] == "shot0"
    reviewed = client.post(
        "/api/persons/review/shot",
        json={
            "source_id": "source",
            "shot_id": "shot0",
            "selections": [{"actor_id": a["id"], "role_id": role["id"]}],
        },
    ).json()
    assert reviewed["shot_id"] == "shot0" and len(reviewed["accepted"]) == 1
    assert client.get("/api/persons/evidence?status=candidate").json()["total"] == 0
    # 旧的逐条审核接口已移除。
    assert client.post("/api/persons/evidence/e0/review", json={}).status_code in {404, 405}
    failed, running = "task-failed", "task-running"
    for tid, status in [(failed, "failed"), (running, "running")]:
        p.store.execute(
            "INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
            (tid, "source", status, config.model_dump_json(), PersonOptions().model_dump_json()),
        )
    assert client.delete(f"/api/persons/tasks/{failed}").json() == {"removed": failed}
    assert client.delete("/api/persons/tasks/missing").status_code == 404
    assert client.delete(f"/api/persons/tasks/{running}").status_code == 409


def test_task_config_roundtrip_keeps_shot_checkpoint_key(catalogue, config):
    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    client = FakeClient()
    PersonProcessor(config, client, Tracker).run("source", PersonOptions())
    frozen = Config.model_validate_json(config.model_dump_json())
    report = PersonProcessor(frozen, client, Tracker).run("source", PersonOptions())
    assert report["reused_shots"] == 3 and client.videos == 3


def test_shot_review_survives_feature_space_changes(catalogue, config):
    from mindex.persons import ShotReview

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    client = FakeClient()
    processor = PersonProcessor(config, client, Tracker)
    processor.run("source", PersonOptions())
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[]))
    client.space["weights"] = "new-model-space"
    processor.run("source", PersonOptions())
    assert all(e["status"] == "rejected" for e in p.evidence("source") if e["shot_id"] == "shot0")


def test_duplicate_role_names_remain_ambiguous_and_sibling_variants_union(catalogue):
    p, work, actor = catalogue
    other_work = p.save(Entity(kind="work", name="别的作品"))
    p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    p.save(Entity(kind="role", name="叶淑柔", work_ids=[other_work["id"]], actor_ids=[actor["id"]]))
    with pytest.raises(ValueError, match="歧义"):
        resolve_names(TextSearch(text="叶淑柔"), p.entities())
    aged = p.save(Entity(kind="role", name="老年叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    young = p.save(Entity(kind="role", name="少年叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    ids, _, _, groups = resolve_names(TextSearch(text="叶淑柔"), [aged, young])
    assert set(ids) == {aged["id"], young["id"]}
    assert len(groups) == 1 and set(groups[0]) == set(ids)


def test_import_existing_actor_without_name_and_duplicate_name_requires_choice(catalogue):
    p, work, actor = catalogue
    draft = p.create_import([photo()])
    body = CastConfirm(
        work_id=work, entries=[{"image_id": draft["images"][0]["id"], "actor_id": actor["id"]}]
    )
    assert p.confirm_import(draft["id"], body)[0]["actor_id"] == actor["id"]
    p.save(Entity(kind="actor", name=actor["name"]))
    draft = p.create_import([photo()])
    with pytest.raises(ValueError, match="歧义"):
        p.confirm_import(
            draft["id"],
            CastConfirm(
                work_id=work, entries=[{"image_id": draft["images"][0]["id"], "actor_name": actor["name"]}]
            ),
        )
    assert p.get_import(draft["id"])["status"] == "draft"


def test_cli_shot_review_and_single_shot_lookup(catalogue, config, tmp_path):
    import yaml
    from typer.testing import CliRunner

    from mindex.cli import app

    _p, _work, actor = catalogue
    file = tmp_path / "review.json"
    file.write_text(
        dumps({"source_id": "source", "shot_id": "shot0", "selections": [{"actor_id": actor["id"]}]})
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    response = CliRunner().invoke(
        app, ["--config", str(cfg), "--no-runtime-overrides", "persons", "review", str(file)]
    )
    assert response.exit_code == 0, response.output
    assert json.loads(response.output)["shot_id"] == "shot0"
    client = TestClient(create_app(config))
    shot = client.get("/api/persons/review/shots/shot0?source_id=source")
    assert shot.status_code == 200 and shot.json()["counts"]["accepted"] == 1
    assert client.get("/api/persons/review/shots/missing").status_code == 404


def test_cast_parser_keeps_valid_candidates_and_requires_user_crop_confirmation(config, monkeypatch):
    from mindex.person_api import parse_cast

    entries = [
        {
            "image_id": "landscape",
            "actor_name": f"演员{i}",
            "role_name": "老年叶淑柔" if i == 0 else "",
            "bbox": [i * 100, 10, i * 100 + 80, 200],
            "single_person": True,
            "actor_id": "model-guessed-id",
        }
        for i in range(8)
    ]
    entries.append({"image_id": "portrait", "actor_name": "错误坐标", "bbox": [0, 0, 900, 900]})

    def complete(*args, **kwargs):
        assert "stage_name" not in kwargs["json"]["messages"][0]["content"][0]["text"]
        return {"choices": [{"message": {"content": dumps({"entries": entries, "warnings": []})}}]}

    monkeypatch.setattr("mindex.person_api.request", complete)
    result = parse_cast(
        config,
        {
            "images": [
                {"id": "landscape", "width": 850, "height": 260, "base64": ""},
                {"id": "portrait", "width": 300, "height": 700, "base64": ""},
            ]
        },
    )
    assert len(result["entries"]) == 8
    assert result["entries"][0]["role_name"] == "老年叶淑柔"
    assert all(not e["single_person"] and e["actor_id"] is None for e in result["entries"])
    assert result["entries"][0]["bbox"] == [0, 10, 80, 200]
    assert result["warnings"]
