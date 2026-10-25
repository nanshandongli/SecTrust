"""
execution/browser.py —— 可编程浏览器技能（Grok Bot "云端电脑/浏览器操作"的本地最小版）。

两种模式，诚实分级：

  **CDP 模式（交互）**：启动 Edge headless + --remote-debugging-port，用
  **手写的最小 WebSocket 客户端**（纯标准库，零依赖）说 Chrome DevTools Protocol：
  goto / get_text / click / type / screenshot / wait_for 全可用。
  WebSocket 握手、帧编解码（客户端掩码、分片重组、ping/pong、64 位长度）
  都是本文件内实现，并用"环回服务器互操作测试"验证线上格式（见 tests）。

  **批处理模式（兜底）**：CDP 起不来（旧版 Edge/端口占用/环境限制）时，降级为
  Edge 一次性 headless 命令：--screenshot 出图、--dump-dom 取 HTML 剥标签。
  只读导航可用；click/type 等交互动作明确报错，**不假装能做**。

与项目纪律的关系（诚实声明）：
- execution 包约定"命令执行走 SandboxManager、不自己起 subprocess"——
  本模块是**文档化的例外**：CDP 需要长驻进程 + 调试端口 + 生命周期自管，
  沙箱的定位是"任务命令的一次性执行"，不是这个场景。越权预检仍由
  SandboxManager 在 daemon 侧负责；浏览器进程的启停完全由本模块 close() 兜底。
- 缺 Edge → BrowserError 明说；CDP 失败 → 降级 batch 并记录原因（_cdp_error）。

运行：
    python -m execution.browser --url https://example.com --text
    python -m execution.browser --url https://example.com --screenshot out.png
    python -m execution.browser --url file:///path/page.html --selector "#btn" --click --text
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager

# ---------------- 错误类型 ----------------

class BrowserError(RuntimeError):
    """浏览器技能的错误（未找到 Edge / CDP 失败 / 批处理不支持的操作）。"""


class WebSocketError(RuntimeError):
    """WebSocket 握手/帧错误。"""


class CdpError(RuntimeError):
    """CDP 协议错误（对端返回 error 响应）。"""


# ---------------- Edge 发现 ----------------

#: Windows 常见安装位置（按优先级）。可被环境变量 SECTRUST_BROWSER_PATH 覆盖。
_EDGE_CANDIDATES = (
    os.environ.get("SECTRUST_BROWSER_PATH", ""),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\Application\msedge.exe"),
    # 非 Windows 回退：chromium 系二进制名（CDP 协议通用）
    "microsoft-edge", "microsoft-edge-stable", "chromium", "chromium-browser",
    "google-chrome", "google-chrome-stable",
)


def find_edge(candidates=None) -> str:
    """返回 Edge/Chromium 可执行路径；找不到返回空串（调用方负责报错）。"""
    for cand in (candidates if candidates is not None else _EDGE_CANDIDATES):
        if not cand:
            continue
        if os.path.isfile(cand):
            return cand
        found = shutil.which(cand)          # PATH 里的名字
        if found:
            return found
    return ""


# ---------------- 最小 WebSocket 客户端（纯标准库） ----------------

class MinimalWebSocket:
    """只说 CDP 需要的子集：文本帧、客户端掩码、分片重组、ping/pong、64 位长度。

    环回互操作测试（tests/test_browser.py）用独立实现的服务器验证线上格式。
    """

    def __init__(self, sock: socket.socket, timeout: float = 15.0):
        self.sock = sock
        self.sock.settimeout(timeout)
        self._buf = b""
        self._send_lock = threading.Lock()
        self._closed = False

    # ---- 握手 ----
    @classmethod
    def connect(cls, url: str, timeout: float = 15.0) -> "MinimalWebSocket":
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)?$", url)
        if not m:
            raise WebSocketError("仅支持 ws://host:port/path 形式的 CDP 地址: %r" % url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3) or "/"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (path, host, port, key)
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock.sendall(req.encode("ascii"))
            # 读握手响应；**关键**：响应后可能已紧随数据帧（CDP 服务器会
            # 立即推送），多出来的字节必须保留到 _buf，不能丢。
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketError("握手阶段连接被对端关闭")
                buf += chunk
                if len(buf) > 64 * 1024 and b"\r\n\r\n" not in buf:
                    raise WebSocketError("握手响应过大")
            header, _, leftover = buf.partition(b"\r\n\r\n")
            if b" 101 " not in header.split(b"\r\n", 1)[0]:
                raise WebSocketError("WebSocket 握手被拒: %r" % header[:120])
            if b"sec-websocket-accept" not in header.lower():
                raise WebSocketError("握手响应缺少 Sec-WebSocket-Accept")
            ws = cls(sock, timeout=timeout)
            ws._buf = leftover
            return ws
        except Exception:
            sock.close()
            raise

    # ---- 帧编解码 ----
    def send_text(self, text: str):
        payload = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)                       # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            if self._closed:
                raise WebSocketError("连接已关闭，不能发送")
            self.sock.sendall(bytes(header) + masked)

    def _recv_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("连接被对端关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self) -> str:
        """接收一条完整的（可能分片的）文本消息。自动回应 ping。"""
        fragments = []
        while True:
            b0, b1 = self._recv_exactly(2)
            fin, opcode = bool(b0 & 0x80), b0 & 0x0F
            masked, length = bool(b1 & 0x80), b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exactly(8))[0]
            if masked:
                mask = self._recv_exactly(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exactly(length)))
            else:
                data = self._recv_exactly(length)
            if opcode == 0x9:                      # ping → pong
                self._send_control(0x0A, data)
                continue
            if opcode == 0x8:                      # close
                self._closed = True
                raise WebSocketError("对端关闭了 WebSocket")
            if opcode in (0x1, 0x0):
                fragments.append(data)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")
            # 二进制帧（0x2）CDP 不用，忽略

    def _send_control(self, opcode: int, data: bytes):
        header = bytearray([0x80 | opcode])
        header.append(0x80 | len(data))            # 控制帧必须掩码且 <126
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._send_lock:
            self.sock.sendall(bytes(header) + masked)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._send_control(0x8, b"")          # close 帧（尽力而为）
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------- CDP 会话 ----------------

class CdpSession:
    """JSON-RPC over WebSocket：call() 按 id 配对；事件进队列可等可取。"""

    def __init__(self, ws: MinimalWebSocket):
        self.ws = ws
        self._id = 0
        self._id_lock = threading.Lock()
        self._inbox: "queue.Queue" = queue.Queue()
        self.events: list = []
        self._reader = None

    def start_reader(self):
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while True:
            try:
                text = self.ws.recv_text()
            except Exception:
                self._inbox.put({"__closed__": True})
                return
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if "method" in msg:
                self.events.append(msg)
            self._inbox.put(msg)

    def call(self, method: str, params: dict = None, timeout: float = 15.0) -> dict:
        with self._id_lock:
            self._id += 1
            mid = self._id
        self.ws.send_text(json.dumps(
            {"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CdpError("CDP 调用超时: %s" % method)
            try:
                msg = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise CdpError("CDP 调用超时: %s" % method)
            if msg.get("__closed__"):
                raise CdpError("CDP 连接已关闭（调用 %s）" % method)
            if msg.get("id") != mid:
                continue                                # 别人的响应/事件，跳过
            if "error" in msg:
                raise CdpError("CDP %s 失败: %s"
                               % (method, msg["error"].get("message", msg["error"])))
            return msg.get("result", {})

    def wait_event(self, name: str, timeout: float = 15.0) -> dict:
        for ev in self.events:                          # 先查已攒的事件（防竞态）
            if ev.get("method") == name:
                return ev
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CdpError("等待 CDP 事件超时: %s" % name)
            try:
                msg = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise CdpError("等待 CDP 事件超时: %s" % name)
            if msg.get("__closed__"):
                raise CdpError("CDP 连接已关闭（等 %s）" % name)
            if msg.get("method") == name:
                return msg

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------- 一次性批处理（兜底） ----------------

def _tmp_profile() -> str:
    return tempfile.mkdtemp(prefix="sectrust-browser-")


def edge_screenshot(edge_path: str, url: str, out_path: str,
                    width: int = 1440, height: int = 900,
                    budget_ms: int = 15000, timeout: float = 90.0) -> str:
    """Edge 一次性 headless 截图（本模块截图技术的产品化）。"""
    profile = _tmp_profile()
    try:
        args = [edge_path, "--headless", "--disable-gpu", "--no-sandbox",
                "--hide-scrollbars", "--window-size=%d,%d" % (width, height),
                "--virtual-time-budget=%d" % budget_ms,
                "--user-data-dir=%s" % profile,
                "--screenshot=%s" % out_path, url]
        subprocess.run(args, capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise BrowserError("Edge 截图超时（%ss）" % timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if not os.path.isfile(out_path):
        raise BrowserError("Edge 截图未产出文件: %s" % out_path)
    return out_path


def edge_dump_dom(edge_path: str, url: str, timeout: float = 90.0) -> str:
    """Edge 一次性 headless 导出 DOM  HTML。"""
    profile = _tmp_profile()
    try:
        args = [edge_path, "--headless", "--disable-gpu", "--no-sandbox",
                "--user-data-dir=%s" % profile, "--dump-dom", url]
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise BrowserError("Edge dump-dom 超时（%ss）" % timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return p.stdout.decode("utf-8", "replace")


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def strip_tags(html: str) -> str:
    """批处理模式的取文本：去 script/style、剥标签、压空白。降级质量，明确标注。"""
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", text)
    lines = [ _WS_RE.sub(" ", ln).strip() for ln in text.splitlines() ]
    return "\n".join(ln for ln in lines if ln)


# ---------------- 浏览器技能 ----------------

class BrowserSkill:
    """可编程浏览器。start() 尝试 CDP，失败降级批处理（诚实分级）。

    可注入缝（测试替身）：launcher / ws_factory / http_get。
    """

    def __init__(self, edge_path: str = None, timeout: float = 15.0,
                 headless: bool = True, launcher=None, ws_factory=None,
                 http_get=None, tracer=None, task_id: str = None,
                 trace_id: str = None, cost=None):
        self.edge_path = edge_path or find_edge()
        self.timeout = timeout
        self.headless = headless
        self.mode = None
        self._proc = None
        self._profile = None
        self.cdp: CdpSession = None
        self._cdp_error = ""
        self._last_url = ""
        self._closed = False
        self._launcher = launcher or self._default_launcher
        self._ws_factory = ws_factory or (lambda u: MinimalWebSocket.connect(
            u, timeout=self.timeout))
        self._http_get = http_get or _http_get_json
        # 轨迹旁路：tracer 是鸭子类型（有 .record(trace_id, kind, ...) 即可）。
        # 未注入或记录失败都绝不影响浏览器动作本身。
        self._tracer = tracer
        self._task_id = task_id
        self._trace_id = trace_id
        # 成本旁路：cost 是鸭子类型（有 .record_tool_call(...) 即可）。
        # 未注入或记账失败都绝不影响浏览器动作本身。
        self._cost = cost

    # ---- 启动 ----
    def start(self) -> str:
        if self._closed:
            raise BrowserError("技能已关闭，不能重复启动")
        if not self.edge_path:
            raise BrowserError("未找到 Edge/Chromium 浏览器（可设 SECTRUST_BROWSER_PATH）")
        try:
            self._start_cdp()
            self.mode = "cdp"
        except Exception as e:                      # 诚实降级：批处理仍可读导航
            self._cdp_error = str(e)
            self._teardown_cdp()
            self.mode = "batch"
        return self.mode

    def _start_cdp(self):
        port = _free_port()
        self._profile = _tmp_profile()
        self._proc = self._launcher(self.edge_path, port, self.headless,
                                    self._profile)
        # 等 /json/version 就绪
        deadline = time.time() + self.timeout
        version = None
        last_err = ""
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise BrowserError("Edge 进程提前退出（code=%s）"
                                   % self._proc.returncode)
            try:
                version = self._http_get("http://127.0.0.1:%d/json/version" % port,
                                         timeout=2.0)
                break
            except Exception as e:
                last_err = str(e)
                time.sleep(0.2)
        if version is None:
            raise BrowserError("CDP 调试端口 %d 未就绪: %s" % (port, last_err))
        targets = self._http_get("http://127.0.0.1:%d/json/list" % port, timeout=5.0)
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise BrowserError("CDP 目标列表里没有 page 目标")
        ws = self._ws_factory(page["webSocketDebuggerUrl"])
        self.cdp = CdpSession(ws)
        self.cdp.start_reader()
        self.cdp.call("Page.enable", timeout=self.timeout)

    def _teardown_cdp(self):
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception:
                pass
            self.cdp = None
        self._kill_proc()

    def _kill_proc(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        # Windows 上 Edge 有子进程树；taskkill /T 尽力兜底
        if os.name == "nt" and getattr(proc, "pid", None):
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._teardown_cdp()
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 交互动作（CDP） ----
    def _require_cdp(self, action: str):
        if self.mode != "cdp":
            raise BrowserError(
                "当前为批处理模式（CDP 不可用：%s），不支持 %s。"
                "批处理只读导航/截图/取文本。" % (self._cdp_error or "未知", action))

    # ---- 轨迹旁路（best-effort，绝不挡动作本身）----
    def _trace_event(self, kind: str, summary: str, detail: dict = None,
                     content: str = None, duration: float = None):
        if self._tracer is None or not self._trace_id:
            return
        try:
            d = dict(detail) if detail else {}
            if duration is not None:
                d["duration_s"] = round(duration, 3)
            self._tracer.record(self._trace_id, kind, task_id=self._task_id,
                                actor="browser", summary=summary, detail=d,
                                content=content)
        except Exception:  # noqa: BLE001 —— 留痕失败不影响浏览器动作
            pass

    @contextmanager
    def _traced_action(self, summary: str, detail: dict = None,
                       content: str = None):
        """包住一个浏览器动作：成功/失败都留痕（失败带 error），带耗时。"""
        t0 = time.time()
        try:
            yield
        except Exception as e:  # noqa: BLE001
            d = dict(detail) if detail else {}
            d["error"] = str(e)
            self._trace_event("tool_call", summary + "（失败）", detail=d,
                              duration=time.time() - t0)
            self._cost_tool_call(summary + "（失败）", d, time.time() - t0)
            raise
        self._trace_event("tool_call", summary, detail=detail, content=content,
                          duration=time.time() - t0)
        self._cost_tool_call(summary, detail, time.time() - t0)

    def _cost_tool_call(self, summary: str, detail: dict = None,
                        duration: float = None):
        """浏览器动作 = 一次工具成本（旁路：记账失败不影响动作本身）。"""
        if self._cost is None:
            return
        try:
            d = dict(detail) if detail else {}
            if duration is not None:
                d["duration_s"] = round(duration, 3)
            self._cost.record_tool_call(
                task_id=self._task_id, trace_id=self._trace_id,
                tool=summary.split(" ", 1)[0] or "browser",
                actor="browser", detail=d)
        except Exception:  # noqa: BLE001 —— 记账失败不影响浏览器动作
            pass

    def goto(self, url: str):
        self._last_url = url
        detail = {"url": url}
        with self._traced_action(f"goto {url}", detail):
            if self.mode != "cdp":
                detail["mode"] = "batch"
                return
            self.cdp.call("Page.navigate", {"url": url}, timeout=self.timeout)
            try:
                self.cdp.wait_event("Page.loadEventFired", timeout=self.timeout)
            except CdpError:
                pass                                # 页面可能已提前 load 或不触发
            detail["mode"] = "cdp"
        if detail.get("mode") == "batch":
            return {"ok": True, "mode": "batch", "url": url,
                    "note": "批处理模式：URL 已记录，导航发生在后续取数时"}
        return {"ok": True, "mode": "cdp", "url": url}

    def get_text(self) -> dict:
        if self.mode != "cdp":
            return {"ok": True, "mode": "batch", "degraded": True,
                    "text": strip_tags(edge_dump_dom(self.edge_path, self._last_url))}
        res = self.cdp.call("Runtime.evaluate", {
            "expression": "document.body ? document.body.innerText : ''",
            "returnByValue": True}, timeout=self.timeout)
        return {"ok": True, "mode": "cdp", "text": res.get("result", {}).get("value", "")}

    def _element_center(self, selector: str):
        expr = (
            "(() => { const el = document.querySelector(%s);"
            " if (!el) return null;"
            " const r = el.getBoundingClientRect();"
            " return {x: r.x + r.width/2, y: r.y + r.height/2}; })()"
        ) % json.dumps(selector)
        res = self.cdp.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True},
                            timeout=self.timeout)
        return res.get("result", {}).get("value")

    def click(self, selector: str) -> dict:
        self._require_cdp("click")
        detail = {"selector": selector}
        with self._traced_action(f"click {selector}", detail):
            center = self._element_center(selector)
            if not center:
                raise BrowserError("找不到元素: %s" % selector)
            for event_type in ("mousePressed", "mouseReleased"):
                self.cdp.call("Input.dispatchMouseEvent", {
                    "type": event_type, "x": center["x"], "y": center["y"],
                    "button": "left", "clickCount": 1}, timeout=self.timeout)
            result = {"ok": True, "mode": "cdp", "selector": selector,
                      "at": {"x": round(center["x"], 1),
                             "y": round(center["y"], 1)}}
            detail["at"] = result["at"]
        return result

    def type_text(self, selector: str, text: str) -> dict:
        self._require_cdp("type")
        detail = {"selector": selector}
        with self._traced_action(f"type {selector}", detail, content=text):
            focus_expr = (
                "(() => { const el = document.querySelector(%s);"
                " if (!el) return false; el.focus(); return true; })()"
            ) % json.dumps(selector)
            res = self.cdp.call("Runtime.evaluate",
                                {"expression": focus_expr, "returnByValue": True},
                                timeout=self.timeout)
            if not res.get("result", {}).get("value"):
                raise BrowserError("找不到元素: %s" % selector)
            self.cdp.call("Input.insertText", {"text": text}, timeout=self.timeout)
            result = {"ok": True, "mode": "cdp", "selector": selector,
                      "chars": len(text)}
            detail["chars"] = len(text)
        return result

    def screenshot(self, out_path: str, width: int = 1440,
                   height: int = 900) -> dict:
        detail = {"path": out_path, "width": width, "height": height}
        with self._traced_action(f"screenshot {out_path}", detail):
            if self.mode != "cdp":
                edge_screenshot(self.edge_path, self._last_url, out_path,
                                width=width, height=height)
                detail["mode"] = "batch"
                result = {"ok": True, "mode": "batch", "path": out_path}
            else:
                res = self.cdp.call("Page.captureScreenshot",
                                    {"format": "png"}, timeout=max(self.timeout, 30.0))
                data = res.get("data", "")
                with open(out_path, "wb") as f:
                    f.write(base64.b64decode(data))
                result = {"ok": True, "mode": "cdp", "path": out_path,
                          "bytes": os.path.getsize(out_path)}
                detail["mode"] = "cdp"
                detail["bytes"] = result["bytes"]
        return result

    def wait_for(self, selector: str, timeout: float = None) -> bool:
        self._require_cdp("wait_for")
        detail = {"selector": selector,
                  "timeout": timeout if timeout is not None else self.timeout}
        with self._traced_action(f"wait_for {selector}", detail):
            deadline = time.time() + detail["timeout"]
            expr = "!!document.querySelector(%s)" % json.dumps(selector)
            found = False
            while time.time() < deadline:
                res = self.cdp.call("Runtime.evaluate",
                                    {"expression": expr, "returnByValue": True},
                                    timeout=self.timeout)
                if res.get("result", {}).get("value"):
                    found = True
                    break
                time.sleep(0.25)
            detail["found"] = found
        return found

    @staticmethod
    def _default_launcher(edge_path: str, port: int, headless: bool,
                          profile_dir: str):
        args = [edge_path]
        if headless:
            args.append("--headless=new")
        args += ["--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                 "--remote-debugging-port=%d" % port,
                 "--user-data-dir=%s" % profile_dir,
                 "about:blank"]
        return subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------------- 辅助 ----------------

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _http_get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ---------------- CLI ----------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="可编程浏览器（CDP + 批处理兜底）")
    ap.add_argument("--url", required=True, help="目标 URL（http/https/file）")
    ap.add_argument("--edge", default="", help="Edge 可执行路径（默认自动发现）")
    ap.add_argument("--selector", default="", help="CSS 选择器（click/type/wait_for）")
    ap.add_argument("--click", action="store_true", help="点击 selector 元素")
    ap.add_argument("--type", dest="type_text", default="",
                    help="向 selector 元素输入文本")
    ap.add_argument("--wait-for", action="store_true",
                    help="等待 selector 出现（超时退出码 2）")
    ap.add_argument("--text", action="store_true", help="取页面文本并打印")
    ap.add_argument("--screenshot", default="", help="截图输出路径")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args(argv)

    skill = BrowserSkill(edge_path=args.edge or None, timeout=args.timeout)
    try:
        mode = skill.start()
        print("[browser] 模式=%s%s" % (
            mode, "" if mode == "cdp" else "（CDP 不可用：%s）" % skill._cdp_error))
        skill.goto(args.url)
        if args.wait_for:
            ok = skill.wait_for(args.selector, timeout=args.timeout)
            print("[browser] wait_for(%s) -> %s" % (args.selector, ok))
            if not ok:
                return 2
        if args.click:
            print("[browser] click -> %s" % skill.click(args.selector))
        if args.type_text:
            print("[browser] type -> %s" % skill.type_text(args.selector,
                                                           args.type_text))
        if args.text:
            print(skill.get_text()["text"])
        if args.screenshot:
            print("[browser] screenshot -> %s" % skill.screenshot(args.screenshot))
        return 0
    except BrowserError as e:
        print("[browser] 失败: %s" % e, file=sys.stderr)
        return 1
    finally:
        skill.close()


if __name__ == "__main__":
    raise SystemExit(main())
