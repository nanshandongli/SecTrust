"""
llm_rerank.py —— 认知升级第3步：LLM 语义重排（W-认知，建议①）

在 `retrieval.py` 的「3路召回 + RRF 融合 + 规则式 Rerank（heuristic-v1）」之上，
叠一层 LLM 语义重排：把候选技能（已通过角色白名单 / 分级 / 熔断过滤的「安全候选」）
连同用户 query 与可选对话上下文一起交给 LLM，让其按语义相关性重排 Top-K。

诚实边界（与全仓一致，不可为「更智能」破防）：
  1. LLM 重排**只重排、不增删**：只能在已过滤的「安全候选」集合内调整顺序，
     绝不能把被角色白名单 / 分级 / 熔断剔除的技能复活——安全过滤在 router.select
     的更上游，本层不越权。
  2. LLM 不可用（无配置模型 → RuntimeError("no_llm_available")）、返回不可解析、
     或返回空 → **整体降级回 heuristic-v1**，`applied=False` 并附 reason，绝不冒充
     已做语义重排。
  3. LLM 是「语义判断」，不是「决策执行」：用 `LlmEngine.complete()`（推理路径），
     不混用 `execute()`（干活）；`router` 只选技能不替模型决策。
  4. 不引入任何重依赖：本模块仅标准库 + `retrieval.SkillCandidate`；真实 embedding
     仍走 `memory` extra 的可插拔接口，哈希向量作零依赖兜底（见 retrieval.py）。

零第三方依赖：仅标准库。
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .retrieval import RERANKER_ID, SkillCandidate

LLM_RERANKER_ID = "llm-v1"

# 竞争力阈值（替代"候选总数阈值"）：经 bench_rerank_latency.py 实测发现，小技能库里
# recall 返回的是「全部技能」（召回上限 k*3 远大于库容），所以"保留集总数"≈库大小，
# 单纯按总数阈值拦不住。真正该区分的是「有多少技能跟榜首真有竞争力」——其余只是噪声。
# 实测 RRF：榜首约 0.016、其余挤在 0.0076~0.0081（近乎并列、零相关），heuristic 已自信
# 选对。故仅在「竞争力候选数 >= 阈值」时才付一次 LLM 推理，否则纯 heuristic 已足够，
# 省掉 300~800ms 无谓开销（详见 bench_rerank_latency.py）。
# 经 SECTRUST_LLM_RERANK_MIN_COMPETITIVE 环境变量可调（设为 0 关闭 gate、始终触发）。
def _read_min_competitive() -> int:
    try:
        v = int(os.environ.get("SECTRUST_LLM_RERANK_MIN_COMPETITIVE", "2"))
    except (TypeError, ValueError):
        v = 2
    return max(0, v)

DEFAULT_MIN_COMPETITIVE = _read_min_competitive()

# 竞争力判据：候选 RRF >= 榜首 RRF * 此系数 即视为"有竞争力"（与榜首并列相关）。
COMPETITIVE_MARGIN = 0.6


def _extract_json_array(text: str) -> Optional[List[dict]]:
    """从 LLM 自由文本里抠出第一个 JSON 数组，容忍 ```json 代码围栏。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1).strip()
    s, e = t.find("["), t.rfind("]")
    if s == -1 or e == -1 or e < s:
        return None
    block = t[s:e + 1]
    try:
        arr = json.loads(block)
    except (ValueError, TypeError):
        return None
    if not isinstance(arr, list):
        return None
    out: List[dict] = []
    for item in arr:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({"name": item})
    return out


def _pick_names(parsed: List[dict], valid: set) -> List[str]:
    """按 LLM 返回顺序收集命中候选集的技能名（精确 → 大小写不敏感）。"""
    names: List[str] = []
    seen = set()
    lowered = {n.lower(): n for n in valid}
    for item in parsed:
        raw = str(item.get("name") or "").strip()
        if not raw:
            continue
        if raw in valid:                      # 精确
            key = raw
        elif raw.lower() in lowered:          # 大小写不敏感
            key = lowered[raw.lower()]
        else:
            continue                         # 候选集之外（被过滤）一律忽略，不复活
        if key not in seen:
            seen.add(key)
            names.append(key)
    return names


def llm_rerank(query: str, candidates: Sequence[SkillCandidate],
              descriptions: Dict[str, str], llm=None, k: int = 5,
              model: Optional[str] = None,
              conversation: Optional[List[Dict[str, str]]] = None,
              min_competitive: int = DEFAULT_MIN_COMPETITIVE
              ) -> Tuple[List[SkillCandidate], Dict]:
    """对 heuristic 已排序的「安全候选」做 LLM 语义重排。

    入参：
      query        用户原始请求
      candidates   heuristic rerank 后的候选（已过滤安全，按相关度降序）
      descriptions {name: 技能描述}（取自 L1 元数据，供 LLM 理解）
      llm          鸭子类型：需有 `.complete(question, context="", history=None,
                   model=None) -> {"answer": str, ...}`；为 None 时跳过重排
      k            最终 Top-K
      model        指定 profile id（None=active）
      conversation 可选对话历史 [{role, content}]，注入 LLM 上下文
      min_competitive 竞争力阈值：与榜首 RRF 接近（>= COMPETITIVE_MARGIN）的候选数
                   小于此值时跳过 LLM（默认 2，即只有 1 个真相关就纯 heuristic）。
                   经 SECTRUST_LLM_RERANK_MIN_COMPETITIVE 可调，0=始终触发。

    返回：(重排后的候选列表, meta)。
      meta = {"applied": bool, "model": str, "reason": str,
              "chosen": [name,...], "reranker": str}
    """
    meta: Dict = {"applied": False, "model": "", "reason": "",
                  "chosen": [], "reranker": RERANKER_ID}
    cand_list = list(candidates)
    if llm is None:
        meta["reason"] = "未注入 LLM（无配置模型），跳过"
        return cand_list, meta
    if min_competitive and len(cand_list) >= 2:
        # 竞争力 gate：只数"与榜首真有竞争力"的候选。heuristic 已自信选对时（仅 1 个
        # 真相关），LLM 重排只是重排噪声，省掉一次推理（速度优先，实测省 300~800ms）。
        top_rrf = max((c.scores.get("rrf", 0.0) for c in cand_list), default=0.0)
        if top_rrf > 0:
            competitive = sum(
                1 for c in cand_list
                if c.scores.get("rrf", 0.0) >= top_rrf * COMPETITIVE_MARGIN)
            if competitive < min_competitive:
                meta["reason"] = ("竞争力候选 %d < 阈值 %d（榜首 RRF=%.4f），"
                                  "heuristic 已能定序，跳过 LLM"
                                  % (competitive, min_competitive, top_rrf))
                return cand_list, meta
    if not cand_list or len(cand_list) < 2:
        meta["reason"] = "候选不足 2 个，无需重排"
        return cand_list, meta

    valid = {c.name for c in cand_list}
    # 构造提示：只列候选集内的技能，明确「只能从中选」
    lines = []
    for i, c in enumerate(cand_list, start=1):
        desc = str(descriptions.get(c.name) or "").strip()
        lines.append("%d. %s：%s（分级=%s）" % (i, c.name, desc, c.tier or "-"))
    conv_txt = ""
    if conversation:
        conv_txt = "\n对话上下文：\n" + "\n".join(
            "%s: %s" % (h.get("role"), h.get("content"))
            for h in conversation[-6:]
            if isinstance(h, dict)
            and h.get("role") in ("user", "assistant") and h.get("content"))
    prompt = (
        "你是一个技能路由助手。请从下面的候选技能中，根据用户请求和对话上下文，"
        "选出最相关的 Top-%d 个，按相关性从高到低排列。\n\n"
        "用户请求：%s%s\n\n候选技能（已通过安全过滤，只能从中选择，不要选列表之外）：\n%s\n\n"
        "只返回 JSON 数组，格式：\n"
        "[{\"name\": \"技能名\", \"reason\": \"简短相关性说明\"}, ...]\n"
        "若都不相关返回 []。不要输出任何额外文字或 Markdown 围栏之外的内容。"
        % (min(k, len(cand_list)), query, conv_txt, "\n".join(lines)))

    try:
        resp = llm.complete(prompt, context="", history=conversation, model=model)
        answer = ((resp or {}).get("answer", "") if isinstance(resp, dict)
                  else str(resp))
    except RuntimeError as e:
        # 无配置模型：诚实降级回 heuristic
        meta["reason"] = "LLM 不可用（%s），降级 %s" % (e, RERANKER_ID)
        return cand_list, meta
    except Exception as e:  # noqa: BLE001 - LLM 调用异常统一降级
        meta["reason"] = "LLM 调用异常（%s: %s），降级 %s" % (type(e).__name__, e, RERANKER_ID)
        return cand_list, meta

    parsed = _extract_json_array(answer)
    if parsed is None:
        meta["reason"] = "LLM 返回不可解析，降级 %s" % RERANKER_ID
        return cand_list, meta
    ordered = _pick_names(parsed, valid)
    if not ordered:
        meta["reason"] = "LLM 未返回有效技能名，降级 %s" % RERANKER_ID
        return cand_list, meta

    # 重排：LLM 顺序在前，剩余候选按原 heuristic 顺序补在后
    by_name = {c.name: c for c in cand_list}
    reason_map = {str(it.get("name", "")).strip(): str(it.get("reason", "")).strip()
                  for it in parsed}
    reordered: List[SkillCandidate] = []
    for n in ordered:
        c = by_name.pop(n, None)
        if c is None:
            continue
        c.reranker = LLM_RERANKER_ID
        c.llm_reason = reason_map.get(n, "")
        reordered.append(c)
    for c in cand_list:                      # 补上 LLM 未提及的
        if c.name in by_name:
            reordered.append(by_name[c.name])

    meta.update(applied=True,
                model=str((resp or {}).get("model", "") or ""),
                reason="ok", chosen=ordered, reranker=LLM_RERANKER_ID)
    return reordered, meta
