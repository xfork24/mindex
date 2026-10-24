"""POST /api/persons/identify：确定性人物服务桩验证排名、接受条件、未覆盖参考图与错误码。"""

from __future__ import annotations

import io
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mindex.api import create_app
from mindex.models.adapters import ModelError
from mindex.persons.person_api import identify_faces
from mindex.persons.person_models import Entity
from mindex.persons.persons import Persons
from mindex.shared.config import fingerprint
from mindex.shared.storage import Store, dumps

SPACE = {"model": "cheetah_l", "weights": "fixture-identify"}


def photo(color="red", size=(160, 120)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def face(feature, quality=0.95, box=(10, 10, 40, 40)):
    return {
        "body_bbox": [0, 0, 100, 120],
        "face_bbox": list(box),
        "face_feature": feature,
        "body_feature": [1, 0, 0],
        "face_quality": quality,
        "det_score": 0.99,
    }


class StubPersonClient:
    """确定性人物服务：固定空间与观测；记录收到的超时与图片。"""

    observations: ClassVar[list] = []
    fail: str | None = None
    seen: ClassVar[list] = []

    def __init__(self, config):
        self.config = config.person

    def identity(self):
        StubPersonClient.seen.append(("timeout", self.config.timeout))
        if self.fail == "identity":
            raise ModelError("人物服务不可用：connection refused")
        return dict(SPACE)

    def image(self, path):
        with Image.open(path) as image:
            StubPersonClient.seen.append(("image", image.format, image.size))
        if self.fail == "image":
            raise ModelError("人物服务请求失败：read timeout")
        space = {"model": "cheetah_l", "weights": "other"} if self.fail == "space" else dict(SPACE)
        return {"space": space, "observations": self.observations}


@pytest.fixture
def library(config, monkeypatch):
    return seed_library(config, monkeypatch)


def seed_library(config, monkeypatch):
    """两名演员、三个角色、五张参考图（四张有当前空间特征缓存）；人物服务替换为确定性桩。"""
    monkeypatch.setattr("mindex.persons.person_api.PersonClient", StubPersonClient)
    StubPersonClient.observations, StubPersonClient.fail, StubPersonClient.seen = [], None, []
    store = Store(config.data_dir)
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        ("source", "影片", "source.mp4", dumps({"duration": 3}), "layout"),
    )
    persons = Persons(store)
    work = persons.work_for("source")
    alice = persons.save(Entity(kind="actor", name="演员甲"))
    bob = persons.save(Entity(kind="actor", name="演员乙"))
    young = persons.save(Entity(kind="role", name="青年叶淑柔", work_ids=[work], actor_ids=[alice["id"]]))
    old = persons.save(Entity(kind="role", name="老年叶淑柔", work_ids=[work], actor_ids=[alice["id"]]))
    villain = persons.save(Entity(kind="role", name="反派", work_ids=[work], actor_ids=[bob["id"]]))

    def cached(reference, feature, quality=0.95):
        store.execute(
            "INSERT INTO person_features VALUES(?,?,?,?,?)",
            (
                fingerprint(["reference-v1", reference["digest"], SPACE]),
                None,
                dumps(SPACE),
                "reference",
                dumps({"space": SPACE, "observations": [face(feature, quality)]}),
            ),
        )

    refs = {
        "young": persons.add_reference(alice["id"], photo("red"), role_id=young["id"]),
        "old": persons.add_reference(alice["id"], photo("blue"), role_id=old["id"]),
        "villain": persons.add_reference(bob["id"], photo("green"), role_id=villain["id"]),
        "bob": persons.add_reference(bob["id"], photo("gold")),
        "uncached": persons.add_reference(alice["id"], photo("pink")),
    }
    cached(refs["young"], [1, 0, 0])
    cached(refs["old"], [0.6, 0.8, 0])
    cached(refs["villain"], [0, 0, 1])
    cached(refs["bob"], [0, 0.6, 0.8])
    ids = {"alice": alice["id"], "bob": bob["id"], "young": young["id"], "old": old["id"],
           "villain": villain["id"]}
    return TestClient(create_app(config)), ids, refs


def post(client, data=None, raw=None, name="q.png"):
    return client.post(
        "/api/persons/identify",
        files={"file": (name, raw if raw is not None else photo(size=(1600, 1200)), "image/png")},
        data=data or {},
    )


def test_identify_ranks_roles_and_actors_and_counts_uncovered(library, config):
    client, ids, refs = library
    StubPersonClient.observations = [
        face([1, 0, 0]),                         # 明确是青年叶淑柔
        face([0.85, 0.5, 0], box=(50, 10, 80, 40)),  # 在两个角色之间，领先不足
        {**face(None), "face_bbox": None},       # 只有人体，没有人脸 → 跳过
    ]
    response = post(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["space_model"] == "cheetah_l"
    assert body["references_total"] == 5 and body["uncovered_references"] == 1
    assert len(body["faces"]) == 2

    clear, ambiguous = body["faces"]
    assert [r["id"] for r in clear["roles"]] == [ids["young"], ids["old"], ids["villain"]]
    assert clear["roles"][0] == {
        "id": ids["young"], "name": "青年叶淑柔", "kind": "role", "score": 1.0, "accepted": True,
        "reference_id": refs["young"]["id"],
        "reference_url": f"/api/persons/references/{refs['young']['id']}/image",
    }
    assert [r["accepted"] for r in clear["roles"]] == [True, False, False]
    assert clear["accepted_role"] == {"id": ids["young"], "name": "青年叶淑柔", "score": 1.0}
    # 演员分组取各自最高分的参考图：演员甲由青年角色照片胜出。
    assert [a["id"] for a in clear["actors"]] == [ids["alice"], ids["bob"]]
    assert clear["actors"][0]["reference_id"] == refs["young"]["id"] and clear["actors"][0]["accepted"]
    assert clear["actors"][1]["name"] == "演员乙" and clear["actors"][1]["kind"] == "actor"

    scores = [r["score"] for r in ambiguous["roles"]]
    assert scores == sorted(scores, reverse=True)
    assert ambiguous["roles"][0]["id"] == ids["old"] and not ambiguous["roles"][0]["accepted"]
    assert ambiguous["accepted_role"] is None
    assert ambiguous["face_bbox"] == [50, 10, 80, 40]

    # 上传图归一化为 PNG 且受像素上限约束；识别请求的人物服务超时收紧到 60 秒。
    image = next(s for s in StubPersonClient.seen if s[0] == "image")
    assert image[1] == "PNG" and image[2][0] * image[2][1] <= config.video.max_pixels < 1600 * 1200
    assert body["image_size"] == list(image[2])
    assert ("timeout", 60.0) in StubPersonClient.seen


def test_identify_top_k_and_no_faces(library):
    client, ids, _refs = library
    StubPersonClient.observations = [face([1, 0, 0])]
    body = post(client, {"top_k": "1"}).json()
    assert [r["id"] for r in body["faces"][0]["roles"]] == [ids["young"]]
    assert len(body["faces"][0]["actors"]) == 1
    assert post(client, {"top_k": "0"}).status_code == 422
    StubPersonClient.observations = []
    assert post(client).json()["faces"] == []


def test_identify_errors(library):
    client, _ids, _refs = library
    assert post(client, raw=b"not an image", name="x.png").status_code == 422
    assert post(client, raw=b"").status_code == 422
    StubPersonClient.fail = "identity"
    response = post(client)
    assert response.status_code == 503 and response.json()["detail"].startswith("人物服务不可用")
    StubPersonClient.fail = "space"
    response = post(client)
    assert response.status_code == 503 and "空间" in response.json()["detail"]
    # 特征抽取阶段失败（身份校验已通过）同样是 503，中文原因原样返回。
    StubPersonClient.fail = "image"
    response = post(client)
    assert response.status_code == 503 and response.json()["detail"] == "人物服务请求失败：read timeout"


def reference(identifier, feature, *, role=None, actor="actor", quality=0.9):
    return {"id": identifier, "role_id": role, "actor_id": actor, "face_feature": feature,
            "face_quality": quality, "face_bbox": [0, 0, 10, 10]}


def test_identify_faces_ranking_rules(config):
    """纯计算：同一角色多张参考图取最高分；低质量参考图不参与；接受条件看全量第二名而非截断后的列表。"""
    person = config.person.model_copy(update={"min_reference_quality": 0.5})
    references = [
        reference("ra1", [1, 0, 0], role="A", actor="x"),
        reference("ra2", [0, 1, 0], role="A", actor="x"),
        reference("rb", [0.8, 0.6, 0], role="B", actor="y"),
        reference("rc", [1, 0, 0], role="C", actor="y", quality=0.1),  # 低于参考图门槛
        reference("ry", [0, 0, 1], actor="y"),                         # 只有演员、没有角色
    ]
    entities = {"A": {"name": "甲角色", "kind": "role"}, "B": {"name": "乙角色", "kind": "role"},
                "x": {"name": "演员X", "kind": "actor"}}

    def run(feature, quality=0.95, top_k=5):
        return identify_faces([face(feature, quality)], references, entities, person, top_k)[0]

    clear = run([1, 0, 0])
    assert [(r["id"], r["score"], r["accepted"]) for r in clear["roles"]] == [("A", 1.0, True), ("B", 0.8, False)]
    assert clear["roles"][0]["reference_id"] == "ra1" and clear["accepted_role"]["id"] == "A"
    # 演员 y 的最高分来自角色 B 的照片（rc 被质量门槛挡下，ry 正交）。
    assert [(a["id"], a["reference_id"]) for a in clear["actors"]] == [("x", "ra1"), ("y", "rb")]
    assert clear["actors"][1]["name"] is None and clear["actors"][1]["kind"] == "actor"

    # 同一角色取多张参考图中的最高分：A 的第二张照片 [0,1,0] 不会拉低它。
    other = run([0.8, 0.6, 0])
    assert [r["id"] for r in other["roles"]] == ["B", "A"] and other["roles"][1]["score"] == 0.8
    assert other["roles"][1]["reference_id"] == "ra1" and other["accepted_role"]["id"] == "B"

    # 两个角色几乎同分：领先不足，top_k=1 截断后仍按全量第二名判定为不接受。
    close = run([0.95, 0.31, 0], top_k=1)
    assert len(close["roles"]) == 1 and close["roles"][0]["id"] == "A"
    assert not close["roles"][0]["accepted"] and close["accepted_role"] is None

    # 人脸质量低于 min_face_quality：分数最高也不接受。
    blurry = run([1, 0, 0], quality=0.3)
    assert blurry["roles"][0]["score"] == 1.0 and not any(r["accepted"] for r in blurry["roles"] + blurry["actors"])
    assert blurry["accepted_role"] is None


def test_identify_ignores_references_cached_in_other_space(library, config):
    client, ids, _refs = library
    store = Store(config.data_dir)
    persons = Persons(store)
    stale = persons.add_reference(ids["bob"], photo("teal"), role_id=ids["villain"])
    other = {"model": "cheetah_l", "weights": "previous"}
    store.execute(
        "INSERT INTO person_features VALUES(?,?,?,?,?)",
        (fingerprint(["reference-v1", stale["digest"], other]), None, dumps(other), "reference",
         dumps({"space": other, "observations": [face([0, 1, 0])]})),
    )
    StubPersonClient.observations = [face([0, 1, 0])]
    body = post(client).json()
    assert body["references_total"] == 6 and body["uncovered_references"] == 2
    villain = next(r for r in body["faces"][0]["roles"] if r["id"] == ids["villain"])
    # 旧空间缓存的完全相同特征不参与比对：反派得分仍来自当前空间的那张参考图。
    assert villain["reference_id"] != stale["id"] and villain["score"] < 0.5
