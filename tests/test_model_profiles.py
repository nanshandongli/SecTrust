"""
测试 model_profiles.py —— 可持久化的算力模型配置。
覆盖：默认 seed、增删改查、active 唯一性、key 掩码、磁盘持久化、provider 校验。
"""
import json
import os
import tempfile

import pytest

from model_profiles import ALLOWED_PROVIDERS, ModelProfileStore


@pytest.fixture
def tmp_store():
    d = tempfile.mkdtemp()
    s = ModelProfileStore(data_dir=d)
    return s, d


def test_seed_creates_local_ollama(tmp_store):
    s, _ = tmp_store
    ids = {p.id for p in s.iter_profiles()}
    assert "ollama-local" in ids
    # 有且仅有一个 active
    assert sum(1 for p in s.iter_profiles() if p.active) == 1
    # 本地 Ollama 无需 key 即 configured
    assert s.get("ollama-local").configured is True


def test_list_public_masks_api_key(tmp_store):
    s, _ = tmp_store
    s.upsert({"provider": "openai", "name": "我的云端",
               "base_url": "https://api.example.com/v1",
               "api_key": "sk-secret-1234", "model": "gpt-4o-mini"})
    pub = s.list_public()
    op = next(p for p in pub["profiles"] if p["provider"] == "openai")
    assert op["api_key_masked"] == "****1234"
    assert "api_key" not in op          # 明文 key 不在返回里
    assert op["configured"] is True


def test_api_key_persisted_plaintext_on_disk(tmp_store):
    s, d = tmp_store
    s.upsert({"provider": "openai", "name": "x",
               "api_key": "sk-secret-9999", "model": "gpt-4o-mini"})
    # 磁盘上是明文（data/ 已被 .gitignore 排除，绝不进仓库）
    raw = json.load(open(os.path.join(d, "model_profiles.json"), encoding="utf-8"))
    op = next(p for p in raw["profiles"] if p["provider"] == "openai")
    assert op["api_key"] == "sk-secret-9999"


def test_masked_key_roundtrip_keeps_original(tmp_store):
    s, _ = tmp_store
    s.upsert({"id": "x", "provider": "openai", "name": "x",
               "api_key": "sk-secret-keep", "model": "gpt-4o-mini"})
    # 再次 upsert 同一 id，key 传掩码 -> 保留磁盘原值，不覆盖成掩码
    s.upsert({"id": "x", "provider": "openai", "name": "x2",
               "api_key": "****keep", "model": "gpt-4o-mini"})
    assert s.get("x").api_key == "sk-secret-keep"
    assert s.get("x").name == "x2"


def test_set_active_unique(tmp_store):
    s, _ = tmp_store
    s.upsert({"provider": "openai", "name": "云端", "model": "gpt-4o-mini"})
    s.set_active("云端") if False else None  # 占位，下面用 id
    # 找到 openai profile 的 id
    oid = next(p.id for p in s.iter_profiles() if p.provider == "openai")
    s.set_active(oid)
    assert s.get_active_id() == oid
    assert all((not p.active) for p in s.iter_profiles() if p.id != oid)


def test_delete_drops_profile_and_reassigns_active(tmp_store):
    s, _ = tmp_store
    s.upsert({"provider": "openai", "name": "云端", "model": "gpt-4o-mini"})
    oid = next(p.id for p in s.iter_profiles() if p.provider == "openai")
    active_before = s.get_active_id()
    # 若删的是 active，应自动把另一个设为 active
    was_active = (oid == active_before)
    assert s.delete(oid) is True
    assert s.get(oid) is None
    if was_active:
        assert s.get_active_id() is not None


def test_invalid_provider_rejected(tmp_store):
    s, _ = tmp_store
    with pytest.raises(ValueError):
        s.upsert({"provider": "bogus", "name": "x"})


def test_load_from_disk_restores_state(tmp_store):
    s, d = tmp_store
    s.upsert({"provider": "openai", "name": "云端", "model": "gpt-4o-mini"})
    # 新实例从同一磁盘文件加载
    s2 = ModelProfileStore(data_dir=d)
    assert any(p.provider == "openai" and p.model == "gpt-4o-mini"
               for p in s2.iter_profiles())
    assert s2.list_public()["active_id"] == s.get_active_id()


def test_allowed_providers_cover_scope():
    assert set(ALLOWED_PROVIDERS) == {"ollama", "openai", "anthropic"}
