#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动 AI 同事平台（控制台 + 执行 daemon），均以 DETACHED 方式拉起，
使其脱离本脚本 / 父 shell 独立存活（会话回收后仍能自愈，只要看门狗在跑）。

用法：python launch_platform.py
"""
import os
import sys
import time
import socket
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "task_registry.db")
PORT = 8099
HOST = "127.0.0.1"
BUNDLED_PY = r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
PY = BUNDLED_PY if os.path.exists(BUNDLED_PY) else sys.executable


def tcp_alive(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch(module_args, log_name):
    log_path = os.path.join(ROOT, "data", log_name)
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    with open(log_path, "a", encoding="utf-8") as lf:
        subprocess.Popen(
            [PY] + module_args,
            cwd=ROOT, env=env,
            stdout=lf, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True,
        )


def main():
    # 1) 控制台
    if not tcp_alive(HOST, PORT):
        print("[launch] 启动控制台 console_web --port %d" % PORT)
        launch(["console_web.py", "--port", str(PORT), "--db-path", DB],
               "console_8099.log")
        for _ in range(15):
            time.sleep(1)
            if tcp_alive(HOST, PORT):
                break
    else:
        print("[launch] 控制台已在 %s:%d 监听" % (HOST, PORT))

    # 2) 执行 daemon（独立于控制台，避免控制台重启时任务卡住）
    print("[launch] 启动执行 daemon")
    launch(["-m", "execution.daemon", "--db-path", DB], "daemon.log")
    time.sleep(2)
    print("[launch] 完成。控制台=%s:%d，daemon 已拉起（日志 data/console_8099.log / data/daemon.log）" % (HOST, PORT))


if __name__ == "__main__":
    main()
