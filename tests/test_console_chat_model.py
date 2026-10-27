"""
测试 console_chat 的「按 profile 选择模型」接线：
  - 规则优先的问题不依赖 LLM 即可答（engine=rule）；
  - 选中的 profile 未配置 key 时，LLM 兜底会诚实报 unavailable，而非伪造回答；
  - status() 透出 profiles 与 active_id。
"""
import tempfile

from console_chat import ChatService, LlmEngine
from model_profiles import ModelProfileStore


def _empty_ctx():
    return {"overview": {}, "tasks": [], "approvals": [], "cost": {}, "security": {}}


def _store_with_unconfigured_openai_active():
    d = tempfile.mkdtemp()
    s = ModelProfileStore(data_dir=d)
    # 清掉默认 ollama 的 active，改为一个没 key 的 openai profile
    for p in list(s.iter_profiles()):
        p.active = False
    s.upsert({"provider": "openai", "name": "未配置云端",
               "model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"})
    oid = next(p.id for p in s.iter_profiles() if p.provider == "openai")
    s.set_active(oid)
    assert s.get_active().configured is False
    return s


def test_rule_answer_does_not_need_llm():
    svc = ChatService(_empty_ctx)
    r = svc.ask("帮我总结一下当前审计状态")
    assert r["engine"] == "rule"
    assert "答案" in r or "审计" in r["answer"]


def test_unconfigured_profile_is_honest_unavailable():
    store = _store_with_unconfigured_openai_active()
    engine = LlmEngine(store)
    # 一个规则引擎答不了、需要 LLM 的问题
    try:
        engine.complete("请用一句话解释零信任架构", "facts here", model=None)
        raised = False
    except RuntimeError as e:
        raised = str(e) == "no_llm_available"
    assert raised, "未配置密钥时应当诚实报 no_llm_available，而非伪造回答"


def test_ask_with_unconfigured_model_returns_unavailable_engine():
    store = _store_with_unconfigured_openai_active()
    svc = ChatService(_empty_ctx, llm=LlmEngine(store))
    r = svc.ask("帮我写一首关于审计的诗")  # 规则引擎接不住，落到 LLM 兜底
    assert r["engine"] == "unavailable"
    assert r["model"] is None


def test_status_exposes_profiles_and_active():
    store = _store_with_unconfigured_openai_active()
    svc = ChatService(_empty_ctx, llm=LlmEngine(store))
    st = svc.status()
    assert st["rule_engine"] is True
    assert st["active_id"] == store.get_active_id()
    assert any(p["provider"] == "openai" for p in st["profiles"])
