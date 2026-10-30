"""
kb_control.py —— 知识库与数据管控（国标六项能力之"知识库与数据管控"）

在既有 assets.py（内容寻址素材库）/ content_guard.py（PII 脱敏）之上补齐两件
此前缺口：**知识库内容分级标签** 与 **出域审批流**。

设计纪律（与 circuit_breaker 相反，本模块核心是 fail-closed）：
  · 出域审批 **fail-closed**：批准记录读不到 / 文件损坏 / 任何异常 = 拒绝出域。
    熔断读不到按"未熔断"放行（fail-open，怕自伤）；出域读不到必须拒绝
    （fail-closed，怕泄密）——两者方向相反，不要照抄。
  · 四级分类复用 asset_registry.CLASSIFICATIONS：public/internal/restricted/confidential。
  · 出域规则：public/internal 可出域；restricted 需 approval.act 批准；
    confidential 默认拒绝、需 governance.emergency 级批准。无有效批准 → 拒绝并留痕。
  · 状态进程无关：落盘 data/kb_labels.json 与 data/egress_approvals.json，
    控制台（打标/审批）与 daemon（出域门）共享同一份。
  · token / 凭据不进本模块；本模块只判"能不能出域"，不碰外发通道本身。

诚实边界：这是**应用层管控**，不是"数据不出域"（该项用户已豁免）。出域批准
只约束"平台自己的出域动作是否放行"，不等于数据离开本机后仍受控。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

from . import asset_registry, content_guard, rbac

DEFAULT_KB_PATH = os.path.join("data", "kb_labels.json")
DEFAULT_EGRESS_PATH = os.path.join("data", "egress_approvals.json")

# 四级分类（单一事实来源）
CLASSIFICATIONS = asset_registry.CLASSIFICATIONS

# 各分级出域所需权限；None = 无需批准（可自由出域）
_REQUIRED_PERM = {
    "public": None,
    "internal": None,
    "restricted": "approval.act",
    "confidential": "governance.emergency",
}

# 显式涉密关键词（命中即 confidential，不看 PII）
_SECRET_HINTS = ("绝密", "机密", "涉密", "confidential", "top secret")


def classify_text(text: str) -> str:
    """按内容把 KB 条目定级（供打标时给建议，不强制）。

    复用 content_guard 的 PII 命中：身份证/银行卡/护照 → confidential；
    手机/邮箱 → restricted；显式涉密关键词 → confidential；其余 → internal。
    纯规则、零依赖，不谎称做了语义理解。
    """
    if not text:
        return "internal"
    low = str(text).lower()
    for kw in _SECRET_HINTS:
        if kw.lower() in low:
            return "confidential"
    de = content_guard.desensitize(str(text), level="medium")
    types = {f["type"] for f in de.get("findings", [])}
    if types & {"身份证", "银行卡", "护照"}:
        return "confidential"
    if types & {"手机", "邮箱"}:
        return "restricted"
    return "internal"


def _ref_key(item_ref) -> str:
    """item_ref 支持 'category::key' 字符串或 dict(category,key)，统一成规范串。"""
    if isinstance(item_ref, dict):
        return "%s::%s" % (item_ref.get("category", ""), item_ref.get("key", ""))
    return str(item_ref or "")


class _JsonState:
    """共享的原子落盘底座（json + os.replace，与 circuit_breaker 同风格）。

    读语义：文件不存在 = 正常初始状态（blank）；存在但损坏/不可读 = 抛
    RuntimeError（上层据此 fail-closed 拒绝），绝不把损坏当空白放行。
    """

    def __init__(self, path: str, blank):
        self.path = path
        self._blank = blank
        self._lock = threading.RLock()

    def read(self) -> Dict:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except FileNotFoundError:
                return self._blank()
            except (OSError, ValueError) as e:
                raise RuntimeError("状态文件损坏或不可读：%s" % e)
            if not isinstance(data, dict):
                raise RuntimeError("状态文件格式错误（根节点非对象）")
            return data

    def write(self, data: Dict) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)


def _kb_blank() -> Dict:
    return {"labels": {}, "updated_at": None}


class KBLabelStore:
    """KB 条目 (category, key) → classification 分级标签，落盘可查。"""

    def __init__(self, path: str = DEFAULT_KB_PATH):
        self.path = path
        self._state = _JsonState(path, _kb_blank)

    def set_label(self, category: str, key: str, classification: str,
                  by: str = "unknown") -> Dict:
        """给条目打分级标签（非法分级直接抛 ValueError，不静默降级）。"""
        if classification not in CLASSIFICATIONS:
            raise ValueError("未知分级：%s（应为 %s）"
                             % (classification, "/".join(CLASSIFICATIONS)))
        data = self._state.read()
        now = time.time()
        rec = {
            "category": category, "key": key,
            "classification": classification,
            "set_by": by, "set_at": now,
        }
        data["labels"]["%s::%s" % (category, key)] = rec
        data["updated_at"] = now
        self._state.write(data)
        return rec

    def get_label(self, category: str, key: str) -> Optional[str]:
        """返回分级；无标签返回 None；文件损坏抛 RuntimeError（上层 fail-closed）。"""
        data = self._state.read()
        rec = data.get("labels", {}).get("%s::%s" % (category, key))
        return rec["classification"] if rec else None

    def list_labels(self) -> List[Dict]:
        data = self._state.read()
        return sorted(data.get("labels", {}).values(),
                      key=lambda r: (r["category"], r["key"]))

    def state(self) -> Dict:
        data = self._state.read()
        return {"labels": data.get("labels", {}),
                "updated_at": data.get("updated_at"), "path": self.path}


def _egress_blank() -> Dict:
    return {"requests": {}, "denials": [], "updated_at": None}


class EgressApproval:
    """出域申请 / 审批 / 放行判定（fail-closed）。

    decide() 内建 RBAC 校验：restricted 需 approval.act，confidential 需
    governance.emergency；role 不给或不匹配 → 拒绝决策（不改变 pending 状态）。
    """

    def __init__(self, path: str = DEFAULT_EGRESS_PATH,
                 labels: Optional[KBLabelStore] = None):
        self.path = path
        self._state = _JsonState(path, _egress_blank)
        self.labels = labels or KBLabelStore()

    def _load(self) -> Dict:
        return self._state.read()

    def request(self, item_ref, classification: str, destination: str,
                by: str = "unknown") -> Dict:
        """登记一条出域申请（pending）。classification 非法直接抛。"""
        if classification not in CLASSIFICATIONS:
            raise ValueError("未知分级：%s" % classification)
        data = self._load()
        now = time.time()
        req_id = "egress-" + os.urandom(6).hex()
        rec = {
            "req_id": req_id,
            "item_ref": _ref_key(item_ref),
            "classification": classification,
            "destination": str(destination or "*"),
            "required_permission": _REQUIRED_PERM[classification],
            "requested_by": by, "requested_at": now,
            "status": "pending",
            "decided_by": None, "decided_at": None, "decision_role": None,
        }
        data["requests"][req_id] = rec
        data["updated_at"] = now
        self._state.write(data)
        return rec

    def decide(self, req_id: str, approve: bool, by: str = "unknown",
               role: Optional[str] = None) -> Dict:
        """审批一条出域申请。返回 {ok, request?, error?}。

        权限不足 / 申请不存在 / 已决策 → ok=False，且不改变原状态。
        """
        try:
            data = self._load()
        except RuntimeError as e:
            return {"ok": False, "error": "批准记录不可读（fail-closed）：%s" % e}
        rec = data.get("requests", {}).get(req_id)
        if not rec:
            return {"ok": False, "error": "出域申请不存在：%s" % req_id}
        if rec["status"] != "pending":
            return {"ok": False, "error": "申请已决策（%s），不可重复审批" % rec["status"]}
        need = rec.get("required_permission") or "approval.act"
        ok, err = rbac.require_permission(role, need)
        if not ok:
            return {"ok": False, "error": err, "need_permission": need}
        rec["status"] = "approved" if approve else "rejected"
        rec["decided_by"] = by
        rec["decided_at"] = time.time()
        rec["decision_role"] = role
        data["updated_at"] = rec["decided_at"]
        self._state.write(data)
        return {"ok": True, "request": rec}

    def is_allowed(self, item_ref, destination: str = "*",
                   classification: Optional[str] = None) -> Dict:
        """判定某条目能否出域到某目标，返回 {allowed, reason, ...}。

        fail-closed：批准记录读不到 / 标签读取异常 / 任何异常 → allowed=False。
        classification 可由调用方显式给出（优先于 KB 标签）；无标签且未显式
        给出时按默认 internal 处理（memory_hybrid 的默认值），并在 reason 说明。
        """
        ref = _ref_key(item_ref)
        dest = str(destination or "*")
        base = {"allowed": False, "item_ref": ref, "destination": dest,
                "classification": None, "reason": "", "approval_id": None}
        try:
            # 1) 定级：显式 > KB 标签 > 默认 internal
            if classification is not None:
                level = classification
            else:
                cat, sep, key = ref.partition("::")
                if sep:
                    level = self.labels.get_label(cat, key)
                else:
                    level = None
                if level is None:
                    level = "internal"
                    base["reason"] = "未打分级标签，按默认 internal 处理（建议先打标）"
            base["classification"] = level
            if level not in _REQUIRED_PERM:
                base["reason"] = "未知分级 %s，拒绝出域" % level
                self._deny(ref, dest, level, base["reason"])
                return base
            need = _REQUIRED_PERM[level]
            if need is None:
                base["allowed"] = True
                if not base["reason"]:
                    base["reason"] = "%s 级可自由出域（无需出域审批）" % level
                return base
            # 2) 需批准：必须有 status=approved、item_ref 匹配、目标匹配、
            #    且审批权限等级 >= 该分级要求（confidential 必须 emergency 级）。
            data = self._load()          # 异常 → 下方 except 兜底拒绝
            for rid, rec in data.get("requests", {}).items():
                if rec.get("status") != "approved":
                    continue
                if rec.get("item_ref") != ref:
                    continue
                ok_dest = (rec.get("destination") in ("*", dest) or dest == "*")
                if not ok_dest:
                    continue
                if rec.get("required_permission") != need:
                    continue
                base["allowed"] = True
                base["approval_id"] = rid
                base["reason"] = "%s 级出域已有有效批准（%s，审批角色 %s）" % (
                    level, rid, rec.get("decision_role") or "未知")
                return base
            base["reason"] = ("%s 级出域无有效批准（需 %s 权限审批）——fail-closed 拒绝"
                              % (level, need))
            self._deny(ref, dest, level, base["reason"])
            return base
        except Exception as e:  # noqa: BLE001 - fail-closed：任何异常都拒绝
            base["reason"] = "出域校验异常，fail-closed 拒绝：%s" % e
            return base

    def _deny(self, ref: str, dest: str, level: str, reason: str) -> None:
        """拒绝留痕（旁路：留痕失败不影响"拒绝"这一结论本身）。"""
        try:
            data = self._load()
            denials = data.setdefault("denials", [])
            denials.append({"item_ref": ref, "destination": dest,
                            "classification": level, "reason": reason,
                            "at": time.time()})
            data["denials"] = denials[-50:]
            self._state.write(data)
        except Exception:  # noqa: BLE001
            pass

    def list_requests(self, status: Optional[str] = None) -> List[Dict]:
        data = self._load()
        out = list(data.get("requests", {}).values())
        if status:
            out = [r for r in out if r.get("status") == status]
        return sorted(out, key=lambda r: r.get("requested_at", 0), reverse=True)

    def state(self) -> Dict:
        data = self._load()
        return {"requests": data.get("requests", {}),
                "denials": data.get("denials", []),
                "updated_at": data.get("updated_at"), "path": self.path}


# --------------------------------------------------------------------------
# 模块级便捷函数（供 daemon 出域门 / 控制台直接调用）
# --------------------------------------------------------------------------
def is_allowed(item_ref, destination: str = "*", classification: Optional[str] = None,
               egress_path: str = DEFAULT_EGRESS_PATH,
               kb_path: str = DEFAULT_KB_PATH) -> Dict:
    return EgressApproval(egress_path, KBLabelStore(kb_path)).is_allowed(
        item_ref, destination=destination, classification=classification)


def check_egress(refs, destination: str = "*",
                 egress_path: str = DEFAULT_EGRESS_PATH,
                 kb_path: str = DEFAULT_KB_PATH) -> Dict:
    """批量出域判定（daemon 用）：任一不允许即整体拒绝。

    refs 为空但声明了 egress_destination 时，用 "*" 作为"任务产出整体"的引用。
    返回 {ok, decision:{...} 或 failed:[...]}；ok=False 时调用方应挂起 needs_human。
    """
    appr = EgressApproval(egress_path, KBLabelStore(kb_path))
    if isinstance(refs, str):
        refs = [refs]
    targets = [r for r in (refs or []) if r]
    if not targets:
        targets = ["*"]       # 仅声明外发目标、未列具体条目
    failed = []
    for ref in targets:
        d = appr.is_allowed(ref, destination=destination or "*")
        if not d.get("allowed"):
            failed.append(d)
    if failed:
        return {"ok": False, "failed": failed,
                "reason": "；".join("%s → %s：%s" % (f["item_ref"], f["destination"],
                                                    f["reason"]) for f in failed)}
    return {"ok": True, "failed": [], "reason": "出域审批通过（%d 条）" % len(targets)}
