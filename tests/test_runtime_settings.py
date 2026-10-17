"""runtime_settings：模型服务配置档案、热生效覆盖层与密钥掩码。"""

from __future__ import annotations

import json

import pytest

from mindex.runtime_settings import (
    CLEAR_KEY,
    PROFILE_FIELDS,
    PROVIDER_PRESETS,
    ROLES,
    RuntimeConfig,
    activate_profile,
    apply_overrides,
    apply_runtime_overrides,
    delete_profile,
    list_profiles,
    mask_key,
    save_profile,
)
from mindex.storage import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def _tokenhub_profile(store, **extra):
    fields = {
        "role": "embedding",
        "provider": "tencent",
        "name": "腾讯TokenHub",
        "config": {
            "backend": "tokenhub",
            "base_url": "https://tokenhub.tencentcloudmaas.com/v1",
            "model": "kinfra-vl-embedding-8b",
            "dimensions": 4096,
        },
    }
    fields.update(extra)
    return save_profile(store, **fields)


def test_mask_key():
    assert mask_key(None) == {"has_key": False, "hint": ""}
    assert mask_key("") == {"has_key": False, "hint": ""}
    masked = mask_key("sk-secret-abcd")
    assert masked == {"has_key": True, "hint": "…abcd"}


def test_profile_crud_and_mask(store):
    profile = _tokenhub_profile(store, api_key="sk-secret-abcd")
    assert profile["api_key"] == {"has_key": True, "hint": "…abcd"}
    serialized = json.dumps(list_profiles(store, "embedding"), ensure_ascii=False)
    assert "sk-secret" not in serialized
    assert list_profiles(store, "generator") == []

    # 更新：api_key 传 None 表示保留原值
    save_profile(
        store,
        role="embedding",
        provider="tencent",
        name="腾讯TokenHub",
        config={"backend": "tokenhub", "base_url": "https://x/v1", "model": "kinfra-vl-embedding-2b", "dimensions": 2048},
        api_key=None,
        profile_id=profile["id"],
    )
    row = store.one("SELECT api_key FROM model_profiles WHERE id=?", (profile["id"],))
    assert row["api_key"] == "sk-secret-abcd"

    # CLEAR_KEY 显式清除
    save_profile(
        store,
        role="embedding",
        provider="tencent",
        name="腾讯TokenHub",
        config={"backend": "tokenhub", "base_url": "https://x/v1", "model": "kinfra-vl-embedding-2b", "dimensions": 2048},
        api_key=CLEAR_KEY,
        profile_id=profile["id"],
    )
    row = store.one("SELECT api_key FROM model_profiles WHERE id=?", (profile["id"],))
    assert row["api_key"] is None

    delete_profile(store, profile["id"])
    assert list_profiles(store, "embedding") == []


def test_profile_unique_name_per_role(store):
    _tokenhub_profile(store)
    with pytest.raises(ValueError, match="同名"):
        _tokenhub_profile(store)


def test_role_and_provider_validation(store):
    with pytest.raises(ValueError, match="角色"):
        save_profile(store, role="detector", provider="custom", name="x", config={})
    with pytest.raises(ValueError, match="厂商"):
        save_profile(store, role="asr", provider="openai", name="x", config={})


def test_profile_fields_whitelist(store):
    with pytest.raises(ValueError, match="data_dir"):
        save_profile(store, role="embedding", provider="custom", name="x", config={"data_dir": "/tmp"})
    with pytest.raises(ValueError, match="aligner_model"):
        save_profile(store, role="asr", provider="custom", name="x", config={"aligner_model": "other"})
    with pytest.raises(ValueError):
        save_profile(store, role="embedding", provider="custom", name="x", config={"dimensions": -1})


def test_apply_overrides_replaces_section_and_keeps_base(store, config):
    before = config.embedding.model_dump()
    profile = _tokenhub_profile(store, api_key="key12345678")
    activate_profile(store, profile["id"])
    merged = apply_overrides(config, store)
    assert merged.embedding.backend == "tokenhub"
    assert merged.embedding.model == "kinfra-vl-embedding-8b"
    assert merged.embedding.dimensions == 4096
    assert merged.embedding.api_key == "key12345678"
    assert merged.embedding.headers()["Authorization"] == "Bearer key12345678"
    # 原配置对象不被修改
    assert config.embedding.model_dump() == before
    assert config.embedding.api_key == ""
    # quantization 等未覆盖字段承袭原值
    assert merged.embedding.quantization == config.embedding.quantization


def test_delete_active_profile_rejected(store):
    profile = save_profile(
        store,
        role="tts",
        provider="minimax",
        name="Minimax TTS",
        config={"backend": "minimax", "base_url": "https://api.minimax.cn/v1", "model": "speech-2.8-hd"},
    )
    activate_profile(store, profile["id"])
    with pytest.raises(ValueError, match="正在使用"):
        delete_profile(store, profile["id"])


def test_activate_unknown_profile_rejected(store):
    with pytest.raises(ValueError):
        activate_profile(store, 9999)


def test_dangling_active_pointer_falls_back(store, config):
    profile = save_profile(
        store,
        role="asr",
        provider="aliyun",
        name="qwen3-asr",
        config={"backend": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3-asr-flash"},
    )
    activate_profile(store, profile["id"])
    store.execute("DELETE FROM model_profiles WHERE id=?", (profile["id"],))
    merged = apply_overrides(config, store)
    assert merged.asr.backend == config.asr.backend


def test_search_and_voice_overrides(store, config):
    store.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('override.search',?)",
        (json.dumps({"weights": {"video": 2, "description": 1, "style": 1, "dialogue": 0.7, "lexical": 0.5}, "rrf_k": 40}),),
    )
    store.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('override.studio.voice',?)", ("克隆音色A",)
    )
    merged = apply_overrides(config, store)
    assert merged.search.weights["video"] == 2
    assert merged.search.rrf_k == 40
    assert merged.search.candidates == config.search.candidates
    assert merged.studio.tts.voice == "克隆音色A"


def test_search_override_requires_full_weights(store, config):
    store.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('override.search',?)",
        (json.dumps({"weights": {"video": 2}}),),
    )
    with pytest.raises(ValueError):
        apply_overrides(config, store)


def test_search_override_rejects_unknown_field(store, config):
    store.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('override.search',?)",
        (json.dumps({"candidates": 50, "max_upload_mb": 1}),),
    )
    with pytest.raises(ValueError, match="max_upload_mb"):
        apply_overrides(config, store)


def test_apply_runtime_overrides_without_db(config):
    # 无 metadata.sqlite3 时原样返回同一对象，不产生副作用
    assert not (config.data_dir / "metadata.sqlite3").exists()
    assert apply_runtime_overrides(config) is config
    Store(config.data_dir)
    assert apply_runtime_overrides(config) is config


def test_runtime_config_current_reflects_update(store, config):
    runtime = RuntimeConfig(config, store)
    assert runtime.current().studio.generator.model == config.studio.generator.model
    profile = save_profile(
        store,
        role="generator",
        provider="deepseek",
        name="deepseek-chat",
        config={
            "backend": "openai",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "llama_template": False,
        },
    )
    activate_profile(store, profile["id"])
    current = runtime.current()
    assert current.studio.generator.model == "deepseek-chat"
    assert current.studio.generator.llama_template is False
    # 未覆盖字段承袭 yaml
    assert current.studio.generator.max_tokens == config.studio.generator.max_tokens


def test_fingerprints_exclude_api_key(config):
    assert config.analysis_key
    assert config.asr_key
    assert config.studio_key
    keyed = config.model_copy(deep=True)
    keyed.analyzer.api_key = "sk-xyz"
    keyed.asr.api_key = "sk-xyz"
    keyed.studio.generator.api_key = "sk-xyz"
    keyed.studio.tts.api_key = "sk-xyz"
    assert keyed.analysis_key == config.analysis_key
    assert keyed.asr_key == config.asr_key
    assert keyed.studio_key == config.studio_key
    # 序列化同样不泄漏
    assert "sk-xyz" not in json.dumps(keyed.studio.tts.model_dump(), ensure_ascii=False)


def test_provider_presets_validate():
    """预设里的 config 必须能通过对应配置段的 pydantic 校验，防止手滑。"""
    from mindex.runtime_settings import SECTION_TYPES

    for roles in PROVIDER_PRESETS.values():
        for role, preset in roles.items():
            assert set(preset["config"]) <= PROFILE_FIELDS[role], f"{role} 预设越界"
            SECTION_TYPES[role].model_validate({**SECTION_TYPES[role]().model_dump(), **preset["config"]})


def test_minimax_presets_use_cn_domain():
    """MiniMax 三个角色预设统一国内站域名；密钥与域名错站只会得到难排查的 4xx。"""
    for role, preset in PROVIDER_PRESETS["minimax"].items():
        assert preset["config"]["base_url"] == "https://api.minimax.cn/v1", f"{role} 预设域名未统一"


def test_roles_cover_all_sections():
    assert set(ROLES) == {"analyzer", "generator", "embedding", "asr", "tts", "person"}


def test_voices_table_roundtrip(store):
    """M5 音色库的表结构在 M1 就建好：基础读写冒烟。"""
    store.execute(
        "INSERT INTO voices(name,kind,provider,voice_id) VALUES(?,?,?,?)",
        ("旁白A", "clone", "minimax", "narrator-a"),
    )
    rows = store.rows("SELECT name,kind,provider,voice_id,status FROM voices")
    assert rows == [{"name": "旁白A", "kind": "clone", "provider": "minimax", "voice_id": "narrator-a", "status": "ready"}]
