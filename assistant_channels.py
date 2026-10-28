"""assistant_channels.py —— 助理的远程通道与偏好设置。

对标 WorkBuddy「设置 → 助理」的远程通道卡片 + 上下文管理开关，但坚持
诚实边界：只收录本产品**真能接通**的通道（HTTP webhook 类，无需 SDK、
无需第三方授权服务器）——企业微信/钉钉/飞书群机器人与通用 Webhook。
微信助理、QQ 机器人这类需要官方机器人平台接入的通道，未接入就**不列**，
不伪造「连接」按钮。

- 通道配置落盘 ``data/assistant_channels.json``（已 gitignore 的 data/ 目录）。
- 公开视图（to_public）绝不返回 webhook 完整 URL 与加签密钥——webhook
  里的 key/access_token 参数会被掩成 ``****+末4位``。
- ``send()`` 是真实 HTTP POST；未配置/发送失败都如实返回错误，不假装成功。
- 偏好 ``context_memory``：关闭后对话服务不再携带历史轮次（省 token、
  每次提问独立），由 Web 层在调用 ChatService 前剥离 history。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_DATA_DIR = "data"
ALLOWED_PREFS = ("context_memory",)

# ---------------------------------------------------------------------------
# 通道目录：只列真实可接通的
# ---------------------------------------------------------------------------
CHANNELS: List[Dict[str, Any]] = [
    {
        "id": "wecom_bot",
        "name": "企业微信群机器人",
        "desc": "在企业微信群添加群机器人，把 Webhook 地址粘进来即可推送审计提醒与任务结果。",
        "letter": "企",
        "hue": 210,
        "webhook_prefix": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "secret_label": "",
        "fields": [
            {"key": "webhook", "label": "机器人 Webhook 地址", "type": "text",
             "placeholder": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx"},
        ],
        "note": "群聊右上角 → 群机器人 → 添加，可拿到 Webhook 地址。",
    },
    {
        "id": "dingtalk_bot",
        "name": "钉钉群机器人",
        "desc": "接入钉钉群自定义机器人，推送审计提醒与任务结果；支持加签安全模式。",
        "letter": "钉",
        "hue": 210,
        "webhook_prefix": "https://oapi.dingtalk.com/robot/send",
        "secret_label": "加签密钥（SEC 开头，可选）",
        "fields": [
            {"key": "webhook", "label": "机器人 Webhook 地址", "type": "text",
             "placeholder": "https://oapi.dingtalk.com/robot/send?access_token=xxxx"},
            {"key": "secret", "label": "加签密钥（SEC 开头，可选）", "type": "password",
             "placeholder": "SECxxxx"},
        ],
        "note": "群设置 → 智能群助手 → 自定义机器人，安全设置建议选「加签」。",
    },
    {
        "id": "feishu_bot",
        "name": "飞书群机器人",
        "desc": "接入飞书群自定义机器人，在飞书群里接收审计提醒与任务结果；支持签名校验。",
        "letter": "飞",
        "hue": 210,
        "webhook_prefix": "https://open.feishu.cn/open-apis/bot/v2/hook/",
        "secret_label": "签名校验密钥（可选）",
        "fields": [
            {"key": "webhook", "label": "机器人 Webhook 地址", "type": "text",
             "placeholder": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"},
            {"key": "secret", "label": "签名校验密钥（可选）", "type": "password",
             "placeholder": "留空表示未开启签名校验"},
        ],
        "note": "群设置 → 群机器人 → 自定义机器人，可拿到 Webhook 地址。",
    },
    {
        "id": "generic_webhook",
        "name": "通用 Webhook",
        "desc": "把审计事件以 JSON POST 到任意 HTTP 端点，适合自建通知网关或自动化脚本接收。",
        "letter": "W",
        "hue": 268,
        "webhook_prefix": "",
        "secret_label": "",
        "fields": [
            {"key": "webhook", "label": "接收端 URL（http/https）", "type": "text",
             "placeholder": "https://your-gateway.example.com/hook"},
        ],
        "note": "推送体：{\"event\":\"sectrust.notify\",\"title\":…,\"text\":…,\"ts\":…}。",
    },
]

CHANNEL_MAP = {c["id"]: c for c in CHANNELS}


def mask_webhook(url: str) -> str:
    """掩去 webhook 中的敏感部分，只保留末 4 位。

    两类形态都要掩（缺一即泄露）：
    1. **路径型**：飞书 `…/bot/v2/hook/<uuid>` —— 凭据在路径末段，没有 `?key=`
       可掩。曾只掩 query 参数，导致飞书地址被完整明文返回给不鉴权的
       `GET /api/assistant/settings`（拿到即可往群里发消息）。
    2. **参数型**：企微 `?key=xxx` / 钉钉 `?access_token=xxx` —— 必须**单轮**
       组合正则替换，多轮逐参数会互相击穿（`token=` 会命中 `access_token=****`
       的结果）。
    """
    if not url:
        return ""

    # 1) 路径型：命中任一已知通道的 webhook_prefix，就把 prefix 之后整段掩掉
    for ch in CHANNELS:
        prefix = ch.get("webhook_prefix") or ""
        if prefix and url.startswith(prefix):
            tail = url[len(prefix):].split("?")[0]
            if tail:
                keep = tail[-4:] if len(tail) > 4 else ""
                return "%s****%s" % (prefix, keep)

    # 2) 参数型：单轮组合正则
    def _mask(m: "re.Match[str]") -> str:
        val = m.group(2)
        tail = val[-4:] if len(val) > 4 else "****"
        return "%s=****%s" % (m.group(1), tail)

    return re.sub(r"((?:access_)?token|key|secret)=([^&]+)", _mask, url)


class AssistantSettings:
    """远程通道配置 + 助理偏好的持久化与真实发送。"""

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR,
                 path: Optional[str] = None):
        self._data_dir = data_dir
        self._path = path or os.path.join(data_dir, "assistant_channels.json")
        self._data: Dict[str, Any] = self._load()

    # -- 持久化 --
    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    d.setdefault("channels", {})
                    d.setdefault("prefs", {"context_memory": True})
                    return d
            except (OSError, ValueError):
                pass  # 损坏文件按全新配置处理，不抛崩溃
        return {"channels": {}, "prefs": {"context_memory": True}}

    def _save(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # -- 通道配置 --
    def get_config(self, cid: str) -> Dict[str, Any]:
        return dict(self._data["channels"].get(cid) or {})

    def configure(self, cid: str, webhook: str = "",
                  secret: str = "", enabled: Optional[bool] = None) -> Dict[str, Any]:
        if cid not in CHANNEL_MAP:
            raise ValueError("未知通道：%s" % cid)
        old = self.get_config(cid)
        # 掩码回传语义：值留空或含 ****（URL 内嵌掩码/整段掩码）→ 保留磁盘原值
        if old.get("webhook") and (not webhook or "****" in webhook):
            webhook = old["webhook"]
        if old.get("secret") and (not secret or "****" in secret):
            secret = old["secret"]
        if not webhook or not webhook.startswith(("http://", "https://")):
            raise ValueError("Webhook 地址必须以 http(s) 开头")
        cfg = {"webhook": webhook.strip(), "secret": (secret or "").strip(),
               "enabled": bool(old.get("enabled", False)) if enabled is None else bool(enabled)}
        self._data["channels"][cid] = cfg
        self._save()
        return cfg

    def set_enabled(self, cid: str, enabled: bool) -> None:
        if cid not in CHANNEL_MAP:
            raise ValueError("未知通道：%s" % cid)
        cfg = self.get_config(cid)
        if enabled and not cfg.get("webhook"):
            raise ValueError("请先配置 Webhook 地址再启用")
        cfg["enabled"] = bool(enabled)
        self._data["channels"][cid] = cfg
        self._save()

    # -- 偏好 --
    def prefs(self) -> Dict[str, Any]:
        return dict(self._data.get("prefs") or {"context_memory": True})

    def set_pref(self, key: str, value: Any) -> None:
        if key not in ALLOWED_PREFS:
            raise ValueError("不支持的偏好项：%s" % key)
        if not isinstance(value, bool):
            raise ValueError("偏好值必须是布尔")
        self._data.setdefault("prefs", {})[key] = value
        self._save()

    # -- 真实发送 --
    def send(self, cid: str, title: str, text: str,
             webhook: str = "", secret: str = "") -> Dict[str, Any]:
        """真实 POST。未配置/失败都如实返回，不假装成功。"""
        ch = CHANNEL_MAP.get(cid)
        if ch is None:
            return {"ok": False, "error": "未知通道：%s" % cid}
        cfg = self.get_config(cid)
        url = webhook or cfg.get("webhook") or ""
        sec = secret if secret else (cfg.get("secret") or "")
        if not url:
            return {"ok": False, "error": "通道未配置 Webhook 地址"}
        body: Dict[str, Any]
        headers = {"Content-Type": "application/json"}
        try:
            if cid == "wecom_bot":
                body = {"msgtype": "markdown",
                        "markdown": {"content": "**%s**\n%s" % (title, text)}}
            elif cid == "dingtalk_bot":
                if sec:
                    ts = str(round(time.time() * 1000))
                    sign_str = "%s\n%s" % (ts, sec)
                    sign = base64.b64encode(hmac.new(
                        sec.encode(), sign_str.encode(), hashlib.sha256).digest())
                    sep = "&" if "?" in url else "?"
                    url = "%s%stimestamp=%s&sign=%s" % (
                        url, sep, ts, urllib.parse.quote_plus(sign))
                body = {"msgtype": "markdown",
                        "markdown": {"title": title, "text": "**%s**\n%s" % (title, text)}}
            elif cid == "feishu_bot":
                body = {"msg_type": "text",
                        "content": {"text": "%s\n%s" % (title, text)}}
                if sec:
                    # 飞书签名：string_to_sign = "timestamp\nsecret"，以它为 HMAC key、
                    # 空消息做 SHA256，再 base64。**必须 decode 成 str**——b64encode 返回
                    # bytes，bytes 进 json.dumps 会抛 TypeError（曾导致带签名的飞书通道崩溃）。
                    ts = str(int(time.time()))
                    sign_str = "%s\n%s" % (ts, sec)
                    sign = base64.b64encode(hmac.new(
                        sign_str.encode(), b"", hashlib.sha256).digest()).decode("utf-8")
                    body["timestamp"] = ts
                    body["sign"] = sign
            else:  # generic_webhook
                body = {"event": "sectrust.notify", "title": title,
                        "text": text, "ts": int(time.time())}
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "replace")[:500]
                try:
                    out = json.loads(raw)
                except ValueError:
                    out = {"raw": raw}
                # 各平台的应用层错误码也要检查（HTTP 200 不代表推送成功）
                errcode = out.get("errcode", out.get("code"))
                if errcode not in (None, 0):
                    return {"ok": False, "error": "平台返回错误码 %s：%s"
                            % (errcode, out.get("errmsg") or out.get("msg") or raw)}
                return {"ok": True, "detail": out}
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": "HTTP %s" % e.code}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    def test(self, cid: str, webhook: str = "", secret: str = "") -> Dict[str, Any]:
        return self.send(cid, "SecTrust 测试消息",
                         "这是一条来自协作审计台的测试推送。收到即表示通道配置成功。",
                         webhook=webhook, secret=secret)

    # -- 公开视图 --
    def to_public(self) -> Dict[str, Any]:
        chans = []
        for ch in CHANNELS:
            cfg = self.get_config(ch["id"])
            chans.append({
                "id": ch["id"], "name": ch["name"], "desc": ch["desc"],
                "letter": ch["letter"], "hue": ch["hue"],
                "fields": ch["fields"], "note": ch["note"],
                "configured": bool(cfg.get("webhook")),
                "enabled": bool(cfg.get("enabled")),
                "webhook_masked": mask_webhook(cfg.get("webhook", "")),
                "has_secret": bool(cfg.get("secret")),
            })
        return {"channels": chans, "prefs": self.prefs(),
                "note": "只收录真实可接通的通道（webhook 类）；未接入的通道不伪造连接入口。"}
