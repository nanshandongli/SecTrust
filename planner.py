"""
编排层 · 任务拆解器（planner.py）—— 规则模板版。

这是"多 AI 同事协同"从底座走向应用的第 2 块砖：
DAG 调度（orchestration.py）解决"子任务之间怎么排队"，
本模块解决第一公里——"**一句需求怎么变成那张 DAG**"。

## 定位与诚实边界

本版是**规则模板版**：内置 5 个领域模板（内容营销 / 技术研发 / 报告撰写 /
客服工单 / 通用兜底），用关键词打分选模板，用正则从需求文本里抽取槽位
（主题、预算、渠道、受众、截止日期），产出带技能/模型/质量规则的子任务 DAG。

它**不是** LLM 拆解器。规则版的优势是可解释、可测试、零成本、零延迟；
代价是只认识模板内的任务结构。槽位抽不到时不瞎编——留空并在 payload 里标注
`needs_input`，交给下游 worker（或人类）补全。后续可在同一接口上叠 LLM 版
（decompose() 是纯函数，替换 _match_template 即可，输出结构不变）。

## 与 DAG 的关系

    decompose(需求) -> Plan（纯数据，可 JSON 序列化、可回放）
    Plan.build(scheduler) -> 在共享任务表上建图（自动拓扑排序 + 计划前缀防撞名）

子任务 spec 里的 skill / model / quality 三段是给下游消费的：
  - skill：技能提示（哪个专长 AI 适合干，如 agentskills 的 writing-plans）
  - model：模型提示（哪个模型适合，空 = 用默认）
  - quality：质量规则（喂给质量回流回路 QualityGate，见 agentskills/quality.py）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

# v2 Skill 检索执行路径的 payload 白名单（与 console_api.SKILL_PAYLOAD_KEYS 一致；
# 铁律：_payload / decompose / gather_payload 三处都要透传，否则静默丢弃）
SKILL_PAYLOAD_KEYS = ("query", "skill_hint", "security_tier", "script", "args",
                      "artifacts", "skill_k", "session_id", "task_type",
                      "approval_id", "timeout")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 槽位抽取：从需求文本里尽量抽参数；抽不到就留空 + needs_input，绝不编造
# ---------------------------------------------------------------------------

# 常见渠道词（内容营销场景）
_CHANNELS = ("抖音", "小红书", "微信", "公众号", "视频号", "微博", "B站", "哔哩哔哩",
             "知乎", "快手", "淘宝", "京东", "拼多多", "天猫", "百度", "谷歌")

# 常见受众/人群词
_AUDIENCE = ("Z世代", "年轻人", "白领", "宝妈", "学生", "银发族", "中产", "下沉市场",
             "男性用户", "女性用户", "新手妈妈", "程序员", "大学生")

# 预算：数字 + 单位（万元/万/元/w/k）。注意分支顺序："万元"必须排在"元"前，
# 否则 `万?元` 会优先匹配光杆"元"，把"3万元"解析成 3 元。
_BUDGET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|万|元|w|W|k|K)")

# 截止日期：2026-10-23 / 10月23日 / 10/23 / 23号
_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?"),
    re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]"),
    re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)"),
)

# 主题：引号内容优先，其次"关于/主题是/做一个/做一场 + 的 + 名词短语"
_QUOTED_RE = re.compile(r"[「\"'“‘（(]([^」\"'”’）)]{2,40})[」\"'”’）)]")
_TOPIC_AFTER_RE = re.compile(
    r"(?:关于|主题是|主题为|围绕|针对)\s*([^\s，。；、,.!?！？]{2,20})")
_CAMPAIGN_RE = re.compile(
    r"(?:做一?[场个期次]|策划一?[场个期次]|筹备一?[场个期次]|组织一?[场个期次])"
    r"([^，。；、,.!?！？\s]{2,20})")
# "写一份X报告 / 做一份X方案 / 出一篇X" —— 最常见的两种句式，
# 之前抽不到 topic，instruction 里会留下 "<待补:topic>" 占位符被模型原样吐出来。
# 主题取到句末标点为止（含"报告/方案"等后缀——它是主题的一部分，不是另算）。
#
# **ReDoS 纪律**：这里不写 `[^\s...]{2,30}` 同时又在别处叠 `\s*`——量词与
# 字符类重叠写法会退化。正解是：先把待匹配文本里的空白**整体去掉**再匹配
# （中文需求里的空格基本只是排版），字符类里就不必再排除空白。
_WRITE_RE = re.compile(
    r"(?:写|做|出|生成|产出|整理|撰写|编|起草)一?[份个篇版]"
    r"([^，。；、,.!?！？]{2,40})")
# 抽 topic 前先把空白去掉（"写一份 2026 年中国…" → "写一份2026年中国…"）
_WS_RE = re.compile(r"\s+")


def _norm_year(y: int) -> int:
    return y if y > 100 else 2000 + y


def extract_slots(text: str) -> dict:
    """从需求文本抽取槽位。抽不到的键**不出现**（调用方按缺失处理）。"""
    slots: dict = {}
    if not text:
        return slots

    # 主题：引号 > "做一场X" > "写一份X报告" > "关于X"
    # 匹配用**去空白**的副本：中文需求里的空格多为排版（"写一份 2026 年…"），
    # 带空格会让主题被截断成 "2026"。引号内的内容仍按原样取。
    ns = _WS_RE.sub("", text)
    m = _QUOTED_RE.search(text)
    if m:
        slots["topic"] = m.group(1).strip()
    else:
        for rx in (_CAMPAIGN_RE, _WRITE_RE, _TOPIC_AFTER_RE):
            m = rx.search(ns)
            if m and m.group(1).strip():
                slots["topic"] = m.group(1).strip()
                break

    # 预算
    m = _BUDGET_RE.search(text)
    if m:
        num, unit = m.group(1), m.group(2)
        mult = {"万元": 10000, "万": 10000, "w": 10000, "W": 10000,
                "k": 1000, "K": 1000}.get(unit, 1)  # 光杆"元" = 1
        slots["budget_cny"] = float(num) * mult

    # 渠道（可多个）
    chans = [c for c in _CHANNELS if c in text]
    if chans:
        slots["channels"] = chans

    # 受众（可多个）
    auds = [a for a in _AUDIENCE if a in text]
    if auds:
        slots["audience"] = auds

    # 截止日期
    for rx in _DATE_PATTERNS:
        m = rx.search(text)
        if m:
            g = m.groups()
            if len(g) == 3:
                y, mo, d = _norm_year(int(g[0])), int(g[1]), int(g[2])
            else:
                mo, d = int(g[0]), int(g[1])
                y = time.localtime().tm_year
            slots["deadline"] = f"{y:04d}-{mo:02d}-{d:02d}"
            break

    return slots


# ---------------------------------------------------------------------------
# 模板定义
# 每个模板：keywords（打分用）、stages（有序子任务定义）
# stage 字段：key/名称/指令模板/技能提示/模型提示/依赖 key 列表/质量规则
# ---------------------------------------------------------------------------

def _fmt(instruction: str, slots: dict) -> str:
    """把槽位填进指令模板。未抽到的槽位用 <待补:key> 标出，不静默编造。"""
    def rep(m):
        k = m.group(1)
        if k in slots and slots[k] not in (None, "", []):
            v = slots[k]
            if isinstance(v, list):
                return "、".join(str(x) for x in v)
            return str(v)
        return f"<待补:{k}>"
    return re.sub(r"\{(\w+)\}", rep, instruction)


TEMPLATES: list[dict] = [
    {
        "name": "content_marketing",
        "title": "内容营销全案",
        "keywords": ("营销", "推广", "campaign", "文案", "种草", "投放", "品牌",
                     "双十一", "双11", "618", "预热", "转化", "获客"),
        "stages": [
            {"key": "research", "name": "市场调研",
             "instruction": "拉取{topic}近3年的赛道数据、竞品活动数据与目标用户偏好，"
                            "输出带数据来源的结构化调研简报。",
             "skill": "research-analyst", "model": "",
             "quality": {"min_chars": 300, "required": ["数据", "来源"],
                         "forbidden": ["TODO"], "format": "markdown"}},
            {"key": "strategy", "name": "策略制定",
             "instruction": "基于调研简报，输出{topic}的活动策略：目标人群、核心主张、"
                            "渠道分配（可用渠道：{channels}）与节奏排期。",
             "skill": "strategy-planner", "model": "",
             "depends_on": ["research"],
             "quality": {"min_chars": 300, "required": ["策略", "渠道"],
                         "forbidden": ["TODO"], "format": "markdown"}},
            {"key": "copy", "name": "文案撰写",
             "instruction": "按策略为各渠道撰写{topic}宣传文案（每渠道至少2版），"
                            "风格适配渠道特性。",
             "skill": "copywriter", "model": "",
             "depends_on": ["strategy"],
             "quality": {"min_chars": 200, "forbidden": ["TODO", "待补充"],
                         "format": "markdown"}},
            {"key": "design_brief", "name": "设计需求",
             "instruction": "输出{topic}主视觉与各渠道素材的设计需求文档（尺寸/风格/"
                            "文案摆放/禁忌），供设计同事执行。",
             "skill": "design-brief-writer", "model": "",
             "depends_on": ["strategy"],
             "quality": {"min_chars": 150, "required": ["视觉", "尺寸"],
                         "format": "markdown"}},
            {"key": "roi", "name": "投放测算",
             "instruction": "测算{topic}各渠道的ROI预估与预算分配（预算：{budget_cny}元），"
                            "给出保守/中性/乐观三档。",
             "skill": "finance-analyst", "model": "",
             "depends_on": ["strategy"],
             "quality": {"min_chars": 200, "required": ["ROI", "预算"],
                         "format": "markdown"}},
            {"key": "compliance", "name": "合规审核",
             "instruction": "审核全部文案与素材是否违反广告法（绝对化用语/虚假宣传/"
                            "功效承诺/未成年人相关内容），列出风险点与修改建议。",
             "skill": "compliance-reviewer", "model": "",
             "depends_on": ["copy", "design_brief"],
             "quality": {"min_chars": 100, "required": ["合规", "风险"],
                         "format": "markdown"}},
            {"key": "assembly", "name": "方案汇总",
             "instruction": "把调研、策略、文案、设计需求、投放测算、合规意见汇总为"
                            "{topic}完整方案包，标注未决事项。",
             "skill": "editor", "model": "",
             "depends_on": ["compliance", "roi"],
             "approval_action": "publish",
             "quality": {"min_chars": 400, "required": ["方案", "汇总"],
                         "format": "markdown"}},
        ],
    },
    {
        "name": "software_dev",
        "title": "软件功能研发",
        "keywords": ("开发", "功能", "APP", "应用", "软件", "接口", "API", "需求",
                     "架构", "前端", "后端", "测试", "部署", "上线", "重构", "修bug",
                     "小程序", "网站"),
        "stages": [
            {"key": "requirements", "name": "需求梳理",
             "instruction": "把{topic}的原始诉求整理为用户故事 + 验收标准 + 非功能需求"
                            "（性能/安全/兼容），标注模糊点。",
             "skill": "requirements-analyst", "model": "",
             "quality": {"min_chars": 200, "required": ["验收"],
                         "format": "markdown"}},
            {"key": "architecture", "name": "架构设计",
             "instruction": "基于需求给出{topic}技术架构：模块划分、数据模型、接口契约、"
                            "技术选型与风险。",
             "skill": "architect", "model": "",
             "depends_on": ["requirements"],
             "quality": {"min_chars": 300, "required": ["架构", "接口"],
                         "format": "markdown"}},
            {"key": "frontend", "name": "前端实现",
             "instruction": "按架构实现{topic}前端模块（组件/页面/状态管理），附自测说明。",
             "skill": "frontend-dev", "model": "",
             "depends_on": ["architecture"],
             "quality": {"min_chars": 100, "required": ["组件"],
                         "format": "markdown"}},
            {"key": "backend", "name": "后端实现",
             "instruction": "按架构实现{topic}后端模块（接口/数据层/业务逻辑），附自测说明。",
             "skill": "backend-dev", "model": "",
             "depends_on": ["architecture"],
             "quality": {"min_chars": 100, "required": ["接口"],
                         "format": "markdown"}},
            {"key": "write_code", "name": "代码落盘",
             "instruction": "把前端实现与后端实现产出的代码提取为文件并写入工作区，"
                            "随后自动跑密钥扫描；命中高危凭证则挂起等人工确认。",
             "skill": "", "model": "",
             "op": "write_files", "from_upstream": True, "overwrite": True,
             "depends_on": ["frontend", "backend"],
             "quality": {"min_chars": 10, "required": ["落盘"]}},
            {"key": "testing", "name": "测试验证",
             "instruction": "为{topic}编写并执行测试用例（单元+集成），记录覆盖率与失败项。"
                            "代码已落盘到工作区，请基于实际文件测试。",
             "skill": "qa-engineer", "model": "",
             "depends_on": ["write_code"],
             "quality": {"min_chars": 100, "required": ["测试"],
                         "forbidden": ["TODO"], "format": "markdown"}},
            {"key": "docs", "name": "文档编写",
             "instruction": "编写{topic}使用说明与部署运维文档。",
             "skill": "tech-writer", "model": "",
             "depends_on": ["testing"],
             "quality": {"min_chars": 150, "required": ["文档"],
                         "format": "markdown"}},
        ],
    },
    {
        "name": "research_report",
        "title": "数据研究报告",
        "keywords": ("报告", "研报", "分析", "行业", "调研报告", "白皮书", "年报",
                     "市场分析", "竞品分析", "尽调"),
        "stages": [
            {"key": "data", "name": "数据拉取",
             "instruction": "拉取{topic}最新数据（多源交叉验证），记录来源、口径与时间。",
             "skill": "research-analyst", "model": "",
             "quality": {"min_chars": 300, "required": ["数据", "来源"],
                         "format": "markdown"}},
            {"key": "analysis", "name": "分析建模",
             "instruction": "基于数据做{topic}深度分析：结构/趋势/归因/对比，给出可证伪的结论。",
             "skill": "analyst", "model": "",
             "depends_on": ["data"],
             "quality": {"min_chars": 400, "required": ["结论", "分析"],
                         "format": "markdown"}},
            {"key": "writing", "name": "报告撰写",
             "instruction": "把分析结果写成{topic}结构化报告（摘要/正文/图表建议/风险）。",
             "skill": "report-writer", "model": "",
             "depends_on": ["analysis"],
             "quality": {"min_chars": 500, "required": ["摘要"],
                         "format": "markdown"}},
            {"key": "review", "name": "校验纠错",
             "instruction": "校验{topic}报告：数据引用与源一致、逻辑无跳步、术语统一、"
                            "错别字与格式问题清单。",
             "skill": "reviewer", "model": "",
             "depends_on": ["writing"],
             "quality": {"min_chars": 100, "required": ["校验"],
                         "format": "markdown"}},
            {"key": "typeset", "name": "排版交付",
             "instruction": "按模板对{topic}报告排版，生成目录/图表编号/页眉页脚，输出最终版。",
             "skill": "typesetter", "model": "",
             "depends_on": ["review"],
             "quality": {"min_chars": 500, "required": ["目录"],
                         "format": "markdown"}},
        ],
    },
    {
        "name": "customer_service",
        "title": "客服工单处理",
        "keywords": ("客服", "咨询", "工单", "投诉", "售后", "退换货", "用户运营",
                     "分层", "私域", " retention", "留存"),
        "stages": [
            {"key": "triage", "name": "问题分流",
             "instruction": "对{topic}相关咨询按类型分流（常见问题/售后复杂问题/投诉），"
                            "标注优先级与情绪等级。",
             "skill": "triage-agent", "model": "",
             "quality": {"min_chars": 100, "required": ["分类"],
                         "format": "markdown"}},
            {"key": "faq", "name": "常见问题应答",
             "instruction": "基于知识库撰写{topic}常见问题的标准应答（含安抚话术）。",
             "skill": "faq-agent", "model": "",
             "depends_on": ["triage"],
             "quality": {"min_chars": 150, "required": ["应答"],
                         "format": "markdown"}},
            {"key": "aftersale", "name": "售后处理",
             "instruction": "给出{topic}退换货/投诉的处理方案与补偿边界，标注需人工介入的情形。",
             "skill": "aftersale-agent", "model": "",
             "depends_on": ["triage"],
             "quality": {"min_chars": 150, "required": ["方案"],
                         "format": "markdown"}},
            {"key": "insight", "name": "反馈分析",
             "instruction": "汇总{topic}服务数据，分析高频问题与流程瓶颈，给出优化建议。",
             "skill": "data-analyst", "model": "",
             "depends_on": ["faq", "aftersale"],
             "quality": {"min_chars": 200, "required": ["建议"],
                         "format": "markdown"}},
        ],
    },
    {
        "name": "generic",
        "title": "通用任务（兜底）",
        "keywords": (),  # 兜底模板不参与关键词竞争
        "stages": [
            {"key": "understand", "name": "需求理解",
             "instruction": "复述并明确化{topic}：目标、约束、验收标准、未决问题。",
             "skill": "planner", "model": "",
             "quality": {"min_chars": 100, "required": ["目标"],
                         "format": "markdown"}},
            {"key": "draft", "name": "初稿产出",
             "instruction": "完成{topic}的初稿产出。",
             "skill": "writer", "model": "",
             "depends_on": ["understand"],
             "quality": {"min_chars": 200, "forbidden": ["TODO", "待补充"],
                         "format": "markdown"}},
            {"key": "review", "name": "审核修订",
             "instruction": "审核{topic}初稿：完整性/一致性/质量门槛，列出必须修订项。",
             "skill": "reviewer", "model": "",
             "depends_on": ["draft"],
             "quality": {"min_chars": 100, "required": ["审核"],
                         "format": "markdown"}},
            {"key": "finalize", "name": "终稿交付",
             "instruction": "按审核意见修订并交付{topic}终稿，附变更说明。",
             "skill": "editor", "model": "",
             "depends_on": ["review"],
             "quality": {"min_chars": 200, "required": ["交付"],
                         "format": "markdown"}},
        ],
    },
    {
        "name": "secure_dev",
        "title": "内部研发安全工具链",
        "keywords": ("代码审查", "密钥扫描", "安全扫描", "内审", "合规审查",
                     "代码安全", "secret", "密钥", "凭证", "安全工具链", "安全审计"),
        "stages": [
            {"key": "code_review", "name": "代码安全审查",
             "instruction": "对 {repo_path} 做安全视角的代码审查：注入/越权/反序列化/"
                            "硬编码凭证/日志泄露/依赖风险，输出风险清单与修复建议。",
             "skill": "security-reviewer", "model": "",
             "quality": {"min_chars": 300, "required": ["风险", "修复"],
                         "forbidden": ["TODO"], "format": "markdown"}},
            {"key": "secret_scan", "name": "密钥扫描",
             "instruction": "对 {repo_path} 做密钥/凭证扫描，列出命中的文件、规则与"
                            "处置建议（高危项须阻断合入）。",
             "skill": "", "model": "", "op": "scan", "target": "{repo_path}",
             "depends_on": [],
             "quality": {}},
            {"key": "report", "name": "安全报告汇总",
             "instruction": "综合代码审查与密钥扫描结论，输出安全报告：风险等级、"
                            "必须修复项、是否阻断发布。",
             "skill": "security-reporter", "model": "",
             "depends_on": ["secret_scan"],
             "quality": {"min_chars": 200, "required": ["风险等级", "修复"],
                         "format": "markdown"}},
        ],
    },
]


class PlanError(ValueError):
    """计划结构错误。"""


# 成本路由三档：多 AI 协同的 token 消耗近似 任务数 × 轮次 × 并行度，
# 不分流的后果是"简单任务也付全价 DAG"。
COMPLEXITY_TIERS = ("simple", "medium", "complex")

# 每任务的重做轮次预估（与 QualityGate.max_rework 默认值对齐：打回是常态不是异常）
REWORK_ROUNDS = 3


# ---------------------------------------------------------------------------
# Plan：纯数据（可 JSON 序列化 / 可回放 / 可 diff）
# ---------------------------------------------------------------------------

class Plan:
    """一份拆解结果：子任务列表 + 元信息。不碰数据库——build() 才落库。"""

    def __init__(self, requirement: str, template: str, title: str,
                 slots: dict, subtasks: list[dict],
                 tier: str = "complex", cost: dict = None):
        self.requirement = requirement
        self.template = template
        self.title = title
        self.slots = slots
        self.subtasks = subtasks  # [{key, name, instruction, skill, model,
                                  #   depends_on, quality, fanout?}]
        # 成本路由档位（simple/medium/complex）与成本估算（见 Planner._apply_tier）
        self.tier = tier if tier in COMPLEXITY_TIERS else "complex"
        self.cost = cost or {}

    # ---- 序列化（审计/回放/跨进程传递）----
    def to_dict(self) -> dict:
        return {"requirement": self.requirement, "template": self.template,
                "title": self.title, "slots": self.slots,
                "subtasks": self.subtasks, "tier": self.tier,
                "cost": self.cost, "created_at": time.time()}

    def to_json(self, **kw) -> str:
        kw.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kw)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(d.get("requirement", ""), d.get("template", ""),
                   d.get("title", ""), d.get("slots", {}),
                   d.get("subtasks", []), d.get("tier", "complex"),
                   d.get("cost"))

    @classmethod
    def from_json(cls, s: str) -> "Plan":
        return cls.from_dict(json.loads(s))

    # ---- 视图 ----
    def critical_path(self) -> list[str]:
        """最长依赖链（medium 档线性化的客观依据：模板的主干是什么）。

        返回从入口到出口的最长 key 序列；并列时任取一条（长度相同则成本相同）。
        """
        g = {s["key"]: list(s.get("depends_on", [])) for s in self.subtasks}
        if not g:
            return []
        depth: dict[str, int] = {}
        prev: dict[str, str] = {}

        def d(tid, guard):
            if tid in depth:
                return depth[tid]
            if tid in guard:
                return 0
            guard.add(tid)
            best, bp = 0, None
            for x in g.get(tid, []):
                v = d(x, guard)
                if v + 1 > best:
                    best, bp = v + 1, x
            depth[tid], prev[tid] = best, bp
            guard.discard(tid)
            return best

        for tid in g:
            d(tid, set())
        end = max(depth, key=lambda k: depth[k])
        path = []
        while end is not None:
            path.append(end)
            end = prev.get(end)
        return list(reversed(path))
    def waves(self) -> list[list[str]]:
        """并行波次（与 DagScheduler.waves 同口径，按 key 分层）。"""
        g = {s["key"]: list(s.get("depends_on", [])) for s in self.subtasks}
        depth: dict[str, int] = {}

        def d(tid, guard):
            if tid in depth:
                return depth[tid]
            if tid in guard:
                return 0
            guard.add(tid)
            deps = g.get(tid, [])
            depth[tid] = 0 if not deps else 1 + max(d(x, guard) for x in deps)
            guard.discard(tid)
            return depth[tid]

        for tid in g:
            d(tid, set())
        if not depth:
            return []
        out = [[] for _ in range(max(depth.values()) + 1)]
        for tid, w in depth.items():
            out[w].append(tid)
        return [sorted(w) for w in out]

    def validate(self) -> None:
        """结构自检：依赖必须存在且无环。模板静态定义保证成立，运行时双保险。"""
        keys = [s["key"] for s in self.subtasks]
        if len(set(keys)) != len(keys):
            raise PlanError(f"子任务 key 重复: {keys}")
        g = {s["key"]: list(s.get("depends_on", [])) for s in self.subtasks}
        for k, deps in g.items():
            for d in deps:
                if d not in g:
                    raise PlanError(f"未知依赖: {k} -> {d}")
        # DFS 环检测
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {k: WHITE for k in g}

        def visit(n):
            color[n] = GRAY
            for x in g[n]:
                if color[x] == GRAY:
                    raise PlanError(f"计划内成环: ... -> {n} -> {x} -> ...")
                if color[x] == WHITE:
                    visit(x)
            color[n] = BLACK

        for k in g:
            if color[k] == WHITE:
                visit(k)

    # ---- 落库：在 DagScheduler 上建图 ----
    def build(self, scheduler, plan_id: str = None,
              ballot=None, trace_id: str = None,
              web_search: bool = False) -> list[str]:
        """按拓扑序把子任务 add_task 进 scheduler。返回创建的 task_id 列表。

        task_id 规则：`{plan_id}/{key}`（plan_id 缺省自动生成）。
        同一计划重复 build 会因 task_id 已存在被 DagScheduler 拒绝（幂等保护）。
        payload 里带 name/instruction/skill/model/quality/plan 元信息，
        供 worker 认领后知道"干什么、怎么干、干成什么样算合格"。

        **trace_id**（可选）：传入时写进每个任务 payload 的 `trace_id` 键，
        全链路事件经 DagScheduler 的 tracer 旁路汇成一条可回放轨迹；
        scheduler 注入了 tracer 才会记 `plan_built`（拆解结果）事件。

        **fanout（投票/辩论模式）**：stage 带 `fanout: N`（N>=2）时展开为——
        N 个并行分支 `{pid}/{key}#0..N-1`（payload 带 `ballot` 元信息）
        + 1 个汇合任务 `{pid}/{key}__ballot`（payload 带 `ballot_gather`）。
        下游若依赖该 key，依赖自动改指向汇合任务，因此 DAG 仍是一个
        自洽有向无环图，**DagScheduler 无需知情**（展开纯属建图技巧）。
        ballot 参数：开票用的 Ballot 实例（None 时按需临时创建）。
        """
        self.validate()
        self._web_search = bool(web_search)  # 搜索意图：建图时透传给每个 llm 子任务
        pid = plan_id or f"plan-{int(time.time() * 1000):x}"
        # 拓扑序：按 waves 展开，保证添上游先于下游
        order = [k for wave in self.waves() for k in wave]
        by_key = {s["key"]: s for s in self.subtasks}
        created = []
        # fanout key -> 汇合任务 id（下游依赖必须指向汇合点而非某个分支）
        gather_of: dict[str, str] = {}

        def deps_of(s: dict) -> list[str]:
            return [gather_of.get(d, f"{pid}/{d}")
                    for d in s.get("depends_on", [])]

        for key in order:
            s = by_key[key]
            n = int(s.get("fanout") or 0)
            if n < 2:
                tid = f"{pid}/{key}"
                p = self._payload(pid, s, key)
                if trace_id:
                    p["trace_id"] = trace_id
                scheduler.add_task(tid, payload=p, depends_on=deps_of(s))
                created.append(tid)
                continue

            # ---- fanout 展开：N 个并行分支 + 1 个汇合点 ----
            group = f"{pid}/{key}"
            branch_ids = []
            for i in range(n):
                tid = f"{pid}/{key}#{i}"
                p = self._payload(pid, s, key)
                p["ballot"] = {"group": group, "index": i, "total": n}
                if trace_id:
                    p["trace_id"] = trace_id
                scheduler.add_task(tid, payload=p, depends_on=deps_of(s))
                branch_ids.append(tid)
                created.append(tid)
            gid = f"{pid}/{key}__ballot"
            gather_payload = {
                "op": "llm",
                "name": f"{s['name']}（投票汇总）",
                "instruction": (f"汇合 {n} 个并行方案并择优后交付："
                                f"{s['instruction']}"),
                "skill": s.get("skill", ""),
                "model": s.get("model", ""),
                "quality": s.get("quality", {}),
                "plan": {"id": pid, "template": self.template,
                         "title": self.title,
                         "requirement": self.requirement,
                         "tier": self.tier, "cost": self.cost},
                "needs_input": sorted(set(re.findall(r"<待补:(\w+)>",
                                                     s["instruction"]))),
                "ballot_gather": {"group": group, "n": n,
                                  "branch_ids": branch_ids},
            }
            # 终态阶段的审批动作（如发布审批）随汇合点继承：选出的方案同样
            # 需要人工审批后才算发布——择优与发布审批两条纪律都保留。
            if s.get("approval_action"):
                gather_payload["approval_action"] = s["approval_action"]
            # 出域审批门随汇合点继承（与 approval_action 同理）
            for k in ("kb_refs", "egress_destination"):
                if s.get(k):
                    gather_payload[k] = s[k]
            for k in SKILL_PAYLOAD_KEYS:
                if s.get(k):
                    gather_payload[k] = s[k]
            if trace_id:
                gather_payload["trace_id"] = trace_id
            scheduler.add_task(gid, payload=gather_payload,
                               depends_on=branch_ids)
            gather_of[key] = gid
            created.append(gid)

            # 开票：建图即登记，N 个分支与判据一次性绑定
            # 自建的 Ballot 用完即关（连接纪律：不泄漏）；调用方传入的
            # 实例所有权归调用方，本方法不关。
            own_ballot = ballot is None
            if own_ballot:
                from ballot import Ballot
                ballot = Ballot()
            try:
                ballot.open_ballot(group, branch_ids,
                                   rules=s.get("quality") or {},
                                   title=f"{self.title} / {s['name']}")
            except Exception as e:  # noqa: BLE001
                # 开票失败不阻断建图（重复 build 时组已存在属正常）
                from ballot import BallotError
                if not isinstance(e, BallotError):
                    raise
            finally:
                if own_ballot:
                    ballot.close()
                    ballot = None
        # 轨迹旁路：拆解结果入 Trace（scheduler 注入了 tracer 才记）
        if trace_id:
            hook = getattr(scheduler, "trace_event", None)
            if callable(hook):
                hook("plan_built", trace_id=trace_id,
                     summary=f"拆解 {len(created)} 个任务",
                     detail={"plan_id": pid, "template": self.template,
                             "tier": self.tier, "title": self.title,
                             "tasks": created,
                             "needs_input": sorted({
                                 m for s in self.subtasks
                                 for m in re.findall(r"<待补:(\w+)>",
                                                     s["instruction"])})})
        return created

    def _payload(self, pid: str, s: dict, key: str) -> dict:
        p = {
            # op 必须写全：执行层（execution/daemon.py）按 op 派发。
            # 缺 op 的任务被认领后必然 failed（"未知 op: ''"）——
            # 那是登记时没写全，不是执行失败。
            "op": s.get("op", "llm"),
            "name": s["name"],
            "instruction": s["instruction"],
            "skill": s.get("skill", ""),
            "model": s.get("model", ""),
            "quality": s.get("quality", {}),
            "plan": {"id": pid, "template": self.template,
                     "title": self.title, "requirement": self.requirement,
                     # 成本路由元信息：让"这个需求按什么档位拆的、预计多少调用"
                     # 在每个任务的 payload 里可查（可观测、可归因）
                     "tier": self.tier, "cost": self.cost},
            "needs_input": sorted(set(re.findall(r"<待补:(\w+)>",
                                                 s["instruction"]))),
        }
        # 发布审批等：把 stage 声明的审批动作透传给执行层审批门
        if s.get("approval_action"):
            p["approval_action"] = s["approval_action"]
        # 搜索意图：同一需求的拆解出的 llm 子任务都继承「真实联网搜索」标记，
        # 否则走 planner 路径的搜索类任务（如"今天重要国际新闻"）会漏标、
        # 退化成模型凭记忆编造——与 console_api.create_task 的单任务打标保持一致。
        if getattr(self, "_web_search", False) and p["op"] == "llm":
            p["web_search"] = True
        # scan op 的扫描目标（已在 decompose 阶段做了 {slot} 填充）
        if s.get("target"):
            p["target"] = s["target"]
        # write_files op 的落盘控制（把产出的代码真正写进工作区）：
        #   from_upstream —— 直接消费上游任务产出的代码块；
        #   overwrite     —— 允许覆盖已存在文件（迭代开发场景的常态）；
        #   root          —— 落盘根目录，仍受工作区边界约束。
        for k in ("from_upstream", "overwrite", "root", "files"):
            if s.get(k):
                p[k] = s[k]
        # 出域审批门（A1）：显式声明的 KB 引用 / 外发目标必须透传到执行层，
        # 否则 daemon 看不到、出域门形同虚设（与 _payload 双处白名单纪律一致）。
        for k in ("kb_refs", "egress_destination"):
            if s.get(k):
                p[k] = s[k]
        # v2：Skill 检索执行路径的字段白名单透传（与 console_api 一致）
        for k in SKILL_PAYLOAD_KEYS:
            if s.get(k):
                p[k] = s[k]
        return p


# ---------------------------------------------------------------------------
# Planner：选模板 + 抽槽位 -> Plan
# ---------------------------------------------------------------------------

class Planner:
    """规则模板版拆解器。decompose() 是纯函数，不碰 IO——可单测、可回放。"""

    def __init__(self, templates: list[dict] = None):
        self.templates = templates if templates is not None else TEMPLATES

    def _match_template(self, text: str) -> tuple[dict, dict]:
        """关键词打分选模板。返回 (模板, 得分明细)。全 0 分走兜底。"""
        low = text.lower()
        scores = {}
        for t in self.templates:
            if not t["keywords"]:  # 兜底模板不参与竞争
                continue
            hit = [k for k in t["keywords"] if k.lower() in low]
            if hit:
                scores[t["name"]] = {"score": len(hit), "hits": hit}
        if not scores:
            generic = next(t for t in self.templates if not t["keywords"])
            return generic, {"score": 0, "hits": []}
        best = max(scores, key=lambda n: scores[n]["score"])
        tpl = next(t for t in self.templates if t["name"] == best)
        return tpl, scores[best]

    def decompose(self, requirement: str, params: dict = None,
                  tier: str = None) -> Plan:
        """一句需求 -> 一份 Plan。

        params：调用方显式传入的槽位（优先级高于文本抽取），
        用于"人类补全 <待补:key>"的回路——规则版不猜，但允许被告知。

        **成本路由**：缺省按复杂度自动分档（simple/medium/complex），
        判据全部可解释（见 `_estimate_complexity` 的返回 reasons）；
        tier 参数可强制指定（运维手动覆盖自动判断）。

        **投票模式入口**：需求里出现多方案/择优类关键词时，给模板的
        **最终交付 stage** 注入 `fanout: N`（同任务并行派 N 个 worker 各出方案，
        建图时展开为 N 分支 + 汇合点，由 ballot.py 择优）。这是规则版能做的
        最保守注入——只认明确意图，不猜"这个需求也许需要多方案"。
        """
        if not requirement or not requirement.strip():
            raise PlanError("需求不能为空")
        tpl, match = self._match_template(requirement)
        slots = extract_slots(requirement)
        if params:
            slots.update({k: v for k, v in params.items()
                          if v not in (None, "", [])})
        # v2：调用方显式传入的 skill_hint / query（渐进接入：保留模板硬编码
        # skill 字段作 fallback，只有 op=skill 的 stage 才会消费检索意图）
        skill_hint = str((params or {}).get("skill_hint")
                         or (params or {}).get("query") or "")
        fanout = self._detect_fanout(requirement)
        subtasks = []
        for st in tpl["stages"]:
            # stage 可声明 op（默认 llm）/ approval_action（发布审批等）
            # / target（scan op 的扫描目标，支持 {slot} 占位）——执行层据此派发。
            op = st.get("op", "llm")
            target = st.get("target")
            if target:
                target = _fmt(target, slots)
            sub = {
                "key": st["key"],
                "name": st["name"],
                "instruction": _fmt(st["instruction"], slots),
                "skill": st.get("skill", ""),
                "model": st.get("model", ""),
                "op": op,
                "depends_on": list(st.get("depends_on", [])),
                "quality": dict(st.get("quality", {})),
            }
            if st.get("approval_action"):
                sub["approval_action"] = st["approval_action"]
            if target:
                sub["target"] = target
            # write_files op 的落盘控制（把产出的代码真正写进工作区）：
            #   from_upstream —— 直接消费上游任务产出的代码块；
            #   overwrite     —— 允许覆盖已存在文件（迭代开发的常态）；
            #   root / files  —— 静态落盘根目录与清单。
            # 不写这里，落盘工序拿到的是空 payload（此前正是断在这一步）。
            for k in ("from_upstream", "overwrite", "root", "files"):
                if st.get(k):
                    sub[k] = st[k]
            # 出域审批门（A1）：decompose 不走 _payload，各自白名单构造 subtask，
            # kb_refs/egress_destination 两处都要透传，否则静默丢弃（踩过坑）。
            for k in ("kb_refs", "egress_destination"):
                if st.get(k):
                    sub[k] = st[k]
            if skill_hint:
                sub["skill_hint"] = skill_hint
            for k in SKILL_PAYLOAD_KEYS:
                if st.get(k):
                    sub[k] = st[k]
            subtasks.append(sub)

        # ---- 成本路由：先分档，再按档裁剪 ----
        auto_tier, reasons, score = self._estimate_complexity(
            requirement, tpl, slots, fanout, match)
        eff_tier = tier if tier in COMPLEXITY_TIERS else auto_tier

        # ---- 文生图意图：跳过模板分档，直接单任务 op=image ----
        # 画图请求不需要拆解成多工序 DAG，也不该被塞进 LLM 文本流水线
        # （曾因此触发质量门反复打回、升级 needs_human）。原话即绘制提示，
        # 指令保持原样，不套 _apply_tier 的"单任务直出"重写模板。
        if self._is_image_request(requirement):
            subtasks = [{
                "key": "image",
                "name": "图像生成",
                "instruction": requirement.strip(),
                "skill": "",
                "model": "",
                "op": "image",
                "depends_on": [],
                "quality": {},
            }]
            eff_tier = "simple"
            fanout = 0
        else:
            subtasks = self._apply_tier(subtasks, eff_tier, slots, requirement)
            if eff_tier == "simple":
                # 只有单任务档禁投票：medium 是线性链但叶子仍是唯一交付物，
                # 并行出多方案完全成立（3 分支 + 汇合点，成本翻倍但可择优）
                fanout = 0

        if fanout:
            # fanout 只注入到最后交付阶段（无人依赖它的叶子），
            # 避免把中间环节也并行化——投票比的是"最终方案"，不是每个步骤。
            # 模板有多个叶子时不注入（无法判断哪个是交付物，不猜）。
            depended = {d for s in subtasks for d in s["depends_on"]}
            leaves = [s for s in subtasks if s["key"] not in depended]
            if len(leaves) == 1:
                leaves[0]["fanout"] = fanout
        cost = self._cost_estimate(subtasks, fanout)
        plan = Plan(requirement=requirement.strip(), template=tpl["name"],
                    title=tpl["title"], slots=slots, subtasks=subtasks,
                    tier=eff_tier, cost=cost)
        plan.match_info = {"template": tpl["name"], **match,
                           "tier": eff_tier, "tier_reasons": reasons,
                           "tier_score": score}
        plan.validate()
        return plan

    # ------------------------------------------------------------------
    # 成本路由（P1.5）：判据全部可解释，不确定往 complex 倒（质量优先）
    # ------------------------------------------------------------------
    def _estimate_complexity(self, requirement: str, tpl: dict,
                             slots: dict, fanout: int, match: dict = None):
        """结构特征评估复杂度 -> (tier, reasons, score)。

        判据（每条都可解释、可复现，无黑箱权重）：
          模板关键词命中数 / 模板阶段数 / 需求长度 / 槽位数 /
          多环节连接词 / 列举密度 / 多方案意图
        阈值取向：**score 不够低就升档**——路由错的代价不对称：
        把复杂任务判成 simple 会漏环节（质量事故），
        把简单任务判成 complex 只是多花钱（可观测、可追责）。
        """
        reasons = []
        score = 0
        n_stages = len(tpl["stages"])
        n = len(requirement.strip())

        # 最强信号：用户命中模板关键词的个数——命中越多，越说明他要的是
        # 这个模板的**完整形态**（"包括调研、文案、合规…"就是在点名要环节）。
        hits = list((match or {}).get("hits") or [])
        if len(hits) >= 4:
            score += 2
            reasons.append(f"命中模板 {len(hits)} 个关键词（明确点名多个环节）")
        elif len(hits) >= 2:
            score += 1
            reasons.append(f"命中模板 {len(hits)} 个关键词")

        if n_stages >= 6:
            score += 3
            reasons.append(f"模板 {n_stages} 阶段（>=6，环节多）")
        elif n_stages >= 5:
            score += 2
            reasons.append(f"模板 {n_stages} 阶段")

        if n >= 120:
            score += 2
            reasons.append(f"需求 {n} 字符（>=120，描述详尽）")
        elif n >= 60:
            score += 1
            reasons.append(f"需求 {n} 字符")

        if len(slots) >= 4:
            score += 1
            reasons.append(f"{len(slots)} 个槽位（参数多，需分别落实）")

        connectors = sum(requirement.count(w) for w in
                         ("并且", "同时", "以及", "包括", "接着", "然后", "再", "并"))
        if connectors >= 3:
            score += 2
            reasons.append(f"{connectors} 个多环节连接词（明确要求多步骤）")
        elif connectors >= 1:
            score += 1
            reasons.append(f"{connectors} 个多环节连接词")

        # 列举密度："A、B、C、D" 是多交付物的直接信号
        enum = requirement.count("、") + requirement.count("，")
        if enum >= 4:
            score += 1
            reasons.append(f"{enum} 处列举（多交付物信号）")

        if fanout:
            score += 2
            reasons.append(f"多方案意图（fanout={fanout}，成本翻倍）")

        if fanout:
            # 明确要多方案：至少 medium（单任务无从择优）
            tier = "complex" if score >= 6 else "medium"
        elif score >= 6:
            tier = "complex"
        elif score >= 3:
            tier = "medium"
        else:
            tier = "simple"
        return tier, reasons, score

    def _apply_tier(self, subtasks: list[dict], tier: str,
                    slots: dict = None, requirement: str = "") -> list[dict]:
        """按档位裁剪子任务图。

        complex = 全模板（原行为）；medium = 关键路径线性化（模板主干）；
        simple = 仅唯一交付叶子（单任务，单 AI 直接干）。
        **判据不明时不猜**：叶子不唯一或找不到关键路径 → 退回 complex。

        simple 档会**重写 instruction 使其自包含**：模板叶子的指令常引用
        上游（"按审核意见修订…"），而单任务直出没有上游——不重写就是
        让 worker 对着一份不存在的输入干活。
        """
        if tier == "complex" or not subtasks:
            return subtasks

        if tier == "simple":
            depended = {d for s in subtasks for d in s["depends_on"]}
            leaves = [s for s in subtasks if s["key"] not in depended]
            if len(leaves) != 1:
                return subtasks  # 交付物不唯一：不猜，退回 complex
            leaf = dict(leaves[0])
            leaf["depends_on"] = []
            leaf["name"] = f"{leaf['name']}（直出）"
            topic = ((slots or {}).get("topic")
                     or (requirement or "").strip()[:40] or leaf["name"])
            leaf["instruction"] = (
                f"直接交付「{topic}」的完整成果（单任务直出，无上游输入）："
                f"目标、要点、结论一次说清，并自查是否满足质量门槛。")
            return [leaf]

        # medium：最长依赖链线性化（客观主干，不靠人工挑）
        p = Plan("", "", "", {}, subtasks).critical_path()
        if len(p) < 2:
            return subtasks
        keep = set(p)
        out = []
        for s in subtasks:
            if s["key"] not in keep:
                continue
            i = p.index(s["key"])
            item = dict(s)
            item["depends_on"] = [p[i - 1]] if i > 0 else []
            out.append(item)
        return out if len(out) >= 2 else subtasks

    def _cost_estimate(self, subtasks: list[dict], fanout: int) -> dict:
        """成本估算：用"相对倍数"而非假造 token 数。

        产品自身不调 LLM（除可选 LLMPlanner），token 烧在接入的 worker 上，
        所以只能给**可归因的相对量**：任务数 × 重做轮次 × 并行度。
        """
        n = len(subtasks)
        fanout_n = int(fanout or 0)
        tasks = n - 1 + fanout_n + 1 if fanout_n >= 2 else n
        calls = tasks * REWORK_ROUNDS * max(1, fanout_n)
        return {
            "tier_tasks": n,
            "runtime_tasks": tasks,
            "rework_rounds": REWORK_ROUNDS,
            "llm_calls_estimate": calls,
            "multiplier_vs_single_ai": round(calls / 1.0, 1),
            "note": ("相对倍数（单 AI 基线 = 1），非绝对 token 数——"
                     "本产品自身不计量 token，烧在接入的 worker 上"),
        }

    # 投票模式触发词：出现即认为用户明确要"多方案择优"
    _FANOUT_WORDS = ("多方案", "多个方案", "几个方案", "方案对比", "择优",
                     "投票", "比稿", "不同角度", "多个角度", "多角度",
                     "多视角", "多种思路", "alternative", "options")
    _FANOUT_N = 3  # 默认并行几个方案（成本随 N 线性涨，3 是降幻觉与成本的折中）

    def _detect_fanout(self, text: str) -> int:
        """识别明确的"多方案择优"意图；拿不准就不开（0 = 不开票）。"""
        low = (text or "").lower()
        for w in self._FANOUT_WORDS:
            if w.lower() in low:
                return self._FANOUT_N
        return 0

    # 文生图意图关键词（与 console_chat.detect_image_intent 同源，但 planner
    # 单独持有一份以避免跨模块耦合——两者语义一致，改此需同步改彼）。
    _IMAGE_INTENT_KW = (
        "画一张", "画一幅", "画个", "画一", "画图", "绘制", "画张", "帮我画",
        "生成图片", "生成一张图", "生成图", "画张图", "配图", "插图", "插画",
        "画海报", "海报", "示意图", "漫画", "头像", "封面图", "生成图像",
        "文生图", "画个图", "做个图", "出一张图", "出图",
    )

    def _is_image_request(self, text: str) -> bool:
        """识别"需要文生图"的意图——命中即让 decompose 产出 op=image 叶子。"""
        low = (text or "").lower()
        return any(w in low for w in self._IMAGE_INTENT_KW)


# ---------------------------------------------------------------------------
# LLMPlanner：LLM 版拆解器（同一 decompose()/build() 接口，输出结构不变）
# ---------------------------------------------------------------------------

# LLM 只准输出这些 quality 规则键（与 QualityGate 消费面对齐，见 agentskills/quality.py）
_QUALITY_KEYS = ("min_chars", "max_chars", "required", "forbidden",
                 "no_unfilled", "format", "json_fields", "references")

_LLM_SYSTEM = """你是一个多 AI 协同系统的任务拆解器。把用户的一句需求拆成一张子任务 DAG。

只输出一个 JSON 对象（不要 markdown 代码围栏外的任何文字），格式：
{
  "title": "计划标题",
  "slots": {"topic": "主题", "channels": ["渠道1"], "budget_cny": 5000000},
  "subtasks": [
    {"key": "research", "name": "市场调研", "instruction": "具体指令（可含{topic}等槽位占位）",
     "skill": "专长提示", "model": "",
     "depends_on": [],
     "quality": {"min_chars": 300, "required": ["数据"], "forbidden": ["TODO"],
                 "no_unfilled": true, "format": "markdown"}}
  ]
}

硬规则：
1. key 唯一、小写英文短横线命名；depends_on 只能引用本计划内的 key，不得成环。
2. 子任务数 2-__MAX_TASKS__ 个；无依赖的可并行，有先后的用 depends_on 表达。
3. instruction 必须自包含（下游只看到这一条指令和上游产出），不写"同上"。
4. quality 只能使用这些键：__QUALITY_KEYS__。缺省建议给 no_unfilled=true。
5. 需求里没给的信息（预算/渠道/日期等）不要编造——对应槽位从 slots 省略即可，
   指令里用 {{待补键名}} 形式引用，例如 {{budget_cny}}。
6. 最后一个子任务通常是汇总/交付，依赖其余全部关键环节。"""


class LLMPlannerError(PlanError):
    """LLM 拆解失败（调用出错/输出不可解析/结构不合法）。"""


class LLMPlanner:
    """LLM 版拆解器。

    - chat：可调用对象，签名为 ``chat(messages: list[dict]) -> str``；
      也接受 models.ModelRouter（自动适配）。
    - 任何一步失败（调用异常 / JSON 不可解析 / 结构校验不过）都会
      **自动回退到规则模板版**，并在 plan.fallback_reason 记录原因——
      拆解器是流水线第一公里，宁可退化到可用的模板，也不让整条链路挂掉。
    """

    def __init__(self, chat=None, model: str = None, rule_planner: Planner = None,
                 max_tasks: int = 24):
        self.chat = chat
        self.model = model
        self.rule_planner = rule_planner or Planner()
        self.max_tasks = max_tasks

    # ---- prompt ----
    def _messages(self, requirement: str) -> list[dict]:
        system = (_LLM_SYSTEM
                  .replace("__MAX_TASKS__", str(self.max_tasks))
                  .replace("__QUALITY_KEYS__", ", ".join(_QUALITY_KEYS)))
        return [{"role": "system", "content": system},
                {"role": "user", "content": f"需求：{requirement}"}]

    # ---- 解析：容忍 ```json 围栏与首尾杂音 ----
    @staticmethod
    def _parse(text: str) -> dict:
        if not text or not text.strip():
            raise LLMPlannerError("LLM 返回空内容")
        s = text.strip()
        if "```" in s:  # 取第一个代码围栏内容
            parts = s.split("```")
            for i in range(1, len(parts), 2):
                body = parts[i]
                if body.lstrip().startswith("json"):
                    body = body.lstrip()[4:]
                body = body.strip()
                if body.startswith("{"):
                    s = body
                    break
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise LLMPlannerError("LLM 输出中找不到 JSON 对象")
        try:
            d = json.loads(s[start:end + 1])
        except json.JSONDecodeError as e:
            raise LLMPlannerError(f"JSON 解析失败: {e}") from e
        if not isinstance(d, dict):
            raise LLMPlannerError("顶层不是 JSON 对象")
        return d

    # ---- 结构规范化与校验 ----
    def _normalize(self, d: dict, slots: dict) -> list[dict]:
        subs = d.get("subtasks")
        if not isinstance(subs, list) or not subs:
            raise LLMPlannerError("subtasks 缺失或不是非空列表")
        if len(subs) > self.max_tasks:
            raise LLMPlannerError(f"子任务数超限: {len(subs)} > {self.max_tasks}")
        out = []
        for i, st in enumerate(subs):
            if not isinstance(st, dict):
                raise LLMPlannerError(f"subtasks[{i}] 不是对象")
            key = str(st.get("key", "")).strip()
            name = str(st.get("name", "")).strip()
            instruction = str(st.get("instruction", "")).strip()
            if not key or not name or not instruction:
                raise LLMPlannerError(f"subtasks[{i}] 缺少 key/name/instruction")
            deps = st.get("depends_on") or []
            if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
                raise LLMPlannerError(f"subtasks[{i}].depends_on 必须是字符串列表")
            q = st.get("quality") or {}
            if not isinstance(q, dict):
                raise LLMPlannerError(f"subtasks[{i}].quality 必须是对象")
            quality = {}
            for k in _QUALITY_KEYS:
                if k not in q:
                    continue
                v = q[k]
                if k in ("min_chars", "max_chars"):
                    quality[k] = int(v)
                elif k in ("required", "forbidden", "json_fields", "references"):
                    quality[k] = [str(x) for x in (v if isinstance(v, list) else [v])]
                elif k == "no_unfilled":
                    quality[k] = bool(v)
                else:
                    quality[k] = str(v)
            out.append({
                "key": key, "name": name,
                "instruction": _fmt(instruction, slots),
                "skill": str(st.get("skill", "") or ""),
                "model": str(st.get("model", "") or ""),
                "depends_on": list(deps),
                "quality": quality,
            })
        return out

    # ---- 主入口 ----
    def decompose(self, requirement: str, params: dict = None) -> Plan:
        if not requirement or not requirement.strip():
            raise PlanError("需求不能为空")
        fallback = lambda reason: self._fallback(requirement, params, reason)  # noqa: E731
        if self.chat is None:
            return fallback("未配置 chat，直接走规则模板版")
        try:
            text = self.chat(self._messages(requirement.strip()))
            if hasattr(text, "content"):  # ChatResult 适配
                text = text.content
            d = self._parse(text)
            # 槽位：params（人类补全）> LLM 给出 > 文本正则抽取
            slots = extract_slots(requirement)
            llm_slots = d.get("slots") or {}
            if isinstance(llm_slots, dict):
                slots.update({k: v for k, v in llm_slots.items()
                              if v not in (None, "", [])})
            if params:
                slots.update({k: v for k, v in params.items()
                              if v not in (None, "", [])})
            subtasks = self._normalize(d, slots)
            plan = Plan(requirement=requirement.strip(), template="llm",
                        title=str(d.get("title", "") or "").strip() or "LLM 拆解计划",
                        slots=slots, subtasks=subtasks)
            plan.match_info = {"template": "llm", "engine": "llm"}
            plan.validate()
            plan.fallback_reason = None
            return plan
        except PlanError as e:  # 含 LLMPlannerError
            return fallback(str(e))
        except Exception as e:  # chat 调用本身的任何异常
            return fallback(f"{type(e).__name__}: {e}")

    def _fallback(self, requirement: str, params: dict, reason: str) -> Plan:
        plan = self.rule_planner.decompose(requirement, params=params)
        plan.fallback_reason = reason
        plan.match_info = {"template": plan.template, "engine": "rule-fallback",
                           "fallback_reason": reason}
        return plan


if __name__ == "__main__":
    # 演示：规则模板版四个场景 + LLM 版（用假 chat 模拟一次成功与一次失败回退）
    demo = Planner()
    cases = [
        "做一场双十一美妆营销方案，预算500万，主投抖音和小红书，面向Z世代，10月23日前交",
        "开发一个用户登录功能，含前端页面和后端接口",
        "写一份2026年中国新能源汽车行业分析报告",
        "帮我整理一下团建方案",  # 兜底模板
    ]
    for req in cases:
        p = demo.decompose(req)
        print(f"\n=== 需求: {req}")
        print(f"模板: {p.template}（命中 {getattr(p, 'match_info', {}).get('hits')}）")
        print(f"槽位: {p.slots}")
        for w in p.waves():
            print("  波次:", w)
        print("  task_ids:", [f"{s['key']}" for s in p.subtasks])
