"""
circuit_breaker.py —— 应急处置与熔断（国标六项能力之"应急处置与熔断"）

全局紧急熔断开关：一旦触发，执行 daemon 在认领新任务前检查到"熔断开启"即
跳过认领（任务留在 registered，不被任何执行器消费），实现"出事能熔断"。

设计纪律：
  · 状态进程无关：落盘 data/circuit_breaker.json，控制台（触发/解除）与
    daemon（读取检查）共享同一份，避免内存态不同步。
  · 热路径友好：daemon 每次认领前都会查，is_open() 用 mtime 缓存，文件无变化
    不重复解析。
  · 诚实：触发/解除都留痕（who/when/why），不可静默抹除；状态只增不减地记历史。
  · 不抛异常：任何 IO 失败都按"未熔断"（fail-open）处理，避免自愈逻辑把正常
    执行也堵死——但会在 error 字段标注，便于排障。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

DEFAULT_PATH = os.path.join("data", "circuit_breaker.json")


class CircuitBreaker:
    """进程无关的全局紧急熔断开关（单文件状态 + mtime 缓存）。"""

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._cache: Optional[Dict] = None
        self._cache_mtime: float = -1.0

    # ---- 底层读写（带缓存） ----
    def _read(self) -> Dict:
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                mtime = -1.0
            if self._cache is not None and mtime == self._cache_mtime:
                return self._cache
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                # 文件不存在 / 损坏 → 视为未熔断（fail-open）
                data = self._blank()
            if not isinstance(data, dict) or "open" not in data:
                data = self._blank()
            self._cache = data
            self._cache_mtime = mtime
            return data

    def _write(self, data: Dict) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换
            self._cache = data
            try:
                self._cache_mtime = os.path.getmtime(self.path)
            except OSError:
                self._cache_mtime = time.time()

    @staticmethod
    def _blank() -> Dict:
        return {
            "open": False,
            "tripped_by": None,
            "tripped_at": None,
            "reason": None,
            "reset_by": None,
            "reset_at": None,
            "history": [],
        }

    # ---- 对外 API ----
    def is_open(self) -> bool:
        """熔断是否开启（热路径：daemon 认领前调用）。"""
        return bool(self._read().get("open"))

    def state(self) -> Dict:
        """完整状态快照（含历史）。"""
        s = dict(self._read())
        s["path"] = self.path
        return s

    def trip(self, reason: str = "", by: str = "unknown") -> Dict:
        """触发熔断。已开启则幂等，返回当前状态。

        reason 为空时给默认提示，但**必须留痕**（谁、何时、为何）。
        """
        with self._lock:
            data = self._read()
            now = time.time()
            if not data.get("open"):
                data["open"] = True
                data["tripped_by"] = by
                data["tripped_at"] = now
                data["reason"] = reason or "人工触发紧急熔断（未填写原因）"
                data["history"].append({
                    "event": "trip", "by": by, "at": now,
                    "reason": data["reason"],
                })
                data["history"] = data["history"][-50:]
                self._write(data)
            return self.state()

    def reset(self, by: str = "unknown") -> Dict:
        """解除熔断（恢复执行）。"""
        with self._lock:
            data = self._read()
            now = time.time()
            if data.get("open"):
                data["history"].append({
                    "event": "reset", "by": by, "at": now,
                    "reason": "解除熔断，恢复任务执行",
                })
                data["history"] = data["history"][-50:]
            data["open"] = False
            data["reset_by"] = by
            data["reset_at"] = now
            self._write(data)
            return self.state()

    def history(self) -> List[Dict]:
        return list(self._read().get("history", []))


# 模块级便捷实例（与控制台/daemon 共享同一文件路径）
_default = CircuitBreaker()


def is_open(path: str = DEFAULT_PATH) -> bool:
    return CircuitBreaker(path).is_open()


def trip(reason: str = "", by: str = "unknown", path: str = DEFAULT_PATH) -> Dict:
    return CircuitBreaker(path).trip(reason=reason, by=by)


def reset(by: str = "unknown", path: str = DEFAULT_PATH) -> Dict:
    return CircuitBreaker(path).reset(by=by)


def state(path: str = DEFAULT_PATH) -> Dict:
    return CircuitBreaker(path).state()
