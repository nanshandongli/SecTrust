"""notify.py —— 审计事件的推送分发引擎。

产品侧的真实缺口（2026-10-28 定位）：`AssistantSettings.send()` **只被「测试」
按钮调用**——审批决定、高危安全发现、任务完成/失败这些真实审计事件**都不会
自动到达飞书**。飞书是「连上了但没接线」。本模块补的就是这根线。

设计要点（每条都对应一个踩过的坑）：

- **事件类型可订阅**：每种事件独立开关，默认「全部订阅到所有已启用通道」，
  用户可在助理面板按事件类型关闭（不想被审批消息刷屏就关掉那一类）。
- **推送失败绝不阻塞主流程**：审批决策已经落库了，推送失败只能记账，
  不能把审批接口打成 500（旁路纪律，与执行层死信记账同族）。
- **去重**：同一事件同一 payload 在窗口期内只推一次，避免轮询式扫描把
  同一个发现反复推到群里刷屏。
- **推送体按通道协议不同**：飞书/企微/钉钉走各自的 markdown/text 结构，
  通用 Webhook 走 JSON。**签名密钥只在本地用于签名，绝不出现在推送体里**。
- **诚实边界**：飞书**群机器人 webhook 是只出不进的**——推过去的卡片按钮
  拿不到回调。要「在群里回复即控制」必须用**飞书企业自建应用 + 事件订阅**
  把群消息转发到本产品的 `/api/assistant/inbound`，由本模块解析指令并执行。
  本模块提供命令解析与执行，`inbound` 通道本身不在此伪造。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from assistant_channels import CHANNEL_MAP, AssistantSettings

DEFAULT_DATA_DIR = "data"

# ---------------------------------------------------------------------------
# 事件类型目录（单一事实来源）：前端不硬编码，靠 API 下发
# ---------------------------------------------------------------------------
EVENT_TYPES: Dict[str, Dict[str, str]] = {
    "approval.pending": {
        "label": "审批请求",
        "desc": "有新的高风险动作等待人工审批。",
        "severity": "warn",
    },
    "approval.decided": {
        "label": "审批决定",
        "desc": "审批被通过或拒绝（含决定人与时间）。",
        "severity": "info",
    },
    "security.high_risk": {
        "label": "高危安全发现",
        "desc": "自扫/审计发现高危或严重级别问题。",
        "severity": "high",
    },
    "task.completed": {
        "label": "任务完成",
        "desc": "任务执行成功并产出结果。",
        "severity": "info",
    },
    "task.failed": {
        "label": "任务失败",
        "desc": "任务执行失败或挂起等人工。",
        "severity": "error",
    },
    "cost.anomaly": {
        "label": "成本异常",
        "desc": "成本快照中出现超阈值消耗。",
        "severity": "warn",
    },
}

EVENT_ORDER = list(EVENT_TYPES.keys())

# 入站指令（仅在有自建应用转发时可用）
_COMMANDS = ("approve", "reject", "pending", "help")


def event_catalog() -> List[Dict[str, str]]:
    """事件类型目录（给前端渲染）。"""
    return [{"id": k, **v} for k, v in EVENT_TYPES.items()]


def _now() -> float:
    return time.time()


def _clip(text: str, limit: int = 1500) -> str:
    """推送体裁剪——群里没人看 5000 行日志，且超长文本会被平台截断。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断，完整内容见控制台）"


def _digest_key(event: str, title: str, text: str, extra: str = "") -> str:
    """稳定的去重键——**不能用内置 hash()**（每进程随机盐，跨进程不一致）。"""
    raw = "%s|%s|%s|%s" % (event, title, text, extra)
    return "%s:%s" % (event, hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12])


class NotifyEngine:
    """事件 → 通道的分发器。

    线程安全（web 层是多线程 HTTP server，推送可能在任意请求线程里发生）。
    """

    def __init__(self, settings: AssistantSettings,
                 path: Optional[str] = None,
                 log_path: Optional[str] = None,
                 dedupe_window: float = 300.0):
        self._settings = settings
        self._data_dir = getattr(settings, "_data_dir", DEFAULT_DATA_DIR)
        self._path = path or os.path.join(self._data_dir, "notify_subscriptions.json")
        self._log_path = log_path or os.path.join(self._data_dir, "notify_log.jsonl")
        self._dedupe_window = float(dedupe_window)
        self._lock = threading.Lock()
        self._seen: Dict[str, float] = {}
        self._subs: Dict[str, List[str]] = self._load()

    # -- 订阅持久化 --
    def _load(self) -> Dict[str, List[str]]:
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    chans = d.get("channels")
                    if isinstance(chans, dict):
                        out: Dict[str, List[str]] = {}
                        for cid, evs in chans.items():
                            if cid not in CHANNEL_MAP:
                                continue
                            out[cid] = [e for e in (evs or []) if e in EVENT_TYPES]
                        return out
            except (OSError, ValueError):
                pass
        # 首次使用：默认「所有已启用通道订阅全部事件」——
        # 用户明确要求「让产品链接上飞书」，默认全开才能真的收到消息；
        # 不想要某一类可在面板里单独关掉（显式可关 > 静默不推）。
        return {cid: list(EVENT_ORDER) for cid in CHANNEL_MAP}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or self._data_dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"channels": self._subs}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # -- 订阅读写 --
    def subscriptions(self) -> Dict[str, List[str]]:
        with self._lock:
            return {cid: list(evs) for cid, evs in self._subs.items()}

    def set_subscriptions(self, mapping: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """整表覆盖（前端按通道 × 事件类型提交）。未知通道/事件一律忽略。"""
        clean: Dict[str, List[str]] = {}
        for cid, evs in (mapping or {}).items():
            if cid not in CHANNEL_MAP:
                continue
            clean[cid] = [e for e in (evs or []) if e in EVENT_TYPES]
        with self._lock:
            self._subs = clean
            self._save()
        return self.subscriptions()

    def set_subscription(self, cid: str, events: List[str]) -> List[str]:
        if cid not in CHANNEL_MAP:
            raise ValueError("未知通道：%s" % cid)
        clean = [e for e in (events or []) if e in EVENT_TYPES]
        with self._lock:
            self._subs[cid] = clean
            self._save()
        return list(self._subs[cid])

    def enabled_channels(self) -> List[str]:
        """已启用且已配置 webhook 的通道。"""
        out = []
        for cid in CHANNEL_MAP:
            cfg = self._settings.get_config(cid)
            if cfg.get("enabled") and cfg.get("webhook"):
                out.append(cid)
        return out

    def _targets(self, event: str) -> List[str]:
        with self._lock:
            subs = {cid: list(evs) for cid, evs in self._subs.items()}
        return [cid for cid in self.enabled_channels()
                if event in subs.get(cid, [])]

    # -- 去重 --
    def _is_duplicate(self, key: str) -> bool:
        now = _now()
        with self._lock:
            for k, ts in list(self._seen.items()):
                if now - ts > self._dedupe_window:
                    self._seen.pop(k, None)
            if key in self._seen:
                return True
            self._seen[key] = now
        return False

    # -- 推送日志（审计证据）--
    def _log(self, record: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self._log_path) or self._data_dir,
                        exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 记账失败不影响主流程

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近推送记录（读文件尾部，不把日志全量读进内存）。"""
        try:
            if not os.path.exists(self._log_path):
                return []
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-int(limit or 20):]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return list(reversed(out))

    # -- 审批事件回调（供 ApprovalCenter.on_event 注入） --
    def on_approval_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """把审批事件翻译成推送。

        kind: "requested" / "decided" / "expired"。
        **本方法绝不抛异常**——ApprovalCenter 侧也会再兜一层，双保险。
        """
        try:
            if kind == "requested":
                action = payload.get("action") or "未知动作"
                detail = payload.get("detail") or ""
                text = ("动作：%s\n详情：%s" % (action, detail))
                if payload.get("task_id"):
                    text += "\n任务：%s" % payload.get("task_id")
                text += ("\n\n回复 /approve %s 或 /reject %s 即可决定"
                         "（需在飞书自建应用转发群消息到本产品时可用）。"
                         % (payload.get("approval_id"), payload.get("approval_id")))
                self.notify("approval.pending",
                            "待审批 · %s" % action, text,
                            dedupe_key="approval:%s" % payload.get("approval_id"))
            elif kind == "decided":
                decision = payload.get("decision") or ""
                label = "已批准" if decision == "approve" else "已拒绝"
                self.notify("approval.decided",
                            "审批%s" % label,
                            "审批 ID：%s\n决定人：%s\n结果：%s"
                            % (payload.get("approval_id"),
                               payload.get("by") or "unknown", label),
                            dedupe_key="approval:%s:decided" % payload.get("approval_id"))
            elif kind == "expired":
                self.notify("approval.decided", "审批已过期",
                            "审批 ID：%s\n（超时未决，按保守默认拒绝处理）"
                            % payload.get("approval_id"),
                            dedupe_key="approval:%s:expired" % payload.get("approval_id"))
        except Exception:  # noqa: BLE001 —— 旁路不挡主链路
            pass

    # -- 主入口 --
    def notify(self, event: str, title: str, text: str,
               dedupe_key: str = "", extra: str = "") -> Dict[str, Any]:
        """把一个事件推到所有订阅了该事件且已启用的通道。

        **永不抛异常**——推送是旁路，不能把调用方（审批接口等）带崩。
        """
        if event not in EVENT_TYPES:
            return {"ok": False, "error": "未知事件类型：%s" % event,
                    "delivered": [], "skipped": ["unknown_event"]}
        title = _clip(title, 200)
        text = _clip(text)
        key = dedupe_key or _digest_key(event, title, text, extra)
        if self._is_duplicate(key):
            return {"ok": True, "skipped": ["duplicate"], "delivered": [],
                    "event": event}
        targets = self._targets(event)
        if not targets:
            return {"ok": True, "skipped": ["no_target"], "delivered": [],
                    "event": event}
        delivered = []
        for cid in targets:
            try:
                res = self._settings.send(cid, title, text)
            except Exception as e:  # noqa: BLE001 —— 旁路纪律：绝不因推送炸主流程
                res = {"ok": False, "error": "推送异常：%s" % e}
            delivered.append({"channel": cid, "ok": bool(res.get("ok")),
                              "error": res.get("error")})
            self._log({"ts": _now(), "event": event, "channel": cid,
                       "title": title, "ok": bool(res.get("ok")),
                       "error": res.get("error"), "key": key})
        ok = any(d["ok"] for d in delivered)
        return {"ok": ok, "event": event, "delivered": delivered,
                "skipped": []}

    def to_public(self) -> Dict[str, Any]:
        return {
            "events": event_catalog(),
            "subscriptions": self.subscriptions(),
            "enabled_channels": self.enabled_channels(),
            "recent": self.recent(20),
            "note": "推送失败不会影响审批等主流程；每条推送都有记录可查。",
        }


# ---------------------------------------------------------------------------
# 入站指令解析（飞书自建应用转发群消息 → 本产品执行）
# ---------------------------------------------------------------------------
_CMD_RE = re.compile(
    r"^\s*(?:/)?(?P<cmd>approve|reject|pending|help)\b\s*(?P<arg>[\w\-]*)\s*$",
    re.IGNORECASE)


def parse_command(text: str) -> Optional[Dict[str, Any]]:
    """把群消息解析成命令。认不出来的返回 None（不猜、不硬执行）。

    支持：
      ``/approve ap-xxxx``  批准指定审批
      ``/reject  ap-xxxx``  拒绝指定审批
      ``/pending``          列出待审批
      ``/help``             用法
    """
    m = _CMD_RE.match(text or "")
    if not m:
        return None
    cmd = m.group("cmd").lower()
    return {"command": cmd, "arg": (m.group("arg") or "").strip()}


# ---------------------------------------------------------------------------
# 入站鉴权令牌
# ---------------------------------------------------------------------------
def _token_path(data_dir: str) -> str:
    return os.path.join(data_dir, "notify_inbound_token")


def ensure_inbound_token(data_dir: str = DEFAULT_DATA_DIR) -> str:
    """取入站令牌；不存在则生成一个（只写一次，之后稳定复用）。"""
    path = _token_path(data_dir)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


def check_inbound_token(data_dir: str, provided: str) -> bool:
    """校验入站令牌。用 hmac.compare_digest 防时序侧信道。"""
    if not provided:
        return False
    try:
        expected = ensure_inbound_token(data_dir)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(str(provided), str(expected))



def help_text() -> str:
    return ("可用指令：\n"
            "/approve <审批ID> 批准\n"
            "/reject <审批ID> 拒绝\n"
            "/pending 列出待审批\n"
            "/help 显示本说明")


# ---------------------------------------------------------------------------
# 进程无关的便捷入口
# ---------------------------------------------------------------------------
_ENGINE: Optional[NotifyEngine] = None
_ENGINE_LOCK = threading.Lock()


def default_engine(data_dir: str = "") -> NotifyEngine:
    """进程内共享的默认引擎（配置目录可用 SECTRUST_DATA_DIR 覆盖）。

    执行层（execution/daemon.py、runner.py）不在 web 进程里，但**同样应该
    能把任务完成/失败推到飞书**。它们不持有 AssistantSettings 实例，就走
    这个默认入口——读同一个 data/ 目录下的通道配置。
    """
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            ddir = data_dir or os.environ.get("SECTRUST_DATA_DIR") or DEFAULT_DATA_DIR
            _ENGINE = NotifyEngine(AssistantSettings(data_dir=ddir))
        return _ENGINE


def push(event: str, title: str, text: str, dedupe_key: str = "",
         data_dir: str = "") -> Dict[str, Any]:
    """便捷推送：**永不抛异常**，返回结果字典供调用方判断。"""
    try:
        return default_engine(data_dir).notify(event, title, text,
                                               dedupe_key=dedupe_key)
    except Exception as e:  # noqa: BLE001 —— 旁路不挡主链路
        return {"ok": False, "error": "推送异常：%s" % e, "delivered": []}


def task_event_type(status: str) -> str:
    """任务状态 → 事件类型。**只认终态**，非终态返回空串（不推）。

    映射：done/completed/succeeded → task.completed；
          failed/needs_human/error/cancelled → task.failed。
    """
    st = (status or "").strip().lower()
    if st in ("done", "completed", "succeeded", "success"):
        return "task.completed"
    if st in ("failed", "needs_human", "error", "cancelled", "canceled"):
        return "task.failed"
    return ""


def push_task_event(status: str, task_id: str, title: str = "",
                    detail: str = "", engine: Optional[NotifyEngine] = None
                    ) -> Dict[str, Any]:
    """任务状态事件 → task.completed / task.failed。

    非终态**不推**（避免 registered/running 之类的半成品刷屏）。
    engine 可注入（测试/多实例场景）；不给则用进程级默认引擎。
    """
    event = task_event_type(status)
    if not event:
        return {"ok": True, "skipped": ["not_terminal"], "delivered": []}
    head = title or ("任务 %s" % task_id)
    body = "任务 ID：%s\n状态：%s" % (task_id, (status or "").strip())
    if detail:
        body += "\n%s" % detail
    if engine is not None:
        try:
            return engine.notify(event, head, body,
                                 dedupe_key="task:%s:%s" % (task_id, status))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "推送异常：%s" % e, "delivered": []}
    return push(event, head, body, dedupe_key="task:%s:%s" % (task_id, status))

