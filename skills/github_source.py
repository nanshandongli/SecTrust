"""
GitHubSkillProvider —— 从 Git 仓库按 tag/commit 拉取并加载技能。

⚠️ 这是「加载并执行远程代码」，务必遵守信任边界（DESIGN.md 3.3）：
  1. ref 必须 pin 到具体 tag 或 commit，禁止生产用浮动分支（如 main）。
  2. 只对可信仓库开启；建议配合 allowlist 只允许指定技能名。
  3. 尽量在隔离容器内运行技能执行。
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from skills import Skill, SkillProvider
from skills.local import LocalSkillProvider


class GitHubSkillProvider(SkillProvider):
    def __init__(self, repo: str, ref: str = "main", cache_dir: str = ".skill_cache",
                 allowlist: Optional[list] = None):
        self.repo = repo
        self.ref = ref
        self.cache_dir = cache_dir
        self.allowlist = set(allowlist) if allowlist else None
        self._local: Optional[LocalSkillProvider] = None

    @property
    def _repo_dir(self) -> str:
        # 用仓库名做子目录，避免多仓库冲突
        name = self.repo.rstrip("/").split("/")[-1].replace(".git", "")
        return os.path.join(self.cache_dir, name)

    @property
    def _skills_dir(self) -> str:
        return os.path.join(self._repo_dir, "skills")

    def sync(self) -> None:
        """git clone（首次）或 pull（之后），并 checkout 到固定 ref。"""
        if not self.repo:
            raise ValueError("GitHubSkillProvider 需要 repo 地址（config.github_repo）")
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.isdir(os.path.join(self._repo_dir, ".git")):
            subprocess.run(["git", "-C", self._repo_dir, "pull", "--ff-only"],
                           check=True, capture_output=True, text=True)
        else:
            subprocess.run(["git", "clone", self.repo, self._repo_dir],
                           check=True, capture_output=True, text=True)
        # pin 到具体 ref（tag / commit）。浮动分支仅用于开发，生产务必 pin。
        subprocess.run(["git", "-C", self._repo_dir, "checkout", self.ref],
                       check=True, capture_output=True, text=True)
        # 本地加载器复用：扫描 <repo>/skills
        self._local = LocalSkillProvider(self._skills_dir)
        self._local.sync()

    def _allowed(self, skill: Skill) -> bool:
        if self.allowlist is None:
            return True
        return skill.name in self.allowlist

    def list_skills(self) -> list[Skill]:
        if self._local is None:
            return []
        return [s for s in self._local.list_skills() if self._allowed(s)]

    def get(self, name: str) -> Optional[Skill]:
        if self._local is None:
            return None
        s = self._local.get(name)
        return s if (s and self._allowed(s)) else None
