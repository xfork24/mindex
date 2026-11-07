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

from api import create_app
from models.adapters import ModelError
from persons.person_models import CastConfirm, Entity, Review
from persons.person_names import overlaps_for, resolve_names
from persons.person_processing import (
    PersonOptions,
    PersonProcessor,
    ShotTracker,
    attribute_frames,
    ensure_reference_quality,
    face_match,
    reference_subject,
)
from persons.persons import Persons
from shared.config import Config, fingerprint
from shared.person_schema import init_person_schema
from shared.storage import Store, dumps
from shared.types import TextSearch

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


def photo(color="red", size=(100, 120)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


@pytest.fixture
def catalogue(config, monkeypatch):
    monkeypatch.setattr(
        "shared.shot_media.probe",
        lambda _: {"duration": 1, "fps": 25, "width": 100, "height": 120, "audio": False},
    )
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


def test_entity_delete_guards_and_pairing_cleanup(catalogue, config):
    p, work, actor = catalogue
    client = TestClient(create_app(config))
    role = p.save(Entity(kind="role", name="待删角色", work_ids=[work], actor_ids=[actor["id"]]))
    empty = p.save(Entity(kind="actor", name="空演员"))

    # 空身份直接删除；配对清理：删除演员后角色不再悬挂 actor_ids。
    assert client.delete(f"/api/persons/entities/{empty['id']}").status_code == 200
    paired = p.save(Entity(kind="actor", name="配对演员"))
    p.save(
        Entity.model_validate({k: v for k, v in role.items() if k != "id"}).model_copy(
            update={"actor_ids": [actor["id"], paired["id"]]}
        ),
        role["id"],
    )
    assert client.delete(f"/api/persons/entities/{paired['id']}").status_code == 200
    assert p.entity(role["id"])["actor_ids"] == [actor["id"]]
    with pytest.raises(ValueError):
        p.entity(paired["id"])

    # 参考图不再阻碍删除，随身份删除；角色照片与演员照片仍独立清理。
    draft = p.create_import([photo()])
    confirmed = p.confirm_import(
        draft["id"],
        CastConfirm(
            work_id=work,
            entries=[
                {
                    "image_id": draft["images"][0]["id"],
                    "actor_name": "带图演员",
                    "role_name": "带图角色",
                    "bbox": [1, 1, 60, 90],
                }
            ],
        ),
    )
    with_ref = confirmed["confirmed"][0]["actor_id"]

    # 有已接受出镜证据的身份拒绝删除；清掉证据后可删。
    store = p.store
    store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("ev1", "source", "shot0", actor["id"], role["id"], None, 0, 1, "accepted", "{}"),
    )
    assert client.delete(f"/api/persons/entities/{actor['id']}").status_code == 422
    assert "出镜证据" in client.delete(f"/api/persons/entities/{actor['id']}").json()["detail"]["message"]
    store.execute("UPDATE person_evidence SET status='rejected' WHERE id='ev1'")
    assert client.delete(f"/api/persons/entities/{actor['id']}").status_code == 200

    # 删除角色只清理其角色级参考图，删除演员再清理演员级参考图。作品暂不支持页面删除。
    role_with_ref = confirmed["confirmed"][0]["role_id"]
    p.add_reference(with_ref, photo("gold"), role_id=role_with_ref)
    removed_role = client.delete(f"/api/persons/entities/{role_with_ref}")
    assert removed_role.status_code == 200 and removed_role.json()["removed_references"] == 1
    assert p.references() and all(r["role_id"] != role_with_ref for r in p.references())
    removed_actor = client.delete(f"/api/persons/entities/{with_ref}")
    assert removed_actor.status_code == 200 and removed_actor.json()["removed_references"] == 1
    assert not p.references()
    assert client.delete(f"/api/persons/entities/{work}").status_code == 422


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
    refs = [{**obs(), "actor_id": a["id"], "role_id": role["id"]}]
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
    assert len(result["confirmed"]) == 8 and len(p.references()) == 8
    assert result["skipped_duplicates"] == [] and result["ignored_existing"] == 0
    with Image.open(p.store.resolve_media(p.references()[0]["path"])) as image:
        assert image.size == (80, 190)
    old_role = result["confirmed"][0]["role_id"]
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
    assert result2["confirmed"][0]["actor_id"] == actor["id"]
    assert len(p.entities("actor")) == 9  # 复用已有演员甲，仅新增 8 个条目演员
    assert result2["confirmed"][0]["role_id"] != old_role
    aged = p.entity(result2["confirmed"][0]["role_id"])
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
    assert result3["confirmed"][0]["role_id"] == result2["confirmed"][0]["role_id"]
    roles = [r for r in p.entities("role") if r["name"] == "老年叶淑柔"]
    assert len(roles) == 1 and set(roles[0]["actor_ids"]) == {actor["id"]}
    with pytest.raises(ValueError, match="已确认"):
        p.confirm_import(draft["id"], CastConfirm(work_id=work, entries=entries))
    # 单人确认门禁已移除：照片框由用户自行把关；批内重复（演员+角色同名且未关联现有身份）静默跳过。
    group = p.create_import([photo()])
    group_image = group["images"][0]["id"]
    group_result = p.confirm_import(
        group["id"],
        CastConfirm(
            work_id=work,
            entries=[
                {
                    "image_id": group_image,
                    "actor_name": "多人合照",
                    "role_name": "群像",
                    "bbox": [0, 0, 50, 50],
                },
                {"image_id": group_image, "actor_name": "多人合照", "role_name": "群像"},
                {
                    "image_id": group_image,
                    "actor_name": "多人合照",
                    "role_name": "群像",
                    "bbox": [5, 5, 20, 20],
                },
            ],
        ),
    )
    assert len(group_result["confirmed"]) == 1 and len(p.references()) == 11
    assert group_result["skipped_duplicates"] == [
        {"actor_name": "多人合照", "role_name": "群像"},
        {"actor_name": "多人合照", "role_name": "群像"},
    ]
    # 同内容同框重复导入：身份与参考图都复用，计入 ignored_existing 而不是新增。
    fourth = p.create_import([photo("purple")])
    repeat = p.confirm_import(
        fourth["id"],
        CastConfirm(
            work_id=work,
            entries=[
                {
                    "image_id": fourth["images"][0]["id"],
                    "actor_name": "演员甲",
                    "role_name": "老年叶淑柔",
                    "bbox": [1, 1, 80, 100],
                }
            ],
        ),
    )
    assert repeat["ignored_existing"] == 1 and len(p.references()) == 11


def test_import_api_failure_draft_and_reference_repair(catalogue, config, monkeypatch):
    p, _work, actor = catalogue
    monkeypatch.setattr("persons.person_api.reference_checker", reference_checker_stub(FakeClient(), config))
    client = TestClient(create_app(config))
    response = client.post("/api/persons/imports", files=[("files", ("cast.png", photo(), "image/png"))])
    assert response.status_code == 201
    draft = response.json()

    def offline(*args):
        raise ModelError("offline")

    monkeypatch.setattr("persons.person_api.parse_cast", offline)
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


def test_import_multi_image_append_list_and_per_image_parse(catalogue, config, monkeypatch):
    p, _work, _actor = catalogue
    client = TestClient(create_app(config))
    created = client.post(
        "/api/persons/imports",
        files=[
            ("files", ("a.png", photo("red"), "image/png")),
            ("files", ("b.png", photo("blue"), "image/png")),
        ],
    ).json()
    assert len(created["images"]) == 2

    # 分次追加：新图进草稿，同内容图跳过并记警告。
    appended = client.post(
        f"/api/persons/imports/{created['id']}/images",
        files=[
            ("files", ("c.png", photo("green"), "image/png")),
            ("files", ("dup.png", photo("red"), "image/png")),
        ],
    ).json()
    assert len(appended["images"]) == 3
    assert any("内容相同" in w for w in appended["warnings"])

    # 历史列表：默认列出全部导入的 cast 图片。
    item = next(i for i in client.get("/api/persons/imports").json() if i["id"] == created["id"])
    assert item["status"] == "draft" and len(item["images"]) == 3

    # 逐张拆分：第二张失败不影响第一张，且失败时已有条目保留。
    calls = []

    def fake_parse(config_used, image):
        calls.append(image["id"])
        if image["id"] == appended["images"][0]["id"]:
            return {
                "entries": [
                    {
                        "image_id": image["id"],
                        "actor_name": "演员甲",
                        "role_name": "角色甲",
                        "bbox": [1, 1, 50, 50],
                    }
                ],
                "warnings": [],
            }
        raise ModelError("boom")

    monkeypatch.setattr("persons.person_api.parse_cast", fake_parse)
    first = client.post(
        f"/api/persons/imports/{created['id']}/parse", json={"image_ids": [appended["images"][0]["id"]]}
    ).json()
    assert first["parse_state"][appended["images"][0]["id"]]["status"] == "parsed"
    assert [e["actor_name"] for e in first["entries"]] == ["演员甲"]
    second = client.post(
        f"/api/persons/imports/{created['id']}/parse", json={"image_ids": [appended["images"][1]["id"]]}
    ).json()
    assert second["parse_state"][appended["images"][1]["id"]]["status"] == "failed"
    assert [e["actor_name"] for e in second["entries"]] == ["演员甲"]
    assert calls == [appended["images"][0]["id"], appended["images"][1]["id"]]
    # 不带 image_ids 时对全部图重拆。
    client.post(f"/api/persons/imports/{created['id']}/parse")
    assert set(calls) == {im["id"] for im in appended["images"]}

    # 删除单张历史图片：条目随之剥离；图片删空后整行消失。
    kept_image = appended["images"][1]["id"]
    removed = client.delete(f"/api/persons/imports/{created['id']}/images/{appended['images'][0]['id']}")
    assert removed.status_code == 200 and removed.json()["remaining_images"] == 2
    after = client.get(f"/api/persons/imports/{created['id']}").json()
    assert [im["id"] for im in after["images"]] == [appended["images"][1]["id"], appended["images"][2]["id"]]
    assert all(e["image_id"] != appended["images"][0]["id"] for e in after["entries"])
    client.delete(f"/api/persons/imports/{created['id']}/images/{appended['images'][2]['id']}")
    client.delete(f"/api/persons/imports/{created['id']}/images/{kept_image}")
    assert client.get(f"/api/persons/imports/{created['id']}").status_code == 422
    assert all(i["id"] != created["id"] for i in client.get("/api/persons/imports").json())

    # 已确认导入的图片同样可删：人物与参考图保留，仅从历史中消失。
    final = p.create_import([photo("gold")])
    p.confirm_import(
        final["id"],
        CastConfirm(
            work_id=_work,
            entries=[
                {"image_id": final["images"][0]["id"], "actor_name": "留档演员", "bbox": [1, 1, 60, 90]}
            ],
        ),
    )
    kept_actor_count = len(p.entities("actor"))
    kept_refs = len(p.references())
    assert (
        client.delete(f"/api/persons/imports/{final['id']}/images/{final['images'][0]['id']}").status_code
        == 200
    )
    assert client.get("/api/persons/imports").json() == []
    assert len(p.entities("actor")) == kept_actor_count and len(p.references()) == kept_refs


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
    assert evidence[0]["data"]["reference_id"] == paired["id"]


def test_matched_reference_provenance_and_legacy_cache_recovery(catalogue, config):
    from persons.person_processing import EvidenceReferences, matched_reference

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="匹配角色", work_ids=[work], actor_ids=[actor["id"]]))
    other_role = p.save(Entity(kind="role", name="其他角色", work_ids=[work], actor_ids=[actor["id"]]))
    reference = p.add_reference(actor["id"], photo(), role_id=role["id"])
    other = p.add_reference(actor["id"], photo("blue"), role_id=other_role["id"])
    refs = [
        {**obs(), "id": reference["id"], "actor_id": actor["id"], "role_id": role["id"]},
        {
            **obs(),
            "face_feature": [0, 1],
            "id": other["id"],
            "actor_id": actor["id"],
            "role_id": other_role["id"],
        },
    ]
    assert matched_reference(obs(), refs, config.person, actor["id"], role["id"])["id"] == reference["id"]
    assert matched_reference(obs(), refs, config.person, actor["id"], other_role["id"]) is None
    space = {"model": "fixture"}
    frames = [{"timestamp": 0, "observations": [obs()]}, {"timestamp": 0.2, "observations": [obs(False)]}]
    shot = {"id": "shot0", "source_id": "source", "cache_id": "cached-shot", "end": 1, "space": space}
    rows = attribute_frames(frames, refs, config.person, shot, work, [role], Tracker)
    assert [r["data"]["reference_id"] for r in rows] == [reference["id"], reference["id"]]
    for key, kind, data in [
        ("cached-shot", "shot", {"space": space, "frames": frames}),
        (fingerprint(["reference-v1", reference["digest"], space]), "reference", {"observations": [obs()]}),
        (
            fingerprint(["reference-v1", other["digest"], space]),
            "reference",
            {"observations": [{**obs(), "face_feature": [0, 1]}]},
        ),
    ]:
        p.store.execute(
            "INSERT INTO person_features VALUES(?,?,?,?,?)", (key, None, dumps(space), kind, dumps(data))
        )
    resolver = EvidenceReferences(config, p.store)
    assert resolver.url(rows[0]) == reference["url"]
    legacy = {
        **rows[0],
        "data": {k: v for k, v in rows[0]["data"].items() if k not in {"reference_id", "space"}},
    }
    assert resolver.url(legacy) == reference["url"]
    assert resolver.url({**legacy, "data": {**legacy["data"], "score": 0.5}}) is None
    assert resolver.url({**rows[0], "role_id": other_role["id"]}) is None
    assert resolver.url({**rows[0], "data": {"image_path": "screenshot.png"}}) is None
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            legacy["id"],
            "source",
            "shot0",
            actor["id"],
            role["id"],
            None,
            0,
            0.2,
            "accepted",
            dumps(legacy["data"]),
        ),
    )
    from persons.person_models import ShotReview

    reviewed = p.shot_review(
        ShotReview(
            source_id="source", shot_id="shot0", selections=[{"actor_id": actor["id"], "role_id": role["id"]}]
        )
    )
    manual = next(e for e in p.evidence() if e["id"] in reviewed["accepted"])
    assert EvidenceReferences(config, p.store).url(manual) == reference["url"]
    p.store.execute("DELETE FROM person_references WHERE id=?", (reference["id"],))
    assert EvidenceReferences(config, p.store).url(rows[0]) is None


def test_role_reference_api_upload_reassign_and_remove(catalogue, config, monkeypatch):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    other_role = p.save(Entity(kind="role", name="少女叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    monkeypatch.setattr("persons.person_api.reference_checker", reference_checker_stub(FakeClient(), config))
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


def test_role_reference_keeps_the_face_actor_and_competes_with_other_performers(
    catalogue, config, monkeypatch
):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"], other["id"]]))
    monkeypatch.setattr("persons.person_api.reference_checker", reference_checker_stub(FakeClient(), config))
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
    assert face_match(obs(), refs, config.person) == (role["id"], 1.0, True)
    # Updating role membership cannot transfer an existing face to a different performer.
    p.save(Entity(kind="role", name=role["name"], work_ids=[work], actor_ids=[other["id"]]), role["id"])
    report = PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    assert report["reference_errors"] == [
        {
            "reference_id": created.json()["id"],
            "actor_id": actor["id"],
            "role_id": role["id"],
            "actor_name": actor["name"],
            "role_name": role["name"],
            "error": "参考图演员已不属于该角色，请重新配对",
        }
    ]
    assert all(e["actor_id"] == actor["id"] for e in p.evidence())


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
    monkeypatch.setattr("shared.shot_media.ShotMedia.frame_bytes", lambda *args: photo("orange", (320, 240)))
    monkeypatch.setattr("persons.person_api.reference_checker", reference_checker_stub(FakeClient(), config))
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
    foreign_role = p.save(
        Entity(kind="role", name="外片角色", work_ids=[foreign["id"]], actor_ids=[actor["id"]])
    )
    monkeypatch.setattr(
        "shared.shot_media.ShotMedia.frame_bytes", lambda *args: pytest.fail("无效配对不应调用 ffmpeg")
    )
    assert (
        client.post(
            "/api/persons/references/from-source",
            json={"source_id": "source", "timestamp": 0, "role_id": foreign_role["id"]},
        ).status_code
        == 422
    )


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
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    ref = p.add_reference(actor["id"], photo(), role_id=role["id"])
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

    # 门槛抬高到 0.5：参考图显式报错，已有自动确认仍然保留。
    config.person = config.person.model_copy(update={"min_reference_quality": 0.5})
    report = PersonProcessor(config, client, Tracker).run("source", PersonOptions())
    assert len(report["reference_errors"]) == 1
    assert report["reference_errors"][0]["reference_id"] == ref["id"]
    assert report["reference_errors"][0]["actor_name"] == actor["name"]
    assert report["reference_errors"][0]["role_name"] == role["name"]
    assert "清晰度 0.07" in report["reference_errors"][0]["error"]
    assert all(e["status"] == "accepted" for e in p.evidence() if e["actor_id"] == actor["id"])

    # 帧侧门槛不受影响：模糊帧依旧不做自动确认；默认门槛下低质量参考图仍可命中。
    refs = [dict(obs(), face_quality=0.07, actor_id=actor["id"], role_id=role["id"])]
    assert face_match(dict(obs(), face_quality=0.1), refs, config.person)[2] is False
    assert (
        face_match(obs(), refs, config.person.model_copy(update={"min_reference_quality": 0.0}))[0]
        == role["id"]
    )


def test_reference_upload_quality_gate_rejects_and_caches(catalogue, config, monkeypatch):
    """添加参考图即质检：不合格拒绝入库且不写 person_references，检测（含被拒图）缓存复用。"""
    p, _work, actor = catalogue
    stub = ReferenceClient(observations=[obs(), obs(box=[10, 0, 110, 120])])
    monkeypatch.setattr("persons.person_api.reference_checker", reference_checker_stub(stub, config))
    client = TestClient(create_app(config))
    group = {"file": ("group.png", photo(), "image/png")}
    rejected = client.post("/api/persons/references", data={"actor_id": actor["id"]}, files=group)
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "参考照片需要恰好一张清晰人脸，且属于画面主体"
    assert p.references() == []
    assert stub.images == 1
    assert p.store.one("SELECT COUNT(*) c FROM person_features WHERE kind='reference'")["c"] == 1
    # 重试同一张坏图命中缓存，不再调服务，仍拒绝。
    again = client.post("/api/persons/references", data={"actor_id": actor["id"]}, files=group)
    assert again.status_code == 422 and stub.images == 1
    # 换合格图片入库成功；重传同文件命中去重，跳过质检不调服务。
    stub.observations = [obs()]
    created = client.post(
        "/api/persons/references",
        data={"actor_id": actor["id"]},
        files={"file": ("good.png", photo("blue"), "image/png")},
    )
    assert created.status_code == 201, created.text
    duplicate = client.post(
        "/api/persons/references",
        data={"actor_id": actor["id"]},
        files={"file": ("good.png", photo("blue"), "image/png")},
    )
    assert duplicate.status_code == 201 and stub.images == 2
    # 质量门槛可配：抬高后低质量图拒绝并给出数值。
    config.person = config.person.model_copy(update={"min_reference_quality": 0.5})
    stub.observations = [dict(obs(), face_quality=0.2)]
    low = client.post(
        "/api/persons/references",
        data={"actor_id": actor["id"]},
        files={"file": ("blur.png", photo("gray"), "image/png")},
    )
    assert low.status_code == 422 and "清晰度 0.20 低于门槛 0.50" in low.json()["detail"]
    assert p.references() and len(p.references()) == 1
    # 添加时缓存的检测被处理任务复用：跑任务不再调图片提取，也不再报参考图错误。
    processor_client = FakeClient()
    report = PersonProcessor(config, processor_client, Tracker).run("source", PersonOptions())
    assert report["reference_errors"] == [] and processor_client.images == 0


def test_reference_upload_service_unavailable_rejects_with_503(catalogue, config, monkeypatch):
    """人物服务不可用时拒绝添加参考图（强约束），不入库。"""
    p, _work, actor = catalogue
    monkeypatch.setattr(
        "persons.person_api.reference_checker",
        reference_checker_stub(ReferenceClient(ready=False), config),
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/api/persons/references",
        data={"actor_id": actor["id"]},
        files={"file": ("a.png", photo(), "image/png")},
    )
    assert response.status_code == 503
    assert response.json()["detail"].startswith("人物服务不可用")
    assert p.references() == []


def test_from_source_capture_validated_before_insert(catalogue, config, monkeypatch):
    """原片截图入库前同步质检：不合格 422，服务不可用 503，均不入库。"""
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="叶淑柔", work_ids=[work], actor_ids=[actor["id"]]))
    monkeypatch.setattr("shared.shot_media.ShotMedia.frame_bytes", lambda *args: photo("orange", (320, 240)))
    stub = ReferenceClient(observations=[obs(), obs(box=[10, 0, 110, 120])])
    monkeypatch.setattr("persons.person_api.reference_checker", reference_checker_stub(stub, config))
    client = TestClient(create_app(config))
    body = {
        "source_id": "source",
        "timestamp": 1.5,
        "actor_id": actor["id"],
        "role_id": role["id"],
        "bbox": [10, 20, 110, 140],
    }
    rejected = client.post("/api/persons/references/from-source", json=body)
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "参考照片需要恰好一张清晰人脸，且属于画面主体"
    assert p.references() == []
    stub.ready = False
    body["timestamp"] = 1.6
    offline = client.post("/api/persons/references/from-source", json=body)
    assert offline.status_code == 503 and offline.json()["detail"].startswith("人物服务不可用")
    assert p.references() == []


def test_import_confirm_validates_crops_and_stays_transactional(catalogue, config, monkeypatch):
    """演员表确认逐条目质检：任一不合格整批拒绝并点名条目，草稿保持未确认、零部分写入。"""
    p, work, _actor = catalogue
    draft = p.create_import([photo(size=(850, 260))])
    entries = [
        {
            "image_id": draft["images"][0]["id"],
            "actor_name": "好演员",
            "role_name": "角色甲",
            "bbox": [10, 10, 90, 110],
            "single_person": True,
        },
        {
            "image_id": draft["images"][0]["id"],
            "actor_name": "坏演员",
            "role_name": "角色乙",
            # 尺寸与好条目不同，避免纯色图同尺寸裁剪 digest 相同命中缓存绕过检测。
            "bbox": [110, 10, 190, 105],
            "single_person": True,
        },
    ]
    stub = reference_checker_stub(SequenceClient([[obs()], [obs(), obs(box=[10, 0, 110, 120])]]), config)
    monkeypatch.setattr("persons.person_api.reference_checker", stub)
    client = TestClient(create_app(config))
    rejected = client.post(
        f"/api/persons/imports/{draft['id']}/confirm", json={"work_id": work, "entries": entries}
    )
    assert rejected.status_code == 422
    assert "条目 2（坏演员）" in rejected.json()["detail"]
    assert "参考照片需要恰好一张清晰人脸" in rejected.json()["detail"]
    assert p.get_import(draft["id"])["status"] == "draft"
    assert p.references() == [] and len(p.entities("actor")) == 1 and p.entities("role") == []
    # 服务不可用同样整批拒绝（503），草稿保持未确认。
    monkeypatch.setattr(
        "persons.person_api.reference_checker",
        reference_checker_stub(ReferenceClient(ready=False), config),
    )
    offline = client.post(
        f"/api/persons/imports/{draft['id']}/confirm", json={"work_id": work, "entries": entries}
    )
    assert offline.status_code == 503 and offline.json()["detail"].startswith("人物服务不可用")
    assert p.get_import(draft["id"])["status"] == "draft" and p.references() == []


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
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])
    processor = PersonProcessor(config, FakeClient(), Tracker)
    processor.run("source", PersonOptions())
    p.shot_review(
        ShotReview(
            source_id="source", shot_id="shot0", selections=[{"actor_id": actor["id"], "role_id": role["id"]}]
        )
    )
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
        db.execute("PRAGMA user_version=0")  # 引入版本号之前的旧库
    Store(config.data_dir)  # 旧库首次启动：v1 迁移补列 + 建索引
    with store.connect() as db:
        init_person_schema(db)  # 迁移步骤必须幂等
        assert "role_id" in {row["name"] for row in db.execute("PRAGMA table_info(person_references)")}
        assert "person_references_role" in {
            row["name"] for row in db.execute("PRAGMA index_list(person_references)")
        }
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
    p.store.execute("PRAGMA user_version=0")  # 旧库早于版本化迁移
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


@pytest.mark.parametrize(
    "reference",
    [
        "档案中关于阿公郑木生的记录",
        "郑木生的照片",
        "有关郑木生的资料",
    ],
)
def test_auto_names_distinguish_document_references(catalogue, reference):
    p, work, actor = catalogue
    subject = p.save(Entity(kind="role", name="晓伟", work_ids=[work], actor_ids=[actor["id"]]))
    elder = p.save(Entity(kind="role", name="郑木生", work_ids=[work], actor_ids=[actor["id"]]))
    text = f"晓伟凝视着{reference}，神色凝重"
    ids, recognized, rest, groups = resolve_names(TextSearch(text=text), p.entities())
    assert ids == [subject["id"]] and groups == [[subject["id"]]]
    assert rest == f"凝视着{reference}，神色凝重"
    assert next(r for r in recognized if r["id"] == elder["id"])["usage"] == "reference"
    for query in [
        TextSearch(text=text, person_mode="onscreen"),
        TextSearch(text=text, person_ids=[elder["id"]]),
    ]:
        assert set(resolve_names(query, p.entities())[0]) == {subject["id"], elder["id"]}
    # The same name can be a reference once and an onscreen subject elsewhere.
    both = TextSearch(text=text + "，郑木生站在一旁")
    assert set(resolve_names(both, p.entities())[0]) == {subject["id"], elder["id"]}
    coordinated = TextSearch(text="晓伟和郑木生凝视着档案，神色凝重")
    assert set(resolve_names(coordinated, p.entities())[0]) == {subject["id"], elder["id"]}
    reference_only = TextSearch(text=reference)
    assert resolve_names(reference_only, p.entities())[0] == []
    assert resolve_names(reference_only, p.entities())[2] == reference


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
    refs = [{**obs(), "actor_id": actor["id"], "role_id": "r"}]
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
    # Score gap compares distinct roles, never two references of the same role.
    assert face_match(obs(), refs * 3, config.person)[2]
    assert not face_match(obs(), refs + [dict(refs[0], role_id="other")], config.person)[2]
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


class ReferenceClient(FakeClient):
    """添加参考图质检链路的替身：observations 可配置，ready=False 模拟人物服务不可用。"""

    def __init__(self, observations=None, ready=True):
        super().__init__()
        self.observations = observations
        self.ready = ready

    def identity(self):
        if not self.ready:
            raise ModelError("人物服务不可用：fixture offline")
        return super().identity()

    def image(self, path):
        self.images += 1
        return {"space": self.identity(), "observations": self.observations or [obs()]}


class SequenceClient(FakeClient):
    """按调用顺序依次返回预设观测：一次请求内多张图分别判合格/不合格。"""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def image(self, path):
        self.images += 1
        return {"space": self.identity(), "observations": self.results.pop(0)}


def reference_checker_stub(client, config):
    """API 测试注入用 checker 工厂：绕过 HTTP，把替身接到真实 ensure_reference_quality 判定链路。"""

    def factory(_runtime, store):
        def check(item):
            ensure_reference_quality(store, config, client, item)

        return check

    return factory


def test_checkpoint_append_rename_no_video_reinference_space_isolation(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])
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
    p.add_reference(actor["id"], photo("green"), role_id=role["id"])
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

    from persons.person_server import create_person_app

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
    from processing.indexing import VectorIndex
    from shared.types import Annotation

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


@pytest.mark.parametrize(
    "query",
    [
        TextSearch(text="演员甲", granularity="shot"),
        TextSearch(text="演员甲快回来", granularity="shot"),
        TextSearch(text="演员甲", person_mode="mentioned", granularity="shot"),
        TextSearch(text="快回来", granularity="shot"),
        TextSearch(text="演员甲", granularity="shot", target_duration=1.1),
    ],
)
def test_person_projection_matches_reference_without_full_snapshots(catalogue, config, monkeypatch, query):
    from admin.curation import Library
    from search.search import SearchEngine
    from shared.lexical_store import build_lexical_index

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="角色甲", work_ids=[work], actor_ids=[actor["id"]]))
    version = publish_content(p, config)
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "visible",
            "source",
            "shot0",
            actor["id"],
            role["id"],
            None,
            0.0,
            1.0,
            "accepted",
            dumps({"score": 0.9}),
        ),
    )
    p.publish()

    class Offline:
        def identity(self):
            raise ModelError("离线，使用词法检索")

    expected = SearchEngine(config, Offline()).text(query)
    build_lexical_index(p.store, version)
    original_overlays = Library.overlays

    def bounded_overlays(self, *, snapshots=None):
        assert snapshots is not None, "投影查询不能建立全库 curation map"
        return original_overlays(self, snapshots=snapshots)

    monkeypatch.setattr(Library, "overlays", bounded_overlays)
    monkeypatch.setattr(Store, "snapshots_shared", lambda *a: pytest.fail("投影查询读取全量快照"))
    actual = SearchEngine(config, Offline()).text(query)
    for result in (expected, actual):
        result.pop("elapsed_ms")
    assert actual == expected


def test_mentioned_role_variants_union_and_latin_name_boundaries(catalogue, config):
    from search.search import SearchEngine

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


def test_auto_document_query_reaches_embedding_with_subject_evidence(catalogue, config, monkeypatch):
    from processing.indexing import VectorIndex
    from search.search import SearchEngine

    p, work, actor = catalogue
    subject = p.save(Entity(kind="role", name="晓伟", work_ids=[work], actor_ids=[actor["id"]]))
    elder = p.save(Entity(kind="role", name="郑木生", work_ids=[work], actor_ids=[actor["id"]]))
    publish_content(p, config)
    for n, role in enumerate([subject, elder]):
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"ev{n}", "source", f"shot{n}", actor["id"], role["id"], None, n, n + 1, "accepted", dumps({})),
        )
    p.publish()
    calls = []

    class Embedder:
        def identity(self):
            return {"model": "fixture"}

        def text(self, texts, role="query"):
            calls.append(texts)
            return [[1, 0]]

    def query_vectors(self, vector, view, ids, count):
        assert ids == ["shot0"]
        return [{"id": i, "distance": 0.1} for i in ids]

    monkeypatch.setattr(VectorIndex, "query", query_vectors)
    engine = SearchEngine(config, Embedder())
    text = "晓伟凝视着档案中关于阿公郑木生的记录，神色凝重"
    result = engine.text(TextSearch(text=text, granularity="shot"))
    assert calls == [["凝视着档案中关于阿公郑木生的记录，神色凝重"]]
    assert [r["id"] for r in result["results"]] == ["shot0"]
    assert result["person_ids"] == [subject["id"]]
    assert [r["id"] for r in result["recognized_people"]] == [subject["id"]]
    assert [r["id"] for r in result["referenced_people"]] == [elder["id"]]
    strict = engine.text(TextSearch(text=text, granularity="shot", person_mode="onscreen"))
    assert strict["eligible_segments"] == 0 and strict["results"] == []
    assert len(calls) == 1


def test_strict_no_dialogue_leak_pretruncate_filter_offline_name_and_rollback(catalogue, config, monkeypatch):
    from admin.curation import CurationPatch, Library
    from processing.indexing import VectorIndex
    from search.search import SearchEngine

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    version = publish_content(p, config)
    engine = SearchEngine(config)
    # Registered name with no published person processing must never degrade to mention mode.
    assert engine.text(TextSearch(text="演员甲", granularity="shot"))["results"] == []
    p.add_reference(actor["id"], photo(), role_id=role["id"])
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


def test_person_release_operations_ignore_content_index_lock(catalogue, config):
    from admin.source_tasks import publish_work
    from shared.processing_lock import ProcessingLock, index_write_lock

    p, _work, _actor = catalogue
    config.enforce_gpu_lock = False
    publish_content(p, config, "first-content")
    first = p.publish()
    assert p.publish()["release_id"] == first["release_id"]
    publish_content(p, config, "second-content")
    second = p.publish()
    client = TestClient(create_app(config))
    with ProcessingLock(config), index_write_lock(p.store.root):
        assert client.post("/api/persons/publish", json={}).status_code == 200
        assert publish_work(config, "source", "persons")["release_id"] == second["release_id"]
        assert client.post(f"/api/persons/releases/{first['release_id']}/activate").status_code == 200
        assert client.delete(f"/api/persons/releases/{second['release_id']}").status_code == 200


def test_person_publish_binds_active_content_at_transaction_start(catalogue, config):
    p, _work, _actor = catalogue
    publish_content(p, config, "old-content")
    publish_content(p, config, "new-content")
    p.store.activate("old-content")

    def progress(value):
        if value["stage"] == "person_index":
            p.store.activate("new-content")

    result = p.publish(progress=progress)
    assert result["content_id"] == "new-content"
    assert p.store.active_index()["id"] == "new-content"


def test_release_delete_rejects_active_and_cleans_snapshot(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    client = TestClient(create_app(config))
    publish_content(p, config, "content1")
    first = p.publish()
    # 新增一条已接受证据 → 人物快照改变 → 第二条组合使用新的快照。
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("ev-extra", "source", "shot0", actor["id"], role["id"], None, 0, 1, "accepted", dumps({})),
    )
    publish_content(p, config, "content2")
    second = p.publish()
    assert first["person_version"] != second["person_version"]

    # 当前生效的组合不能删（否则检索失去人物快照）。
    active = p.store.one("SELECT value FROM settings WHERE key='active.release'")["value"]
    assert active == second["release_id"]
    failed = client.delete(f"/api/persons/releases/{second['release_id']}")
    assert failed.status_code == 422 and "生效" in failed.json()["detail"]
    assert p.store.one("SELECT * FROM search_releases WHERE id=?", (second["release_id"],))

    # 非生效组合可删；其人物快照无其他引用时一并清理，仍生效的组合与快照保留。
    p.store.activate_release(first["release_id"])
    assert client.delete(f"/api/persons/releases/{second['release_id']}").status_code == 200
    assert not p.store.one("SELECT * FROM search_releases WHERE id=?", (second["release_id"],))
    assert not p.store.one("SELECT * FROM person_versions WHERE id=?", (second["person_version"],))
    assert p.store.one("SELECT * FROM search_releases WHERE id=?", (first["release_id"],))
    assert p.store.one("SELECT * FROM person_versions WHERE id=?", (first["person_version"],))
    assert client.delete("/api/persons/releases/missing").status_code == 422
    # 身份与参考图不受发布组合删除影响。
    assert p.entity(actor["id"])


def test_release_listing_reports_the_active_ids_used_by_studio_recall(catalogue, config):
    """设置页「索引版本」显示的生效 ID，与创作召回签名读取的内容索引 / 检索发布版一致。"""
    from studio.workspace.signatures import _settings

    p, _work, _actor = catalogue
    client = TestClient(create_app(config))
    publish_content(p, config, "content1")
    first = p.publish()
    publish_content(p, config, "content2")
    second = p.publish()

    body = client.get("/api/persons/releases").json()
    assert body["active"] == {
        "content_id": "content2",
        "release_id": second["release_id"],
        "person_id": second["person_version"],
    }
    assert [r["id"] for r in body["releases"]][:2] == [second["release_id"], first["release_id"]]  # 新到旧
    recall = _settings(config, {"options": {"recall_limit": 20}}, "recall")
    assert (recall["index_id"], recall["person_release"]) == ("content2", second["release_id"])

    # 恢复旧组合后，列表与召回签名同时改变；发布版 ID 随内容索引变化。
    assert client.post(f"/api/persons/releases/{first['release_id']}/activate").status_code == 200
    body = client.get("/api/persons/releases").json()
    assert body["active"]["release_id"] == first["release_id"] and body["active"]["content_id"] == "content1"
    recall = _settings(config, {"options": {"recall_limit": 20}}, "recall")
    assert (recall["index_id"], recall["person_release"]) == ("content1", first["release_id"])
    # 人物页列表不再携带发布记录。
    assert "releases" not in client.get("/api/persons").json()


def test_purge_keeps_global_references_and_removes_all_source_evidence(catalogue, config):
    from maintenance.purge import purge_source

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
    from persons.person_frames import sample_frames
    from persons.person_processing import PersonClient

    frames = list(sample_frames(video_file, 0, 1, 5))
    assert frames and frames[0][0] == 0
    assert [t for t, _ in frames] == [t for t, _ in sample_frames(video_file, 0, 1, 1)][::5]
    assert len(list(sample_frames(video_file, 0, 1, 10000))) == 1
    assert [t for t, _ in sample_frames(video_file, 1, 2, 5)] == [
        t for t, _ in sample_frames(video_file, 1, 2, 1)
    ][::5]
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
    assert len(batches) == (len(result["frames"]) + 15) // 16
    all_frames = list(sample_frames(video_file, 0, 1, 1))
    assert len(all_frames) > len(frames)
    assert all(b[0] > a[0] for a, b in itertools.pairwise(all_frames))
    result = client.video(video_file, 0, 1, 1)
    assert [f["timestamp"] for f in result["frames"]] == [t for t, _ in all_frames]


def test_partial_coverage_is_reported_and_review_roles_are_work_scoped(catalogue):
    from persons.person_names import coverage_missing

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
    config.enforce_gpu_lock = True
    from filelock import FileLock

    from persons.person_processing import PersonTasks
    from settings.runtime_settings import RuntimeConfig

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])
    processor = PersonProcessor(config, FakeClient(), Tracker)
    monkeypatch.setattr("persons.person_processing.PersonProcessor", lambda cfg: processor)
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
    from shared.config import fingerprint

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
    from search.search import SearchEngine

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])
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


def test_onscreen_search_resolves_reference_urls_only_for_returned_results(catalogue, config, monkeypatch):
    """reference_url 要读特征缓存、较贵：只为最终返回的 limit 条结果计算，而不是全部候选。"""
    from persons.person_processing import EvidenceReferences
    from search.search import SearchEngine

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])
    PersonProcessor(config, FakeClient(), Tracker).run("source", PersonOptions())
    publish_content(p, config)
    p.publish()
    calls = []
    original = EvidenceReferences.url
    monkeypatch.setattr(
        EvidenceReferences,
        "url",
        lambda self, evidence: calls.append(evidence["id"]) or original(self, evidence),
    )
    result = SearchEngine(config).text(TextSearch(person_ids=[actor["id"]], granularity="shot", limit=1))
    assert len(result["results"]) == 1 and result["eligible_segments"] == 3
    returned = {item["id"] for item in result["results"][0]["person_evidence"]}
    assert returned and set(calls) == returned  # 只解析了返回结果里的证据
    assert all("reference_url" in item for item in result["results"][0]["person_evidence"])


def test_shot_review_marks_persons_and_rejects_unmarked_rows(catalogue):
    from persons.person_models import ShotReview

    p, work, a = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    other_role = p.save(Entity(kind="role", name="另一角色", work_ids=[work], actor_ids=[other["id"]]))
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
            selections=[
                {"actor_id": a["id"], "role_id": role["id"]},
                {"actor_id": other["id"], "role_id": other_role["id"]},
            ],
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
    p.shot_review(
        ShotReview(
            source_id="source",
            shot_id="shot0",
            selections=[{"actor_id": other["id"], "role_id": other_role["id"]}],
        )
    )
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
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[a["id"]]))
    other = p.save(Entity(kind="actor", name="演员乙"))
    other_role = p.save(Entity(kind="role", name="另一角色", work_ids=[work], actor_ids=[other["id"]]))
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
                (role["id"] if actor_id == a["id"] else other_role["id"] if actor_id else None),
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
    assert item["counts"] == {"accepted": 1, "candidate": 1, "rejected": 0}
    assert len(item["evidence"]) == 2
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
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[a["id"]]))
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
                role["id"],
                None,
                start,
                start + 0.2,
                status,
                dumps(
                    {
                        "work_id": work,
                        **(
                            {"confirmation": "shot_review", "shot_excluded": True}
                            if status == "rejected"
                            else {}
                        ),
                    }
                ),
            ),
        )
    ids = lambda status: [i["shot_id"] for i in p.review_shots(status=status)["items"]]
    assert ids("accepted") == ["shot0"]
    assert ids("candidate") == ["shot1"]
    assert ids("rejected") == ["shot2"]
    assert p.review_shots()["total"] == 3
    # 已接受的镜头仍要能整镜核对结清，卡内证据不因状态过滤而缺失。
    assert len(p.review_shots(status="accepted")["items"][0]["evidence"]) == 2
    recognized = p.review_shots(status="accepted")["items"][0]["recognized_people"]
    assert recognized == [{"actor_id": a["id"], "role_id": role["id"], "score": None, "observations": 2}]
    assert p.review_shots(status="accepted", compact=True)["items"][0]["recognized_people"] == recognized
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
    from persons.person_processing import PersonTasks
    from settings.runtime_settings import RuntimeConfig

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    processor = PersonProcessor(config, FakeClient(), Tracker)
    monkeypatch.setattr("persons.person_processing.PersonProcessor", lambda cfg: processor)
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
    from persons.person_models import ShotReview

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
    assert p.confirm_import(draft["id"], body)["confirmed"][0]["actor_id"] == actor["id"]
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

    from cli import app

    _p, _work, actor = catalogue
    role = _p.save(Entity(kind="role", name="测试角色", work_ids=[_work], actor_ids=[actor["id"]]))
    file = tmp_path / "review.json"
    file.write_text(
        dumps(
            {
                "source_id": "source",
                "shot_id": "shot0",
                "selections": [{"actor_id": actor["id"], "role_id": role["id"]}],
            }
        )
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
    from persons.person_api import parse_cast

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

    def complete(*args, **kwargs):
        assert "stage_name" not in kwargs["json"]["messages"][0]["content"][0]["text"]
        return {"choices": [{"message": {"content": dumps({"entries": entries, "warnings": []})}}]}

    monkeypatch.setattr("persons.person_api.request", complete)
    result = parse_cast(config, {"id": "landscape", "width": 850, "height": 260, "raw": b""})
    assert len(result["entries"]) == 8
    assert result["entries"][0]["role_name"] == "老年叶淑柔"
    assert all(not e["single_person"] and e["actor_id"] is None for e in result["entries"])
    assert result["entries"][0]["bbox"] == [0, 10, 80, 200]
    # 单图解析：模型返回其他图的 image_id 直接判为无效条目。
    assert parse_cast(config, {"id": "portrait", "width": 300, "height": 700, "raw": b""})["warnings"]


def test_review_pair_filter_uses_highest_score_before_pagination(catalogue, config):
    status = "candidate"
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="演员乙"))
    role = p.save(Entity(kind="role", name="角色甲", work_ids=[work], actor_ids=[actor["id"]]))
    other_role = p.save(Entity(kind="role", name="角色乙", work_ids=[work], actor_ids=[other["id"]]))
    for identifier, shot, who, character, score, start in [
        ("first", "shot0", actor["id"], role["id"], 0.7, 0),
        ("highest", "shot0", other["id"], other_role["id"], 0.92, 0.5),
        ("unknown-high", "shot0", None, None, 0.99, 0.7),
        ("next", "shot1", actor["id"], role["id"], 0.95, 1),
        ("unknown", "shot2", None, None, 0.99, 2),
    ]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                "source",
                shot,
                who,
                character,
                None,
                start,
                start + 0.1,
                status,
                dumps({"score": score, "body_feature": [1, 2], "space": "private"}),
            ),
        )
    client = TestClient(create_app(config))
    params = {"source_id": "source", "status": status, "limit": 1}
    listing = client.get("/api/persons/review/shots", params=params).json()
    assert listing["total"] == 3
    assert listing["items"][0]["candidate"] == {
        "actor_id": other["id"],
        "role_id": other_role["id"],
        "score": 0.92,
    }
    assert {(f["actor_id"], f["role_id"], f["count"]) for f in listing["filters"]} == {
        (actor["id"], role["id"], 1),
        (other["id"], other_role["id"], 1),
        (None, None, 1),
    }
    filtered = client.get(
        "/api/persons/review/shots",
        params={**params, "actor_id": actor["id"], "role_id": role["id"]},
    ).json()
    assert filtered["total"] == 1 and filtered["items"][0]["shot_id"] == "shot1"
    assert filtered["filters"] == listing["filters"]
    both = client.get(
        "/api/persons/review/shots",
        params={**params, "actor_id": other["id"], "role_id": other_role["id"]},
    ).json()["items"][0]
    assert len(both["evidence"]) == 2  # 分类过滤不截断卡内其他身份的证据。
    assert all("body_feature" not in e["data"] and "space" not in e["data"] for e in both["evidence"])
    none = client.get("/api/persons/review/shots", params={**params, "no_candidate": True}).json()
    assert none["total"] == 1 and none["items"][0]["shot_id"] == "shot2"
    compact = client.get("/api/persons/review/shots", params={**params, "compact": True}).json()
    assert compact["total"] == listing["total"] and compact["filters"] == listing["filters"]
    assert all(set(e) == {"id", "actor_id", "role_id", "status"} for e in compact["items"][0]["evidence"])


def test_review_filter_distinguishes_roles_without_actor_inference(catalogue):
    p, work, actor = catalogue
    first = p.save(Entity(kind="role", name="角色一", work_ids=[work], actor_ids=[actor["id"]]))
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("actor-only", "source", "shot0", actor["id"], None, None, 0, 0.5, "candidate", '{"score":0.9}'),
    )
    assert p.review_shots(actor_id=actor["id"], role_id=first["id"])["total"] == 0
    second = p.save(Entity(kind="role", name="角色二", work_ids=[work], actor_ids=[actor["id"]]))
    for n, role in enumerate([first, second], 1):
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"pair{n}",
                "source",
                f"shot{n}",
                actor["id"],
                role["id"],
                None,
                n,
                n + 0.5,
                "candidate",
                '{"score":0.9}',
            ),
        )
    assert p.review_shots(actor_id=actor["id"])["total"] == 0
    assert p.review_shots(no_candidate=True)["items"][0]["shot_id"] == "shot0"
    for n, role in enumerate([first, second], 1):
        result = p.review_shots(actor_id=actor["id"], role_id=role["id"])
        assert result["total"] == 1 and result["items"][0]["shot_id"] == f"shot{n}"


def test_review_manual_identity_is_filterable_and_supersedes_old_review(catalogue):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="人工纠正演员"))
    role = p.save(Entity(kind="role", name="人工纠正角色", work_ids=[work], actor_ids=[other["id"]]))
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("unknown", "source", "shot0", None, None, None, 0, 0.5, "candidate", '{"score":0.99}'),
    )
    assert p.review_shots(no_candidate=True)["total"] == 1
    first_role = p.save(Entity(kind="role", name="初始角色", work_ids=[work], actor_ids=[actor["id"]]))
    body = ShotReview(
        source_id="source",
        shot_id="shot0",
        selections=[{"actor_id": actor["id"], "role_id": first_role["id"]}],
    )
    p.shot_review(body)
    body = ShotReview(
        source_id="source",
        shot_id="shot0",
        selections=[{"actor_id": other["id"], "role_id": role["id"]}],
    )
    p.shot_review(body)
    result = p.review_shots(status="accepted", actor_id=other["id"], role_id=role["id"])
    assert result["total"] == 1 and result["items"][0]["candidate"]["score"] == 1.0
    assert p.review_shots(no_candidate=True)["total"] == 0
    assert p.review_shots(actor_id=actor["id"])["total"] == 0
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[]))
    rejected = p.review_shots(status="rejected", actor_id=other["id"], role_id=role["id"])
    assert rejected["total"] == 1 and rejected["filters"] == []
    assert rejected["items"][0]["candidate"] is None


def test_review_no_candidate_includes_processed_empty_shots_only(catalogue):
    from persons.person_models import ShotReview

    p, _work, _actor = catalogue
    for cache in ["empty-cache", "same-shot-other-run"]:
        p.store.execute(
            "INSERT INTO person_coverage VALUES(?,?,?)",
            ("source", cache, dumps({"shot_id": "shot0", "start": 0, "end": 1})),
        )
    result = p.review_shots(status="candidate", no_candidate=True)
    assert result["total"] == 1 and result["items"][0]["shot_id"] == "shot0"
    assert result["items"][0]["evidence"] == []
    assert p.review_summary()["source"]["candidate"] == 1
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[]))
    assert p.review_shots(status="candidate")["total"] == 0
    assert p.review_shots(status="rejected", no_candidate=True)["total"] == 1
    assert p.review_summary()["source"] == {"accepted": 0, "candidate": 0, "rejected": 1}
    marker = p.evidence()[0]
    assert marker["data"]["reviewed"] and marker["data"]["confirmation"] == "shot_review"


def test_rejected_review_ignores_identity_filters_and_keeps_pagination(catalogue, config):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="已排除角色", work_ids=[work], actor_ids=[actor["id"]]))
    for n, selections in enumerate(
        [
            [{"actor_id": actor["id"], "role_id": role["id"]}],
            [{"actor_id": actor["id"], "role_id": role["id"]}],
            [],
        ]
    ):
        p.shot_review(ShotReview(source_id="source", shot_id=f"shot{n}", selections=selections))
        p.shot_review(ShotReview(source_id="source", shot_id=f"shot{n}", selections=[]))
    client = TestClient(create_app(config))
    for extra in [{}, {"actor_id": "missing", "role_id": "missing"}, {"no_candidate": True}]:
        result = client.get(
            "/api/persons/review/shots",
            params={"source_id": "source", "status": "rejected", "offset": 1, "limit": 1, **extra},
        ).json()
        assert result["total"] == 3 and result["filters"] == []
        assert result["items"][0]["shot_id"] == "shot1"
        assert result["items"][0]["candidate"] is None
        assert result["items"][0]["evidence"]  # 仍保留证据供重新核对。


@pytest.mark.parametrize("kind", ["actor", "role"])
def test_manual_rejected_shots_do_not_block_identity_deletion(catalogue, config, kind):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="待删角色", work_ids=[work], actor_ids=[actor["id"]]))
    identifier = actor["id"] if kind == "actor" else role["id"]
    client = TestClient(create_app(config))
    p.shot_review(
        ShotReview(
            source_id="source",
            shot_id="shot0",
            selections=[{"actor_id": actor["id"], "role_id": role["id"]}],
        )
    )
    assert client.delete(f"/api/persons/entities/{identifier}").status_code == 422
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[]))
    before = p.evidence()
    assert before and all(e["status"] == "rejected" for e in before)
    assert client.delete(f"/api/persons/entities/{identifier}").status_code == 200
    assert p.evidence() == before  # 删除身份不清掉已排除分镜或审核留档。
    listing = client.get("/api/persons/review/shots", params={"source_id": "source", "status": "rejected"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1 and listing.json()["items"][0]["candidate"] is None
    if kind == "actor":
        assert p.entity(role["id"])["actor_ids"] == []


def test_person_process_end_zero_runs_from_start_to_movie_end(catalogue, config, monkeypatch):
    from persons.person_processing import PersonTasks

    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    monkeypatch.setattr(
        PersonTasks,
        "start",
        lambda self, source_id, options: {"source_id": source_id, "options": options.model_dump()},
    )
    client = TestClient(create_app(config))
    response = client.post("/api/persons/sources/source/process", json={"start": 1.2, "end": 0})
    assert response.status_code == 202, response.text
    options = PersonOptions.model_validate(response.json()["options"])
    assert options.end == 0 and PersonOptions().end == 0
    assert (
        client.post("/api/persons/sources/source/process", json={"start": 1.2, "end": None}).status_code
        == 202
    )
    for end in [-1, 1, 1.2]:
        assert (
            client.post(
                "/api/persons/sources/source/process",
                json={"start": 1.2, "end": end},
            ).status_code
            == 422
        )
    report = PersonProcessor(config, FakeClient(), Tracker).run("source", options)
    assert report["shots"] == 2
    intervals = sorted(
        (json.loads(r["data"])["start"], json.loads(r["data"])["end"])
        for r in p.store.rows("SELECT data FROM person_coverage WHERE source_id='source'")
    )
    assert intervals == [(1.2, 2), (2, 3)]


def test_reference_face_error_records_actor_role_names_at_processing_time(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="问题角色", work_ids=[work], actor_ids=[actor["id"]]))
    reference = p.add_reference(actor["id"], photo(), role_id=role["id"])
    client = FakeClient()
    key = fingerprint(["reference-v1", reference["digest"], client.identity()])
    p.store.execute(
        "INSERT OR REPLACE INTO person_features VALUES(?,?,?,?,?)",
        (key, None, dumps(client.identity()), "reference", dumps({"observations": []})),
    )
    report = PersonProcessor(config, client, Tracker).run("source", PersonOptions())
    assert report["reference_errors"] == [
        {
            "reference_id": reference["id"],
            "actor_id": actor["id"],
            "role_id": role["id"],
            "actor_name": actor["name"],
            "role_name": role["name"],
            "error": "参考照片需要恰好一张清晰人脸，且属于画面主体",
        }
    ]
    # 保存在任务结果里的提示不依赖参考图继续存在，也不随人物改名变化。
    p.store.execute(
        "INSERT INTO person_tasks(id,source_id,status,config,options,result) VALUES(?,?,?,?,?,?)",
        (
            "reference-error-log",
            "source",
            "complete",
            config.model_dump_json(),
            PersonOptions().model_dump_json(),
            dumps(report),
        ),
    )
    p.store.execute("DELETE FROM person_references WHERE id=?", (reference["id"],))
    p.save(Entity(kind="actor", name="后来改名"), actor["id"])
    task = TestClient(create_app(config)).get("/api/persons/tasks/reference-error-log").json()
    logged = task["result"]["reference_errors"]
    assert [{k: v for k, v in item.items() if k != "error_message"} for item in logged] == report[
        "reference_errors"
    ]
    assert logged[0]["error_message"]["message_key"].startswith("ui.")


@pytest.mark.parametrize("status", ["accepted", "candidate"])
@pytest.mark.parametrize("kind", ["actor", "role"])
def test_identity_linked_shots_include_non_top_evidence_and_block_deletion(catalogue, config, status, kind):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="最高分演员"))
    other_role = p.save(Entity(kind="role", name="另一角色", work_ids=[work], actor_ids=[other["id"]]))
    role = p.save(Entity(kind="role", name="较低分角色", work_ids=[work], actor_ids=[actor["id"]]))
    ref = p.add_reference(actor["id"], photo(), role_id=role["id"])
    identifier = actor["id"] if kind == "actor" else role["id"]
    for key, shot, who, role_id, state, score in [
        ("top", "shot0", other["id"], other_role["id"], status, 0.9),
        ("lower", "shot0", actor["id"], role["id"], status, 0.4),
        ("lower-again", "shot0", actor["id"], role["id"], status, 0.3),
        ("excluded", "shot1", actor["id"], role["id"], "rejected", 0.8),
        ("obsolete", "removed-shot", actor["id"], role["id"], "accepted", 0.8),
    ]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, "source", shot, who, role_id, None, 0, 0.5, state, dumps({"score": score})),
        )
    assert p.review_shots(status="accepted", actor_id=actor["id"], role_id=role["id"])["total"] == int(
        status == "accepted"
    )
    client = TestClient(create_app(config))
    url = f"/api/persons/entities/{identifier}"
    linked = client.get(url + "/linked-shots").json()
    assert linked["entity"]["id"] == identifier and linked["total"] == 1
    assert linked["items"][0]["shot_id"] == "shot0"
    assert linked["items"][0][status] == 2
    assert linked["items"][0]["source_name"] == "影片"
    blocked = client.delete(url)
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["linked_shots"] == linked
    assert "shot0" in blocked.json()["detail"]["message"]
    assert "0.000–1.000" in blocked.json()["detail"]["message"]
    assert p.reference(ref["id"]) and actor["id"] in p.entity(role["id"])["actor_ids"]
    # 核对整镜仅保留最高分演员后，较低分身份不再阻碍删除；留档与过期布局不拦截。
    p.shot_review(
        ShotReview(
            source_id="source",
            shot_id="shot0",
            selections=[{"actor_id": other["id"], "role_id": other_role["id"]}],
        )
    )
    assert client.get(url + "/linked-shots").json()["total"] == 0
    before = p.evidence()
    removed = client.delete(url)
    assert removed.status_code == 200 and removed.json()["removed_references"] == 1
    assert p.evidence() == before
    assert not p.references()
    if kind == "actor":
        assert p.entity(role["id"])["actor_ids"] == []


def test_linked_shots_paginates_all_associations(catalogue, config):
    p, _work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[_work], actor_ids=[actor["id"]]))
    for n in range(3):
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"linked{n}", "source", f"shot{n}", actor["id"], role["id"], None, n, n + 0.5, "accepted", "{}"),
        )
    response = TestClient(create_app(config)).get(
        f"/api/persons/entities/{actor['id']}/linked-shots",
        params={"limit": 1, "offset": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3 and data["items"][0]["shot_id"] == "shot1"


def test_accepted_review_filters_by_all_confirmed_people_and_deduplicates_shots(catalogue, config):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="同镜已确认演员"))
    pending = p.save(Entity(kind="actor", name="尚未确认演员"))
    rejected = p.save(Entity(kind="actor", name="已排除演员"))
    role = p.save(Entity(kind="role", name="角色甲", work_ids=[work], actor_ids=[actor["id"]]))
    other_role = p.save(Entity(kind="role", name="角色乙", work_ids=[work], actor_ids=[other["id"]]))
    for key, shot, who, character, status, score in [
        ("a0", "shot0", actor["id"], role["id"], "accepted", 0.4),
        ("a0-duplicate", "shot0", actor["id"], None, "accepted", 0.3),
        ("b0", "shot0", other["id"], other_role["id"], "accepted", 0.9),
        ("pending0", "shot0", pending["id"], None, "candidate", 0.99),
        ("rejected0", "shot0", rejected["id"], None, "rejected", 1.0),
        ("a1", "shot1", actor["id"], role["id"], "accepted", 0.6),
        ("b2", "shot2", other["id"], other_role["id"], "candidate", 0.7),
    ]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, "source", shot, who, character, None, 0, 0.5, status, dumps({"score": score})),
        )
    client = TestClient(create_app(config))
    base = {"source_id": "source", "status": "accepted"}
    listing = client.get("/api/persons/review/shots", params=base).json()
    assert listing["total"] == listing["scope_total"] == 2
    assert {(f["actor_id"], f["role_id"], f["count"]) for f in listing["filters"]} == {
        (actor["id"], role["id"], 2),
        (other["id"], other_role["id"], 1),
    }
    for who, character, expected in [(actor, role, 2), (other, other_role, 1)]:
        result = client.get(
            "/api/persons/review/shots",
            params={**base, "actor_id": who["id"], "role_id": character["id"]},
        ).json()
        assert result["total"] == expected and result["scope_total"] == 2
        assert result["items"][0]["shot_id"] == "shot0"
        assert {(p["actor_id"], p["role_id"]) for p in result["items"][0]["confirmed_people"]} == {
            (actor["id"], role["id"]),
            (other["id"], other_role["id"]),
        }
    page = client.get(
        "/api/persons/review/shots",
        params={
            **base,
            "actor_id": actor["id"],
            "role_id": role["id"],
            "offset": 1,
            "limit": 1,
            "compact": True,
        },
    ).json()
    assert page["total"] == 2 and page["items"][0]["shot_id"] == "shot1"
    assert page["items"][0]["confirmed_people"] == [
        {
            "actor_id": actor["id"],
            "role_id": role["id"],
            "score": 0.6,
            "original_score": None,
            "manual": False,
        }
    ]
    for who in [pending, rejected]:
        assert (
            client.get("/api/persons/review/shots", params={**base, "actor_id": who["id"]}).json()["total"]
            == 0
        )
    assert client.get("/api/persons/review/shots", params={**base, "no_candidate": True}).json()["total"] == 0
    remaining = client.get("/api/persons/review/shots", params={"status": "candidate"}).json()
    assert remaining["total"] == 1 and remaining["items"][0]["shot_id"] == "shot2"


@pytest.mark.parametrize(
    "target_state,other_state,blocked",
    [
        ("candidate", "accepted", False),  # 已确认其他角色，残留候选不拦截。
        ("accepted", "accepted", True),
        ("candidate", "candidate", True),
        ("rejected", "candidate", False),
        ("rejected", "rejected", False),
    ],
)
def test_role_delete_guard_uses_whole_shot_state(catalogue, config, target_state, other_state, blocked):
    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="其他演员"))
    role = p.save(Entity(kind="role", name="待删角色", work_ids=[work], actor_ids=[actor["id"]]))
    other_role = p.save(Entity(kind="role", name="其他确认角色", work_ids=[work], actor_ids=[other["id"]]))
    ref = p.add_reference(actor["id"], photo(), role_id=role["id"])
    for identifier, who, character, status in [
        ("target", actor["id"], role["id"], target_state),
        ("other", other["id"], other_role["id"], other_state),
    ]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identifier, "source", "shot0", who, character, None, 0, 1, status, '{"score":0.8}'),
        )
    client = TestClient(create_app(config))
    url = f"/api/persons/entities/{role['id']}"
    assert client.get(url + "/linked-shots").json()["total"] == int(blocked)
    response = client.delete(url)
    assert response.status_code == (422 if blocked else 200)
    assert p.evidence("source")  # 审核证据始终保留。
    assert p.entity(other_role["id"])
    if blocked:
        assert p.reference(ref["id"])
    else:
        assert not p.references()


@pytest.mark.parametrize("status,blocked", [("candidate", True), ("accepted", True), ("rejected", False)])
def test_role_delete_ignores_actor_only_evidence(catalogue, config, status, blocked):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="唯一角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("actor-only", "source", "shot0", actor["id"], None, None, 0, 1, status, '{"score":0.8}'),
    )
    client = TestClient(create_app(config))
    url = f"/api/persons/entities/{role['id']}"
    assert client.get(url + "/linked-shots").json()["total"] == 0
    assert client.delete(url).status_code == 200


def test_confirmed_person_scores_preserve_manual_original_and_support_legacy(catalogue, config):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="无原始候选演员"))
    other_role = p.save(Entity(kind="role", name="另一角色", work_ids=[work], actor_ids=[other["id"]]))
    role = p.save(Entity(kind="role", name="确认角色", work_ids=[work], actor_ids=[actor["id"]]))
    for identifier, score, status, character in [
        ("accepted-frame", 0.4, "accepted", role["id"]),
        ("original-frame", 0.6, "candidate", role["id"]),
        ("actor-frame", 0.9, "candidate", None),
    ]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                "source",
                "shot0",
                actor["id"],
                character,
                None,
                0,
                1,
                status,
                dumps({"score": score}),
            ),
        )
    before = p.review_shots(status="accepted")["items"][0]["confirmed_people"]
    assert before == [
        {
            "actor_id": actor["id"],
            "role_id": role["id"],
            "score": 0.4,
            "original_score": None,
            "manual": False,
        }
    ]
    body = ShotReview(
        source_id="source",
        shot_id="shot0",
        selections=[
            {"actor_id": actor["id"], "role_id": role["id"]},
            {"actor_id": other["id"], "role_id": other_role["id"]},
        ],
    )
    p.shot_review(body)
    manual = next(e for e in p.evidence() if e["actor_id"] == actor["id"] and e["status"] == "accepted")
    assert manual["data"]["score"] == 1.0 and manual["data"]["original_score"] == 0.6
    client = TestClient(create_app(config))

    def people(compact):
        result = client.get(
            "/api/persons/review/shots", params={"status": "accepted", "compact": compact}
        ).json()
        return {v["actor_id"]: v for v in result["items"][0]["confirmed_people"]}

    for compact in [False, True]:
        details = people(compact)
        assert details[actor["id"]] == {
            "actor_id": actor["id"],
            "role_id": role["id"],
            "score": 0.6,
            "original_score": 0.6,
            "manual": True,
        }
        assert details[other["id"]]["manual"] and details[other["id"]]["original_score"] is None
        assert details[other["id"]]["score"] is None
    # 旧人工记录没有 original_score 时，从已保留的帧还原；不能把固定 1.0 当成原始分数。
    legacy = dict(manual["data"])
    legacy.pop("original_score")
    p.store.execute("UPDATE person_evidence SET data=? WHERE id=?", (dumps(legacy), manual["id"]))
    assert people(False)[actor["id"]]["original_score"] == people(True)[actor["id"]]["original_score"] == 0.6
    # 再次核对不会把上一次人工记录的固定 1.0 覆盖到原始分数。
    p.shot_review(body)
    assert people(False)[actor["id"]]["original_score"] == 0.6


def test_manual_score_ignores_actor_only_and_other_roles(catalogue):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    roles = [
        p.save(Entity(kind="role", name=f"角色{n}", work_ids=[work], actor_ids=[actor["id"]]))
        for n in range(3)
    ]
    for identifier, character, score in [("other-role", roles[0]["id"], 0.95), ("actor-only", None, 0.42)]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                "source",
                "shot0",
                actor["id"],
                character,
                None,
                0,
                1,
                "candidate",
                dumps({"score": score}),
            ),
        )
    p.shot_review(
        ShotReview(
            source_id="source",
            shot_id="shot0",
            selections=[
                {
                    "actor_id": actor["id"],
                    "role_id": roles[1]["id"],
                }
            ],
        )
    )
    person = p.review_shots(status="accepted")["items"][0]["confirmed_people"][0]
    assert person["manual"] and person["original_score"] is None
    # 仅确认演员与确认角色是两个不同身份，人工标记不能串到同演员另一角色。
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "auto-other-role",
            "source",
            "shot0",
            actor["id"],
            roles[2]["id"],
            None,
            0,
            1,
            "accepted",
            '{"score":0.7}',
        ),
    )
    details = {v["role_id"]: v for v in p.review_shots(status="accepted")["items"][0]["confirmed_people"]}
    assert details[roles[2]["id"]]["score"] == 0.7 and not details[roles[2]["id"]]["manual"]


def frame_evidence(
    p,
    actor,
    identifier,
    path,
    *,
    shot="shot0",
    status="accepted",
    score=0.7,
    kind="face",
    original=None,
    role=None,
):
    data = {"image_path": path, "score": score, "evidence_type": kind}
    if original is not None:
        data.update(confirmation="shot_review", original_score=original)
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (identifier, "source", shot, actor["id"], role, None, 0, 1, status, dumps(data)),
    )


def test_frame_encoding_and_cache_config(catalogue, config):
    p, _, _ = catalogue
    raw = photo(size=(1280, 720))
    item = p.image(raw, "frames/source", max_edge=640, image_format="JPEG")
    assert (item["width"], item["height"]) == (640, 360)
    assert item["path"].endswith(".jpg")
    assert item == p.image(raw, "frames/source", max_edge=640, image_format="JPEG")
    assert p.image(raw)["width"] == 1280
    assert p.image(raw)["path"].endswith(".png")
    assert p.image(raw, "frames/source", max_edge=0, image_format="JPEG")["width"] == 1280
    original = config.person.model_dump(exclude={"frames_max_edge", "frames_jpeg_quality"})
    config.person.frames_max_edge = 320
    assert config.person.model_dump(exclude={"frames_max_edge", "frames_jpeg_quality"}) == original


def test_frame_retention_real_scores_and_pending(catalogue):
    from persons.persons import retained_frame_paths

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    for identifier, score, kind, original in (
        ("face", 0.6, "face", None),
        ("manual", 1, "manual", 0.5),
        ("track", 1, "track", None),
    ):
        frame_evidence(
            p,
            actor,
            identifier,
            identifier + ".png",
            score=score,
            kind=kind,
            original=original,
            role=role["id"],
        )
    frame_evidence(p, actor, "pending", "pending.png", shot="shot1", status="candidate")
    frame_evidence(p, actor, "rejected", "rejected.png", shot="shot1", status="rejected")
    frame_evidence(p, actor, "track-only", "track-only.png", shot="shot2", kind="track")
    assert retained_frame_paths(p.store) == {
        "source": {"face.png", "pending.png", "rejected.png", "track-only.png"}
    }
    rows = [
        {**r, "data": json.loads(r["data"])}
        for r in p.store.rows("SELECT * FROM person_evidence WHERE shot_id='shot0'")
    ]
    assert p.original_score(rows, actor["id"], role["id"]) == 0.6
    assert p.review_shots(shot_id="shot0")["items"][0]["image_url"].endswith("/face/image")


def test_migrate_frames_preview_apply_resume_and_vectors(catalogue, config):
    from persons.persons import migrate_frames

    p, _, actor = catalogue
    first = p.image(photo(size=(1280, 720)), "frames/source")["path"]
    discarded = p.image(photo("blue"), "frames/source")["path"]
    frame_evidence(p, actor, "best", first)
    frame_evidence(p, actor, "other", discarded, status="candidate")
    frame_evidence(p, actor, "shared", first, status="rejected")
    vectors = {"frames": [{"image_path": first, "observations": [{"face_feature": [1, 0, 0.5]}]}]}
    p.store.execute(
        "INSERT INTO person_features VALUES(?,?,?,?,?)", ("cache", "source", "space", "shot", dumps(vectors))
    )
    version = {
        "evidence": [{"id": "best", "source_id": "source", "shot_id": "shot0", "data": {"image_path": first}}]
    }
    p.store.execute("INSERT INTO person_versions(id,data) VALUES(?,?)", ("published", dumps(version)))
    before = {str(f): f.read_bytes() for f in (p.store.root / "persons/frames").rglob("*") if f.is_file()}
    preview = migrate_frames(config)
    assert preview["total"]["reencoded"] == 1
    assert preview["total"]["rewritten_evidence"] == 2
    assert all(Path(f).read_bytes() == raw for f, raw in before.items())
    assert len(list((p.store.root / "persons/frames/source").iterdir())) == 2
    result = migrate_frames(config, apply=True)
    assert result["sources"][0]["error"] is None
    assert result["total"]["deleted_bytes"] == preview["total"]["deleted_bytes"]
    updated = json.loads(p.store.one("SELECT data FROM person_features WHERE id='cache'")["data"])
    new = updated["frames"][0]["image_path"]
    assert new.endswith(".jpg") and (p.store.root / new).is_file()
    assert updated["frames"][0]["observations"] == vectors["frames"][0]["observations"]
    assert json.loads(p.store.one("SELECT data FROM person_versions WHERE id='published'")["data"]) == version
    assert (p.store.root / first).is_file(), "发布快照引用的原图仍须保留"
    again = migrate_frames(config, apply=True)
    assert again["total"]["reencoded"] == again["total"]["deleted_files"] == 0
    (p.store.root / "persons/frames/source/unreferenced.png").write_bytes(photo())
    assert migrate_frames(config, apply=True)["total"]["deleted_files"] == 1


def test_migrate_missing_frame_never_deletes_source(catalogue, config):
    from persons.persons import migrate_frames

    p, _, actor = catalogue
    other = p.image(photo(), "frames/source")["path"]
    frame_evidence(p, actor, "missing", "persons/frames/source/missing.png")
    result = migrate_frames(config, apply=True)
    assert result["sources"][0]["error"]
    assert (p.store.root / other).is_file()


def test_frame_api_current_snapshot_thumbnail_and_mime(catalogue, config):
    p, _, actor = catalogue
    thumb = p.image(photo(), "frames/source", image_format="JPEG")["path"]
    p.store.execute("UPDATE segments SET thumbnail=? WHERE id='shot0'", (thumb,))
    frame_evidence(p, actor, "missing", "persons/frames/source/missing.png")
    client = TestClient(create_app(config))
    response = client.get("/api/persons/evidence/missing/image")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    snapshot = {
        "evidence": [
            {
                "id": "historic",
                "source_id": "source",
                "shot_id": "shot0",
                "data": {"image_path": "missing.png"},
            }
        ]
    }
    p.store.execute("INSERT INTO person_versions(id,data) VALUES(?,?)", ("historic", dumps(snapshot)))
    assert client.get("/api/persons/evidence/historic/image").status_code == 200
    (p.store.root / thumb).unlink()
    assert client.get("/api/persons/evidence/historic/image").status_code == 404


def test_frame_clean_rechecks_references_after_scan(catalogue, config):
    from datetime import UTC, datetime

    from maintenance.clean import Scan, person_frames

    p, _, actor = catalogue
    retained = p.image(photo(), "frames/source")["path"]
    new_reference = p.image(photo("blue"), "frames/source")["path"]
    orphan = p.image(photo("green"), "frames/source")["path"]
    frame_evidence(p, actor, "best", retained)
    action = person_frames(Scan(config, p.store, datetime.now(UTC)))
    frame_evidence(p, actor, "new", new_reference, shot="shot1", status="candidate")
    assert action.execute()["deleted"] == 1
    assert (p.store.root / new_reference).exists()
    assert not (p.store.root / orphan).exists()


def test_migrate_frames_respects_pipeline_lock(catalogue, config):
    from persons.persons import migrate_frames
    from shared.locks import SharedFileLock

    p, _, _ = catalogue
    with SharedFileLock(p.store.root / "pipeline.lock"), pytest.raises(ValueError, match="离线处理任务"):
        migrate_frames(config, apply=True)


def test_frame_review_prefers_existing_actor_frame(catalogue):
    from persons.persons import existing_frame

    p, _, actor = catalogue
    image = p.image(photo(), "frames/source")["path"]
    frame_evidence(p, actor, "missing-best", "missing.png", score=0.9)
    frame_evidence(p, actor, "existing", image, score=0.7)
    rows = [{**r, "data": json.loads(r["data"])} for r in p.store.rows("SELECT * FROM person_evidence")]
    assert existing_frame(p.store, rows, "thumbnail.jpg") == image
    (p.store.root / image).unlink()
    assert existing_frame(p.store, rows, "thumbnail.jpg") == "thumbnail.jpg"


def test_frame_api_uses_snapshot_when_current_image_missing(catalogue, config):
    p, _, actor = catalogue
    path = p.image(photo(), "frames/source")["path"]
    frame_evidence(p, actor, "fallback", "missing.png")
    version = {
        "evidence": [
            {"id": "fallback", "source_id": "source", "shot_id": "shot0", "data": {"image_path": path}}
        ]
    }
    p.store.execute("INSERT INTO person_versions(id,data) VALUES(?,?)", ("fallback", dumps(version)))
    response = TestClient(create_app(config)).get("/api/persons/evidence/fallback/image")
    assert response.status_code == 200 and response.content == (p.store.root / path).read_bytes()


def test_frame_retention_identity_union_and_empty_image(catalogue):
    from persons.persons import retained_frame_paths

    p, _, actor = catalogue
    second = p.save(Entity(kind="actor", name="演员乙"))
    frame_evidence(p, actor, "first", "shared.png")
    frame_evidence(p, second, "second", "shared.png")
    frame_evidence(p, actor, "role", "role.png", role="separate-role")
    frame_evidence(p, actor, "no-image", None, score=0.99)
    frame_evidence(p, actor, "candidate", "candidate.png", status="candidate")
    assert retained_frame_paths(p.store, "source") == {"source": {"shared.png", "role.png"}}


def test_scoped_frame_migration_preserves_cross_source_reference(catalogue, config):
    from persons.persons import migrate_frames

    p, _, actor = catalogue
    path = p.image(photo(), "frames/source")["path"]
    frame_evidence(p, actor, "external-owner", path)
    p.store.execute("UPDATE person_evidence SET source_id='other-source' WHERE id='external-owner'")
    result = migrate_frames(config, source_id="source", apply=True)
    assert result["sources"][0]["error"] is None
    assert (p.store.root / path).exists()


def test_frame_cache_reuse_after_missing_images_needs_no_model_extraction(catalogue, config):
    p, _, actor = catalogue
    p.add_reference(actor["id"], photo())
    thumbnail = p.image(photo("blue"))["path"]
    p.store.execute("UPDATE segments SET thumbnail=?", (thumbnail,))
    client = FakeClient()
    processor = PersonProcessor(config, client, Tracker)
    processor.run("source", PersonOptions())
    original_calls = (client.images, client.videos)
    features = p.store.rows("SELECT id,data FROM person_features WHERE kind='shot' ORDER BY id")
    for path in (p.store.root / "persons/frames/source").iterdir():
        path.unlink()
    report = processor.run("source", PersonOptions())
    assert report["reused_shots"] == 3 and report["extracted_shots"] == 0
    assert (client.images, client.videos) == original_calls
    assert p.store.rows("SELECT id,data FROM person_features WHERE kind='shot' ORDER BY id") == features
    assert all(e["data"]["image_path"] == thumbnail for e in p.evidence())


def test_frame_migration_lock_blocks_review(catalogue, config):
    from shared.locks import SharedFileLock

    p, _, actor = catalogue
    client = TestClient(create_app(config))
    with SharedFileLock(p.store.root / "pipeline.lock", exclusive=True):
        response = client.post(
            "/api/persons/review/shot",
            json={
                "source_id": "source",
                "shot_id": "shot0",
                "selections": [{"actor_id": actor["id"]}],
            },
        )
    assert response.status_code == 422 and "迁移或清理" in response.text
    assert p.store.rows("SELECT id FROM person_evidence") == []


def test_repeat_completed_person_task_reuses_extraction_and_matching(catalogue, config, monkeypatch):
    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    client = FakeClient()
    processor = PersonProcessor(config, client, Tracker)
    first = processor.run("source", PersonOptions())
    assert first["extracted_shots"] == 3
    before = (client.videos, client.images)
    matches = p.store.rows("SELECT id FROM person_features WHERE kind='matching' ORDER BY id")

    def unexpected(*args, **kwargs):
        pytest.fail("unchanged completed person tasks must reuse extraction and matching")

    monkeypatch.setattr(client, "video", unexpected)
    monkeypatch.setattr(client, "image", unexpected)
    monkeypatch.setattr("persons.person_processing.attribute_frames", unexpected)
    result = processor.run("source", PersonOptions())
    assert result["reused_shots"] == 3 and result["extracted_shots"] == 0
    assert (client.videos, client.images) == before
    assert p.store.rows("SELECT id FROM person_features WHERE kind='matching' ORDER BY id") == matches


def test_source_display_name_follows_curation_without_changing_work_identity(catalogue, config):
    p, work, _actor = catalogue
    client = TestClient(create_app(config))
    from admin.curation import CurationPatch, Library

    library = Library(p.store)
    for name in ["新的素材名", "再次改名", None]:
        library.patch("source", "source", CurationPatch(display_name=name))
        expected = name or "影片"
        response = client.get("/api/persons")
        assert response.status_code == 200
        data = response.json()
        assert data["sources"] == [{"id": "source", "name": expected, "work_id": work}]
        assert next(e for e in data["entities"] if e["id"] == work)["name"] == expected
        assert p.entity(work)["name"] == expected
        assert p.work_for("source") == work
        assert p.store.sources()[0]["name"] == "影片"

    p.save(Entity(kind="work", name="独立作品名"), work)
    library.patch("source", "source", CurationPatch(display_name="又一个素材名"))
    assert p.entity(work)["name"] == "独立作品名"
    assert next(e for e in p.entities() if e["id"] == work)["name"] == "独立作品名"


def test_linked_shots_use_current_source_display_name(catalogue):
    p, work, actor = catalogue
    from admin.curation import CurationPatch, Library

    p.store.execute(
        "INSERT INTO person_evidence(id,source_id,shot_id,actor_id,start,end,status,data) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("rename-evidence", "source", "shot0", actor["id"], 0, 1, "candidate", "{}"),
    )
    role = p.save(Entity(kind="role", name="测试角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.store.execute(
        "UPDATE person_evidence SET role_id=? WHERE id='rename-evidence'",
        (role["id"],),
    )
    Library(p.store).patch("source", "source", CurationPatch(display_name="关联分镜新名"))
    linked = p.linked_shots(actor["id"])
    assert linked["total"] == 1
    assert linked["items"][0]["source_name"] == "关联分镜新名"


def test_reassign_work_removes_only_unused_default_work(catalogue):
    p, work, _actor = catalogue
    replacement = p.save(Entity(kind="work", name="独立作品"))
    p.assign_work("source", replacement["id"])
    assert p.work_for("source") == replacement["id"]
    assert not p.store.one("SELECT id FROM person_entities WHERE id=?", (work,))


@pytest.mark.parametrize("protected", ["name", "role", "import", "shared"])
def test_reassign_work_preserves_referenced_or_custom_work(catalogue, protected):
    p, work, actor = catalogue
    if protected == "name":
        p.save(Entity(kind="work", name="自定义作品"), work)
    elif protected == "role":
        p.save(Entity(kind="role", name="角色", work_ids=[work], actor_ids=[actor["id"]]))
    elif protected == "import":
        p.store.execute("INSERT INTO person_imports(id,data) VALUES(?,?)", ("cast", dumps({"work_id": work})))
    else:
        p.store.execute(
            "INSERT INTO sources(id,name,path,media,layout_key) "
            "SELECT 'another-source',name,path,media,layout_key FROM sources WHERE id='source'"
        )
        p.store.execute("INSERT INTO person_sources VALUES(?,?)", ("another-source", work))
    replacement = p.save(Entity(kind="work", name="独立作品"))
    p.assign_work("source", replacement["id"])
    assert p.entity(work)["id"] == work


def test_purge_removes_unused_default_work(catalogue):
    from persons.persons import purge_person_source

    p, work, _actor = catalogue
    with p.store.connect() as db:
        purge_person_source(db, "source")
    assert not p.store.one("SELECT id FROM person_entities WHERE id=?", (work,))


def test_role_only_migration_deletes_actor_evidence_and_preserves_pairs_exclusions_and_releases(catalogue):
    from shared.person_schema import migrate_actor_only_evidence

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="保留角色", work_ids=[work], actor_ids=[actor["id"]]))
    records = [
        ("old-auto", "shot0", None, "accepted", {}),
        ("old-manual", "shot0", None, "accepted", {"reviewed": True, "confirmation": "shot_review"}),
        ("old-candidate", "shot0", None, "candidate", {}),
        ("old-unselected", "shot0", None, "rejected", {"reviewed": True, "confirmation": "shot_review"}),
        ("excluded", "shot1", None, "rejected", {"reviewed": True, "confirmation": "shot_review"}),
        ("pair-auto", "shot2", role["id"], "accepted", {"score": 0.8}),
        ("pair-manual", "shot2", role["id"], "accepted", {"reviewed": True, "confirmation": "shot_review"}),
        ("pair-rejected", "shot2", role["id"], "rejected", {"reviewed": True}),
    ]
    for identifier, shot, character, status, data in records:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                "source",
                shot,
                actor["id"],
                character,
                None,
                0,
                1,
                status,
                dumps(data),
            ),
        )
    before = {r["id"]: r for r in p.store.rows("SELECT * FROM person_evidence")}
    snapshot = dumps({"format": 1, "entities": p.entities(), "evidence": p.evidence(), "coverage": []})
    p.store.execute("INSERT INTO person_versions(id,data) VALUES('legacy-release',?)", (snapshot,))
    with p.store.connect() as db:
        db.execute("DELETE FROM settings WHERE key='migration.person-role-only-v3'")
        migrate_actor_only_evidence(db)
        migrate_actor_only_evidence(db)
    after = {r["id"]: r for r in p.store.rows("SELECT * FROM person_evidence")}
    assert after == {
        key: before[key]
        for key in ["old-unselected", "excluded", "pair-auto", "pair-manual", "pair-rejected"]
    }
    assert p.store.one("SELECT data FROM person_versions WHERE id='legacy-release'")["data"] == snapshot
    assert p.review_summary() == {"source": {"accepted": 1, "candidate": 1, "rejected": 1}}
    unknown = p.review_shots(status="candidate", no_candidate=True)["items"]
    assert [s["shot_id"] for s in unknown] == ["shot0"] and unknown[0]["evidence"] == []


def test_review_all_endpoints_exclude_foreign_invalid_and_actor_only_evidence(catalogue, config):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="当前角色", work_ids=[work], actor_ids=[actor["id"]]))
    outsider = p.save(Entity(kind="actor", name="外片演员"))
    foreign = p.save(Entity(kind="work", name="外片"))
    foreign_role = p.save(
        Entity(kind="role", name="外片角色", work_ids=[foreign["id"]], actor_ids=[outsider["id"]])
    )
    for key, who, character, score in [
        ("local", actor["id"], role["id"], 0.4),
        ("foreign", outsider["id"], foreign_role["id"], 0.99),
        ("mismatch", outsider["id"], role["id"], 0.98),
        ("actor-only", actor["id"], None, 0.97),
    ]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                "source",
                "shot0",
                who,
                character,
                None,
                0,
                1,
                "accepted",
                dumps({"score": score}),
            ),
        )
    client = TestClient(create_app(config))
    for compact in [False, True]:
        result = client.get(
            "/api/persons/review/shots", params={"source_id": "source", "compact": compact}
        ).json()
        shot = result["items"][0]
        assert shot["candidate"] == {"actor_id": actor["id"], "role_id": role["id"], "score": 0.4}
        assert [r["id"] for r in shot["evidence"]] == ["local"]
        assert shot["counts"] == {"accepted": 1, "candidate": 0, "rejected": 0}
        assert [(r["actor_id"], r["role_id"]) for r in shot["recognized_people"]] == [
            (actor["id"], role["id"])
        ]
        assert result["filters"] == [{"actor_id": actor["id"], "role_id": role["id"], "count": 1}]
    listing = client.get("/api/persons/evidence").json()
    assert listing["total"] == 1 and [r["id"] for r in listing["items"]] == ["local"]
    for identifier in ["foreign", "mismatch", "actor-only"]:
        assert client.get("/api/persons/evidence/" + identifier).status_code == 404
    for selection in [
        {"actor_id": actor["id"]},
        {"actor_id": outsider["id"], "role_id": foreign_role["id"]},
        {"actor_id": outsider["id"], "role_id": role["id"]},
    ]:
        response = client.post(
            "/api/persons/review/shot",
            json={"source_id": "source", "shot_id": "shot0", "selections": [selection]},
        )
        assert response.status_code == 422
    p.shot_review(ShotReview(source_id="source", shot_id="shot0", selections=[]))
    assert p.review_shots(status="rejected")["total"] == 1


def test_processing_uses_only_work_role_references_and_roles_compete(catalogue, config):
    from admin.source_tasks import person_setup

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="本片角色", work_ids=[work], actor_ids=[actor["id"]]))
    foreign = p.save(Entity(kind="work", name="另一片"))
    other_role = p.save(
        Entity(kind="role", name="外片同演员", work_ids=[foreign["id"]], actor_ids=[actor["id"]])
    )
    p.add_reference(actor["id"], photo("blue"))
    p.add_reference(actor["id"], photo("green"), role_id=other_role["id"])
    assert not person_setup(p.store, "source")["ready"]
    local = p.add_reference(actor["id"], photo(), role_id=role["id"])
    assert person_setup(p.store, "source")["ready"]
    client = FakeClient()
    processor = PersonProcessor(config, client, Tracker)
    first = processor.run("source", PersonOptions())
    assert client.images == 1
    assert {e["role_id"] for e in p.evidence()} == {role["id"]}
    assert {e["data"]["reference_id"] for e in p.evidence()} == {local["id"]}
    second = p.save(Entity(kind="role", name="本片另一角色", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo("yellow"), role_id=second["id"])
    report = processor.run("source", PersonOptions())
    assert report["reused_shots"] == 3 and first["extracted_shots"] == 3 and client.videos == 3
    assert client.images == 2
    assert all(e["status"] == "accepted" and e["role_id"] == role["id"] for e in p.evidence())
    assert p.review_shots(status="accepted")["total"] == 3


def test_new_release_actor_queries_use_snapshot_roles_and_keep_multi_person_semantics(catalogue, config):
    from search.search import SearchEngine

    p, work, actor = catalogue
    other = p.save(Entity(kind="actor", name="替演演员"))
    role = p.save(Entity(kind="role", name="同一角色", work_ids=[work], actor_ids=[actor["id"], other["id"]]))
    second = p.save(Entity(kind="role", name="另一个角色", work_ids=[work], actor_ids=[other["id"]]))
    for key, character, start, end in [("first", role["id"], 0, 0.6), ("second", second["id"], 0.4, 1)]:
        p.store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                "source",
                "shot0",
                other["id"],
                character,
                None,
                start,
                end,
                "accepted",
                "{}",
            ),
        )
    p.store.execute(
        "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy",
            "source",
            "shot1",
            actor["id"],
            None,
            None,
            1,
            2,
            "accepted",
            "{}",
        ),
    )
    publish_content(p, config)
    release = p.publish()
    snapshot = p.store.one("SELECT data FROM person_versions WHERE id=?", (release["person_version"],))[
        "data"
    ]
    assert {e["id"] for e in json.loads(snapshot)["evidence"]} == {"first", "second"}
    p.save(Entity(kind="role", name=role["name"], work_ids=[work], actor_ids=[other["id"]]), role["id"])
    engine = SearchEngine(config)

    def shots(identifiers, relation="all"):
        return [
            r["id"]
            for r in engine.text(
                TextSearch(person_ids=identifiers, person_relation=relation, granularity="shot")
            )["results"]
        ]

    assert shots([actor["id"]]) == ["shot0"]  # The winning photo belongs to the other performer.
    assert shots([actor["id"], second["id"]]) == ["shot0"]
    p.store.execute("UPDATE person_evidence SET start=.7 WHERE id='second'")
    assert shots([actor["id"], second["id"]]) == ["shot0"]  # Draft changes cannot alter the release.
    p.publish()
    assert shots([role["id"], second["id"]], "all") == []
    assert shots([role["id"], second["id"]], "any") == ["shot0"]
    assert shots([actor["id"]]) == []
    assert (
        p.store.one("SELECT data FROM person_versions WHERE id=?", (release["person_version"],))["data"]
        == snapshot
    )


def test_frame_interval_cache_and_review_preservation(catalogue, config):
    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="间隔测试", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])

    class IntervalClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.intervals = []

        def video(self, path, start, end, frame_interval):
            self.intervals.append(frame_interval)
            return super().video(path, start, end, frame_interval)

    client = IntervalClient()
    processor = PersonProcessor(config, client, Tracker)
    processor.run("source", PersonOptions())
    original = {e["id"]: e for e in p.evidence()}
    rejected = next(iter(original))
    p.review(rejected, Review(accept=False, reason="排除"))
    processor.run("source", PersonOptions())
    assert client.intervals == [15] * 3
    config.person.frame_interval = 1
    processor.run("source", PersonOptions())
    assert client.intervals == [15] * 3 + [1] * 3
    for identifier in original:
        row = p.store.one("SELECT * FROM person_evidence WHERE id=?", (identifier,))
        assert row["status"] == ("rejected" if identifier == rejected else "accepted")
        if identifier != rejected:
            assert not json.loads(row["data"]).get("reviewed")


def test_interval_changes_preserve_confirmed_and_excluded_shots(catalogue, config):
    from persons.person_models import ShotReview

    p, work, actor = catalogue
    role = p.save(Entity(kind="role", name="审核保留", work_ids=[work], actor_ids=[actor["id"]]))
    p.add_reference(actor["id"], photo(), role_id=role["id"])
    processor = PersonProcessor(config, FakeClient(), Tracker)
    processor.run("source", PersonOptions())
    p.shot_review(ShotReview(source_id="source", shot_id="shot0"))
    p.shot_review(
        ShotReview(
            source_id="source",
            shot_id="shot1",
            selections=[
                {"actor_id": actor["id"], "role_id": role["id"]},
            ],
        )
    )
    for interval in (5, 1, 15):
        config.person.frame_interval = interval
        report = processor.run("source", PersonOptions())
        assert report["accepted_shots"] == 2 and report["candidate_shots"] == 0
        assert p.review_summary()["source"] == {"accepted": 2, "candidate": 0, "rejected": 1}
        assert [s["shot_id"] for s in p.review_shots(status="rejected")["items"]] == ["shot0"]
        assert p.review_shots(status="accepted", limit=1)["total"] == 2


def test_review_without_person_evidence_uses_shot_thumbnail(catalogue, config):
    p, *_ = catalogue
    thumbnail = photo()
    (p.store.root / "shot-preview.jpg").write_bytes(thumbnail)
    p.store.execute("UPDATE segments SET thumbnail='shot-preview.jpg' WHERE id='shot0'")
    p.store.execute(
        "INSERT INTO person_coverage VALUES(?,?,?)",
        (
            "source",
            "empty-shot",
            dumps({"shot_id": "shot0", "start": 0, "end": 1}),
        ),
    )
    client = TestClient(create_app(config))
    for compact in (False, True):
        result = client.get("/api/persons/review/shots", params={"compact": compact, "status": "candidate"})
        assert result.status_code == 200
        shot = result.json()["items"][0]
        assert shot["candidate"] is None and shot["evidence"] == []
        assert shot["image_url"] == "/api/admin/segments/shot0/thumbnail"
        assert client.get(shot["image_url"]).content == thumbnail
    detail = client.get("/api/persons/review/shots/shot0", params={"source_id": "source"})
    assert detail.status_code == 200
    assert detail.json()["image_url"] == "/api/admin/segments/shot0/thumbnail"


def test_persons_polling_backoff_unit():
    node = shutil.which("node")
    if not node:
        pytest.skip("人物页面轮询退避检查需要 Node.js")
    result = subprocess.run(
        [node, "tests/persons_poll_unit.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0 and "checks passed" in result.stdout, result.stdout + result.stderr


def test_person_features_reuse_same_intervals_after_shot_ids_change(catalogue, config, monkeypatch):
    p, _work, actor = catalogue
    p.add_reference(actor["id"], photo())
    client = FakeClient()
    processor = PersonProcessor(config, client, Tracker)
    processor.run("source", PersonOptions())
    original = (client.videos, client.images)
    p.store.execute("UPDATE segments SET id='alias-' || id WHERE source_id='source'")
    p.store.execute("UPDATE sources SET layout_key='layout-alias' WHERE id='source'")

    def unexpected(*args, **kwargs):
        pytest.fail("镜头 ID 变化不改变原片区间，不能重新提取人物特征")

    monkeypatch.setattr(client, "video", unexpected)
    monkeypatch.setattr(client, "image", unexpected)
    result = processor.run("source", PersonOptions())
    assert result["reused_shots"] == 3 and result["extracted_shots"] == 0
    assert (client.videos, client.images) == original


def test_partial_person_sampling_uses_actual_shot_offset_and_reuses_it(catalogue, config):
    _catalogue, _work, _actor = catalogue

    class Observed(FakeClient):
        def __init__(self):
            super().__init__()
            self.ranges = []

        def video(self, path, start, end, interval):
            self.ranges.append((start, end))
            return super().video(path, start, end, interval)

    client = Observed()
    processor = PersonProcessor(config, client, Tracker)
    options = PersonOptions(start=1.2, end=1.8)
    first = processor.run("source", options)
    second = processor.run("source", options)
    assert client.ranges == [pytest.approx((0.2, 0.8))]
    assert first["extracted_shots"] == 1 and second["reused_shots"] == 1


def test_frame_retention_protects_unmatched_features_and_published_versions(catalogue, config):
    from persons.persons import retained_frame_paths

    p, _work, actor = catalogue
    p.store.execute(
        "INSERT INTO person_features VALUES(?,?,?,?,?)",
        ("feature", "source", "{}", "shot", json.dumps({"frames": [{"image_path": "unmatched.jpg"}]})),
    )
    p.store.execute(
        "INSERT INTO person_coverage VALUES(?,?,?)", ("source", "feature", json.dumps({"shot_id": "shot0"}))
    )
    p.store.execute(
        "INSERT INTO person_versions(id,data) VALUES(?,?)",
        (
            "release",
            json.dumps({"evidence": [{"source_id": "source", "data": {"image_path": "published.jpg"}}]}),
        ),
    )
    frame_evidence(p, actor, "accepted", "current.jpg", shot="shot1")
    assert retained_frame_paths(p.store)["source"] == {"unmatched.jpg", "published.jpg", "current.jpg"}
