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
                 "搜索一下", "搜一下", "查一下", "帮我查", "搜索", "查查",
                 "检索一下", "检索", "查找一下", "查找")


def build_search_query(instruction: str) -> str:
    """从任务指令里提取真正的搜索词。

    任务指令的两种典型形态：
      1. 单任务（控制台"新建任务"）："查一下大模型最新进展，给出3条要点"
         ——尾段是给模型的要求，不是搜索词；
      2. planner finalize 工序："直接交付「今天重要的国际新闻」的完整成果
         （单任务直出）：目标、要点、结论一次说清…"——真实查询被包进「」、
         前面是动作套话，整句当搜索词必应只会命中垃圾。
    处理：
      1. 优先抽取「」/『』/"" 内的短语（planner 把用户原话包在里面）；
      2. 否则取首个分隔符（：/（/，等）前的短句，任务套话在分隔符之后；
      3. 剥掉开头的引导性搜索动词（搜索一下/查一下…）。
    清洗后为空则原样返回，绝不凭空造词。
    """
    q = (instruction or "").strip()
    if not q:
        return ""
    # 1) 引号/书名号内的真实查询（planner finalize 把原需求包进「」）
    m = re.search(r"[\u300c\u300e\u201c](.+?)[\u300d\u300f\u201d]", q)
    if m and len(m.group(1).strip()) >= 2:
        q = m.group(1).strip()
    # 2) 取首个分隔符前的短句（冒号/全角括号/逗号等），任务套话在分隔符之后
    head = q  # 分隔符前的短句：verb 剥离后若变空，可退回它而非整句
    for sep in ("：", ":", "（", "(", "，", ",", "。"):
        if sep in q:
            h = q.split(sep, 1)[0].strip()
            if len(h) >= 2:
                head = h
                q = h
                break
    # 3) 剥掉引导性搜索动词（长词在前，避免"搜索一下"被"搜索"截断）
    for verb in _SEARCH_VERBS:
        if q.startswith(verb):
            q = q[len(verb):].strip()
            break
    return q or head or (instruction or "").strip()


# 程度/重要性修饰词：在新闻查询里只是「重大/重要」这类程度副词，不该当主题关键词
# 去匹配（"重大"会歧义命中「重庆大学/重大工程」，实测让用户「检索今天的重大国际新闻」
# 翻车返回学术检索平台而非新闻）；含这些词即触发归一并剥离。
_DEGREE_KW = ("重大", "重要", "主要", "最新", "核心", "关键", "热点", "要闻", "头条")


def _normalize_query(q: str) -> str:
    """新闻类查询归一：把「(时间词) + (程度修饰词) + 主题(含新闻类词)」重排成
    Bing 友好的「主题 今日」自然短语。

    实测教训（用户「检索今天的重大国际新闻」翻车链路）：
      - 原实现只在「今天/今日 + 新闻词」时才归一，于是「重大国际新闻」（无时间词）
        原样丢给必应 → "重大"歧义命中「重庆大学/重大工程」；
      - 且 build_search_query 的引导动词表没有"检索"，"检索"前缀被带进查询 → 命中学术
        检索平台（CNKI/维普/Sci-Hub/百度百科"检索"词条）。
      两者都返回 ok=True 却毫无新闻内容，前端只显示「🌐 联网产出」而任务无结果。

    修复：触发条件放宽为「含新闻词 且（含时间词 或 含程度修饰词）」——只要带「重大/
    重要」等程度词或时间词，就整体归一成「主题 今日」，剥掉时间词、程度词、冗余虚词。
    已验证「国际新闻 今日」必应返回 9–10 条带真实时间戳的资讯，命中可靠。
    非新闻查询 / 纯主题（如「人工智能 行业新闻」无程度词无时间词）保持原样，不改主题、不伪造。
    """
    has_recency = any(w in q for w in ("今天", "今日", "最近", "近日", "近期"))
    has_news = any(k in q for k in _NEWS_KW)
    has_degree = any(k in q for k in _DEGREE_KW)
    if not (has_news and (has_recency or has_degree)):
        return q
    topic = q
    for rec in ("今天", "今日", "最近", "近日", "近期"):
        topic = topic.replace(rec, "")
    for deg in _DEGREE_KW:
        topic = topic.replace(deg, "")
    for filler in ("的", "相关", "方面"):
        topic = topic.replace(filler, "")
    topic = topic.strip()
    if not topic:
        return "今日国际新闻"
    return topic + " 今日"


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

# 泛词字符：国/际/新/闻 等新闻·地理·虚词。由两个泛词字符构成的 2-gram（如「国际」
# 「新闻」）不算主题判据——它们出现在任何新闻页上，无法区分「国际新闻」与
# 「国际科技新闻」。真正区分主题的是「科技」「人工」这类含实义字符的单元。
_GENERIC_CHARS = frozenset(
    "国国际新闻资讯动态消息今日最相关方面重要全球世界内外时事焦点要报道快讯"
    "重大的了是在和与被等关于如何怎么样介绍情况查搜找询给出总结及与"
)


def _is_generic_unit(u: str) -> bool:
    """泛词单元（国/际/新/闻 等新闻地理虚词构成）不算主题判据。"""
    if u.isascii():
        return False
    return all(c in _GENERIC_CHARS for c in u)


def _query_terms(q: str) -> List[str]:
    """查询的匹配单元：拉丁词原样；中文按 2-gram 滑动窗口切分。

    为何用 2-gram 而非整串：中文无词边界，"国际科技新闻" 当作一个 token 要求结果
    片段出现该连续串，而必应结果多是「科技新闻」「国际 科技」等拆开写法 → 整串匹配
    会把相关结果全滤掉（实测『今天重要的国际科技新闻』就被全滤，仅剩 0 条）。
    2-gram 让「科技」「国际」等子串可独立命中：既保留去噪能力，又不会过过滤。
    纯停用词构成的单元（如整串就是「新闻」「今日」）直接丢弃。
    """
    units: List[str] = []
    for tok in _TOKEN_RE.findall(q or ""):
        if tok.isascii():  # 拉丁/数字词
            low = tok.lower()
            if len(tok) >= 2 and low not in _STOPWORDS:
                units.append(low)
        else:  # 中日韩连续段：按 2-gram 滑窗切分
            if len(tok) < 2:
                continue
            for i in range(len(tok) - 1):
                bg = tok[i:i + 2]
                if bg not in _STOPWORDS:
                    units.append(bg)
    return units


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


def _score_result(result: Dict[str, str], terms: List[str]) -> int:
    """相关性打分：命中主题单元（科技/人工…）加权，泛词（国际/新闻）计 1。"""
    text = (result.get("title", "") + " " + result.get("snippet", "")).lower()
    score = 0
    for t in terms:
        if t.lower() in text:
            score += 2 if not _is_generic_unit(t) else 1
    return score


def _query_variants(q: str) -> List[str]:
    """查询变体兜底链：必应/代理对「国际 + 科技 + 今日」这类三段组合时而返回
    垃圾页（爱奇艺/WhatsApp）或空页。主查询无果时，依次尝试：去泛地理前缀、
    去 recency 前后缀、组合去，直到拿到真实相关结果。去序保序去重。"""
    norm = _normalize_query(q)
    GEO = r"(国际|国内|全球|世界|国外|海外|中国)\s*"
    LEAD_REC = r"^(最新|近日|今天|近期)\s*"
    TAIL_REC = r"\s*(今日|最新|近日|近期|今天)$"
    v_geo = re.sub("^" + GEO, "", norm)
    v_tail = re.sub(TAIL_REC, "", norm)
    v_lead = re.sub(LEAD_REC, "", norm)
    v_geo_tail = re.sub("^" + GEO, "", v_tail)
    v_all = re.sub(LEAD_REC, "", v_geo_tail)
    variants = []
    for v in (norm, v_geo, v_tail, v_lead, v_geo_tail, v_all):
        if v and v not in variants:
            variants.append(v)
    return variants


def _fetch_blocks(query: str, timeout: float) -> List[Dict[str, str]]:
    """抓一个查询的 Bing SERP 并解析结果块。任何异常都向上抛（由调用方诚实降级）。"""
    url = BING_ENDPOINT + "?q=" + urllib.parse.quote(query) + "&count=30"
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    opener, _proxy = _build_opener()
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", "ignore")
    return _extract_blocks(data)


def web_search(query: str, max_results: int = 5,
               timeout: float = 15) -> Dict[str, object]:
    """真实联网搜索。失败（网络 / 代理 / 反爬 / 必应返回垃圾页）时 ok=False +
    error，**绝不抛异常、绝不伪造**。

    健壮性设计（实测必应经本机代理间歇返回无关/空页，是用户「查不到结果」的深层
    根因）：
      - **多查询变体兜底**：主查询（如「国际科技新闻 今日」）可能触发必应垃圾页，
        自动退化为「科技新闻」等更稳形态；
      - **OR 收录 + 打分排序**：结果只要命中任一查询词就收录（绝不因过滤过严归零），
        但命中主题词（科技）的排前面；
      - **诚实降级**：所有变体都无相关结果 → ok=False，上层让模型声明「未获取真实资料」。

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

    variants = _query_variants(q)
    fallback = None  # 首个「有结果」的变体（泛型兜底）
    last_err: Optional[str] = None
    chosen_cq = chosen_kept = chosen_terms = None
    for cq in variants:
        try:
            blocks = _fetch_blocks(cq, timeout)
        except urllib.error.HTTPError as e:
            last_err = "搜索引擎返回 HTTP %s（可能被代理拦截或触发反爬）" % e.code
            continue
        except urllib.error.URLError as e:
            last_err = "联网失败（代理/网络不可达）：%s" % getattr(e, "reason", e)
            continue
        except Exception as e:  # noqa: BLE001
            last_err = "搜索请求异常：%s" % e
            continue

        if not blocks:
            last_err = "搜索引擎返回了内容但未能解析出结果（可能触发反爬/代理拦截）"
            continue
        terms = _query_terms(cq)
        kept = _filter_relevant(blocks, terms)
        if not kept:
            last_err = ("搜索返回了结果但均与查询词不相关（查询可能过于宽泛），"
                        "已全部滤除，不当作真实资料使用")
            continue
        # 主题单元命中（score>=2）即视为「够相关」，优先采用并停止尝试。
        best = max(_score_result(r, terms) for r in kept)
        if best >= 2:
            chosen_cq, chosen_kept, chosen_terms = cq, kept, terms
            break
        if fallback is None:
            fallback = (cq, kept, terms)
    else:
        # 所有变体都未出现主题命中：用首个有结果的泛型变体兜底，或全失败。
        if fallback is None:
            out["error"] = last_err or "搜索未返回可用结果"
            return out
        chosen_cq, chosen_kept, chosen_terms = fallback

    # 打分排序：主题单元命中加权，相关结果浮到顶部
    chosen_kept.sort(key=lambda r: _score_result(r, chosen_terms), reverse=True)
    out["results"] = chosen_kept[:max_results]
    out["ok"] = True
    out["query"] = chosen_cq  # 实际生效的查询（可能已退化）
    if chosen_cq != q:
        note = out["note"] or ""
        out["note"] = (note + f"；主查询无果，改用「{chosen_cq}」").strip("；")
    return out


if __name__ == "__main__":  # pragma: no cover - 手动试跑
    import json
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "人工智能 行业新闻"
    print(json.dumps(web_search(q), ensure_ascii=False, indent=2))
