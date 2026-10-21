from __future__ import annotations

import json

from mindex.studio import studio
from mindex.studio.studio_artifacts import ArtifactSession, current, safe_path


def test_diagnostics_redact_credentials_and_keep_raw_model_response(config, monkeypatch):
    from mindex.models.adapters import request

    job_id = studio.create_job(config, "测试文案。")["id"]
    state = studio.begin_step(config, job_id, "split")
    session = ArtifactSession(config, job_id, "split", state)

    class Response:
        status_code = 200
        text = '{"choices":[]}'

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": []}

    monkeypatch.setattr("mindex.models.adapters.httpx.request", lambda *args, **kwargs: Response())
    token = current.set(session)
    try:
        request(
            "POST",
            "http://user:secret@localhost/v1/chat?token=secret",
            headers={"Authorization": "Bearer secret"},
            json={"input": "文案内容"},
        )
    finally:
        current.reset(token)
    content = "\n".join(path.read_text() for path in (session.directory / "debug").glob("*.json"))
    assert "secret" not in content
    assert "文案内容" in content
    response = json.loads(min((session.directory / "debug").glob("*model-response.json")).read_text())
    assert response["data"]["body"] == Response.text


def test_safe_path_rejects_symlink_escape(tmp_path):
    import pytest

    root = tmp_path / "job"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(studio.StudioError):
        safe_path(root, "link/overwritten.wav")
