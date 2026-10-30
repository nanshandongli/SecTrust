"""控制台 daemon 自动启动（含请求路径兜底重生）的单元测试。

覆盖：
- 启动时若已有存活 daemon → 跳过，不重复派发
- 请求路径 `_ensure_daemon_alive` 的冷却逻辑：冷却期内不重复建连/拉起
- 冷却过期后能再次尝试
"""
import time
import threading

import console_web as cw


def test_ensure_daemon_skips_when_alive(monkeypatch):
    """检测到一个非 stale、非 console_web 的 runner → 不拉起。"""
    calls = []
    monkeypatch.setattr(cw.TaskRegistry, "list_runners",
                        lambda self, stale_after=90: [
                            {"runner_id": "MS-XYZ-123", "status": "alive"}])
    # 若误拉起会写文件/起进程，这里断言它根本没走到 Popen
    monkeypatch.setattr(cw.subprocess, "Popen", lambda **k: calls.append(k))

    started = cw._ensure_daemon("data/task_registry.db", ".")
    assert started is False
    assert calls == [], "已有存活 daemon 时不应 Popen 拉起"


def test_ensure_daemon_spawns_when_none(monkeypatch):
    """没有任何存活 daemon → detached 拉起。"""
    monkeypatch.setattr(cw.TaskRegistry, "list_runners",
                        lambda self, stale_after=90: [])
    spawned = {}
    class FakeProc:
        pass
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda **k: spawned.update(k) or FakeProc())

    started = cw._ensure_daemon("data/task_registry.db", ".")
    assert started is True
    assert spawned, "无存活 daemon 时应 Popen 拉起"
    # 脱离进程组：Windows 用 creationflags，POSIX 用 start_new_session
    assert ("creationflags" in spawned) or spawned.get("start_new_session") is True


def test_ensure_daemon_alive_cooldown(monkeypatch):
    """请求路径：冷却期内即使无存活 daemon 也不重复拉起（防刷 + 防并发双拉起）。"""
    monkeypatch.setattr(cw.TaskRegistry, "list_runners",
                        lambda self, stale_after=90: [])
    spawn_count = {"n": 0}
    class FakeProc:
        pass
    def fake_popen(**k):
        spawn_count["n"] += 1
        return FakeProc()
    monkeypatch.setattr(cw.subprocess, "Popen", fake_popen)

    cw._daemon_ensure_last = 0.0  # 重置冷却基准
    # 第一次：冷却到期 → 拉起
    assert cw._ensure_daemon_alive("data/task_registry.db", ".") is True
    # 紧接着第二次：冷却期内 → 跳过，不新增拉起
    assert cw._ensure_daemon_alive("data/task_registry.db", ".") is False
    assert spawn_count["n"] == 1, "冷却期内不应重复 Popen"


def test_ensure_daemon_alive_after_cooldown(monkeypatch):
    """冷却过期后，请求路径能再次尝试拉起。"""
    monkeypatch.setattr(cw.TaskRegistry, "list_runners",
                        lambda self, stale_after=90: [])
    spawn_count = {"n": 0}
    class FakeProc:
        pass
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda **k: (spawn_count.__setitem__("n", spawn_count["n"] + 1) or FakeProc()))

    cw._daemon_ensure_last = 0.0
    cw._ensure_daemon_alive("data/task_registry.db", ".")
    # 模拟冷却已过期
    cw._daemon_ensure_last = time.time() - (cw._DAEMON_ENSURE_COOLDOWN + 1)
    cw._ensure_daemon_alive("data/task_registry.db", ".")
    assert spawn_count["n"] == 2, "冷却过期后应能再次拉起"


def test_ensure_daemon_alive_concurrent_no_double_spawn(monkeypatch):
    """并发请求：锁保证只有一个拉起。"""
    monkeypatch.setattr(cw.TaskRegistry, "list_runners",
                        lambda self, stale_after=90: [])
    spawn_count = {"n": 0}
    ready = threading.Event()
    class FakeProc:
        pass
    def fake_popen(**k):
        # 故意在持锁窗口内制造并发窗口
        ready.set()
        time.sleep(0.05)
        spawn_count["n"] += 1
        return FakeProc()
    monkeypatch.setattr(cw.subprocess, "Popen", fake_popen)

    cw._daemon_ensure_last = 0.0
    results = {}
    def worker(tag):
        results[tag] = cw._ensure_daemon_alive("data/task_registry.db", ".")
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert spawn_count["n"] == 1, "并发下只应拉起一次，避免双派发"
