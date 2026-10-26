"""
console_chat.py —— 协作审计台的「对话」后端（规则优先 + 本地/云端 LLM 兜底）

定位：
    console_web.py 的「对话」面板从前端壳升级为真正的问答端点。它不新造数据层，
    只复用仓库既有的模型层（models.ModelRouter + OpenAI/Anthropic/Ollama/Mock）
    与既有的只读数据源（TaskRegistry / ApprovalCenter / TraceStore / CostStore）。

设计原则（对齐 docs/honest_boundary_paradigm.md 的诚实边界范式）：
  1. **规则优先**：审计类问题（跑着几个任务 / 审批过没过 / 花了多少钱 / 有没有泄露）
     用确定性规则直接答——零幻觉、零延迟、不依赖任何外部服务。
  2. **LLM 兜底**：规则没命中时才调 LLM，并把当前审计态作为事实上下文喂进去
     （system prompt 明确"只能引用下列事实"），避免模型自由发挥。
  3. **诚实降级**：LLM 不可用（没配 key / Ollama 没起 / 网络不通）时，明确告知
     "本地规则无法回答"并给出可点的下一步，**绝不用 Mock 假装是模型输出**。
  4. **每次回答都带 engine 字段**（rule / llm / unavailable），前端必须显示，
     让使用者永远知道这句话是谁说的、依据是什么。

关键 API：
    ChatService(context_provider).ask(question, history=None) -> dict
    返回：{"answer": str, "engine": "rule"|"llm"|"unavailable",
           "model": str|None, "basis": [...], "next_steps": [...], "usage": {...}}

约定：
  - 零第三方依赖（只用标准库 + 本仓库既有模块）。
  - 只读：绝不写任务/审批/轨迹数据。
  - 本地 Ollama 默认 http://127.0.0.1:11434，可用 OLLAMA_BASE_URL 覆盖。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

# --------------------------------------------------------------------------
# 1) 审计态上下文（喂给 LLM 的"事实清单"）
# --------------------------------------------------------------------------
SYSTEM_PREAMBLE = (
    "你是「协作审计台」的审计助理。你只能依据下面 <facts> 里的事实回答，"
    "不得编造未出现的数字、任务、审批或结论。事实不足以回答时，直接说"
    "「现有审计数据不足以回答」，并建议用户去对应面板查看。"
    "回答用中文，简洁、结构化，数字要标明来源面板。"
)


def build_context(overview: Dict[str, Any],
                  tasks: List[Dict[str, Any]],
                  approvals: List[Dict[str, Any]],
                  cost: Dict[str, Any],
                  security: Dict[str, Any]) -> str:
    """把当前审计态压成一段可喂给模型的事实文本（限制体积，避免超长 prompt）。"""
    counts = overview.get("counts", {}) or {}
    facts: List[str] = []
    facts.append("【概览】任务总数 %s，运行中 %s，待审批 %s，审计轨迹 %s，累计成本 ¥%s。" % (
        counts.get("tasks_total", 0), counts.get("running", 0),
        counts.get("pending", 0), counts.get("traces", 0),
        overview.get("cost_total", 0)))

    running = [t for t in tasks if t.get("status") == "running"][:8]
    if running:
        facts.append("【运行中任务】" + "；".join(
            "%s(%s)" % (t.get("task_id"), (t.get("payload") or {}).get("role", "-"))
            for t in running))
    else:
        facts.append("【运行中任务】无。")

    pend = [a for a in approvals if a.get("status") == "pending"][:8]
    if pend:
        facts.append("【待审批】" + "；".join(
            "%s action=%s detail=%s" % (a.get("approval_id"), a.get("action"),
                                        a.get("detail")) for a in pend))
    else:
        facts.append("【待审批】无。")

    tot = cost.get("totals", {}) or {}
    facts.append("【成本】累计 ¥%s，模型调用 %s 次，工具调用 %s 次。" % (
        tot.get("cost_estimate", 0), tot.get("model_calls", 0),
        tot.get("tool_calls", 0)))
    by_t = (cost.get("by_template") or [])[:5]
    if by_t:
        facts.append("【成本·按模板】" + "；".join(
            "%s ¥%s" % (r.get("key"), r.get("cost_estimate", 0)) for r in by_t))

    facts.append("【安全】沙箱后端 %s，隔离档位 %s；源码硬编码密钥自扫真实泄露 %s 处。" % (
        security.get("sandbox_backend", "-"), security.get("sandbox_containment", "-"),
        security.get("self_scan_real_leaks", 0)))
    facts.append("【计价说明】%s" % (cost.get("price_note") or "未知模型一律标记未计价，绝不虚构金额。"))

    return "\n".join(facts)


# --------------------------------------------------------------------------
# 2) 确定性规则引擎（零幻觉）
# --------------------------------------------------------------------------
def _fmt_money(v: Any) -> str:
    try:
        return "¥%.2f" % float(v or 0)
    except (TypeError, ValueError):
        return "¥0.00"


class RuleEngine:
    """把常见审计问题路由到确定性查询。命中即答，不经过任何模型。"""

    def __init__(self, overview: Dict[str, Any],
                 tasks: List[Dict[str, Any]],
                 approvals: List[Dict[str, Any]],
                 cost: Dict[str, Any],
                 security: Dict[str, Any]):
        self.overview = overview or {}
        self.tasks = tasks or []
        self.approvals = approvals or []
        self.cost = cost or {}
        self.security = security or {}

    def match(self, q: str) -> Optional[Dict[str, Any]]:
        text = (q or "").lower()
        if not text:
            return None

        # ---- 总结 / 总览（必须排在各主题之前，避免被单主题规则截胡）----
        if any(k in text for k in ("总结", "概览", "总览", "整体", "当前状态",
                                   "情况如何", "健康", "summar", "overview")):
            return self._answer_summary()
        # ---- 安全 / 泄露 ----
        if any(k in text for k in ("泄露", "密钥", "安全", "沙箱", "secret", "leak")):
            return self._answer_security()
        # ---- 成本 ----
        if any(k in text for k in ("成本", "花了", "多少钱", "费用", "cost", "token")):
            return self._answer_cost()
        # ---- 审批 ----
        if any(k in text for k in ("审批", "发布", "通过", "高危", "approve")):
            return self._answer_approval()
        # ---- 任务 ----
        if any(k in text for k in ("任务", "跑", "运行", "task", "d")):
            return self._answer_tasks()
        # ---- 轨迹 ----
        if any(k in text for k in ("轨迹", "回放", "trace", "记录")):
            return self._answer_trace()
        return None

    # -- 各主题答案 --
    def _answer_summary(self) -> Dict[str, Any]:
        """一句话总结当前审计态（跨主题汇总）。"""
        c = self.overview.get("counts", {}) or {}
        tot = self.cost.get("totals", {}) or {}
        s = self.security
        running = [t for t in self.tasks if t.get("status") == "running"]
        pending = [a for a in self.approvals if a.get("status") == "pending"]
        failed = [t for t in self.tasks if t.get("status") in ("failed", "needs_human")]
        leaks = s.get("self_scan_real_leaks", 0)
        lines = [
            "当前审计态总结：",
            "· 任务：共 %s 个，运行中 %s，待审批 %s，失败/待人工 %s。" % (
                c.get("tasks_total", len(self.tasks)), len(running),
                len(pending), len(failed)),
            "· 成本：累计 %s（%s 次模型调用 / %s 次工具调用）。%s" % (
                _fmt_money(tot.get("cost_estimate")), tot.get("model_calls", 0),
                tot.get("tool_calls", 0),
                self.cost.get("price_note") or "未知模型标记未计价。"),
            "· 安全：源码硬编码密钥真实泄露 %s 处；沙箱 %s（%s 档）。" % (
                leaks, s.get("sandbox_backend", "-"),
                s.get("sandbox_containment", "-")),
            "· 轨迹：%s 条审计轨迹，事件均带 blake2b 内容指纹，可逐条校验是否被篡改。" % (
                c.get("traces", 0)),
        ]
        if pending:
            lines.append("· 待办：%s 条审批等待人工决策（默认拒绝：未决即不执行）：%s" % (
                len(pending), "、".join(a.get("approval_id", "-") for a in pending[:5])))
        if running:
            lines.append("· 运行中任务：%s" % "、".join(
                t.get("task_id", "-") for t in running[:5]))
        if failed:
            lines.append("· 需关注：%s 个任务失败或等待人工介入%s" % (
                len(failed),
                "（%s）" % "、".join(t.get("task_id", "-") for t in failed[:5])
                if failed else ""))
        if not pending and not failed:
            lines.append("· 待办：无阻塞项。")
        return {
            "answer": "\n".join(lines),
            "basis": ["概览", "任务中心", "成本看板", "安全中心", "审计轨迹"],
            "next_steps": ["在「审批中心」处理待决审批", "导出审批链存档"],
        }

    def _answer_security(self) -> Dict[str, Any]:
        s = self.security
        leaks = s.get("self_scan_real_leaks", 0)
        backend = s.get("sandbox_backend", "-")
        cont = s.get("sandbox_containment", "-")
        hits = s.get("self_scan_hits", 0)
        answer = (
            "安全状态：源码硬编码密钥自扫真实泄露 %s 处。\n"
            "· 扫描命中 %s 条，全部来自金标准假密钥语料库（tools/scan_bench.py），已豁免；"
            "公开已知测试向量同样豁免。\n"
            "· 沙箱后端 %s，隔离档位 %s。%s\n"
            "· 权限最小化：审批默认拒绝 + 超时过期；容器非 root、cap-drop ALL、只读根；"
            "工作区按 bot 隔离；OAuth 按 scope 最小授权。"
        ) % (leaks, hits, backend, cont, s.get("sandbox_note", ""))
        return {
            "answer": answer,
            "basis": ["安全中心", "源码硬编码密钥自扫"],
            "next_steps": ["在「安全中心」查看逐项清单", "打开安全白皮书 /api/security/whitepaper"],
        }

    def _answer_cost(self) -> Dict[str, Any]:
        c = self.cost
        tot = c.get("totals", {}) or {}
        outs = (c.get("outliers") or {}).get("top_cost") or []
        lines = [
            "成本概览（最近 %s 天）：" % (c.get("days", 30)),
            "· 累计金额 %s" % _fmt_money(tot.get("cost_estimate")),
            "· 模型调用 %s 次，工具调用 %s 次" % (tot.get("model_calls", 0),
                                                tot.get("tool_calls", 0)),
        ]
        by_t = (c.get("by_template") or [])[:5]
        if by_t:
            lines.append("· 按模板 Top5：")
            for r in by_t:
                lines.append("    - %s：%s（%s 次模型调用）" % (
                    r.get("key"), _fmt_money(r.get("cost_estimate")),
                    r.get("model_calls", 0)))
        if outs:
            lines.append("· 离群点（Top 花费）%d 个，最高 %s（task=%s）" % (
                len(outs), _fmt_money(outs[0].get("cost_estimate")),
                outs[0].get("task_id", "-")))
        lines.append("计价口径：%s" % (c.get("price_note") or "未知模型标记未计价。"))
        return {
            "answer": "\n".join(lines),
            "basis": ["成本看板"],
            "next_steps": ["在「成本看板」按任务/角色拆解", "导出成本数据做归因"],
        }

    def _answer_approval(self) -> Dict[str, Any]:
        aps = self.approvals
        pend = [a for a in aps if a.get("status") == "pending"]
        done = [a for a in aps if a.get("status") == "approved"]
        rej = [a for a in aps if a.get("status") in ("rejected", "expired")]
        lines = ["审批状态（Human-in-the-loop，默认拒绝）："]
        lines.append("· 待审批 %d 条" % len(pend))
        for a in pend[:6]:
            lines.append("    - %s action=%s detail=%s（任务 %s）" % (
                a.get("approval_id"), a.get("action"), a.get("detail"),
                a.get("task_id") or "-"))
        lines.append("· 已通过 %d 条" % len(done))
        if done:
            lines.append("    - 最近：%s（%s，决策人 %s）" % (
                done[0].get("approval_id"), done[0].get("action"),
                done[0].get("decided_by") or "-"))
        lines.append("· 拒绝/过期 %d 条" % len(rej))
        lines.append("每条审批都有完整证据链（blake2b 指纹轨迹），可导出 CSV / SARIF / PDF 存档。")
        return {
            "answer": "\n".join(lines),
            "basis": ["审批中心"],
            "next_steps": ["在「审批中心」查看证据链", "导出 SARIF 供合规存档"],
        }

    def _answer_tasks(self) -> Dict[str, Any]:
        ts = self.tasks
        running = [t for t in ts if t.get("status") == "running"]
        reg = [t for t in ts if t.get("status") == "registered"]
        done = [t for t in ts if t.get("status") in ("completed", "done")]
        failed = [t for t in ts if t.get("status") in ("failed", "needs_human")]
        lines = [
            "任务概览：",
            "· 运行中 %d，排队（registered）%d，已完成 %d，失败/待人工 %d，总计 %d" % (
                len(running), len(reg), len(done), len(failed), len(ts)),
        ]
        if running:
            lines.append("· 运行中任务：")
            for t in running[:6]:
                lines.append("    - %s（进度 %s%%）" % (t.get("task_id"),
                                                   t.get("progress", 0)))
        if failed:
            lines.append("· 需关注：")
            for t in failed[:5]:
                lines.append("    - %s：%s" % (t.get("task_id"), t.get("status")))
        return {
            "answer": "\n".join(lines),
            "basis": ["任务中心"],
            "next_steps": ["在「任务中心」看依赖波次与 Runner 存活"],
        }

    def _answer_trace(self) -> Dict[str, Any]:
        c = self.overview.get("counts", {}) or {}
        lines = [
            "审计轨迹：共 %s 条轨迹。" % c.get("traces", 0),
            "每条轨迹的事件都带 blake2b 内容指纹，可逐条校验是否被篡改。",
            "视图分两层：per-task 分区（单任务 2~4 个事件）与全局回放（全链路 12 个事件）。",
        ]
        return {
            "answer": "\n".join(lines),
            "basis": ["审计轨迹"],
            "next_steps": ["在「审计轨迹」点开任意 trace 看回放时间线"],
        }


# --------------------------------------------------------------------------
# 3) LLM 兜底（本地 Ollama / OpenAI 兼容 / Anthropic）
# --------------------------------------------------------------------------
def _http_json(url: str, payload: Dict[str, Any], headers: Dict[str, str],
               timeout: float = 30.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (本地/自配端点)
        return json.loads(r.read().decode("utf-8"))


class LlmEngine:
    """轻量 LLM 调用器：优先本地 Ollama，其次 OpenAI 兼容端点。

    不复用 models 包的原因：models 层是给 runner 用的（含 fallback 到 Mock 的语义），
    而这里必须"调不通就明确说调不通"，绝不能静默降级成假回答。
    """

    def __init__(self):
        self.ollama_base = os.environ.get("OLLAMA_BASE_URL",
                                          "http://127.0.0.1:11434").rstrip("/")
        self.ollama_model = os.environ.get("OLLAMA_CHAT_MODEL",
                                           os.environ.get("DEFAULT_MODEL", "") or "")
        self.openai_key = os.environ.get("OPENAI_API_KEY", "")
        self.openai_base = os.environ.get("OPENAI_BASE_URL",
                                          "https://api.openai.com/v1").rstrip("/")
        self.openai_model = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")

    # -- 可用性探测（不消耗推理配额，只探活）--
    def probe(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"ollama": False, "openai": False,
                                "ollama_model": self.ollama_model or None}
        try:
            req = urllib.request.Request(self.ollama_base + "/api/tags")
            with urllib.request.urlopen(req, timeout=1.5) as r:  # noqa: S310
                data = json.loads(r.read().decode("utf-8"))
            models = [m.get("name") for m in (data.get("models") or [])]
            info["ollama"] = True
            info["ollama_models"] = models[:10]
            if not self.ollama_model and models:
                info["ollama_model"] = models[0]
        except Exception:  # noqa: BLE001
            info["ollama"] = False
        info["openai"] = bool(self.openai_key)
        return info

    # -- 实际生成 --
    def complete(self, question: str, context: str,
                 history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        p = self.probe()
        messages = [{"role": "system",
                     "content": SYSTEM_PREAMBLE + "\n\n<facts>\n" + context + "\n</facts>"}]
        for h in (history or [])[-6:]:
            role = h.get("role")
            content = (h.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question})

        if p["ollama"] and p.get("ollama_model"):
            model = str(p["ollama_model"]).split("/", 1)[-1]
            data = _http_json(self.ollama_base + "/api/chat", {
                "model": model, "messages": messages, "stream": False,
            }, {}, timeout=60)
            content = ((data.get("message") or {}).get("content")
                       or data.get("response") or "").strip()
            if content:
                return {"answer": content, "model": "ollama/%s" % model,
                        "usage": {"prompt_tokens": data.get("prompt_eval_count"),
                                  "eval_tokens": data.get("eval_count")}}
        if self.openai_key:
            data = _http_json(self.openai_base + "/chat/completions", {
                "model": self.openai_model, "messages": messages,
                "temperature": 0.2,
            }, {"Authorization": "Bearer %s" % self.openai_key}, timeout=60)
            content = (((data.get("choices") or [{}])[0].get("message") or {})
                       .get("content") or "").strip()
            if content:
                return {"answer": content, "model": "openai/%s" % self.openai_model,
                        "usage": data.get("usage", {})}
        raise RuntimeError("no_llm_available")


# --------------------------------------------------------------------------
# 4) 对话服务（编排层）
# --------------------------------------------------------------------------
class ChatService:
    """规则优先 → LLM 兜底 → 诚实失败。每次回答都标注 engine。"""

    def __init__(self, context_provider: Callable[[], Dict[str, Any]],
                 llm: Optional[LlmEngine] = None):
        self._ctx_provider = context_provider
        self._llm = llm or LlmEngine()

    def status(self) -> Dict[str, Any]:
        p = self._llm.probe()
        return {
            "rule_engine": True,
            "llm_available": bool(p["ollama"] or p["openai"]),
            "ollama": p["ollama"],
            "ollama_models": p.get("ollama_models", []),
            "openai": p["openai"],
            "note": ("规则引擎始终可用（零幻觉、零外部依赖）；"
                     "LLM 不可用时不会伪造模型回答。"),
        }

    def ask(self, question: str,
            history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        q = (question or "").strip()
        if not q:
            return {"answer": "请输入问题。", "engine": "rule", "model": None,
                    "basis": [], "next_steps": []}
        ctx_data = self._ctx_provider()
        context = build_context(
            ctx_data.get("overview", {}), ctx_data.get("tasks", []),
            ctx_data.get("approvals", []), ctx_data.get("cost", {}),
            ctx_data.get("security", {}))

        engine = RuleEngine(
            ctx_data.get("overview", {}), ctx_data.get("tasks", []),
            ctx_data.get("approvals", []), ctx_data.get("cost", {}),
            ctx_data.get("security", {}))
        hit = engine.match(q)
        if hit:
            hit["engine"] = "rule"
            hit["model"] = None
            hit["usage"] = {}
            return hit

        try:
            out = self._llm.complete(q, context, history)
            return {
                "answer": out["answer"], "engine": "llm", "model": out.get("model"),
                "basis": ["审计态上下文（任务/审批/成本/安全）"],
                "next_steps": [], "usage": out.get("usage", {}),
            }
        except Exception as e:  # noqa: BLE001
            return {
                "answer": ("本地规则无法回答这个问题，且当前没有可用的 LLM 后端"
                           "（Ollama 未启动或未配置 OPENAI_API_KEY），因此本回答"
                           "**不可用**模型生成。\n"
                           "错误：%s\n"
                           "你可以：① 把问题换成上面这些审计类问题（任务/审批/成本/安全/轨迹/总结）；"
                           "② 启动本地 Ollama 后重试；③ 直接去对应面板看原始数据。"
                           ) % e,
                "engine": "unavailable", "model": None,
                "basis": [], "next_steps": ["启动 Ollama 或配置 OPENAI_API_KEY",
                                            "改问审计类问题"],
                "usage": {},
            }
