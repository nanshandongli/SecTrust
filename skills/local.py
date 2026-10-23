"""
LocalSkillProvider —— 从本地目录加载技能。

约定：目录下每个 .py 模块定义一个函数 `register(registry)`，在其中调用
`registry.register(Skill(...))`。参考 skills/builtin/echo.py。

零依赖、可信（代码就在本仓库里）。
"""

from __future__ import annotations

import importlib.util
import os
from typing import Optional

from skills import Skill, SkillProvider


class LocalSkillProvider(SkillProvider):
    def __init__(self, directory: str):
        self.directory = directory
        self._skills: dict[str, Skill] = {}

    def sync(self) -> None:
        """扫描目录并导入所有模块的 register()。"""
        self._skills.clear()
        if not os.path.isdir(self.directory):
            return
        for fname in sorted(os.listdir(self.directory)):
            if not fname.endswith(".py") or fname == "__init__.py":
                continue
            path = os.path.join(self.directory, fname)
            spec = importlib.util.spec_from_file_location(f"_skill_{fname[:-3]}", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                # 临时把 register 的目标指到本 provider
                class _Reg:
                    def register(self, skill: Skill):
                        self._owner._skills[skill.name] = skill
                reg = _Reg(); reg._owner = self
                mod.register(reg)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)
