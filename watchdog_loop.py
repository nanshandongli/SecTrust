#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SecTrust 控制台自愈看门狗（常驻循环版）。

每 CHECK_INTERVAL 秒调用一次 ensure_console 的探活/拉起逻辑：
- 服务活着 → 什么都不做；
- 服务挂了 → 以 DETACHED 方式拉起 console_web.py，使其独立存活。

这样服务在会话内崩溃可自愈。跨会话的持久化请用 Windows 计划任务
（见 ensure_console.bat + 注册命令），本循环进程本身也需被会话/计划任务保活。
"""
import os
import time
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
CHECK_INTERVAL = 30

_spec = importlib.util.spec_from_file_location(
    "ensure_console", os.path.join(ROOT, "ensure_console.py")
)
_ensure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ensure)


def main():
    while True:
        try:
            _ensure.main()
        except Exception as e:  # noqa: BLE001 - 看门狗自身不能因异常退出
            try:
                _ensure.log.error("watchdog loop exception: %s", e)
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
