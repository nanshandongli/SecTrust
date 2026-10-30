"""
reflection.py —— 「从防守到智能」智能运行时演进：自我反思闭环 + 分层记忆 + 模型自适应

三者在 agentskills 运行时内串成一条**可验证的闭环**：

  方向一·自我反思闭环
    ErrorInfo(结构化错误) + cached_params(参数上下文) + Trace(执行轨迹)
      → 错误归因(工具失败 / 参数缺失 / 权限不足)
      → 修正假设(LLM 生成，诚实降级规则式)
      → 经验库持久化(data/experience.json)
      → 下次相似 query 命中即预填 router / feedback（经验复用）。

  方向二·分层记忆
    工作记忆(已有 SkillStateStore.cached_params) +
    情景记忆(成功/失败轨迹结构化落盘，源自 Trace) +
    程序记忆(从历史 trace 提炼工具组合 / 参数模式)。
    关键：方向一产生的经验必须经本层可被后续 query 检索 → 闭环，不是只存不用。

  方向三·模型自适应升级(Confidence Loop)
    先用便宜/小模型跑 LLM rerank，用「排序一致性」作 confidence proxy；
    confidence 不够则升级到更强模型。与竞争力 gate 同哲学（能省则省，该花则花）。
    区别：gate 决定「要不要 rerank」，Confidence Loop 决定「要不要换模型」。

诚实边界（不可破防，与全仓一致）：
  · 无模型 / 调不通 → 整体降级到规则式修正假设，绝不 Mock 假数据。
  · ExperienceStore 落盘失败(只读/损坏) → 退化为内存态，不抛异常阻断执行。
  · 经验匹配是「提示」不是「自动改道」：命中的修正方案仍要过检索 + 角色白名单
    + 门禁，不因「上次经验」绕过任何安全。
  · 分层记忆的「程序记忆」是频率统计，不宣称神经/因果推断。
  · Confidence Loop 不编造模型能力：无分级配置时退化为普通 rerank。

零第三方依赖：仅标准库（经验匹配用字符二元组 + 拉丁词项 Jaccard，不依赖 BM25/向量）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# 错误归因分类
# ---------------------------------------------------------------------------
CAT_TOOL_FAILURE = "tool_failure"                  # 工具/脚本执行失败、传输/上游错误、质量违例
CAT_MISSING_PARAM = "missing_param"                # 缺参数 / 缺证据 / 占位符未填
CAT_INSUFFICIENT_PERMISSION = "insufficient_permission"  # 审批挂起 / 安全策略 / 凭证缺失 / 域名拦截
CAT_UNKNOWN = "unknown"

# stage → 大类（归因表，Skill 侧 + MCP 侧共用语义）
_STAGE_TO_CATEGORY = {
    "sandbox": CAT_TOOL_FAILURE, "transport": CAT_TOOL_FAILURE,
    "upstream": CAT_TOOL_FAILURE, "quality": CAT_TOOL_FAILURE,
    "evidence": CAT_MISSING_PARAM, "artifacts": CAT_MISSING_PARAM,
    "approval": CAT_INSUFFICIENT_PERMISSION, "tier_escalation": CAT_INSUFFICIENT_PERMISSION,
    "domain": CAT_INSUFFICIENT_PERMISSION, "credential": CAT_INSUFFICIENT_PERMISSION,
    "secret_scan_in": CAT_INSUFFICIENT_PERMISSION, "secret_scan_out": CAT_INSUFFICIENT_PERMISSION,
    "registry": CAT_UNKNOWN, "install": CAT_UNKNOWN, "l2": CAT_UNKNOWN,
    "pii_check": CAT_UNKNOWN,
}

# 显式 error_code → 大类（覆盖 stage 映射不到的情况，如 MISSING_PARAM）
_ERROR_CODE_TO_CATEGORY = {
    "SANDBOX_FAILED": CAT_TOOL_FAILURE, "TRANSPORT_ERROR": CAT_TOOL_FAILURE,
    "UPSTREAM_ERROR": CAT_TOOL_FAILURE, "QUALITY_VIOLATION": CAT_TOOL_FAILURE,
    "EVIDENCE_MISSING": CAT_MISSING_PARAM, "ARTIFACT_MISSING": CAT_MISSING_PARAM,
    "MISSING_PARAM": CAT_MISSING_PARAM,
    "APPROVAL_PENDING": CAT_INSUFFICIENT_PERMISSION,
    "SECURITY_POLICY_VIOLATION": CAT_INSUFFICIENT_PERMISSION,
    "DOMAIN_NOT_ALLOWED": CAT_INSUFFICIENT_PERMISSION,
    "CREDENTIAL_UNAVAILABLE": CAT_INSUFFICIENT_PERMISSION,
    "SECRET_DETECTED": CAT_INSUFFICIENT_PERMISSION,
    "UNKNOWN_SKILL": CAT_UNKNOWN, "INSTALL_INVALID": CAT_UNKNOWN,
    "L2_LOAD_FAILED": CAT_UNKNOWN, "PII_OUTPUT_DETECTED": CAT_UNKNOWN,
    "SERVER_UNKNOWN": CAT_UNKNOWN, "CIRCUIT_OPEN": CAT_UNKNOWN,
    "UNKNOWN": CAT_UNKNOWN,
}


def classify_error(error_info: Optional[Dict]) -> Dict:
    """把 ErrorInfo 字典收敛成 {category, error_code, stage, technical_detail}。

    无 error_info 或字段缺失 → 归 unknown，不编造。
    """
    if not error_info or not isinstance(error_info, dict):
        return {"category": CAT_UNKNOWN, "error_code": "UNKNOWN",
                "stage": "", "technical_detail": ""}
    code = str(error_info.get("error_code") or "")
    stage = str(error_info.get("stage") or "")
    tech = str(error_info.get("technical_detail") or "")
    if code in _ERROR_CODE_TO_CATEGORY:
        cat = _ERROR_CODE_TO_CATEGORY[code]
    elif stage in _STAGE_TO_CATEGORY:
        cat = _STAGE_TO_CATEGORY[stage]
    else:
        cat = CAT_UNKNOWN
    return {"category": cat, "error_code": code, "stage": stage,
            "technical_detail": tech}


def _normalize_query(query: str) -> str:
    """query → 规范化 token 串（小写、去标点、去空白折叠），用于相似度匹配与去重。"""
    q = str(query or "").lower()
    q = re.sub(r"[\s\u3000]+", " ", q)
    q = re.sub(r"[^\w\u4e00-\u9fff]+", " ", q)
    return " ".join(t for t in q.split() if t)


def _query_signature(query: str) -> str:
    toks = sorted(set(_normalize_query(query).split()))
    return " ".join(toks)


def _match_tokens(text: str) -> set:
    """匹配用的 token 集：拉丁/数字词项 + CJK 字符二元组（跨词、抗无空格）。

    纯标准库、确定性强。「清理磁盘」与「帮我清理电脑释放磁盘空间」共享
    「清理」「磁盘」等二元组 → 相似 query 可命中经验（方向一闭环的关键）。
    """
    t = str(text or "").lower()
    latin = set(re.findall(r"[a-z0-9_]+", t))
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", t)
    grams = set()
    for run in cjk_runs:
        if len(run) == 1:
            grams.add(run)
        else:
            grams.update(run[i:i + 2] for i in range(len(run) - 1))
    return latin | grams


# ---------------------------------------------------------------------------
# 经验库存储（方向一 + 方向二的结构化落盘）
# ---------------------------------------------------------------------------
DEFAULT_EXPERIENCE_PATH = "data/experience.json"
# 长期运行容量护栏：经验/情景/程序都设上限，防止单个 JSON 无限膨胀拖慢落盘与匹配。
# 淘汰策略：经验按「复用次数 + 新旧」保留高价值；情景按时间保留最近。
MAX_EXPERIENCES = 300
MAX_EPISODES = 500
MAX_PROCEDURE_SKILLS = 200


class ExperienceStore:
    """经验库 + 情景记忆 + 程序记忆的持久化层（单一 JSON 文件，进程无关）。

    诚实边界：落盘失败(只读/损坏/磁盘满) → 退化为内存态，不抛异常、不阻断执行。
    """

    def __init__(self, path: str = DEFAULT_EXPERIENCE_PATH, auto_load: bool = True):
        self.path = path
        self._lock = threading.RLock()
        self._data = {"experiences": [], "episodes": [], "procedures": {},
                      "updated_at": 0.0}
        if auto_load:
            self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                if isinstance(d, dict):
                    for k in ("experiences", "episodes", "procedures", "updated_at"):
                        if k in d:
                            self._data[k] = d[k]
        except Exception:  # noqa: BLE001 - 损坏/只读 → 保留内存态
            pass

    def _save(self) -> bool:
        try:
            parent = os.path.dirname(self.path) or "."
            os.makedirs(parent, exist_ok=True)
            tmp = "%s.tmp" % self.path
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._data["updated_at"] = time.time()
            return True
        except Exception:  # noqa: BLE001 - 落盘失败不阻断
            return False

    # ---- 经验（方向一）----
    def add_experience(self, rec: Dict) -> Dict:
        """新增/合并一条经验（按 id 去重，存在则累加 used_count）。"""
        rid = rec.get("id") or self._make_id(rec)
        rec["id"] = rid
        with self._lock:
            for i, e in enumerate(self._data["experiences"]):
                if e.get("id") == rid:
                    merged = dict(e)
                    merged["used_count"] = int(e.get("used_count", 0))
                    merged["success_count"] = int(e.get("success_count", 0))
                    merged.update({k: rec[k] for k in rec if k not in
                                   ("used_count", "success_count", "id")})
                    self._data["experiences"][i] = merged
                    self._save()
                    return merged
            self._data["experiences"].append(rec)
            self._prune_experiences()
            self._save()
            return rec

    def _prune_experiences(self) -> None:
        """超上限时淘汰低价值经验：优先保留「复用多、命中成功多、较新」的。

        诚实边界：淘汰是存储层容量护栏，不是「判定经验无效」——被淘汰只是
        因低频/陈旧腾位置，不宣称其修正假设错误。绝不淘汰刚写入的新经验。

        实现要点（历史 bug 2026-10-31 修）：刚写入的经验在 exps[-1]，必须
        **显式留位**。旧写法先按价值排序取 topN、再对原列表做 [:MAX] 切片，
        末尾的新经验会被切片截掉——即使 keep 集合里补了 fresh_id 也没用。
        正确做法：从「旧经验」里挑 (MAX-1) 个最高价值，最后把新经验追加回
        末尾，既保证总数不超上限，又保证刚写入的必留。
        """
        exps = self._data["experiences"]
        if len(exps) <= MAX_EXPERIENCES:
            return
        fresh = exps[-1]
        rest = exps[:-1]

        def value(e):
            return (int(e.get("used_count", 0)) + int(e.get("success_count", 0)),
                    float(e.get("created_at", 0.0)))

        top = sorted(rest, key=value, reverse=True)[:MAX_EXPERIENCES - 1]
        top_ids = {e.get("id", "") for e in top}
        kept = [e for e in rest if e.get("id", "") in top_ids]
        if len(kept) > MAX_EXPERIENCES - 1:   # 防御：损坏数据含重复 id
            kept = kept[-(MAX_EXPERIENCES - 1):]
        kept.append(fresh)   # 刚写入的必留，且位于末尾，不会被切片截掉
        self._data["experiences"] = kept

    @staticmethod
    def _make_id(rec: Dict) -> str:
        raw = "%s|%s|%s" % (rec.get("error_code", ""), rec.get("stage", ""),
                            rec.get("query_signature", ""))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def match_experience(self, query: str = "", error_info: Optional[Dict] = None,
                         limit: int = 3) -> Optional[Dict]:
        """经验复用：先按 error_code+stage 精确命中（最强），再按 query 相似度。

        返回 {"hit": <经验dict>, "score": float, "match_type": str} 或 None。
        """
        with self._lock:
            exps = list(self._data["experiences"])
        if not exps:
            return None
        # 1) 精确错误命中
        if error_info:
            code = str(error_info.get("error_code") or "")
            stage = str(error_info.get("stage") or "")
            if code:
                for e in exps:
                    if e.get("error_code") == code and e.get("stage") == stage:
                        return {"hit": e, "score": 1.0, "match_type": "error_exact"}
        # 2) query 相似度（传原文，_match_tokens 自行提取 CJK 二元组/拉丁词项）
        if query and _normalize_query(query):
            sims = self._query_sims(query, exps)
            if sims:
                top = max(sims, key=lambda x: x[1])
                if top[1] >= 0.2:
                    return {"hit": top[0], "score": round(top[1], 4),
                            "match_type": "query_similar"}
        return None

    def mark_used(self, exp_id: str, success: Optional[bool] = None) -> None:
        with self._lock:
            for e in self._data["experiences"]:
                if e.get("id") == exp_id:
                    e["used_count"] = int(e.get("used_count", 0)) + 1
                    if success is True:
                        e["success_count"] = int(e.get("success_count", 0)) + 1
                    self._save()
                    return

    def _query_sims(self, query: str, exps: List[Dict]) -> List[tuple]:
        """计算 query 与各经验的相似度（0~1 overlap 覆盖率），返回 [(exp, score), ...]。

        用「拉丁词项 + CJK 字符二元组」token 集，纯标准库、确定性强。
        用 overlap 覆盖率（|A∩B| / min(|A|,|B|)）而非 Jaccard：短查询往往是长
        查询的语义子集（「清理磁盘」vs「帮我清理电脑释放磁盘空间」），Jaccard 会
        因长短悬殊而低估；要求交集 ≥2 且较短方 ≥2 个 token，防单词噪声误命中。
        """
        q_toks = _match_tokens(query)
        if not q_toks:
            return []
        out = []
        for e in exps:
            e_toks = _match_tokens(e.get("query") or e.get("query_signature") or "")
            if not e_toks:
                continue
            inter = len(q_toks & e_toks)
            mn = min(len(q_toks), len(e_toks))
            if inter >= 2 and mn >= 2:
                out.append((e, inter / mn))   # overlap coefficient, 0~1
        return out

    # ---- 情景记忆（方向二）----
    def add_episode(self, ep: Dict) -> Dict:
        with self._lock:
            self._data["episodes"].append(ep)
            # 防止无限增长：只保留最近 MAX_EPISODES 条情景
            if len(self._data["episodes"]) > MAX_EPISODES:
                self._data["episodes"] = self._data["episodes"][-MAX_EPISODES:]
            self._save()
            return ep

    def recall_episodes(self, skill: Optional[str] = None,
                        ok: Optional[bool] = None, limit: int = 10) -> List[Dict]:
        with self._lock:
            eps = list(self._data["episodes"])
        if skill is not None:
            eps = [e for e in eps if e.get("skill") == skill]
        if ok is not None:
            eps = [e for e in eps if bool(e.get("ok")) == ok]
        eps.sort(key=lambda e: e.get("at", 0.0), reverse=True)
        return eps[:limit]

    # ---- 程序记忆（方向二，从历史 trace 提炼）----
    def add_procedure(self, skill: str, query: str, context: Optional[Dict] = None,
                      tools: Optional[Sequence[str]] = None) -> None:
        """成功执行后提炼程序记忆：**参数模式**（query token / task_type）+ **工具组合**
        （该技能 allowed_tools 的共现频率），两大类都从历史成功轨迹累积。

        用 _match_tokens（CJK 二元组 + 拉丁词项），与经验匹配同口径——中文无空格
        也能跨词命中（「清理磁盘」↔「帮我清理电脑释放磁盘空间」）。
        tools 只记工具名、不含任何参数值（避免敏感信息落库）。
        """
        ctx = dict(context or {})
        tags = set(_match_tokens(query))
        ttype = str(ctx.get("task_type") or "").strip()
        if ttype:
            tags.add("task:%s" % ttype)
        # 工具组合：以 "tool:<name>" 前缀入 tags，与 query token 同空间可共现检索
        tool_names = sorted({str(t).strip() for t in (tools or []) if str(t).strip()})
        for t in tool_names:
            tags.add("tool:%s" % t)
        if not tags:
            return
        with self._lock:
            procs = self._data.setdefault("procedures", {})
            # 容量护栏：技能数超上限时淘汰使用次数最低的（当前技能必留）
            if skill not in procs and len(procs) >= MAX_PROCEDURE_SKILLS:
                victim = min(procs.items(),
                             key=lambda kv: int(kv[1].get("count", 0)))[0]
                procs.pop(victim, None)
            entry = procs.setdefault(skill, {"count": 0, "tags": {}, "tools": {}})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry.setdefault("tools", {})
            for t in tags:
                entry["tags"][t] = int(entry["tags"].get(t, 0)) + 1
            for t in tool_names:
                entry["tools"][t] = int(entry["tools"].get(t, 0)) + 1
            self._save()

    def recall_procedures(self, query: str = "", limit: int = 5) -> List[Dict]:
        """根据 query token 召回「历史上成功用过的技能」及命中度，附带该技能历史上
        成功用到的**工具组合**（按频次降序）——即程序记忆的两类产物一起返回。"""
        q_toks = set(_match_tokens(query))
        if not q_toks:
            return []
        with self._lock:
            procs = dict(self._data.get("procedures", {}))
        scored = []
        for skill, entry in procs.items():
            tags = set(entry.get("tags", {}))
            if not tags:
                continue
            hit = len(q_toks & tags)
            if hit:
                tools = sorted(entry.get("tools", {}).items(),
                               key=lambda kv: (-kv[1], kv[0]))
                scored.append({"skill": skill, "hits": hit,
                               "count": int(entry.get("count", 0)),
                               "tools": [t for t, _ in tools]})
        scored.sort(key=lambda x: (-x["hits"], -x["count"]))
        return scored[:limit]

    def snapshot(self) -> Dict:
        with self._lock:
            return {
                "experiences": len(self._data["experiences"]),
                "episodes": len(self._data["episodes"]),
                "procedures": len(self._data.get("procedures", {})),
                "path": self.path,
            }


# ---------------------------------------------------------------------------
# 方向一·反思引擎
# ---------------------------------------------------------------------------
class ReflectionEngine:
    """把一次失败收敛成可复用经验（归因 → 生成修正假设 → 落库），并支持经验复用。"""

    def __init__(self, store: Optional[ExperienceStore] = None,
                 llm=None, model: Optional[str] = None):
        self.store = store or ExperienceStore()
        self.llm = llm
        self.model = model

    # ---- 反思：失败 → 经验 ----
    def reflect(self, query: str, error_info: Dict,
                context: Optional[Dict] = None) -> Dict:
        """一次失败 → 归因 → 修正假设 → 落库。

        返回 {category, correction, generated_by, experience_id, stored}。
        绝不 Mock：无模型 → 规则式修正。
        """
        cat = classify_error(error_info)
        correction, by = self._propose(query, cat, error_info, context)
        rec = self._make_record(query, cat, error_info, correction, by)
        stored = self.store.add_experience(rec)
        return {"category": cat["category"], "correction": correction,
                "generated_by": by, "experience_id": stored.get("id", ""),
                "stored": True}

    def _make_record(self, query, cat, error_info, correction, by) -> Dict:
        return {
            "id": "",  # 由 store 计算
            "category": cat["category"],
            "error_code": cat["error_code"],
            "stage": cat["stage"],
            "technical_detail": cat["technical_detail"],
            "query": str(query or ""),
            "query_signature": _query_signature(query),
            "correction": correction,
            "source_skill": str((error_info or {}).get("rule_id") or ""),
            "generated_by": by,
            "created_at": time.time(),
            "used_count": 0,
            "success_count": 0,
        }

    def _propose(self, query, cat, error_info, context) -> (str, str):
        """生成修正假设：优先 LLM（LlmEngine.complete 推理路径），失败/无则规则式。"""
        if self.llm is not None:
            try:
                prompt = self._build_prompt(query, cat, error_info, context)
                resp = self.llm.complete(prompt, context="", history=None,
                                         model=self.model)
                answer = ((resp or {}).get("answer", "") if isinstance(resp, dict)
                          else str(resp)) or ""
                corr = self._parse_correction(answer)
                if corr:
                    return corr, "llm"
            except Exception:  # noqa: BLE001 - LLM 不可用 → 诚实降级
                pass
        return self._rule_correction(cat, error_info), "rule"

    @staticmethod
    def _build_prompt(query, cat, error_info, context) -> str:
        ctx = dict(context or {})
        return (
            "你是一个技能运行时的自我反思模块。下面发生了一起技能执行失败，"
            "请基于错误推断**下一步修正策略**（换参数 / 换工具 / 补上下文），"
            "只输出一个 JSON 对象，不要任何额外文字：\n"
            "{\"correction\": \"给 AI 的修正提示，要具体可执行\", "
            "\"action\": \"change_param|switch_tool|add_context|ask_user|escalate\"}\n\n"
            "用户请求：%s\n失败分类：%s\n错误码：%s\n失败阶段：%s\n技术细节：%s\n"
            "已缓存参数键：%s\n"
            % (str(query or "")[:200], cat["category"], cat["error_code"],
               cat["stage"], cat["technical_detail"][:300],
               ", ".join(str(k) for k in ctx.get("cached_param_keys", [])) or "无"))

    @staticmethod
    def _parse_correction(text: str) -> str:
        """从 LLM 自由文本抠出第一个 JSON 对象里的 correction 字段。"""
        if not text:
            return ""
        t = text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
        if m:
            t = m.group(1).strip()
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e == -1 or e < s:
            return ""
        try:
            obj = json.loads(t[s:e + 1])
        except (ValueError, TypeError):
            return ""
        if not isinstance(obj, dict):
            return ""
        corr = str(obj.get("correction") or "").strip()
        return corr[:500]

    @staticmethod
    def _rule_correction(cat: Dict, error_info: Dict) -> str:
        """规则式降级：按分类给出诚实的修正提示（不编造根因）。"""
        tech = str((error_info or {}).get("technical_detail") or "")
        # 从技术细节里抠「field xxx」类的缺参信息
        m = re.search(r"field\s+([\w\u4e00-\u9fff]+)", tech)
        field = m.group(1) if m else ""
        if cat["category"] == CAT_MISSING_PARAM:
            if field:
                return "向用户追问缺失字段：%s（证据/参数未齐备前不要执行）" % field
            return "缺少必要参数或证据，向用户补充收集后再执行。"
        if cat["category"] == CAT_INSUFFICIENT_PERMISSION:
            if cat["error_code"] == "APPROVAL_PENDING":
                return "该操作需要人工审批，已挂起；请在审批中心批准后带 approval_id 重试。"
            if cat["error_code"] == "CREDENTIAL_UNAVAILABLE":
                return "所需凭证不可用，检查凭证配置（环境变量/密钥）后重试。"
            if cat["error_code"] == "DOMAIN_NOT_ALLOWED":
                return "请求域名不在白名单，检查目标地址或放宽域名策略。"
            return "权限/安全策略不足：确认角色授权或走审批流程后再试。"
        if cat["category"] == CAT_TOOL_FAILURE:
            return ("工具/脚本执行失败（阶段=%s）：检查技能依赖、命令路径与运行环境，"
                    "或改用备选技能。" % cat["stage"])
        return "执行失败原因不明，确认错误根因后再试。"

    # ---- 经验复用 ----
    def reuse(self, query: str = "", error_info: Optional[Dict] = None) -> Optional[Dict]:
        """经验复用：相似 query / 相同错误 → 命中即返回修正方案，供 router/feedback 预填。"""
        hit = self.store.match_experience(query=query, error_info=error_info)
        if hit is None:
            return None
        exp = hit["hit"]
        return {"correction": exp.get("correction", ""),
                "category": exp.get("category", ""),
                "error_code": exp.get("error_code", ""),
                "stage": exp.get("stage", ""),
                "generated_by": exp.get("generated_by", ""),
                "score": hit["score"], "match_type": hit["match_type"],
                "experience_id": exp.get("id", "")}


# ---------------------------------------------------------------------------
# 方向二·分层记忆
# ---------------------------------------------------------------------------
class LayeredMemory:
    """工作记忆(引用 cached_params) + 情景记忆(落盘) + 程序记忆(提炼)。

    三层的「写」都走同一个 ExperienceStore（进程无关落盘）；「读」分别暴露。
    方向一产生的经验必须经本层可被后续 query 检索 → 闭环。
    """

    def __init__(self, store: Optional[ExperienceStore] = None):
        self.store = store or ExperienceStore()

    def remember_episode(self, skill: str, query: str, ok: bool,
                         error_info: Optional[Dict] = None,
                         context: Optional[Dict] = None,
                         tools: Optional[Sequence[str]] = None) -> Dict:
        """记录一条执行情景（成功/失败轨迹），成功则顺带提炼程序记忆。

        tools = 该技能 allowed_tools（成功轨迹里真正可用的工具组合），只记名不记值。
        """
        ep = {
            "skill": str(skill or ""), "query": str(query or ""),
            "query_signature": _query_signature(query), "ok": bool(ok),
            "error_code": str((error_info or {}).get("error_code") or ""),
            "stage": str((error_info or {}).get("stage") or ""),
            "context": {k: str(v) for k, v in (dict(context or {}).items())
                        if k in ("task_type", "role", "web_search")},
            "tools": sorted({str(t) for t in (tools or []) if str(t).strip()}),
            "at": time.time(),
        }
        self.store.add_episode(ep)
        if ok:
            self.store.add_procedure(skill, query, context, tools=tools)
        return ep

    def recall_episodes(self, skill=None, ok=None, limit=10):
        return self.store.recall_episodes(skill=skill, ok=ok, limit=limit)

    def recall_procedures(self, query: str = "", limit: int = 5):
        return self.store.recall_procedures(query=query, limit=limit)

    def working_hint(self, skill: str, state=None,
                     session_id: str = "default") -> str:
        """工作记忆：取出该技能会话内已缓存参数（敏感键只记名），提示 AI 勿重复询问。"""
        if state is None or not hasattr(state, "get_cached_params"):
            return ""
        try:
            cp = state.get_cached_params(skill, session_id)
        except Exception:  # noqa: BLE001
            return ""
        if not cp:
            return ""
        keys = ", ".join("%s%s" % (k, "" if v == "<redacted>" else "=%s" % v)
                         for k, v in cp.items())
        return "（已缓存环境变量：%s — 无需重复向用户询问）" % keys

    def snapshot(self) -> Dict:
        return self.store.snapshot()


# ---------------------------------------------------------------------------
# 方向三·模型自适应升级（Confidence Loop）
# ---------------------------------------------------------------------------
class ConfidenceLoop:
    """先用便宜/小模型跑 LLM rerank，用「排序一致性」作 confidence proxy；
    不够则升级更强模型。与竞争力 gate 同哲学：能省则省，该花则花。

    一致性 proxy：LLM rerank 顺序 vs 原始 heuristic 顺序的 **pairwise 顺序一致率**
    （Kendall tau 简化）。0~1，1=完全同意 heuristic 顺序，0=完全反序。

    为什么不用 Jaccard：llm_rerank「只重排不增删」，重排前后候选集合恒相同，
    Jaccard 恒为 1 无区分度——必须用「顺序」而非「集合」衡量分歧。
    """

    def __init__(self, weak_model: Optional[str] = None,
                 strong_model: Optional[str] = None,
                 consistency_threshold: float = 0.5,
                 min_competitive: int = 2):
        self.weak_model = weak_model
        self.strong_model = strong_model
        self.consistency_threshold = consistency_threshold
        self.min_competitive = min_competitive

    @property
    def configured(self) -> bool:
        return bool(self.weak_model or self.strong_model)

    def decide(self, query: str, candidates: Sequence, descriptions: Dict,
               llm=None, conversation=None, k: int = 5,
               min_competitive: Optional[int] = None) -> tuple:
        """对 heuristic 已排序的候选做「小模型优先 + 一致性升级」重排。

        入参 candidates 应为 heuristic 排序后的顺序（作为一致性基准）。
        min_competitive 由调用方（router 的竞争力 gate）透传，None=用本循环默认。
        返回 (reranked_candidates, meta)。meta 含 confidence_loop 决策细节。
        """
        from .llm_rerank import llm_rerank

        mc = self.min_competitive if min_competitive is None else min_competitive
        if llm is None or not self.configured:
            ranked, meta = llm_rerank(query, candidates, descriptions, llm, k=k,
                                      model=self.weak_model or self.strong_model,
                                      conversation=conversation,
                                      min_competitive=mc)
            meta["confidence_loop"] = {"applied": False,
                                       "reason": "无模型或未配置分级（weak/strong）"}
            return ranked, meta

        # pass 1：小/便宜模型
        ranked_w, meta_w = llm_rerank(query, candidates, descriptions, llm, k=k,
                                      model=self.weak_model, conversation=conversation,
                                      min_competitive=mc)
        # gate 未触发 / LLM 降级（applied=False）→ 这一遍根本没花 LLM 推理，
        # 就没有「小模型判断」可供评估，更谈不上升级（能省则省，诚实不冒充）。
        if not meta_w.get("applied"):
            meta_w["confidence_loop"] = {
                "applied": False, "upgraded": False,
                "model": self.weak_model or "", "consistency": None,
                "threshold": self.consistency_threshold,
                "reason": "小模型 rerank 未触发（竞争力 gate / 降级），无需升级"}
            return ranked_w, meta_w

        cons_w = self._consistency(list(candidates), ranked_w)
        if cons_w >= self.consistency_threshold:
            meta_w["confidence_loop"] = {
                "applied": True, "upgraded": False,
                "model": self.weak_model or "", "consistency": round(cons_w, 4),
                "threshold": self.consistency_threshold,
                "reason": "小模型排序与 heuristic 一致，无需升级"}
            return ranked_w, meta_w

        # 升级：更强模型
        model_s = self.strong_model or self.weak_model
        ranked_s, meta_s = llm_rerank(query, candidates, descriptions, llm, k=k,
                                      model=model_s, conversation=conversation,
                                      min_competitive=mc)
        cons_s = self._consistency(list(candidates), ranked_s)
        meta_s["confidence_loop"] = {
            "applied": True, "upgraded": True,
            "weak_model": self.weak_model or "", "model": model_s or "",
            "weak_consistency": round(cons_w, 4),
            "consistency": round(cons_s, 4),
            "threshold": self.consistency_threshold,
            "reason": "小模型排序与 heuristic 不一致，已升级更强模型"}
        return ranked_s, meta_s

    @staticmethod
    def _consistency(heuristic_order, llm_order) -> float:
        """pairwise 顺序一致率（Kendall tau 简化）：两序中相对顺序一致的对 / 总对数。

        只比较两序都出现的候选名。1.0=完全同意，0.0=完全反序；候选 <2 无对可比 →
        返回 1.0（无法证明分歧则不升级，诚实不冒充「发现分歧」）。
        """
        h_names = [c.name for c in heuristic_order]
        l_names = [c.name for c in llm_order]
        l_pos = {n: i for i, n in enumerate(l_names)}
        common = [n for n in h_names if n in l_pos]
        if len(common) < 2:
            return 1.0
        concord = total = 0
        for i in range(len(common)):
            for j in range(i + 1, len(common)):
                total += 1
                # heuristic 里 common[i] 必在 common[j] 前（common 保序取自 h_names）
                if l_pos[common[i]] < l_pos[common[j]]:
                    concord += 1
        return concord / total if total else 1.0


# ---------------------------------------------------------------------------
# 便捷装配
# ---------------------------------------------------------------------------
def build_runtime(reflection_path: str = DEFAULT_EXPERIENCE_PATH,
                  llm=None, llm_model: Optional[str] = None,
                  weak_model: Optional[str] = None,
                  strong_model: Optional[str] = None) -> Dict:
    """一次性装配反思引擎 + 分层记忆 + 置信循环（daemon 接线用）。

    任一组件构造失败都退化为 None，不影响其余组件与执行主链路。
    """
    out: Dict = {"store": None, "reflection": None, "memory": None,
                 "confidence_loop": None}
    try:
        store = ExperienceStore(path=reflection_path)
        out["store"] = store
        out["reflection"] = ReflectionEngine(store=store, llm=llm, model=llm_model)
        out["memory"] = LayeredMemory(store=store)
    except Exception:  # noqa: BLE001
        pass
    try:
        if weak_model or strong_model:
            out["confidence_loop"] = ConfidenceLoop(
                weak_model=weak_model, strong_model=strong_model)
    except Exception:  # noqa: BLE001
        pass
    return out
