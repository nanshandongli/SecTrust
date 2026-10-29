"""
execution/web_search.py —— 真实 Web 搜索（零第三方依赖，仅标准库）

诚实边界（对齐仓库的「诚实边界」范式）：
  - 这是**真·联网搜索**，不是模型凭记忆编。联网失败 / 代理不可达 / 反爬拦截时，
    **如实返回 ok=False + error**，**绝不抛异常、绝不伪造几条假结果**糊弄。
  - 后端：cn.bing.com。本机代理（127.0.0.1:59657）对 Bing 放行；DuckDuckGo /
    Google / Wikipedia 被代理 502 拦截，故以 Bing 为唯一真实后端（不使用易触发
    反爬验证页、结果不可靠的 Baidu 作脆弱兜底）。
  - 代理感知：读 HTTPS_PROXY / HTTP_PROXY；无代理则直连（无代理环境也能用）。
  - recency 归一：代理的「今天 + X新闻」垂直会返回黄历 / 天气垃圾结果，故把该组合
    里的「今天」同义换成「最新」（两者都表示"近期"，不改主题、不伪造）。

返回结构（web_search）：
  {"ok": bool, "query": str, "results": [{"title","url","snippet"}],
   "error": str|None, "engine": "bing", "note": str|None}
"""

from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

BING_ENDPOINT = "https://cn.bing.com/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 「今天 + 这些词」会触发代理上返回无关结果的坏垂直，需要 recency 归一。
_NEWS_KW = ("新闻", "动态", "资讯", "进展", "快讯", "日报", "报道", "消息",
            "news", "latest", "update")

# 任务性尾缀关键词：任务指令里"给出3条要点"这类是给模型的要求，不是搜索词。
_TASK_TAIL_KW = ("给出", "列出", "总结", "要点", "说明", "形成", "写一份",
                 "标注", "来源", "报告", "条")
# 引导性搜索动词前缀（长词在前，避免"搜索一下"被"搜索"截断）。
_SEARCH_VERBS = ("帮我搜索一下", "帮我搜一下", "帮我查一下", "帮我查查", "帮我搜",
                 "搜索一下", "搜一下", "查一下", "帮我查", "搜索", "查查")


def build_search_query(instruction: str) -> str:
    """从任务指令里提取真正的搜索词。

    任务指令往往长这样："搜索一下今天的AI方面的新闻，给出3条要点"。
    直接整句丢给搜索引擎会命中垃圾结果（实测必应返回一堆搜索引擎首页）。
    处理：
      1. 若首段之后的尾段含任务性要求（给出/总结/要点…），只取首段；
      2. 去掉开头的引导性搜索动词（搜索一下/查一下…）。
    清洗后为空则原样返回，绝不凭空造词。
    """
    q = (instruction or "").strip()
    head = q
    for sep in ("，", ",", "。"):
        if sep in q:
            head, tail = q.split(sep, 1)
            head = head.strip()
            if any(k in tail for k in _TASK_TAIL_KW):
                q = head
            break
    for verb in _SEARCH_VERBS:
        if q.startswith(verb):
            q = q[len(verb):].strip()
            break
    # 清洗后为空 → 退回首段（如整句只有动词+任务要求），绝不凭空造词
    return q or head or (instruction or "").strip()


def _normalize_query(q: str) -> str:
    """recency 同义归一：仅把「今天 + 新闻类词」里的「今天」换成「最新」。

    实测 cn.bing.com 的「今天+X新闻」垂直经本机代理返回黄历 / 天气垃圾；
    而「最新动态 / 行业新闻」能返回真实、带时间戳的资讯。两者都表示"近期"，
    属同义归一、不改主题、不伪造内容。
    """
    if "今天" in q and any(k in q for k in _NEWS_KW):
        return q.replace("今天", "最新")
    return q


def _build_opener():
    """代理感知 opener。返回 (opener, proxy_url_or_None)。"""
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if proxy:
        handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        return urllib.request.build_opener(handler), proxy
    return urllib.request.build_opener(), None


def _extract_blocks(data: str) -> List[Dict[str, str]]:
    """从 Bing SERP HTML 抽取结果块：标题 / 链接 / 摘要。"""
    blocks = re.findall(r'<li class="b_algo".*?</li>', data, re.S)
    results: List[Dict[str, str]] = []
    seen = set()
    for b in blocks:
        t = re.search(r'<h2>\s*<a[^>]*>(.*?)</a>', b, re.S)
        if not t:
            continue
        a = re.search(r'<h2>.*?<a[^>]*href="(https?://[^"]+)"', b, re.S)
        p = (re.search(r'<p class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', b, re.S)
             or re.search(r'<p[^>]*>(.*?)</p>', b, re.S))
        title = html.unescape(re.sub(r'<[^>]+>', '', t.group(1))).strip()
        url = a.group(1) if a else ""
        snip = (html.unescape(re.sub(r'<[^>]+>', '', p.group(1))).strip()
                if p else "")
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": snip})
    return results


# 相关性过滤：查询词切成「拉丁/数字词 + 中文连续段」，结果的标题+摘要
# 必须含至少一个词，否则视为跑题噪声丢弃（实测能滤掉搜索引擎主页 /
# 词典页这类明显无关结果）。停用词是搜索语义词，不作为匹配判据。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]{2,}")
_STOPWORDS = frozenset((
    "一下", "最新", "今天", "今日", "最近", "相关", "方面", "什么", "怎么",
    "如何", "介绍", "情况", "新闻", "资讯", "动态", "消息", "快讯", "报告",
    "要点", "查询", "搜索", "查找", "关于", "以及", "还有", "给出", "总结",
))


def _query_terms(q: str) -> List[str]:
    """提取查询里的显著词（拉丁词原样、中文按连续段）。"""
    toks = _TOKEN_RE.findall(q or "")
    return [t for t in toks
            if len(t) >= 2 and t.lower() not in _STOPWORDS]


def _filter_relevant(results: List[Dict[str, str]],
                     terms: List[str]) -> List[Dict[str, str]]:
    """只保留标题/摘要至少命中一个查询词的结果。terms 为空则全保留。"""
    if not terms:
        return results
    kept = []
    for r in results:
        text = (r.get("title", "") + " " + r.get("snippet", "")).lower()
        if any(t.lower() in text for t in terms):
            kept.append(r)
    return kept


def web_search(query: str, max_results: int = 5,
               timeout: float = 15) -> Dict[str, object]:
    """真实联网搜索。失败（网络 / 代理 / 反爬）时 ok=False + error，**绝不抛异常**。

    参数：
      query       搜索词
      max_results 返回条数上限（默认 5）
      timeout     单请求超时（秒，默认 15）
    """
    out: Dict[str, object] = {
        "ok": False, "query": query, "results": [], "error": None,
        "engine": "bing", "note": None,
    }
    q = (query or "").strip()
    if not q:
        out["error"] = "搜索词为空"
        return out

    norm = _normalize_query(q)
    if norm != q:
        out["note"] = ("已将「今天」归一为「最新」（代理对『今天+新闻』垂直返回无关结果）")

    # 一次抓足候选（30 条），后面按相关性过滤——SERP 噪声大，多备几条。
    url = BING_ENDPOINT + "?q=" + urllib.parse.quote(norm) + "&count=30"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        # opener 构造也纳入降级范围：代理环境变量异常等不应让整函数炸。
        opener, _proxy = _build_opener()
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        # 代理/上游返回的 4xx/5xx（如 502 拦截）。
        out["error"] = "搜索引擎返回 HTTP %s（可能被代理拦截或触发反爬）" % e.code
        return out
    except urllib.error.URLError as e:
        out["error"] = "联网失败（代理/网络不可达）：%s" % getattr(e, "reason", e)
        return out
    except Exception as e:  # noqa: BLE001 - 任何异常都诚实降级，不炸
        out["error"] = "搜索请求异常：%s" % e
        return out

    results = _extract_blocks(data)
    if not results:
        note = out["note"] or ""
        out["error"] = "搜索引擎返回了内容但未能解析出结果（可能触发反爬/代理拦截）"
        out["note"] = (note + "；如持续为空请检查代理对 Bing 的放行").strip("；")
        return out

    # 相关性过滤：SERP 噪声大（跑题主页/词典页），按查询词筛一轮。
    # 全被筛掉时**不硬塞**：如实 ok=False + 说明（上层会继续让模型按自身
    # 知识作答，绝不把无关结果伪装成"真实资料"喂给模型）。
    terms = _query_terms(norm)
    kept = _filter_relevant(results, terms)
    if not kept:
        note = out["note"] or ""
        out["error"] = ("搜索返回了结果但均与查询词不相关（查询可能过于宽泛），"
                        "已全部滤除，不当作真实资料使用")
        out["note"] = (note + "；可换更具体的关键词重试").strip("；")
        return out
    out["results"] = kept[:max_results]
    out["ok"] = True
    return out


if __name__ == "__main__":  # pragma: no cover - 手动试跑
    import json
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "人工智能 行业新闻"
    print(json.dumps(web_search(q), ensure_ascii=False, indent=2))
