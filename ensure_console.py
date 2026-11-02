#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SecTrust 控制台自愈看门狗入口（幂等，可反复安全调用）。

职责：
1. 探活 127.0.0.1:8099 —— 先 TCP 连通，再深度检查 /api/chat/status 的 rule_engine 是否在线。
2. 若已存活且规则引擎在线，直接退出，绝不重复拉起（避免多个实例抢端口）。
3. 若未存活，则以「脱离父进程（DETACHED）」方式拉起 console_web.py --port 8099，
   使其在被调度器 / 会话回收后仍能独立存活。
4. 全程写日志到 data/ensure_watchdog.log，便于排查。

典型用法：被 Windows 计划任务每 5 分钟调用一次，实现崩溃 / 会话退出后的自愈。
也可手动执行：python ensure_console.py
"""
import os
import sys
import time
import socket
import subprocess
import json
import logging
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8099
HOST = "127.0.0.1"
LOG_DIR = os.path.join(ROOT, "data")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "ensure_watchdog.log"), encoding="utf-8")],
)
log = logging.getLogger("watchdog")

# 优先使用项目锁定的 Python 解释器，回退到当前解释器。
BUNDLED_PY = r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"


def pick_python():
    if os.path.exists(BUNDLED_PY):
        return BUNDLED_PY
    return sys.executable


def tcp_alive(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def deep_healthy(host, port, timeout=5):
    """深度探活：HTTP 200 且 rule_engine 在线。强制绕过系统代理（127.0.0.1 不应走代理）。"""
    url = "http://%s:%d/api/chat/status" % (host, port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            if r.status != 200:
                return False
            try:
                data = json.loads(r.read().decode("utf-8", "replace"))
            except (ValueError, UnicodeError):
                return False
            return data.get("rule_engine") is True
    except (urllib.error.URLError, OSError, socket.timeout):
        return False


def start_server(py):
    log_path = os.path.join(LOG_DIR, "console_8099.log")
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    args = [py, "console_web.py", "--port", str(PORT)]
    # DETACHED + 新进程组：让子进程不随本看门狗（或被调度器回收的父 shell）一起死。
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    with open(log_path, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    return proc.pid


def main():
    if tcp_alive(HOST, PORT) and deep_healthy(HOST, PORT):
        log.info("healthy: console already listening on %s:%d (rule_engine online); nothing to do", HOST, PORT)
        return 0
    log.warning("unhealthy: console not reachable on %s:%d; attempting restart", HOST, PORT)
    py = pick_python()
    try:
        pid = start_server(py)
    except Exception as e:  # noqa: BLE001 - 看门狗需兜底记录任何拉起失败
        log.error("failed to start console: %s", e)
        return 2
    log.info("launched console_web pid=%s; waiting for bind...", pid)
    for _ in range(10):
        time.sleep(1)
        if tcp_alive(HOST, PORT):
            break
    if tcp_alive(HOST, PORT) and deep_healthy(HOST, PORT):
        log.info("restart ok: console healthy again")
        return 0
    log.error("restart attempted but console still not healthy; scheduler will retry next cycle")
    return 3


if __name__ == "__main__":
    sys.exit(main())
