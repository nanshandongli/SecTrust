"""
transport.py —— MCP 双传输：stdio（子进程）与 HTTP（POST + 可选 SSE）。

两种传输对上层暴露同一个 `request(method, params, timeout) -> result` 契约，
因此安全代理层（proxy.py）不必关心对端是本地子进程还是远程 HTTP 服务。

设计要点：
  · **零依赖**：只用标准库 subprocess / urllib / queue / threading。
  · **stdio 超时跨平台**：Windows 的 select 不能作用于管道，故用后台线程把
    stdout 行泵进队列，主线程 `queue.get(timeout=...)` 实现可超时读取；
    stderr 另起线程读走，避免管道写满把子进程憋死。
  · **串行调用**：一条连接上一个 request 的响应没回来前不发下一个（lock 包住
    写+等），避免 id 串扰；通知（notification）也走同一把写锁。
  · **诚实标注 via**：`via` 明确区分 stdio 子进程 / loopback HTTP / 远程 HTTP，
    绝不把本地 mock 说成"真跨网"。
  · 凭证不写进 params：stdio 走 env 注入，HTTP 走 header 注入，由 proxy 层负责。
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .protocol import (make_notification, make_request, parse_message,
                       is_response_to, result_of, RpcError)


class TransportError(RuntimeError):
    """传输层故障（进程退出 / 超时 / 网络错误），区别于对端返回的 RpcError。"""


def _is_loopback(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def _first_json_frame(raw: str) -> str:
    """从 HTTP 响应体提取 JSON-RPC 帧：纯 JSON 或 SSE 的 `data:` 行。"""
    s = (raw or "").strip()
    if not s:
        raise RpcError(-32700, "HTTP 响应体为空")
    if s.startswith("{"):
        return s
    for line in s.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return payload
    raise RpcError(-32700, "无法从 HTTP 响应体中提取 JSON-RPC 帧（既非 JSON 也非 SSE）")


class Transport(ABC):
    """传输抽象：请求-响应 + 单向通知。"""

    @property
    @abstractmethod
    def via(self) -> str:
        """传输通道的诚实标注（供审计/报告引用）。"""

    @abstractmethod
    def request(self, method: str, params: Optional[Dict] = None,
                timeout: Optional[float] = None) -> Dict:
        """发一个 JSON-RPC 请求并返回 result；对端 error → RpcError。"""

    @abstractmethod
    def notify(self, method: str, params: Optional[Dict] = None) -> None:
        """发一个通知（无 id，不等响应）。"""

    def close(self) -> None:
        """释放资源（默认无操作）。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class StdioTransport(Transport):
    """启动 MCP Server 子进程，用换行分隔的 JSON 帧通信。"""

    def __init__(self, command: List[str], env: Optional[Dict] = None,
                 cwd: Optional[str] = None, timeout: float = 15.0,
                 extra_env: Optional[Dict] = None):
        if not command:
            raise ValueError("stdio 传输需要非空 command")
        self.command = [str(c) for c in command]
        self.env = dict(os.environ if env is None else env)
        if extra_env:
            # 凭证代理注入点：只进子进程 env，绝不进模型可见的 params
            self.env.update({str(k): str(v) for k, v in extra_env.items()})
        self.cwd = cwd
        self.timeout = float(timeout)
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue[Dict]" = queue.Queue()
        self._stderr: List[str] = []
        self._ids = itertools.count(1)
        self._call_lock = threading.RLock()
        self._stderr_lock = threading.Lock()

    @property
    def via(self) -> str:
        return "stdio+json(loopback:subprocess)"

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _pump_stdout(self):
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(parse_message(line))
                except RpcError:
                    # 对端打印了非协议行（诊断日志）：忽略，不污染协议通道
                    continue
        except (OSError, ValueError):
            pass

    def _drain_stderr(self):
        try:
            for line in self._proc.stderr:  # type: ignore[union-attr]
                with self._stderr_lock:
                    self._stderr.append(line.rstrip("\r\n"))
                    del self._stderr[:-50]
        except (OSError, ValueError):
            pass

    def start(self) -> None:
        if self._alive():
            return
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, env=self.env,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as e:
            raise TransportError("无法启动 MCP Server 子进程 %r：%s"
                                 % (self.command[0], e))
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _write(self, frame: Dict) -> None:
        if not self._alive():
            raise TransportError("stdio 子进程已退出：%s" % self._stderr_tail())
        try:
            self._proc.stdin.write(  # type: ignore[union-attr]
                json.dumps(frame, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise TransportError("stdio 写入失败（子进程可能已退出）：%s" % e)

    def _stderr_tail(self) -> str:
        with self._stderr_lock:
            return " | ".join(self._stderr[-5:]) or "（stderr 无输出）"

    def request(self, method: str, params: Optional[Dict] = None,
                timeout: Optional[float] = None) -> Dict:
        self.start()
        rid = next(self._ids)
        deadline = time.monotonic() + float(timeout or self.timeout)
        with self._call_lock:
            self._write(make_request(method, params, rid))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError("stdio 请求超时（>%.1fs，method=%s）"
                                         % (float(timeout or self.timeout), method))
                try:
                    msg = self._queue.get(timeout=min(remaining, 0.25))
                except queue.Empty:
                    if not self._alive():
                        raise TransportError("等待响应期间 stdio 子进程退出：%s"
                                             % self._stderr_tail())
                    continue
                if not is_response_to(msg, rid):
                    continue  # 通知 / 其他 id 的响应，不属于本次请求
                return result_of(msg)

    def notify(self, method: str, params: Optional[Dict] = None) -> None:
        self.start()
        with self._call_lock:
            self._write(make_notification(method, params))

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass


class HttpTransport(Transport):
    """Streamable HTTP 传输：POST 一个 JSON-RPC 帧，读回一个 JSON（或 SSE）帧。

    简化点（如实标注）：不做会话保持 / 不做长轮询 SSE 流，只做"一请求一响应"；
    这足以跑通 Tools/Resources/Prompts 三原语，但不宣称支持完整 Streamable HTTP
    会话语义。opener 可注入，便于离线 mock 测试。
    """

    def __init__(self, url: str, headers: Optional[Dict] = None,
                 timeout: float = 15.0, opener=None, use_proxy: bool = False):
        if not url or not urlparse(url).scheme.startswith("http"):
            raise ValueError("HTTP 传输需要 http(s) URL")
        self.url = url
        # 凭证代理注入点：Authorization 等 header 由 proxy 层构造后传入
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        self.headers.update({str(k): str(v) for k, v in (headers or {}).items()})
        self.timeout = float(timeout)
        # 默认直连（不走系统代理）：MCP 常指向内网/loopback，而本机代理会
        # 拦截 127.0.0.1 导致假性不可达；需要走代理时显式 use_proxy=True。
        self._opener = opener or (urllib.request.build_opener(
            urllib.request.ProxyHandler({})) if not use_proxy else None)
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    @property
    def via(self) -> str:
        return ("http+json(loopback)" if _is_loopback(self.url)
                else "http+json(remote)")

    def _post(self, frame: Dict, timeout: float) -> str:
        data = json.dumps(frame, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data,
                                     headers=self.headers, method="POST")
        opener = self._opener or urllib.request
        open_fn = getattr(opener, "open", None) or urllib.request.urlopen
        try:
            with open_fn(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            raise TransportError("HTTP %s %s（method=%s）"
                                 % (e.code, e.reason, frame.get("method")))
        except (OSError, ValueError) as e:
            raise TransportError("HTTP 请求失败：%s" % e)
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)

    def request(self, method: str, params: Optional[Dict] = None,
                timeout: Optional[float] = None) -> Dict:
        rid = next(self._ids)
        with self._lock:
            raw = self._post(make_request(method, params, rid),
                             float(timeout or self.timeout))
        msg = parse_message(_first_json_frame(raw))
        if not is_response_to(msg, rid):
            raise TransportError("HTTP 响应 id 与请求不匹配（或为通知帧）")
        return result_of(msg)

    def notify(self, method: str, params: Optional[Dict] = None) -> None:
        with self._lock:
            try:
                self._post(make_notification(method, params), self.timeout)
            except TransportError:
                pass  # 通知是 fire-and-forget，失败不阻塞主链路
