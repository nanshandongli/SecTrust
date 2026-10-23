"""
WeComChannel —— 企业微信（官方 API）渠道适配。

入站：起一个 HTTP 回调端点，处理企业微信的「URL 验证(GET)」与「消息回调(POST)」。
出站：调用企业微信「应用消息」接口（/cgi-bin/message/send），用标准库 urllib 发 HTTPS。

⚠️ 真实联调前需要在【企业微信管理后台】配置：
  - 可信回调 URL（指向本服务 port）
  - Token、EncodingAESKey
  - 自建应用 AgentId、CorpId、Secret

安全说明：
  - 入站必须校验 msg_signature，POST 体必须 AES 解密（依赖 pycryptodome）。
  - 本桩把「GET 验签」用标准库完整实现；「POST 解密」给出结构，需 pip install pycryptodome 后启用。
  - 所有凭据从 config（环境变量）读取，不硬编码。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from xml.etree import ElementTree

from channels import ChannelAdapter, InboundMessage, OutboundMessage, _new_id


def _sha1_signature(*parts: str) -> str:
    """企业微信签名：把参数按字典序排序后拼接做 SHA1。"""
    items = sorted(parts)
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def _try_crypto():
    """优先用零依赖的纯 Python AES；环境装有 pycryptodome 时其封装会自动优先。"""
    try:
        from security.crypto import wecom_decrypt
        return wecom_decrypt
    except Exception:  # pragma: no cover - 兜底
        return None


class WeComChannel(ChannelAdapter):
    name = "wecom"

    def __init__(self, config):
        super().__init__(config)
        self.token = getattr(config, "wecom_token", "")
        self.aes_key = getattr(config, "wecom_aes_key", "")
        self.corp_id = getattr(config, "wecom_corp_id", "")
        self.agent_id = getattr(config, "wecom_agent_id", "")
        self.secret = getattr(config, "wecom_secret", "")
        self.port = int(getattr(config, "wecom_callback_port", 8081))
        self._server = None
        self._handler = None
        self._access_token = None
        self._token_expire = 0

    # ---------- 入站：HTTP 回调 ----------
    def _make_handler(self, handler_fn):
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静默默认访问日志
                pass

            def do_GET(self):
                # URL 验证：GET?msg_signature&timestamp&nonce&echostr
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                msg_sig = (q.get("msg_signature") or [""])[0]
                ts = (q.get("timestamp") or [""])[0]
                nonce = (q.get("nonce") or [""])[0]
                echostr = (q.get("echostr") or [""])[0]
                expect = _sha1_signature(outer.token, ts, nonce)
                if msg_sig != expect:
                    self.send_response(403); self.end_headers(); return
                # URL 验证：用 aes_key 解密 echostr，回显解密后的明文（企业微信官方要求）
                try:
                    fn = _try_crypto()
                    if fn is not None and outer.aes_key:
                        plain = fn(outer.aes_key, echostr, receive_id=outer.corp_id)
                    else:
                        plain = echostr  # 未配置 aes_key 时退回回显原文（仅本地调试）
                except Exception as e:  # noqa: BLE001
                    self.send_response(500); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(plain.encode("utf-8") if isinstance(plain, str) else plain)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                msg_sig = (q.get("msg_signature") or [""])[0]
                ts = (q.get("timestamp") or [""])[0]
                nonce = (q.get("nonce") or [""])[0]
                try:
                    root = ElementTree.fromstring(body)
                    encrypt = root.findtext("Encrypt") or ""
                except ElementTree.ParseError:
                    self.send_response(400); self.end_headers(); return
                expect = _sha1_signature(outer.token, ts, nonce, encrypt)
                if msg_sig != expect:
                    self.send_response(403); self.end_headers(); return
                try:
                    plain = outer._decrypt(encrypt)  # 纯 Python AES（兼容 pycryptodome 兜底）
                except Exception as e:  # noqa: BLE001
                    self.send_response(500); self.end_headers()
                    self.wfile.write(str(e).encode("utf-8")); return
                inbound = outer._parse_plain(plain, handler_fn)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"success")

        return _H

    def _decrypt(self, encrypt: str) -> str:
        """AES-256-CBC 解密企业微信消息体（默认零依赖纯 Python AES，装了 pycryptodome 会优先）。"""
        fn = _try_crypto()
        if fn is None:
            raise RuntimeError("未能加载解密模块（security.crypto 缺失）")
        if not self.aes_key:
            raise RuntimeError("未配置企业微信 EncodingAESKey（WECHAT_AES_KEY）")
        return fn(self.aes_key, encrypt, receive_id=self.corp_id)

    def _parse_plain(self, plain: str, handler_fn):
        try:
            root = ElementTree.fromstring(plain)
        except ElementTree.ParseError:
            return None
        msg_type = root.findtext("MsgType")
        if msg_type == "text":
            handler_fn(
                InboundMessage(
                    channel=self.name,
                    message_id=root.findtext("MsgId") or _new_id("wecom"),
                    user_id=root.findtext("FromUserName") or "",
                    text=root.findtext("Content") or "",
                    raw=plain,
                )
            )
        return None

    def start(self, handler):
        self._handler = handler
        self._server = HTTPServer(("0.0.0.0", self.port), self._make_handler(handler))
        print(f"[wecom] 回调服务已启动 http://0.0.0.0:{self.port}/ （企业微信后台配置此 URL）")
        self._server.serve_forever()

    def stop(self):
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    # ---------- 出站：应用消息 ----------
    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expire - 60:
            return self._access_token
        url = (
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            f"?corpid={self.corp_id}&corpsecret={self.secret}"
        )
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (企业微信官方域名)
            data = json.loads(r.read())
        self._access_token = data["access_token"]
        self._token_expire = time.time() + data.get("expires_in", 7200)
        return self._access_token

    def send(self, msg: OutboundMessage) -> None:
        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": msg.user_id,
            "msgtype": "text",
            "agentid": int(self.agent_id) if str(self.agent_id).isdigit() else self.agent_id,
            "text": {"content": msg.text},
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            print(f"[wecom] send -> {msg.user_id}: {r.read().decode('utf-8')}")
