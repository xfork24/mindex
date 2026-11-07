"""引用片段边界与补帧回归。"""

import io
import json

import pytest
from PIL import Image

from persons.person_processing import PersonClient, ensure_reference_quality, reference_check
from shared.config import fingerprint
from shared.storage import Store, dumps


def face():
    return {
        "face_bbox": [30, 40, 100, 120],
        "face_feature": [1.0],
        "face_quality": 0.9,
        "body_bbox": [20, 20, 110, 150],
    }


def test_tight_reference_padding_and_rejected_cache_retry(config, monkeypatch):
    store = Store(config.data_dir)
    path = store.root / "tight.png"
    Image.new("RGB", (100, 120), "orange").save(path)
    client = PersonClient(config)
    space = {"model": config.person.model}
    monkeypatch.setattr(client, "identity", lambda: space)
    sizes = []

    def request(method, route, **kwargs):
        content = kwargs["files"]["file"][1]
        with Image.open(io.BytesIO(content) if isinstance(content, bytes) else content) as image:
            sizes.append(image.size)
        return {"space": space, "observations": [face()] if len(sizes) % 2 == 0 else []}

    monkeypatch.setattr(client, "request", request)
    item = {"path": "tight.png", "digest": "tight-image"}
    key = fingerprint(["reference-v1", item["digest"], space])
    store.execute(
        "INSERT INTO person_features VALUES(?,?,?,?,?)",
        (key, None, dumps(space), "reference", dumps({"space": space, "observations": []})),
    )
    ensure_reference_quality(store, config, client, item)
    assert sizes == [(100, 120), (124, 144)]
    cached = store.one("SELECT data FROM person_features WHERE id=?", (key,))

    extracted = json.loads(cached["data"])
    subject, reason = reference_check(extracted["observations"], 0)
    assert reason is None
    assert subject["face_bbox"] == [18, 28, 88, 108]
    ensure_reference_quality(store, config, client, item)
    assert len(sizes) == 2
    with Image.open(path) as image:
        assert image.size == (100, 120), "stored crop must remain unchanged"


@pytest.mark.parametrize("observations", [[], [face(), face()]])
def test_padding_does_not_bypass_no_face_or_group_checks(config, tmp_path, monkeypatch, observations):
    path = tmp_path / "reference.png"
    Image.new("RGB", (100, 120)).save(path)
    client = PersonClient(config)
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return {"space": {"model": "test"}, "observations": observations}

    monkeypatch.setattr(client, "request", request)
    result = client.image(path)
    assert reference_check(result["observations"], 0)[1] is not None
    assert len(calls) == (2 if not observations else 1)
