"""
FeishuChannel —— 飞书（官方 API）渠道适配。

入站：事件订阅 webhook。处理「URL 验证(url_verification)」与「消息事件(callback)」。
  - 验签：HMAC-SHA256(timestamp + "\n" + secret) 与 header 的 sign 比对（或 verification_token 比对）。
出站：调用飞书「发送消息」/open-apis/im/v1/messages（tenant_access_token 鉴权）。

⚠️ 真实联调前需要在【飞书开放平台】配置：
  - 事件订阅请求地址（指向本服务 port）
  - App ID / App Secret / Verification Token（或 Event Sign Secret）

安全说明：
  - 入站必须验签，拒绝伪造事件。
  - 所有凭据从 config（环境变量）读取，不硬编码。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from channels import ChannelAdapter, InboundMessage, OutboundMessage, _new_id


class FeishuChannel(ChannelAdapter):
    name = "feishu"

    def __init__(self, config):
        super().__init__(config)
        self.app_id = getattr(config, "feishu_app_id", "")
        self.app_secret = getattr(config, "feishu_app_secret", "")
        self.verification_token = getattr(config, "feishu_verification_token", "")
        self.encrypt_key = getattr(config, "feishu_encrypt_key", "")
        self.port = int(getattr(config, "feishu_callback_port", 8082))
        self._server = None
        self._tenant_token = None
        self._token_expire = 0

    # ---------- 入站：事件订阅 ----------
    def _verify_sign(self, timestamp: str, sign: str) -> bool:
        """飞书事件签名：HMAC-SHA256(timestamp + '\\n' + app_secret) 的 hex。"""
        if not self.app_secret:
            return False
        raw = (timestamp + "\n" + self.app_secret).encode("utf-8")
        expect = hmac.new(raw, digestmod=hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, sign)

    def _make_handler(self, handler_fn):
        outer = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    self.send_response(400); self.end_headers(); return
                ts = self.headers.get("X-Lark-Signature-Timestamp", "")
                sig = self.headers.get("X-Lark-Signature", "")
                # 优先按 sign 验签；无 sign 时退化为 verification_token 比对
                ok = outer._verify_sign(ts, sig) or data.get("token") == outer.verification_token
                if not ok:
                    self.send_response(403); self.end_headers(); return

                # 事件加密模式（开启了 Encrypt Key）：body 内含 encrypt 字段，需 AES 解密
                if "encrypt" in data:
                    try:
                        from security.crypto import feishu_decrypt
                        plain = feishu_decrypt(
                            outer.encrypt_key, data["encrypt"],
                            timestamp=ts, nonce=self.headers.get("X-Lark-Signature-Nonce", ""),
                            receive_id=outer.app_id,
                        )
                        data = json.loads(plain)
                    except Exception as e:  # noqa: BLE001
                        self.send_response(500); self.end_headers()
                        self.wfile.write(str(e).encode("utf-8")); return

                if data.get("type") == "url_verification":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"challenge": data["challenge"]}).encode("utf-8"))
                    return

                # 消息事件：event.message.content 是 URL-encoded JSON 文本
                event = data.get("event", {})
                message = event.get("message", {})
                if message.get("message_type") == "text":
                    import urllib.parse
                    content = json.loads(urllib.parse.unquote(message.get("content", "{}")))
                    handler_fn(
                        InboundMessage(
                            channel=outer.name,
                            message_id=message.get("message_id") or _new_id("feishu"),
                            user_id=message.get("sender", {}).get("id") or event.get("sender", {}).get("sender_id", {}).get("open_id", ""),
                            text=content.get("text", ""),
                            raw=data,
                        )
                    )
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"code":0}')

        return _H

    def start(self, handler):
        self._handler = handler
        self._server = HTTPServer(("0.0.0.0", self.port), self._make_handler(handler))
        print(f"[feishu] 事件订阅服务已启动 http://0.0.0.0:{self.port}/ （飞书后台配置此 URL）")
        self._server.serve_forever()

    def stop(self):
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    # ---------- 出站：发送消息 ----------
    def _get_tenant_token(self) -> str:
        if self._tenant_token and time.time() < self._token_expire - 60:
            return self._tenant_token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (飞书官方域名)
            data = json.loads(r.read())
        self._tenant_token = data["tenant_access_token"]
        self._token_expire = time.time() + data.get("expire", 7200)
        return self._tenant_token

    def send(self, msg: OutboundMessage) -> None:
        token = self._get_tenant_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        payload = {
            "receive_id": msg.user_id,
            "msg_type": "text",
            "content": json.dumps({"text": msg.text}),
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            print(f"[feishu] send -> {msg.user_id}: {r.read().decode('utf-8')}")
