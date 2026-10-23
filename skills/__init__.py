"""
技能层（Skill）—— 可插拔的能力单元。

两种来源：
  - LocalSkillProvider  ：从本地目录加载（可信、零依赖）。
  - GitHubSkillProvider ：按 tag/commit 从 Git 仓库拉取并加载（见 github_source.py）。

⚠️ 信任边界：「从 GitHub 拉取技能」= 加载并执行远程代码。必须 pin 到具体 commit/tag，
   只对可信仓库开启，建议配合 allowlist 与隔离环境。详见 DESIGN.md 第 3.3 节。
"""

from __future__ import annotations

import abc
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import ModelRouter
    from config import SkillConfig


@dataclass
class SkillResult:
    content: str = ""
    ok: bool = True
    error: Optional[str] = None


@dataclass
class RunContext:
    """传给技能执行的受限上下文。技能只拿到它，不直接拥有全局能力。"""
    model: "ModelRouter"          # 可在技能内继续调用模型
    config: Any = None            # RunnerConfig（只读）
    replies: list = field(default_factory=list)

    def reply(self, text: str) -> None:
        self.replies.append(text)

    def complete(self, messages, model: str = None, **opts):
        """便捷：在技能里直接调模型。"""
        return self.model.complete(messages, model=model, **opts)


@dataclass
class Skill:
    name: str
    description: str
    entry: Callable[[dict, RunContext], SkillResult]
    source: str = "local"      # "local" / "github"
    version: str = "0.0.0"


class SkillProvider(abc.ABC):
    """技能来源抽象：能同步(sync)、列举(list)、按名取(get)。"""

    @abc.abstractmethod
    def sync(self) -> None:
        """拉取/刷新技能（本地无需网络；GitHub 则 clone/pull）。"""
        raise NotImplementedError

    @abc.abstractmethod
    def list_skills(self) -> list[Skill]:
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, name: str) -> Optional[Skill]:
        raise NotImplementedError


class SkillRegistry(SkillProvider):
    """聚合多个来源的技能登记表。AgentRunner 只用它来按名取技能。"""

    def __init__(self):
        self._providers: list[SkillProvider] = []

    def add_provider(self, provider: SkillProvider) -> None:
        self._providers.append(provider)

    def sync(self) -> None:
        for p in self._providers:
            p.sync()

    def list_skills(self) -> list[Skill]:
        out: list[Skill] = []
        for p in self._providers:
            out.extend(p.list_skills())
        return out

    def get(self, name: str) -> Optional[Skill]:
        for p in self._providers:
            s = p.get(name)
            if s is not None:
                return s
        return None

    # 便捷：本地加载器常直接 register 进这里
    def register(self, skill: Skill) -> None:
        self._local_overrides[skill.name] = skill

    @property
    def _local_overrides(self) -> dict:
        if not hasattr(self, "_ov"):
            self._ov: dict = {}
        return self._ov

    def get_local(self, name: str) -> Optional[Skill]:
        return self._local_overrides.get(name)
