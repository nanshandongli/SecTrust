"""
first_principles.py —— 「第一性原理 vs 标新立异」多视角分析器

灵感来源：抖音上「小韦用AI」一类作品——对同一主题让 AI 生成若干个"不一样"
的解读/方案，然后判别这些"不同"里，哪些是真正回到本质、从底层事实重构的
（第一性原理），哪些只是表面不同、实则是换皮/同质化的（标新立异）。

本模块把这件事落成产品的可调用能力，复用 console_chat 同款真实算力路由
（_build_provider），并严格遵守诚实边界：

  * 没有配置可用的算力模型时，直接抛 RuntimeError("no_llm_available")，
    由 HTTP 层捕获后返回 engine="unavailable"，**绝不伪造分析结论**。
  * 所有视角内容都来自真实 LLM 调用；判别器也是真实 LLM 调用。
  * 解析 LLM 返回的 JSON 时做稳健提取，解析失败只标记为 unknown，不编造。

关键 API：
    FirstPrinciplesAnalyzer(profiles).analyze(topic, model=None) -> dict
    返回：
      {
        "topic": str,
        "perspectives": [
          {"id","name","content","classification","essence_score","reasoning"}
        ],
        "verdict": {"best_id","best_classification","summary"},
        "engine": "llm" | "unavailable",
        "model": str | None,
        "error": str | None
      }

约定：
  - 零第三方依赖（只用标准库 + 仓库既有 models / console_chat）。
  - 只读：不写任何任务/审批/轨迹数据。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from models import ChatMessage
# 复用与对话后端完全一致的 provider 构建逻辑，保证算力路由行为一致
from console_chat import _build_provider


# --------------------------------------------------------------------------
# 视角设定：用不同的"引导语"让模型产出真正不同的思路，而不是让它自由发挥。
# 三个视角分别逼近"本质重构 / 反类比 / 标新立异对照样本"三种典型形态。
# --------------------------------------------------------------------------
PERSPECTIVES: List[Dict[str, str]] = [
    {
        "id": "first_principles",
        "name": "第一性原理视角",
        "brief": (
            "请彻底剥离这个主题上所有的行业惯例、'大家都这么做'的假设、以及既有的"
            "做法模板。回到最底层的事实——物理/逻辑/经济/人性的本质约束——然后从这些"
            "底层事实出发，重新构建一种解法或解读。必须触及'为什么这件事本质上应该"
            "这么做'，而不是'别人怎么做的'。"
        ),
    },
    {
        "id": "analogy_break",
        "name": "反类比 / 破假设视角",
        "brief": (
            "先找出这个主题上最流行、最常被拿来类比或跟风的做法，明确列出它依赖的"
            "隐含假设，然后逐条推翻这些假设，给出一个反直觉但逻辑自洽的新视角。"
            "重点不是'不同而已'，而是'为什么流行的类比在这里是错的'。"
        ),
    },
    {
        "id": "novelty_skin",
        "name": "标新立异（换皮）对照",
        "brief": (
            "请刻意给出一个'只是表面不同、实则是包装/换皮/同质化'的方案——它听起来"
            "新鲜，但并没有触及问题的本质，只是把旧做法换了层外衣。这是用来做对照"
            "样本的，必须诚实暴露它'哪里只是标新立异而非本质创新'。"
        ),
    },
]

# 视角生成阶段的系统提示（不再是审计 preamble，而是第一性原理教练）
GEN_SYSTEM = (
    "你是一个严格的第一性原理思维教练。面对用户给出的主题，你必须产出一段"
    "符合下方'视角设定'的内容：只给内容本身，中文，结构化，控制在 350 字以内；"
    "不要解释你在做什么，也不要加'以下是…'这类开场白。若信息不足以展开，"
    "就基于可推演的底层逻辑给出合理推演，并标注这是推演而非事实。"
)

# 判别器阶段的系统提示
DISC_SYSTEM = (
    "下面是针对同一主题的若干个方案。请逐一判断每个方案属于哪一类，并给出"
    "本质度评分：\n"
    "  - first_principles（第一性原理）：剥离惯例、回归本质、从底层事实重构；\n"
    "  - novelty_seeking（标新立异）：表面不同、实则是包装/换皮/同质化、未触及本质；\n"
    "  - mixed（混合）：部分回归本质、部分仍是惯例。\n"
    "本质度评分 essence_score 取 0-10 的整数，越高越触及本质。\n"
    "对每个方案输出一个 JSON 对象：{\"id\":\"...\",\"classification\":\"...\","
    "\"essence_score\":N,\"reasoning\":\"...\"}\n"
    "最终只输出一个 JSON 数组（不要任何额外文字、不要 markdown 代码块）："
    "[{\"id\":\"...\",\"classification\":\"...\",\"essence_score\":N,\"reasoning\":\"...\"}]"
)

_CLASSIFICATIONS = {"first_principles", "novelty_seeking", "mixed"}


def _extract_json_array(text: str) -> Optional[List[dict]]:
    """从 LLM 输出里尽量稳健地提取 JSON 数组。"""
    if not text:
        return None
    # 1) 直接解析
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) 截取第一个 [ 到最后一个 ]
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1 and e > s:
        try:
            obj = json.loads(text[s:e + 1])
            if isinstance(obj, list):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    # 3) 逐行尝试解析每个 {...}
    items: List[dict] = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{") and line.endswith("}"):
            try:
                items.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    # 4) 正则兜底：抓 "id":"x","classification":"y","essence_score":n
    if not items:
        for m in re.finditer(
                r'"id"\s*:\s*"([^"]+)"\s*,\s*"classification"\s*:\s*"([^"]+)"'
                r'\s*,\s*"essence_score"\s*:\s*(\d+)', text):
            items.append({
                "id": m.group(1),
                "classification": m.group(2),
                "essence_score": int(m.group(3)),
            })
    return items or None


def _provider_opts(provider: str) -> Dict[str, Any]:
    """视角生成想要多样性，判别器想要确定性。"""
    return {}


class FirstPrinciplesAnalyzer:
    """对单一主题做多视角生成 + 第一性原理/标新立异判别。

    profiles: ModelProfileStore 实例（与对话后端共享同一份，保证算力选择一致）。
    """

    def __init__(self, profiles):
        self.profiles = profiles

    def analyze(self, topic: str, model: Optional[str] = None) -> Dict[str, Any]:
        topic = (topic or "").strip()
        if not topic:
            return self._unavailable(topic, "主题为空，请输入要分析的主题。")

        # —— 诚实边界：没有可用算力就明确报错，绝不伪造 ——
        profile = self.profiles.get(model) if model else self.profiles.get_active()
        if profile is None or not getattr(profile, "configured", False):
            raise RuntimeError("no_llm_available")

        provider = _build_provider(profile)
        gen_temp = 0.85 if profile.provider in ("openai", "anthropic", "ollama") else 0.7

        # —— 阶段一：每个视角各生成一段内容 ——
        perspectives: List[Dict[str, Any]] = []
        try:
            for p in PERSPECTIVES:
                messages = [
                    ChatMessage(role="system", content=GEN_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=("主题：%s\n\n视角设定：%s" % (topic, p["brief"]))),
                ]
                res = provider.complete(
                    messages,
                    model=getattr(profile, "model", None) or None,
                    timeout=90,
                    temperature=gen_temp,
                )
                perspectives.append({
                    "id": p["id"],
                    "name": p["name"],
                    "content": getattr(res, "content", "") or "",
                })
        except Exception as e:  # noqa: BLE001  —— 任意生成失败都诚实降级
            return self._unavailable(topic, "视角生成阶段调用算力失败：%s" % e,
                                    model="%s/%s" % (profile.provider,
                                                     getattr(profile, "model", "") or ""))

        # —— 阶段二：判别器对全部方案分类 + 评分 ——
        try:
            disc_payload = json.dumps(
                [{"id": v["id"], "name": v["name"], "content": v["content"]}
                 for v in perspectives],
                ensure_ascii=False)
            disc_messages = [
                ChatMessage(role="system", content=DISC_SYSTEM),
                ChatMessage(role="user", content=disc_payload),
            ]
            dres = provider.complete(
                disc_messages,
                model=getattr(profile, "model", None) or None,
                timeout=90,
                temperature=0.2,
            )
            judged = _extract_json_array(getattr(dres, "content", "") or "")
        except Exception as e:  # noqa: BLE001
            judged = None
            err = "判别阶段调用算力失败：%s" % e
        else:
            err = None

        # 把判别结果按 id 合并回视角
        by_id: Dict[str, dict] = {}
        for item in (judged or []):
            cid = item.get("id")
            cls = item.get("classification")
            if cls not in _CLASSIFICATIONS:
                cls = "unknown"
            try:
                score = int(item.get("essence_score", 0))
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(10, score))
            if cid:
                by_id[cid] = {
                    "classification": cls,
                    "essence_score": score,
                    "reasoning": item.get("reasoning", ""),
                }

        for v in perspectives:
            j = by_id.get(v["id"], {})
            v["classification"] = j.get("classification", "unknown")
            v["essence_score"] = j.get("essence_score", 0)
            v["reasoning"] = j.get("reasoning", "")

        verdict = self._verdict(perspectives)

        model_label = "%s/%s" % (profile.provider,
                                 getattr(profile, "model", "") or "")
        return {
            "topic": topic,
            "perspectives": perspectives,
            "verdict": verdict,
            "engine": "llm",
            "model": model_label,
            "error": err,
        }

    # -- 内部工具 --
    @staticmethod
    def _verdict(perspectives: List[Dict[str, Any]]) -> Dict[str, Any]:
        fp = [p for p in perspectives if p["classification"] == "first_principles"]
        mixed = [p for p in perspectives if p["classification"] == "mixed"]
        novelty = [p for p in perspectives if p["classification"] == "novelty_seeking"]
        unknown = [p for p in perspectives if p["classification"] == "unknown"]

        # 优先：第一性原理里本质度最高的；其次：混合里最高的
        ranked = sorted(fp + mixed, key=lambda p: p["essence_score"], reverse=True)
        if ranked:
            best = ranked[0]
            if best["classification"] == "first_principles":
                summary = ("真正回到本质的是「%s」（本质度 %d/10）：%s"
                           % (best["name"], best["essence_score"], best["reasoning"]))
            else:
                summary = ("没有方案完全回到第一性原理，相对最贴近本质的是「%s」"
                           "（混合，本质度 %d/10）：%s"
                           % (best["name"], best["essence_score"], best["reasoning"]))
        elif novelty:
            best = max(novelty, key=lambda p: p["essence_score"])
            summary = ("这些方案都只是标新立异（换皮/同质化），没有真正回到本质。"
                       "对照样本「%s」最能说明'不同'不等于'创新'。" % best["name"])
        else:
            best = perspectives[0] if perspectives else {}
            summary = "判别信息不足，无法给出明确结论，请查看各视角内容自行判断。"

        return {
            "best_id": best.get("id"),
            "best_classification": best.get("classification"),
            "summary": summary,
            "counts": {
                "first_principles": len(fp),
                "mixed": len(mixed),
                "novelty_seeking": len(novelty),
                "unknown": len(unknown),
            },
        }

    @staticmethod
    def _unavailable(topic: str, error: str,
                     model: Optional[str] = None) -> Dict[str, Any]:
        return {
            "topic": topic,
            "perspectives": [],
            "verdict": {"best_id": None, "best_classification": None,
                        "summary": "无法完成分析。", "counts": {}},
            "engine": "unavailable",
            "model": model,
            "error": error,
        }
