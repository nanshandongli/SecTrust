"""
示例内置技能：echo —— 原样回显输入文本。

演示一个技能的写法：定义 register(registry)，在其中用 registry.register(Skill(...))。
entry 接收 (payload, ctx)，返回 SkillResult。
"""

from skills import Skill, SkillResult


def echo_entry(payload: dict, ctx) -> SkillResult:
    text = payload.get("text", "")
    return SkillResult(content=f"[echo] {text}")


def register(registry) -> None:
    registry.register(
        Skill(
            name="echo",
            description="回显输入文本（示例技能）",
            entry=echo_entry,
            source="local",
            version="1.0.0",
        )
    )
