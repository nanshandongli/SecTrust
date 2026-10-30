"""
breaker.py —— per-server TTL 熔断（进程无关，落盘 data/mcp_breaker.json）。

与 governance.circuit_breaker 的区别（别混用）：
  · governance 的是**全局紧急熔断**（人手动 trip/reset，fail-open：读不到状态
    视为未熔断，避免自愈逻辑把正常执行堵死）；
  · 本模块是**对端故障熔断**（连续失败 threshold 次自动打开，TTL 到期自动
    半开探测）。它只针对单个外部 MCP Server，不影响平台其它任务。

语义：
  closed  → 正常放行；
  open    → 窗口内直接拒绝（默认不信任反复失败的外部依赖）；
  half-open → TTL 到期后放行**一次**探测，成功则清零，失败则立刻重新打开。

状态每次都重新读盘（无内存缓存），因此控制台进程与 daemon 进程看到同一份；
写用临时文件 + os.replace 原子替换。读不到/损坏 → 视为无失败记录（closed），
但绝不把"打开过的熔断"静默抹掉：损坏文件会被保留为 .corrupt 备份供排查。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from typing import Dict, Tuple

DEFAULT_PATH = os.path.join("data", "mcp_breaker.json")


class TTLCircuitBreaker:
    def __init__(self, path: str = DEFAULT_PATH, threshold: int = 3,
                 ttl: float = 30.0):
        self.path = path
        self.threshold = max(1, int(threshold))
        self.ttl = float(ttl)
        self._lock = threading.RLock()

    # ---- 底层读写 ----
    def _load(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            # 损坏文件不静默覆盖：备份后当空状态，便于人工排查
            try:
                shutil.copyfile(self.path, self.path + ".corrupt")
            except OSError:
                pass
            raise RuntimeError("MCP 熔断状态文件损坏（已备份为 .corrupt）：%s" % e)
        if not isinstance(data, dict):
            raise RuntimeError("MCP 熔断状态文件根节点必须是对象")
        return data

    def _save(self, data: Dict) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 读 ----
    def state(self, server: str) -> Dict:
        rec = self._load().get(server) or {}
        until = float(rec.get("open_until") or 0)
        return {
            "server": server,
            "failures": int(rec.get("failures") or 0),
            "open_until": until,
            "open": until > time.time(),
            "half_open": bool(rec.get("half_open")),
            "last_error": rec.get("last_error"),
            "last_at": rec.get("last_at"),
            "threshold": self.threshold,
            "ttl": self.ttl,
        }

    def snapshot(self) -> Dict:
        return {s: self.state(s) for s in self._load()}

    # ---- 门：是否放行 ----
    def allow(self, server: str) -> Tuple[bool, str, Dict]:
        """返回 (放行?, 原因, 状态快照)。"""
        with self._lock:
            st = self.state(server)
            now = time.time()
            if st["open"]:
                return (False,
                        "TTL 熔断开启：连续失败 %d 次，剩余 %.1fs 后自动半开"
                        % (st["failures"], st["open_until"] - now), st)
            if st["open_until"] and st["open_until"] <= now:
                # TTL 到期 → 半开：只放行一次探测，失败立刻重开
                data = self._load()
                rec = dict(data.get(server) or {})
                rec["open_until"] = 0
                rec["half_open"] = True
                rec["failures"] = self.threshold  # 探测失败即再次触发阈值
                data[server] = rec
                self._save(data)
                st = self.state(server)
                return True, "half_open", st
            return True, "closed", st

    # ---- 写：成败计数 ----
    def record_success(self, server: str) -> Dict:
        with self._lock:
            data = self._load()
            data.pop(server, None)
            self._save(data)
            return self.state(server)

    def record_failure(self, server: str, reason: str = "") -> Dict:
        with self._lock:
            data = self._load()
            rec = dict(data.get(server) or {})
            n = int(rec.get("failures") or 0) + 1
            rec["failures"] = n
            rec["last_error"] = str(reason)[:300]
            rec["last_at"] = time.time()
            rec["half_open"] = False
            if n >= self.threshold:
                rec["open_until"] = time.time() + self.ttl
            data[server] = rec
            self._save(data)
            return self.state(server)

    def reset(self, server: str) -> Dict:
        with self._lock:
            data = self._load()
            data.pop(server, None)
            self._save(data)
            return self.state(server)
