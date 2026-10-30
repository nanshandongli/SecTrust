"""岗位 ↔ AI 同事分工路由的单元测试。

覆盖：绑定持久化、默认值、标签翻译、daemon 选同事的诚实回退、
以及 create_task 按绑定写入 profile_id。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from role_routing import (load_bindings, save_binding, binding_for,
                          colleague_label, DEFAULT_LABEL)


class _FakeProfile:
    def __init__(self, pid, name="", kind="chat", configured=True):
        self.id = pid
        self.name = name or pid
        self.kind = kind
        self.configured = configured


class _FakeStore:
    def __init__(self, profiles, active_id=None):
        self._profiles = {p.id: p for p in profiles}
        self._active = active_id or (profiles[0].id if profiles else None)

    def get(self, pid):
        return self._profiles.get(pid)

    def get_active(self):
        return self._profiles.get(self._active)

    def get_image(self):
        for p in self._profiles.values():
            if getattr(p, "kind", "chat") == "image" and p.configured:
                return p
        return None

    def iter_profiles(self):
        return list(self._profiles.values())


def test_defaults_all_empty():
    # 不依赖磁盘：用临时目录隔离
    d = tempfile.mkdtemp()
    os.environ["SECTRUST_DATA_DIR"] = d
    try:
        b = load_bindings()
        # 每个已知岗位默认空串（=平台默认同事）
        assert all(v == "" for v in b.values()), b
        assert binding_for("designer") == ""
        assert binding_for("nonexistent_still_empty") == ""
    finally:
        os.environ.pop("SECTRUST_DATA_DIR", None)


def test_save_and_load_roundtrip():
    d = tempfile.mkdtemp()
    os.environ["SECTRUST_DATA_DIR"] = d
    try:
        save_binding("designer", "img-colleague")
        assert binding_for("designer") == "img-colleague"
        # 重新加载（模拟进程重启）
        assert load_bindings().get("designer") == "img-colleague"
        # 解绑
        save_binding("designer", "")
        assert binding_for("designer") == ""
    finally:
        os.environ.pop("SECTRUST_DATA_DIR", None)


def test_save_rejects_unknown_role():
    d = tempfile.mkdtemp()
    os.environ["SECTRUST_DATA_DIR"] = d
    try:
        try:
            save_binding("ghost", "x")
            raise AssertionError("应拒绝未知岗位")
        except ValueError:
            pass
    finally:
        os.environ.pop("SECTRUST_DATA_DIR", None)


def test_colleague_label():
    store = _FakeStore([_FakeProfile("a", "Alice"), _FakeProfile("b", "Bob")])
    assert colleague_label(store, "a") == "Alice"
    assert colleague_label(store, "") == DEFAULT_LABEL
    assert colleague_label(store, "missing") == DEFAULT_LABEL


# --------------------------------------------------------------------------
# daemon 选同事逻辑（诚实回退）
# --------------------------------------------------------------------------
def _daemon_with(profiles):
    # 轻量构造：只用到 _select_profile，不需要完整 daemon
    from types import SimpleNamespace
    return SimpleNamespace(
        _select_profile=lambda pr, payload, op: _select_profile_impl(pr, payload, op)
    )


# 直接复用 daemon 的实现，避免构造重量级 daemon
import importlib
_daemon_mod = importlib.import_module("execution.daemon")
_select_profile_impl = _daemon_mod.ExecutionDaemon._select_profile
# 绑定为静态式调用（第一个参数 self 由 SimpleNamespace 提供，但我们直接传 profiles）
def _call_select(profiles, payload, op):
    # _select_profile 是实例方法，但只用到 self 之外两个参数；用占位 self
    class _S:
        pass
    return _select_profile_impl(_S(), profiles, payload, op)


def test_select_prefers_intended_when_configured():
    store = _FakeStore([_FakeProfile("writer", "Writer"),
                        _FakeProfile("active", "Active")])
    chosen, intended = _call_select(store, {"profile_id": "writer"}, "llm")
    assert chosen.id == "writer"
    assert intended == "writer"


def test_select_falls_back_to_active_when_intended_missing():
    store = _FakeStore([_FakeProfile("active", "Active")])
    # 绑定同事不存在 → 回退 active，intended 仍回传
    chosen, intended = _call_select(store, {"profile_id": "gone"}, "llm")
    assert chosen.id == "active"
    assert intended == "gone"


def test_select_image_prefers_image_kind_only():
    # 绑定的是 chat 同事，但任务是 image op → 不应被强制当成绘图同事
    store = _FakeStore([_FakeProfile("chat", "Chat", kind="chat"),
                        _FakeProfile("img", "Img", kind="image")])
    chosen, intended = _call_select(store, {"profile_id": "chat"}, "image")
    # 退化为图像专用回退（img），而非绑定的 chat
    assert chosen.id == "img"
    assert intended == "chat"


def test_select_image_uses_bound_image_colleague():
    store = _FakeStore([_FakeProfile("img", "Img", kind="image")])
    chosen, intended = _call_select(store, {"profile_id": "img"}, "image")
    assert chosen.id == "img"


def test_select_none_when_nothing_configured():
    store = _FakeStore([_FakeProfile("x", "X", configured=False)])
    chosen, intended = _call_select(store, {}, "llm")
    assert chosen is None


# --------------------------------------------------------------------------
# create_task 按绑定写入 profile_id
# --------------------------------------------------------------------------
def test_create_task_writes_bound_profile_id():
    d = tempfile.mkdtemp()
    os.environ["SECTRUST_DATA_DIR"] = d
    try:
        from console_api import create_task

        class _FakeReg:
            def __init__(self):
                self.last = None
            def register_task(self, tid, payload, depends_on=None):
                self.last = payload
            def list_tasks(self, limit=10000):
                return []

        save_binding("designer", "img-colleague")
        reg = _FakeReg()
        r = create_task(reg, None, "画一张海报", "designer", priority="normal")
        assert r["ok"] is True
        assert r["payload"]["profile_id"] == "img-colleague"
        assert r["payload"]["intended_colleague"] == "img-colleague"

        # 未绑定岗位：不应写 profile_id，intended 为 None
        reg2 = _FakeReg()
        r2 = create_task(reg2, None, "写一份报告", "writer", priority="normal")
        assert "profile_id" not in r2["payload"]
        assert r2["payload"]["intended_colleague"] is None
    finally:
        os.environ.pop("SECTRUST_DATA_DIR", None)
