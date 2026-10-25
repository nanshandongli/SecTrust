"""浏览器技能回归测试（2026-10-25 执行层建设时新增）。

锁定承诺：
1. **WebSocket 线上格式**：客户端帧必须掩码、分片可重组、ping 自动 pong、
   64 位长度双向正确——用**独立实现的环回服务器**验证（不自己验证自己）；
2. CDP 会话：call() 按 id 配对响应、事件可等、error 响应抛 CdpError；
3. BrowserSkill CDP 全流程（goto/click/type/screenshot/wait_for）命令序列与
   坐标计算正确；CDP 起不来 → 诚实降级批处理，且批处理模式交互动作明确报错；
4. find_edge / strip_tags / 启动参数等辅助行为；
5. 资源纪律：close() 幂等、进程真终止、socket 真关（-W error::ResourceWarning）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution.browser as browser                              # noqa: E402
from execution.browser import (BrowserError, BrowserSkill,        # noqa: E402
                               CdpError, CdpSession, MinimalWebSocket,
                               WebSocketError, find_edge, strip_tags)


# ================= WebSocket 环回互操作 =================

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _LoopbackServer:
    """测试用最小 WS 服务器——独立于客户端代码手工实现帧解析，

    专门验证 MinimalWebSocket 的线上格式（掩码/分片/64 位长度）。
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received_texts = []
        self.masked_flags = []
        self.error = None
        self._sbuf = b""
        self._stop = threading.Event()
        self._script = []                # 待发送的服务器帧序列
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    # ---- 服务器侧帧解析（独立实现）----
    def _read_exact(self, conn, n):
        # 注意：recv 可能一次返回整帧，多余字节必须留在缓冲区——
        # （本测试服务器第一版就在这里丢字节，导致永远读不到客户端帧）
        while len(self._sbuf) < n:
            chunk = conn.recv(65536)
            if not chunk:
                raise ConnectionError("客户端关闭")
            self._sbuf += chunk
        out, self._sbuf = self._sbuf[:n], self._sbuf[n:]
        return out

    def _read_frame(self, conn):
        b0, b1 = self._read_exact(conn, 2)
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        masked, length = bool(b1 & 0x80), b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(conn, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(conn, 8))[0]
        mask = b""
        if masked:
            mask = self._read_exact(conn, 4)
            data = bytes(b ^ mask[i % 4]
                         for i, b in enumerate(self._read_exact(conn, length)))
        else:
            data = self._read_exact(conn, length)
        return fin, opcode, masked, data

    def _send_frame(self, conn, opcode, payload, fin=True):
        header = bytearray()
        header.append((0x80 if fin else 0) | opcode)
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        conn.sendall(bytes(header) + payload)      # 服务器帧不掩码（合法）

    def _serve(self):
        self._sbuf = b""
        try:
            conn, _ = self.sock.accept()
            with conn:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    buf += conn.recv(4096)
                req = buf.decode("ascii", "replace")
                key = ""
                for line in req.split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall((
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("ascii"))
                # 先按脚本发服务器帧
                for opcode, payload, fin in self._script:
                    self._send_frame(conn, opcode, payload, fin=fin)
                while not self._stop.is_set():
                    conn.settimeout(0.2)
                    try:
                        fin, opcode, masked, data = self._read_frame(conn)
                    except socket.timeout:
                        continue
                    except (ConnectionError, OSError):
                        return
                    self.masked_flags.append(masked)
                    if opcode == 0x8:
                        return
                    if opcode == 0x9:              # ping → pong
                        self._send_frame(conn, 0x0A, data)
                        continue
                    if opcode == 0xA:              # 客户端 pong（回应我们的 ping）
                        self.pong_payload = data
                        continue
                    self.received_texts.append(data.decode("utf-8", "replace"))
        except Exception as e:                     # pragma: no cover - 诊断用
            self.error = e

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


class WebSocketWireTest(unittest.TestCase):
    """线上格式互操作。"""

    def setUp(self):
        self.servers = []

    def _server(self, script=()):
        srv = _LoopbackServer()
        srv._script = list(script)
        self.servers.append(srv)
        self.addCleanup(srv.close)
        return srv

    def _connect(self, srv):
        ws = MinimalWebSocket.connect(
            "ws://127.0.0.1:%d/devtools/page/ABC" % srv.port, timeout=5.0)
        self.addCleanup(ws.close)
        return ws

    def test_handshake_and_single_frame(self):
        srv = self._server(script=[(0x1, b"hello-from-server", True)])
        ws = self._connect(srv)
        self.assertEqual(ws.recv_text(), "hello-from-server")
        ws.send_text("hi-from-client")
        for _ in range(50):
            if srv.received_texts:
                break
            time.sleep(0.05)
        self.assertEqual(srv.received_texts, ["hi-from-client"])
        self.assertEqual(srv.masked_flags, [True])     # 客户端帧必须掩码

    def test_fragmented_message_reassembled(self):
        srv = self._server(script=[(0x1, b"frag-", False),
                                   (0x0, b"ab", True)])
        ws = self._connect(srv)
        self.assertEqual(ws.recv_text(), "frag-ab")

    def test_server_ping_gets_pong(self):
        srv = self._server(script=[(0x9, b"ping-payload", True),
                                   (0x1, b"after-ping", True)])
        ws = self._connect(srv)
        self.assertEqual(ws.recv_text(), "after-ping")
        for _ in range(50):
            if getattr(srv, "pong_payload", None) is not None:
                break
            time.sleep(0.05)
        self.assertEqual(getattr(srv, "pong_payload", None), b"ping-payload")

    def test_large_payload_both_directions(self):
        big = b"x" * 100000                       # 触发 64 位长度路径
        srv = self._server(script=[(0x1, big, True)])
        ws = self._connect(srv)
        got = ws.recv_text()
        self.assertEqual(len(got), 100000)
        ws.send_text("y" * 100000)
        for _ in range(100):
            if any(len(t) == 100000 for t in srv.received_texts):
                break
            time.sleep(0.05)
        self.assertEqual(srv.received_texts[-1], "y" * 100000)

    def test_connect_rejects_non_ws_url(self):
        with self.assertRaises(WebSocketError):
            MinimalWebSocket.connect("http://127.0.0.1:1/x", timeout=1.0)

    def test_handshake_rejection_raises(self):
        # 起一个普通 TCP 服务器直接关连接 → 握手失败要明确报错
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]

        def _rude():
            conn, _ = s.accept()
            conn.recv(4096)
            conn.close()

        threading.Thread(target=_rude, daemon=True).start()
        self.addCleanup(s.close)
        with self.assertRaises(WebSocketError):
            MinimalWebSocket.connect("ws://127.0.0.1:%d/x" % port, timeout=3.0)


# ================= CDP 会话 =================

class _FakeWS:
    """按 handler 应答的假 WebSocket（记录发送的命令序列）。"""

    def __init__(self, handler=None):
        self.sent = []
        self.handler = handler or (lambda msg: {"echo": msg.get("method")})
        self.closed = False

    def send_text(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if "id" in msg:
            result = self.handler(msg)
            if result is not None:
                self._deliver({"id": msg["id"], "result": result})

    def _deliver(self, obj):
        # 直接进 CdpSession 的收件箱（模拟对端响应）
        self._on_message(json.dumps(obj))

    _on_message = None                     # 由 CdpSession 注入

    def recv_text(self):                   # pragma: no cover - 不经此路
        raise WebSocketError("FakeWS 不驱动 reader")

    def close(self):
        self.closed = True


class CdpSessionTest(unittest.TestCase):

    def _session(self, handler=None):
        ws = _FakeWS(handler=handler)
        s = CdpSession(ws)
        # 手动代替 reader：把响应直接投递
        ws._on_message = lambda text: s._inbox.put(json.loads(text))
        s.start_reader = lambda: None       # 不起真线程
        return s, ws

    def test_call_matches_by_id(self):
        s, ws = self._session()
        r = s.call("Page.enable", timeout=2)
        self.assertEqual(r, {"echo": "Page.enable"})
        self.assertEqual(ws.sent[0]["method"], "Page.enable")

    def test_error_response_raises(self):
        ws = _FakeWS(handler=lambda msg: None)
        s = CdpSession(ws)

        def send(text):
            msg = json.loads(text)
            ws.sent.append(msg)
            s._inbox.put({"id": msg["id"], "error": {"message": "boom"}})

        ws.send_text = send
        with self.assertRaises(CdpError):
            s.call("Runtime.evaluate", timeout=2)

    def test_wait_event_sees_accumulated(self):
        s, ws = self._session()
        s.events.append({"method": "Page.loadEventFired", "params": {}})
        ev = s.wait_event("Page.loadEventFired", timeout=2)
        self.assertEqual(ev["method"], "Page.loadEventFired")


# ================= BrowserSkill（全 fake） =================

class _FakeProc:
    def __init__(self, exit_code=None):
        self._code = exit_code
        self.terminated = False
        self.killed = False

    @property
    def returncode(self):
        return self._code

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        self._code = 0

    def wait(self, timeout=None):
        self._code = 0
        return 0

    def kill(self):
        self.killed = True
        self._code = 0


def _fake_http(version_ok=True):
    def _get(url, timeout=5.0):
        if url.endswith("/json/version"):
            if not version_ok:
                raise OSError("connection refused")
            return {"Browser": "Edge/Test", "webSocketDebuggerUrl": "ws://x"}
        if url.endswith("/json/list"):
            return [{"type": "page", "id": "P1",
                     "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/P1"}]
        raise OSError("unexpected url %s" % url)
    return _get


class BrowserSkillCdpTest(unittest.TestCase):
    """CDP 全流程：命令序列 + 坐标计算 + 截图落盘。"""

    def _skill(self, handler):
        proc = _FakeProc()

        def launcher(edge, port, headless, profile):
            return proc

        sk = BrowserSkill(edge_path="/fake/msedge.exe", timeout=0.4,
                          launcher=launcher,
                          ws_factory=lambda url: _CdpFakeWS(handler),
                          http_get=_fake_http())
        self.addCleanup(sk.close)
        return sk, proc

    def test_start_and_goto_sequence(self):
        seen = []

        def handler(msg):
            seen.append(msg["method"])
            return {}

        sk, proc = self._skill(handler)
        self.assertEqual(sk.start(), "cdp")
        sk.goto("https://example.com")
        methods = list(seen)
        self.assertEqual(methods[0], "Page.enable")
        self.assertIn("Page.navigate", methods)

    def test_click_computes_center_and_dispatches(self):
        calls = []

        def handler(msg):
            if msg["method"] == "Runtime.evaluate":
                return {"result": {"value": {"x": 101.0, "y": 52.0}}}
            calls.append(msg)
            return {}

        sk, _ = self._skill(handler)
        sk.start()
        sk.goto("https://example.com")
        res = sk.click("#btn")
        self.assertEqual(res["at"], {"x": 101.0, "y": 52.0})
        mouse = [c for c in calls if c["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual(len(mouse), 2)
        self.assertEqual([c["params"]["type"] for c in mouse],
                         ["mousePressed", "mouseReleased"])
        self.assertEqual(mouse[0]["params"]["x"], 101.0)

    def test_click_missing_element_raises(self):
        sk, _ = self._skill(lambda msg: {"result": {"value": None}})
        sk.start()
        with self.assertRaises(BrowserError):
            sk.click("#nope")

    def test_type_focuses_and_inserts(self):
        calls = []

        def handler(msg):
            if msg["method"] == "Runtime.evaluate":
                return {"result": {"value": True}}
            calls.append(msg)
            return {}

        sk, _ = self._skill(handler)
        sk.start()
        res = sk.type_text("#q", "你好")
        self.assertEqual(res["chars"], 2)
        inserts = [c for c in calls if c["method"] == "Input.insertText"]
        self.assertEqual(inserts[0]["params"]["text"], "你好")

    def test_screenshot_writes_decoded_png(self):
        png = b"\x89PNG\r\n\x1a\nFAKEBYTES"
        tmp = tempfile.mkdtemp(prefix="sectrust-bws-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "shot.png")

        def handler(msg):
            if msg["method"] == "Page.captureScreenshot":
                return {"data": base64.b64encode(png).decode("ascii")}
            return {}

        sk, _ = self._skill(handler)
        sk.start()
        res = sk.screenshot(out)
        self.assertEqual(res["mode"], "cdp")
        with open(out, "rb") as f:
            self.assertEqual(f.read(), png)

    def test_wait_for_polls_until_true(self):
        state = {"n": 0}

        def handler(msg):
            if msg["method"] == "Runtime.evaluate":
                state["n"] += 1
                return {"result": {"value": state["n"] >= 3}}
            return {}

        sk, _ = self._skill(handler)
        sk.start()
        self.assertTrue(sk.wait_for("#late", timeout=3.0))

    def test_close_kills_process_and_idempotent(self):
        sk, proc = self._skill(lambda msg: {})
        sk.start()
        sk.close()
        sk.close()                              # 幂等
        self.assertTrue(proc.terminated)


class _CdpFakeWS(_FakeWS):
    """带 reader 兼容的假 WS：send 的请求按 handler 生成响应进队列，

    CdpSession 的 reader 线程通过 recv_text() 正常消费（与真 WS 同路径）。
    """

    def __init__(self, handler=None):
        super().__init__(handler)
        self._incoming: "queue.Queue" = queue.Queue()

    def send_text(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if "id" in msg:
            result = self.handler(msg)
            if result is not None:
                self._incoming.put(json.dumps(
                    {"id": msg["id"], "result": result}))

    def recv_text(self):
        while not self.closed:
            try:
                item = self._incoming.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            return item
        raise WebSocketError("FakeWS closed")

    def push_event(self, method, params=None):
        self._incoming.put(json.dumps({"method": method, "params": params or {}}))

    def close(self):
        self.closed = True
        self._incoming.put(None)


class BrowserSkillBatchTest(unittest.TestCase):
    """CDP 不可用 → 诚实降级批处理。"""

    def _skill(self, ws_factory, proc=None):
        proc = proc or _FakeProc()

        def launcher(edge, port, headless, profile):
            return proc

        sk = BrowserSkill(edge_path="/fake/msedge.exe", timeout=0.4,
                          launcher=launcher, ws_factory=ws_factory,
                          http_get=_fake_http())
        self.addCleanup(sk.close)
        return sk

    def test_ws_failure_degrades_to_batch(self):
        def boom(url):
            raise WebSocketError("handshake refused")

        sk = self._skill(boom)
        self.assertEqual(sk.start(), "batch")
        self.assertIn("refused", sk._cdp_error)

    def test_batch_get_text_strips_tags(self):
        sk = self._skill(lambda url: (_ for _ in ()).throw(
            WebSocketError("no cdp")))
        sk.start()
        html = ("<html><body>\n<h1>标题</h1>\n<script>evil()</script>\n"
                "<p>正文</p>\n</body></html>")
        with mock.patch.object(browser, "edge_dump_dom", return_value=html):
            res = sk.get_text()
        self.assertTrue(res["degraded"])
        self.assertEqual(res["text"], "标题\n正文")     # script 内容被剔除

    def test_batch_screenshot_delegates(self):
        tmp = tempfile.mkdtemp(prefix="sectrust-bws-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "s.png")
        sk = self._skill(lambda url: (_ for _ in ()).throw(
            WebSocketError("no cdp")))
        sk.start()
        sk.goto("https://example.com")
        with mock.patch.object(browser, "edge_screenshot",
                               return_value=out) as m:
            res = sk.screenshot(out)
        self.assertEqual(res["mode"], "batch")
        self.assertEqual(m.call_args[0][1], "https://example.com")

    def test_batch_interactive_actions_refused(self):
        sk = self._skill(lambda url: (_ for _ in ()).throw(
            WebSocketError("no cdp")))
        sk.start()
        with self.assertRaises(BrowserError) as ctx:
            sk.click("#btn")
        self.assertIn("不支持", str(ctx.exception))

    def test_edge_missing_raises(self):
        with mock.patch.object(browser, "find_edge", return_value=""):
            sk = BrowserSkill(edge_path=None, timeout=0.2)
            self.addCleanup(sk.close)
            with self.assertRaises(BrowserError):
                sk.start()

    def test_edge_early_exit_degrades(self):
        proc = _FakeProc(exit_code=7)
        sk = self._skill(lambda url: _CdpFakeWS(), proc=proc)
        self.assertEqual(sk.start(), "batch")
        self.assertIn("提前退出", sk._cdp_error)


class HelperTest(unittest.TestCase):
    """辅助函数。"""

    def test_find_edge_env_override(self):
        self.assertEqual(find_edge(candidates=[__file__]), __file__)

    def test_find_edge_missing_returns_empty(self):
        self.assertEqual(find_edge(candidates=["", "/no/such/edge"]), "")

    def test_strip_tags_removes_script_and_collapses(self):
        html = "<div>  a   b  </div>\n\n<style>.x{}</style><p> c </p>"
        self.assertEqual(strip_tags(html), "a b\nc")

    def test_free_port_is_int(self):
        p = browser._free_port()
        self.assertTrue(1024 < p < 65536)

    def test_default_launcher_args(self):
        with mock.patch.object(browser.subprocess, "Popen") as popen:
            browser.BrowserSkill._default_launcher("/fake/edge", 9222, True, "/tmp/p")
        args = popen.call_args[0][0]
        self.assertEqual(args[0], "/fake/edge")
        self.assertIn("--remote-debugging-port=9222", args)
        self.assertIn("--user-data-dir=/tmp/p", args)
        self.assertIn("about:blank", args)


class CliTest(unittest.TestCase):
    """CLI：注入假技能，不碰真浏览器。"""

    def test_cli_text_and_screenshot(self):
        tmp = tempfile.mkdtemp(prefix="sectrust-bcli-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = os.path.join(tmp, "shot.png")
        with open(out, "wb") as f:
            f.write(b"png")

        class FakeSkill:
            def __init__(self, **kw):
                self._cdp_error = ""

            def start(self):
                return "cdp"

            def goto(self, url):
                self.url = url

            def get_text(self):
                return {"text": "页面文本"}

            def screenshot(self, path):
                return {"path": path}

            def close(self):
                pass

        with mock.patch.object(browser, "BrowserSkill", FakeSkill):
            rc = browser.main(["--url", "https://example.com", "--text",
                               "--screenshot", out])
        self.assertEqual(rc, 0)

    def test_cli_wait_for_failure_exit_2(self):
        class FakeSkill:
            def __init__(self, **kw):
                self._cdp_error = ""

            def start(self):
                return "cdp"

            def goto(self, url):
                pass

            def wait_for(self, selector, timeout=None):
                return False

            def close(self):
                pass

        with mock.patch.object(browser, "BrowserSkill", FakeSkill):
            rc = browser.main(["--url", "https://x", "--selector", "#s",
                               "--wait-for"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
