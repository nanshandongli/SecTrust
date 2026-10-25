"""
connectors/imap.py —— IMAP 邮件连接器 + 邮件触发规则（stdlib imaplib）。

两个东西：
1. IMAPConnector：收邮件（poll_unseen / mark_seen / fetch_recent）。
   凭据从环境变量读（SECTRUST_IMAP_HOST/USER/PASSWORD），**只进连接不落库**。
2. EmailRule + EmailTrigger：Grok Bot"邮件别名触发工作流"的本地最小版——
   未读邮件匹配规则 → 向 TaskRegistry 登记任务 → 标记已读。
   登记成功才 mark_seen（失败留未读，下次重试，不丢邮件）。

诚实边界：只读收件箱 + 打已读标记；**不代发邮件**（发送是 consequential
动作，必须走 ApprovalCenter 审批后由用户/上层执行）。
"""

from __future__ import annotations

import email
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .base import Connector, ConnectorError


class IMAPConnector(Connector):
    name = "imap"

    def __init__(self, host: str = None, user: str = None, password: str = None,
                 port: int = None, imap_cls=None, folder: str = "INBOX"):
        super().__init__()
        self.host = host if host is not None else os.environ.get("SECTRUST_IMAP_HOST", "")
        self.user = user if user is not None else os.environ.get("SECTRUST_IMAP_USER", "")
        self.password = (password if password is not None
                         else os.environ.get("SECTRUST_IMAP_PASSWORD", ""))
        self.port = port or int(os.environ.get("SECTRUST_IMAP_PORT", "993"))
        self.folder = folder
        self._imap_cls = imap_cls      # 测试注入假 IMAP；默认 imaplib.IMAP4_SSL
        self._conn = None

    # ---- 契约 ----
    def _configured(self) -> bool:
        return bool(self.host and self.user and self.password) or self._imap_cls is not None

    def _actions(self) -> list:
        return [
            {"name": "poll_unseen", "description": "拉取未读邮件（不标记已读）",
             "params": {"limit": "int"}},
            {"name": "mark_seen", "description": "把指定 uid 标记为已读",
             "params": {"uid": "str"}},
            {"name": "fetch_recent", "description": "拉取最近 N 封（含已读）",
             "params": {"limit": "int"}},
        ]

    # ---- 连接管理（用完即关，不缓存常连——邮件服务器会踢闲置连接）----
    def _connect(self):
        if self._conn is not None:
            return self._conn
        cls = self._imap_cls
        if cls is None:
            import imaplib
            cls = imaplib.IMAP4_SSL
        try:
            conn = cls(self.host, self.port)
            conn.login(self.user, self.password)
            conn.select(self.folder)
        except Exception as e:
            raise ConnectorError("IMAP 连接失败: %s" % e)
        self._conn = conn
        return conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    # ---- 邮件解析 ----
    @staticmethod
    def _parse(raw_bytes: bytes, uid: str) -> dict:
        # 关键：不能直接用 message_from_bytes——compat32 会把非 ASCII 头字节存成
        # surrogateescape 的"假 str"，编码词解不开、字面量变乱码。先按 UTF-8
        # 解码成文本，再用 policy.default 解析：现代 API 直接返回解码好的
        # unicode 头（RFC2047 自动处理）。
        # 诚实边界：非 UTF-8 的 8bit 正文 snippet 可能乱码（主题不受影响）；
        # base64/QP 正文不受影响。
        text = raw_bytes.decode("utf-8", "replace")
        msg = email.message_from_string(text, policy=email.policy.default)

        def _hdr(name):
            v = msg.get(name)
            return str(v) if v is not None else ""

        snippet = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    snippet = payload.decode("utf-8", "replace")[:300]
                    break
        else:
            payload = msg.get_payload(decode=True) or b""
            snippet = payload.decode("utf-8", "replace")[:300]
        return {
            "uid": uid,
            "from": _hdr("From"),
            "to": _hdr("To"),
            "subject": _hdr("Subject"),
            "date": _hdr("Date"),
            "snippet": snippet,
        }

        snippet = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    snippet = payload.decode("utf-8", "replace")[:300]
                    break
        else:
            payload = msg.get_payload(decode=True) or b""
            snippet = payload.decode("utf-8", "replace")[:300]
        return {
            "uid": uid,
            "from": _hdr("From"),
            "to": _hdr("To"),
            "subject": _hdr("Subject"),
            "date": _hdr("Date"),
            "snippet": snippet,
        }

    def _search(self, criteria: str) -> List[str]:
        conn = self._connect()
        typ, data = conn.uid("search", None, criteria)
        if typ != "OK":
            raise ConnectorError("IMAP 搜索失败: %s" % criteria)
        return data[0].split() if data and data[0] else []

    # ---- action 派发 ----
    def _call(self, action: str, **params):
        limit = int(params.get("limit", 10))
        if action in ("poll_unseen", "fetch_recent"):
            criteria = "UNSEEN" if action == "poll_unseen" else "ALL"
            uids = self._search(criteria)
            conn = self._connect()
            out = []
            for uid in uids[-limit:]:
                uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
                typ, data = conn.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not data or not data[0]:
                    continue
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                out.append(self._parse(raw, uid_s))
            return {"count": len(out), "mails": out}
        if action == "mark_seen":
            conn = self._connect()
            uid = str(params["uid"])
            typ, _ = conn.uid("store", uid, "+FLAGS", "(\\Seen)")
            if typ != "OK":
                raise ConnectorError("标记已读失败: uid=%s" % uid)
            return {"marked": uid}
        raise ConnectorError("未实现的 action: %s" % action)


# ------------------------------------------------------------------ 触发规则
@dataclass
class EmailRule:
    """一条邮件触发规则：未读邮件命中匹配器 → 登记 payload 任务。"""
    name: str
    subject_contains: List[str] = field(default_factory=list)
    from_contains: List[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    def matches(self, mail: dict) -> bool:
        subj = mail.get("subject", "")
        frm = mail.get("from", "")
        if self.subject_contains and not any(s in subj for s in self.subject_contains):
            return False
        if self.from_contains and not any(s in frm for s in self.from_contains):
            return False
        # 两个匹配器都空 = 匹配一切（显式声明才这么做）
        return True


class EmailTrigger:
    """轮询未读邮件，命中规则就登记任务。登记成功才标记已读（不丢邮件）。"""

    def __init__(self, connector: IMAPConnector, registry, rules: List[EmailRule] = None):
        self.connector = connector
        self.registry = registry
        self.rules = rules or []

    def add_rule(self, rule: EmailRule):
        self.rules.append(rule)

    def poll_and_fire(self, limit: int = 20) -> list:
        """返回 [(rule_name, task_id)]。连接器不可用时返回空（不炸主循环）。"""
        res = self.connector.call("poll_unseen", limit=limit)
        if not res.ok:
            return []
        fired = []
        for mail in res.data.get("mails", []):
            for rule in self.rules:
                if not rule.matches(mail):
                    continue
                task_id = "mail-%s-%s" % (
                    re.sub(r"[^A-Za-z0-9_-]", "", rule.name)[:24] or "rule",
                    mail["uid"])
                payload = dict(rule.payload)
                payload["mail"] = {"from": mail["from"], "subject": mail["subject"],
                                   "date": mail["date"], "uid": mail["uid"]}
                payload["fired_by"] = "email:%s" % rule.name
                try:
                    self.registry.register_task(task_id, payload=payload)
                except Exception:
                    continue                    # 登记失败：不标记已读，下次重试
                self.connector.call("mark_seen", uid=mail["uid"])
                fired.append((rule.name, task_id))
                break                           # 一封邮件只命中第一条规则
        return fired
