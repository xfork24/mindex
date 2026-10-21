"""voices_catalog：按 TTS 后端聚合系统音色目录、本地音色库与克隆能力。"""

from __future__ import annotations

from mindex.models import adapters
from mindex.settings.runtime_settings import save_voice
from mindex.settings.voices_catalog import QWEN3_TTS_CUSTOMVOICE, fetch_catalog
from mindex.shared.storage import Store


def test_static_qwen_table_complete():
    ids = [item["voice_id"] for item in QWEN3_TTS_CUSTOMVOICE]
    assert set(ids) == {
        "vivian", "serena", "uncle_fu", "dylan", "eric", "ryan", "aiden", "ono_anna", "sohee",
    }
    assert len(ids) == len(set(ids)) == 9
    labeled = {item["voice_id"]: item for item in QWEN3_TTS_CUSTOMVOICE}
    assert labeled["vivian"]["language"] == "中文"
    assert labeled["ono_anna"]["language"] == "日语"


def test_openai_catalog_merges_static_labels_and_live(config, monkeypatch):
    def reply(method, url, **kwargs):
        assert url.endswith("/audio/voices")
        return {"voices": ["vivian", "serena", "mindex-clone-1"]}

    monkeypatch.setattr(adapters, "request", reply)
    catalog = fetch_catalog(config, Store(config.data_dir))
    assert catalog["backend"] == "openai"
    assert catalog["clone"] == {"supported": True, "mode": "upload", "ref_text": True}
    entries = {voice["voice_id"]: voice["label"] for group in catalog["groups"] for voice in group["voices"]}
    assert "Vivian" in entries["vivian"]  # 静态表提供可读名
    assert "mindex-clone-1" in entries  # 服务端动态注册项原样保留
    assert catalog["error"] is None


def test_openai_catalog_heals_missing_clone_after_restart(config, monkeypatch):
    store = Store(config.data_dir)
    sample = store.root / "voices" / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFF-sample")
    save_voice(
        store, name="我的旁白", provider="self", voice_id="mindex-clone-1",
        kind="clone", sample_path=str(sample.relative_to(store.root)), notes="样本参考文本",
    )
    calls = []

    def reply(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if url.endswith("/audio/voices"):
            return {"voices": ["vivian"]}
        raise AssertionError(f"意外请求 {url}")

    monkeypatch.setattr(adapters, "request", reply)
    catalog = fetch_catalog(config, store)
    registers = [call for call in calls if call[0] == "POST"]
    assert registers, "缺失的克隆音色应触发重注册"
    url, body = registers[0][1], registers[0][2]
    assert url.endswith("/audio/voices")
    assert body["name"] == "mindex-clone-1"
    assert body["ref_text"] == "样本参考文本"
    ids = {voice["voice_id"] for group in catalog["groups"] for voice in group["voices"]}
    assert "mindex-clone-1" in ids


def test_minimax_catalog_groups_and_merges_library(config, monkeypatch):
    def reply(method, url, **kwargs):
        assert url.endswith("/get_voice")
        assert kwargs["json"] == {"voice_type": "all"}
        return {
            "base_resp": {"status_code": 0},
            "system_voice": [
                {"voice_id": "Chinese (Mandarin)_Reliable_Executive", "voice_name": "可靠高管", "description": ["成熟稳重"]},
                {"voice_id": "female-yujie", "voice_name": "御姐", "description": ["成熟女性"]},
            ],
            "voice_cloning": [{"voice_id": "mindex-clone-2", "description": ["平台复刻"]}],
            "voice_generation": [],
        }

    monkeypatch.setattr(adapters, "request", reply)
    config.studio.tts.backend = "minimax"
    config.studio.tts.base_url = "https://api.minimax.cn/v1"
    catalog = fetch_catalog(config, Store(config.data_dir))
    assert catalog["clone"] == {"supported": True, "mode": "upload", "ref_text": False}
    groups = {group["label"]: {voice["voice_id"] for voice in group["voices"]} for group in catalog["groups"]}
    assert "Chinese (Mandarin)_Reliable_Executive" in groups.get("Chinese (Mandarin)", set())
    assert "female-yujie" in groups.get("经典音色", set())
    library = {voice["voice_id"] for voice in catalog["library"]}
    assert "mindex-clone-2" in library


def test_catalog_upstream_error_degrades_to_static(config, monkeypatch):
    def fail(*args, **kwargs):
        raise adapters.ModelError("连接被拒绝")

    monkeypatch.setattr(adapters, "request", fail)
    catalog = fetch_catalog(config, Store(config.data_dir))
    assert catalog["error"]
    ids = {voice["voice_id"] for group in catalog["groups"] for voice in group["voices"]}
    assert "vivian" in ids  # 静态表兜底
