"""
SKILL.md 加载器 —— 渐进式披露（Progressive Disclosure）。

借鉴 obra/superpowers（GitHub 303K stars, MIT）与 agentskills.io 规范：

    Discovery  → 只加载 name + description（约 100 tokens/个）
    Activation → 任务匹配时才读全文
    Execution  → 按需加载 supporting files

为什么要自己实现而不是只在 prompt 里写"请遵守"：
    superpowers 的强制是**软的**——它靠 SKILL.md 正文写着
    "Mandatory workflows, not suggestions"，模型可以选择不听。
    本模块把其中**可判定的部分**（frontmatter 格式、description 写法、
    name 合法性、体积预算）变成**程序化校验**，不合格的技能包直接拒绝加载，
    而不是指望模型自觉。

零依赖：不引入 PyYAML，frontmatter 只需 name/description 两个标量字段，
手写解析足够（本项目承诺 hard_required = 空集）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 规范约束（agentskills.io + superpowers/writing-skills）
MAX_DESCRIPTION = 1024          # description 最大字符数
NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")   # 只允许字母、数字、连字符
DESC_STARTS = ("Use when", "Use for", "Use to")
# description 必须"说明何时用"，绝不总结工作流——因为读了摘要就不会读正文
BANNED_DESC_MARKERS = ("step 1", "first, ", "workflow:", "1. ", "流程：", "步骤：")


@dataclass
class SkillSpec:
    """技能的『元数据 + 正文』。正文只在 activate 时才填充。"""
    name: str
    description: str
    dir_path: str
    body: str = ""                       # 空 = 未激活
    bytes: int = 0                       # SKILL.md 体积，Discovery 阶段用于估预算
    files: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)   # 校验发现的问题

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def tokens_est(self) -> int:
        """粗估 token 数（中英混排按 ~2.2 字节/token 折算）。

        Discovery 阶段不读正文（渐进式披露的核心），因此用文件体积估算，
        让调用方在激活前就能判断上下文预算。
        """
        n = len(self.body) if self.body else self.bytes
        return int(n / 2.2) + 40

    def manifest(self) -> dict:
        """Discovery 段：只暴露 name + description + 体积。"""
        return {
            "name": self.name,
            "description": self.description,
            "tokens_est": self.tokens_est,
            "ok": self.ok,
            "problems": self.problems,
        }


class SkillSpecError(ValueError):
    pass


def parse_frontmatter(text: str) -> Tuple[dict, str]:
    """解析 `---\\n...\\n---\\n<body>`。返回 (meta, body)。

    只支持标量 `key: value`，不支持嵌套/列表——SKILL.md 的 frontmatter
    按规范只有 name 与 description，复杂结构属于误用。
    """
    if not text.startswith("---"):
        raise SkillSpecError("缺少 YAML frontmatter（文件必须以 --- 开头）")
    end = text.find("\n---", 3)
    if end == -1:
        raise SkillSpecError("frontmatter 未闭合（找不到结束的 ---）")
    raw = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: Dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillSpecError(f"frontmatter 行不是 key: value 形式: {line!r}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip("'\"")
    return meta, body


def validate(name: str, description: str, body: str) -> List[str]:
    """把 superpowers 的写作规范变成可判定的检查项。返回问题列表（空=合格）。"""
    p: List[str] = []
    if not name:
        p.append("frontmatter 缺少 name")
    elif not NAME_RE.match(name):
        p.append(f"name 只允许字母/数字/连字符，实际 {name!r}")

    if not description:
        p.append("frontmatter 缺少 description")
    else:
        if len(description) > MAX_DESCRIPTION:
            p.append(f"description 超过 {MAX_DESCRIPTION} 字符（实际 {len(description)}）")
        if not description.startswith(DESC_STARTS):
            p.append(f"description 应以 {'/'.join(DESC_STARTS)} 开头（否则触发率低）")
        low = description.lower()
        for m in BANNED_DESC_MARKERS:
            if m in low:
                p.append(f"description 不应总结工作流（含 {m!r}）——读了摘要就不读正文")
                break
        if re.search(r"^\s*(I|We|My)\b", description):
            p.append("description 应使用第三人称")

    if not body.strip():
        p.append("SKILL.md 正文为空")
    elif len(body.strip()) < 200:
        p.append("SKILL.md 正文过短（<200 字符），难以构成有效指令")
    if len(body) > 20_000:
        p.append("SKILL.md 正文过长（>20k 字符），heavy reference 应拆到 supporting files")
    return p


def load_one(path: str) -> SkillSpec:
    """加载单个 SKILL.md 文件（完成 Discovery + 校验，不激活正文）。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    meta, body = parse_frontmatter(text)
    name = meta.get("name") or os.path.basename(os.path.dirname(path))
    desc = meta.get("description", "")
    problems = validate(name, desc, body)
    d = os.path.dirname(path)
    try:
        body_bytes = os.path.getsize(path)
    except OSError:
        body_bytes = len(body.encode("utf-8"))
    files = sorted(
        os.path.relpath(os.path.join(dp, fn), d).replace("\\", "/")
        for dp, dns, fns in os.walk(d)
        for fn in fns
        if fn != "SKILL.md"
        and not dp.endswith("__pycache__")
    )
    return SkillSpec(name=name, description=desc, dir_path=d, files=files,
                     bytes=body_bytes, problems=problems)


class SkillLibrary:
    """技能库：Discovery（目录扫描）→ Activation（按需读全文）。"""

    def __init__(self, roots: Optional[List[str]] = None):
        self.roots = roots or [default_library_dir()]
        self._specs: Dict[str, SkillSpec] = {}
        self.refresh()

    # ---- Discovery ----
    def refresh(self) -> int:
        self._specs.clear()
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                if "SKILL.md" in filenames:
                    try:
                        spec = load_one(os.path.join(dirpath, "SKILL.md"))
                    except (SkillSpecError, OSError) as e:
                        spec = SkillSpec(name=os.path.basename(dirpath),
                                         description="", dir_path=dirpath,
                                         problems=[str(e)])
                    self._specs[spec.name] = spec
        return len(self._specs)

    def list(self, include_broken: bool = False) -> List[dict]:
        """只返回 name + description —— 渐进式披露的第 1 段。"""
        out = [s.manifest() for s in self._specs.values() if s.ok or include_broken]
        return sorted(out, key=lambda x: x["name"])

    # ---- Activation ----
    def activate(self, name: str) -> SkillSpec:
        """读全文。这是第 2 段，只有任务匹配时才应调用。"""
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"未知技能: {name}（可用: {sorted(self._specs)}）")
        if not spec.body:
            with open(os.path.join(spec.dir_path, "SKILL.md"),
                      encoding="utf-8") as f:
                _, spec.body = parse_frontmatter(f.read())
        return spec

    def get(self, name: str) -> Optional[SkillSpec]:
        return self._specs.get(name)

    # ---- 搜索：让 Agent 在"遇到问题"时能找到技能 ----
    def search(self, query: str, k: int = 5) -> List[dict]:
        """对 name + description 做轻量打分（词项命中 + 覆盖率）。

        刻意不用向量：技能名/描述是短文本，词项匹配比语义更可预测，
        且避免为一个目录检索引入 embedding 依赖。
        """
        q = re.findall(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]+", (query or "").lower())
        if not q:
            return []
        scored = []
        for s in self._specs.values():
            if not s.ok:
                continue
            hay = (s.name + " " + s.description).lower()
            hits = sum(1 for t in q if t in hay)
            if hits:
                scored.append((hits / len(q), hits, s))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2].name))
        return [s.manifest() for _, _, s in scored[:k]]

    def stats(self) -> dict:
        ok = [s for s in self._specs.values() if s.ok]
        return {
            "total": len(self._specs),
            "valid": len(ok),
            "broken": len(self._specs) - len(ok),
            "roots": self.roots,
            "total_tokens_if_all_activated": sum(s.tokens_est for s in ok),
        }


def default_library_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "library")
