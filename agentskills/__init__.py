"""
Agent Skills 子系统（agentskills.io 标准）+ 证据门禁。

借鉴 obra/superpowers（GitHub ~303K stars, MIT）——目前星最多的 Agent Skills
框架。它的价值在于把"怎么做对一件事"沉淀成可组合、可版本化、按需加载的技能包；
它的弱点在于**强制是软的**：SKILL.md 正文写着 "Mandatory workflows, not
suggestions"，但没有任何系统组件会拦下一个不听话的模型。

本模块做两件事：

1. `loader` —— 对齐 agentskills.io 的 SKILL.md 渐进式加载
   （Discovery → Activation → Execution），并把 superpowers 的写作规范
   （description 以 Use when 开头 / 第三人称 / ≤1024 字符 / name 只含连字符…）
   变成**程序化校验**：不合规的技能包拒绝上架，而不是指望模型自觉。

2. `evidence` —— 把 superpowers 最有价值的一条
   `verification-before-completion`（Evidence before claims）升级为**硬门禁**。
   本仓库有 superpowers 没有的两样东西：跨进程任务登记处与审批网关。
   于是"完成任务前必须有证据"可以被真正拦截，而不只是被劝告。

3. `quality` —— 质量回流回路：产出物不合格（缺必含内容/留 TODO/没引用上游）
   就打回重做（running→registered 带反馈），重试超限升级人工（needs_human），
   绝不静默放行。规则与 planner 的 quality 字段同一格式。

一句话：superpowers 教会模型**该做什么**，这里让系统**拦住不做的时候**。

零依赖：仅标准库。
"""

from .loader import (SkillLibrary, SkillSpec, SkillSpecError, load_one,
                     parse_frontmatter, validate)
from .evidence import (Evidence, EvidenceGate, Verdict, scan_rationalizations,
                       looks_like_command, RUN_ALLOWLIST)
from .quality import QualityGate, QualityVerdict

__all__ = [
    "SkillLibrary", "SkillSpec", "SkillSpecError", "load_one",
    "parse_frontmatter", "validate",
    "Evidence", "EvidenceGate", "Verdict", "scan_rationalizations",
    "looks_like_command", "RUN_ALLOWLIST",
    "QualityGate", "QualityVerdict",
]

__version__ = "0.1.0"
