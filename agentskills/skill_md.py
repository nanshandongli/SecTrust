"""
skill_md.py —— 标准 SKILL.md 运行时（社区通用规范 + 平台扩展 manifest）

定位：在现有 `loader.py`（agentskills.io 风格的**严格写作规范**：description 必须
"Use when" 开头、只认 name/description 两个标量）之外，支持社区通用的
**标准 SKILL.md 格式**——更丰富的 frontmatter 字段与列表值：

    ---
    name: competitor-analysis
    description: 当用户需要写竞品分析报告、梳理竞品功能对比时使用。
    version: 1.0.0
    license: MIT
    allowed-tools: [web_search, file_read, file_write]
    risk_level: medium
    cost_tier: standard
    ---
    # 竞品分析技能
    ...正文...

两个加载器**并存、互不破坏**：loader.py 继续服务既有 4 个内置技能与其写作
规范；本模块服务标准格式技能（渐进式 L1/L2/L3 + manifest 安装期校验）。
样板技能放在同一个 library/ 下，SKILL.md 同时满足两套校验。

渐进式加载（Progressive Disclosure，L1/L2/L3）：
  · L1 load_l1_metadata()  —— 只暴露 name/description/risk_level/cost_tier
                              （+ version/license/allowed-tools），用于意图匹配；
                              只读取 frontmatter，**不读正文**。
  · L2 load_l2_body(name)  —— 命中后才读完整 Markdown 指令并缓存。
  · L3 load_l3_resource(name, rel_path) —— references/ assets/ scripts/ 按需读取，
      **路径穿越防护**：realpath 规范化后必须仍在技能目录内，且一级目录白名单。

安装期校验（manifest.json 平台扩展）：
  permissions / requires_approval / allowed_domains / blocked_patterns / cost_estimate，
  与 governance.rbac.PERMISSIONS 权限目录、governance.content_guard 的
  blocked_patterns 对齐（只校验结构与已知平台权限，不擅自放宽）。

诚实边界：
  · 零第三方依赖（不引入 PyYAML），frontmatter 只支持 `key: value` 与
    `key: [a, b]` 两种形式；复杂 YAML 明确报错，不猜测语义。
  · 脚本执行必须走 sandbox/；沙箱外/越界路径一律拒绝，绝不静默执行宿主命令。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
# 标准 frontmatter 允许的标量/列表字段（其余字段保留但不参与校验）
KNOWN_FIELDS = ("name", "description", "version", "license",
                "allowed-tools", "risk_level", "cost_tier", "security_tier",
                "trust_tier")
# L3 资源一级目录白名单（三类均可按需读取；执行仅限 scripts/ 且必须走 sandbox/）
RESOURCE_DIRS = ("references", "assets", "scripts")
RISK_LEVELS = ("low", "medium", "high", "critical")
COST_TIERS = ("low", "standard", "high")
# 方案④ 动态信任：语义化信任声明 → 安全分级声明（仅当 security_tier 未显式设置时作意图参考）
TRUST_TIERS = ("high", "medium", "low")
TRUST_TO_SECURITY = {"low": "white", "medium": "gray", "high": "black"}


class SkillMdError(ValueError):
    """标准 SKILL.md 解析/校验错误。"""


class SkillSecurityError(SkillMdError):
    """安全拒绝：路径穿越 / 越界 / 沙箱外执行。"""


@dataclass
class StandardSkillSpec:
    """标准格式的技能规格（L1 元数据常驻，L2 正文按需）。"""

    name: str
    description: str
    dir_path: str
    version: str = ""
    license: str = ""
    allowed_tools: List[str] = field(default_factory=list)
    risk_level: str = "medium"
    cost_tier: str = "standard"
    body: str = ""                     # 空 = 未激活（L2）
    bytes: int = 0
    files: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    manifest: Dict = field(default_factory=dict)
    manifest_problems: List[str] = field(default_factory=list)
    # v2 安全分级：auto=按硬条件自动判定；white/gray/black=显式声明。
    # 声明 white 但不满足硬条件会被自动降级（见 agentskills/tier.py），不静默放行。
    security_tier: str = "auto"
    # 方案④ 动态信任：语义化信任声明（high/medium/low），仅作意图参考，最终安全
    # 分级以 auto/security_tier 的硬条件判定为准，绝不绕过安全检查。
    trust_tier: str = ""

    @property
    def has_scripts(self) -> bool:
        """是否带可执行脚本目录（有则强制 >= gray，不可 white）。"""
        return any(str(f).startswith("scripts/") for f in self.files)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def installed_ok(self) -> bool:
        """安装期校验通过：SKILL.md 合格 + manifest 无问题。"""
        return self.ok and not self.manifest_problems

    def l1(self) -> Dict:
        """L1 元数据：仅意图匹配所需字段，绝不夹带正文。"""
        return {
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level,
            "cost_tier": self.cost_tier,
            "version": self.version,
            "license": self.license,
            "allowed_tools": list(self.allowed_tools),
            "security_tier": self.security_tier,
            "trust_tier": self.trust_tier,
            "has_scripts": self.has_scripts,
            "ok": self.ok,
            "installed_ok": self.installed_ok,
            "problems": list(self.problems),
        }


def _parse_scalar(value: str):
    """解析 frontmatter 值：`[a, b]` → list；其余 → 去引号标量字符串。"""
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip("'\"") for p in inner.split(",")
                if p.strip()]
    return v.strip("'\"")


def parse_frontmatter_standard(text: str) -> Tuple[Dict, str]:
    """解析 `---\\n...\\n---\\n<body>`（零依赖，支持标量 + 行内列表）。

    与 loader.parse_frontmatter 的区别：允许 `allowed-tools: [a, b]` 这类列表值；
    只支持这两种形式，遇到嵌套 YAML / 多行块直接抛 SkillMdError（不猜测）。
    """
    if not text.startswith("---"):
        raise SkillMdError("缺少 YAML frontmatter（文件必须以 --- 开头）")
    end = text.find("\n---", 3)
    if end == -1:
        raise SkillMdError("frontmatter 未闭合（找不到结束的 ---）")
    raw = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: Dict = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillMdError("frontmatter 行不是 key: value 形式: %r" % line)
        k, v = line.split(":", 1)
        k = k.strip()
        if not k:
            raise SkillMdError("frontmatter 存在空 key")
        if v.strip().startswith(("{", "|", ">", "&", "*")):
            raise SkillMdError("不支持嵌套/多行 YAML 值（key=%s）" % k)
        meta[k] = _parse_scalar(v)
    return meta, body


def read_frontmatter_only(path: str) -> Tuple[Dict, int]:
    """L1 专用：只读到 frontmatter 结束，**不读正文**（渐进式披露第一步）。"""
    meta_lines: List[str] = []
    started = False
    closed = False
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if not started:
                if line.rstrip("\r\n") != "---":
                    raise SkillMdError("缺少 YAML frontmatter 起始 ---：%s" % path)
                started = True
                meta_lines.append("---")
                continue
            meta_lines.append(line.rstrip("\r\n"))
            if line.rstrip("\r\n") == "---":
                closed = True
                break
    if not closed:
        raise SkillMdError("frontmatter 未闭合：%s" % path)
    meta, _ = parse_frontmatter_standard("\n".join(meta_lines) + "\n")
    return meta, os.path.getsize(path)


def _validate_meta(meta: Dict) -> List[str]:
    """必填/格式校验（安装期第一关）。"""
    problems: List[str] = []
    name = str(meta.get("name") or "")
    desc = str(meta.get("description") or "")
    if not name:
        problems.append("frontmatter 缺少 name")
    elif not NAME_RE.match(name):
        problems.append("name 只允许字母/数字/连字符，实际 %r" % name)
    if not desc:
        problems.append("frontmatter 缺少 description")
    elif len(desc) > 1024:
        problems.append("description 超过 1024 字符（实际 %d）" % len(desc))
    risk = str(meta.get("risk_level") or "medium")
    if risk not in RISK_LEVELS:
        problems.append("未知 risk_level：%s（应为 %s）"
                        % (risk, "/".join(RISK_LEVELS)))
    cost = str(meta.get("cost_tier") or "standard")
    if cost not in COST_TIERS:
        problems.append("未知 cost_tier：%s（应为 %s）" % (cost, "/".join(COST_TIERS)))
    trust = str(meta.get("trust_tier") or "")
    if trust and trust not in TRUST_TIERS:
        problems.append("未知 trust_tier：%s（应为 %s）" % (trust, "/".join(TRUST_TIERS)))
    tools = meta.get("allowed-tools") or []
    if isinstance(tools, str):
        tools = [tools]
    if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
        problems.append("allowed-tools 必须是字符串列表")
    return problems


def parse_manifest(path: str) -> Dict:
    """读取 manifest.json；不存在返回空 dict，损坏抛 SkillMdError（不静默当无配置）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise SkillMdError("manifest.json 损坏或不可读：%s" % e)
    if not isinstance(data, dict):
        raise SkillMdError("manifest.json 根节点必须是对象")
    return data


def validate_manifest(manifest: Dict, spec: "StandardSkillSpec") -> List[str]:
    """安装期校验：结构 + 与 governance.rbac / content_guard 对齐。

    返回问题列表（空 = 可安装）。平台权限域（含 "."）必须在 rbac.PERMISSIONS
    中存在；技能工具权限（如 web_search / file_read）只校验非空字符串。
    """
    problems: List[str] = []
    if not manifest:
        problems.append("缺少 manifest.json（平台扩展：权限/审批/域名/密钥模式声明）")
        return problems
    if str(manifest.get("name") or "") != spec.name:
        problems.append("manifest.name 与 SKILL.md 的 name 不一致：%r != %r"
                        % (manifest.get("name"), spec.name))
    if not str(manifest.get("version") or ""):
        problems.append("manifest 缺少 version")

    perms = manifest.get("permissions", [])
    if not isinstance(perms, list) or any(
            not isinstance(p, str) or not p.strip() for p in perms):
        problems.append("manifest.permissions 必须是非空字符串列表")
    else:
        try:
            from governance.rbac import PERMISSIONS as _PLATFORM_PERMS
            for p in perms:
                if "." in p and p not in _PLATFORM_PERMS:
                    problems.append("未知平台权限：%s（不在 governance.rbac.PERMISSIONS）"
                                    % p)
        except ImportError:  # pragma: no cover - 仓库内恒可导入
            pass

    for key in ("allowed_domains", "blocked_patterns"):
        val = manifest.get(key, [])
        if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
            problems.append("manifest.%s 必须是字符串列表" % key)
        elif key == "blocked_patterns":
            # 与 content_guard 的 blocked_patterns 对齐：至少要是可被正则使用的串
            for pat in val:
                try:
                    re.compile(pat)
                except re.error as e:
                    problems.append("blocked_patterns 非法正则 %r：%s" % (pat, e))

    if "requires_approval" in manifest and not isinstance(
            manifest["requires_approval"], bool):
        problems.append("manifest.requires_approval 必须是布尔值")
    ce = manifest.get("cost_estimate", "low")
    if not isinstance(ce, str) or not ce:
        problems.append("manifest.cost_estimate 必须是非空字符串")
    st = manifest.get("security_tier")
    if st is not None and str(st) not in ("auto", "white", "gray", "black"):
        problems.append("未知 security_tier：%s（应为 auto/white/gray/black）" % st)
    return problems


def parse_skill_md(path: str, include_body: bool = False,
                   load_manifest: bool = True) -> StandardSkillSpec:
    """解析一个标准 SKILL.md → StandardSkillSpec。

    include_body=False（默认）时**只解析 frontmatter**（L1，不读正文）；
    include_body=True 时连正文一起读入（L2）。
    """
    if include_body:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        meta, body = parse_frontmatter_standard(text)
        size = os.path.getsize(path)
    else:
        meta, size = read_frontmatter_only(path)
        body = ""
    name = str(meta.get("name") or os.path.basename(os.path.dirname(path)))
    desc = str(meta.get("description") or "")
    tools = meta.get("allowed-tools") or []
    if isinstance(tools, str):
        tools = [tools]
    spec = StandardSkillSpec(
        name=name, description=desc,
        dir_path=os.path.dirname(os.path.abspath(path)),
        version=str(meta.get("version") or ""),
        license=str(meta.get("license") or ""),
        allowed_tools=list(tools) if isinstance(tools, list) else [],
        risk_level=str(meta.get("risk_level") or "medium"),
        cost_tier=str(meta.get("cost_tier") or "standard"),
        security_tier=str(meta.get("security_tier") or "auto"),
        trust_tier=str(meta.get("trust_tier") or ""),
        body=body, bytes=size,
        problems=_validate_meta(meta),
    )
    d = spec.dir_path
    spec.files = sorted(
        os.path.relpath(os.path.join(dp, fn), d).replace("\\", "/")
        for dp, dns, fns in os.walk(d)
        for fn in fns
        if fn not in ("SKILL.md", "manifest.json") and "__pycache__" not in dp
    )
    if load_manifest:
        mpath = os.path.join(d, "manifest.json")
        try:
            spec.manifest = parse_manifest(mpath)
        except SkillMdError as e:
            spec.manifest_problems.append(str(e))
        spec.manifest_problems.extend(validate_manifest(spec.manifest, spec))
        # manifest 的 security_tier 优先于 frontmatter（运维可在安装期显式收紧）
        msec = str(spec.manifest.get("security_tier") or "")
        if msec:
            spec.security_tier = msec
    # 方案④：trust_tier 仅作意图参考——仅当 security_tier 未显式声明时，把
    # high/medium/low 映射为 white/gray/black 声明，交由 classify() 重新硬判定；
    # 若映射后的声明仍不满足硬条件，classify 会照常降级，绝不绕过安全检查。
    if (not spec.security_tier or spec.security_tier == "auto") and spec.trust_tier:
        mapped = TRUST_TO_SECURITY.get(spec.trust_tier)
        if mapped:
            spec.security_tier = mapped
    return spec


# --------------------------------------------------------------------------
# L3 路径安全（路径穿越防护，realpath 规范化 + 一级目录白名单）
# --------------------------------------------------------------------------
def _safe_resource_path(spec: StandardSkillSpec, rel_path: str) -> str:
    if not rel_path or os.path.isabs(rel_path):
        raise SkillSecurityError("资源路径不得为空或绝对路径：%r" % rel_path)
    base = os.path.realpath(spec.dir_path)
    target = os.path.realpath(os.path.join(base, rel_path))
    nb, nt = os.path.normcase(base), os.path.normcase(target)
    if nt != nb and not nt.startswith(nb + os.sep):
        raise SkillSecurityError("资源路径越界（路径穿越被拒绝）：%s" % rel_path)
    rel = os.path.relpath(target, base).replace("\\", "/")
    top = rel.split("/", 1)[0]
    if top not in RESOURCE_DIRS:
        raise SkillSecurityError(
            "资源必须位于 %s/ 内：%s" % ("/".join(RESOURCE_DIRS), rel_path))
    if not os.path.isfile(target):
        raise SkillSecurityError("资源不存在：%s" % rel_path)
    return target


class StandardSkillLibrary:
    """标准 SKILL.md 技能库：L1 元数据 → L2 正文 → L3 资源 → 沙箱脚本。"""

    def __init__(self, roots: Optional[List[str]] = None):
        if roots is None:
            roots = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "library")]
        self.roots = list(roots)
        self._specs: Dict[str, StandardSkillSpec] = {}
        self.refresh()

    def refresh(self) -> int:
        self._specs.clear()
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                if "SKILL.md" not in filenames:
                    continue
                try:
                    spec = parse_skill_md(os.path.join(dirpath, "SKILL.md"))
                except (SkillMdError, OSError) as e:
                    spec = StandardSkillSpec(
                        name=os.path.basename(dirpath), description="",
                        dir_path=os.path.abspath(dirpath),
                        problems=[str(e)])
                self._specs[spec.name] = spec
        return len(self._specs)

    # ---- L1：元数据常驻（只 name/description/risk_level/cost_tier）----
    def load_l1_metadata(self, include_broken: bool = False) -> List[Dict]:
        out = [s.l1() for s in self._specs.values()
               if (s.ok and s.installed_ok) or include_broken]
        return sorted(out, key=lambda x: x["name"])

    def match_l1(self, query: str, k: int = 5) -> List[Dict]:
        """L1 意图匹配：只对 name + description 做词项打分，不读正文。"""
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
        return [s.l1() for _, _, s in scored[:k]]

    # ---- L2：命中后才加载完整正文 ----
    def load_l2_body(self, name: str) -> str:
        spec = self.get(name)
        if spec is None:
            raise KeyError("未知技能：%s（可用：%s）"
                           % (name, sorted(self._specs)))
        if not spec.body:
            full = parse_skill_md(os.path.join(spec.dir_path, "SKILL.md"),
                                  include_body=True, load_manifest=False)
            spec.body = full.body
        return spec.body

    # ---- L3：按需读取 references/ assets/ scripts/（路径穿越防护）----
    def load_l3_resource(self, name: str, rel_path: str,
                         binary: bool = False):
        spec = self.get(name)
        if spec is None:
            raise KeyError("未知技能：%s" % name)
        target = _safe_resource_path(spec, rel_path)
        mode = "rb" if binary else "r"
        kw = {} if binary else {"encoding": "utf-8"}
        with open(target, mode, **kw) as fh:
            return fh.read()

    # ---- 安装期校验 ----
    def install_validate(self, name: str) -> Dict:
        spec = self.get(name)
        if spec is None:
            return {"ok": False, "name": name, "problems": ["技能不存在"],
                    "manifest": {}}
        return {"ok": spec.installed_ok, "name": name,
                "problems": list(spec.problems) + list(spec.manifest_problems),
                "manifest": dict(spec.manifest)}

    # ---- 脚本沙箱执行（B3：产出还要过 EvidenceGate，见 mcp_adapter.proxy）----
    def run_script(self, name: str, rel_path: str, args: Optional[List[str]] = None,
                   sandbox=None, timeout: float = 30.0) -> Dict:
        """脚本必须走 sandbox/；沙箱外/越界一律拒绝，不静默执行宿主命令。"""
        spec = self.get(name)
        if spec is None:
            raise KeyError("未知技能：%s" % name)
        target = _safe_resource_path(spec, rel_path)
        rel = os.path.relpath(target, os.path.realpath(spec.dir_path)).replace("\\", "/")
        if not rel.startswith("scripts/"):
            raise SkillSecurityError("只有 scripts/ 下的文件可作为脚本执行：%s" % rel_path)
        if sandbox is None:
            from sandbox import SandboxManager
            sandbox = SandboxManager.auto()
        cmd = [sys.executable, target] + [str(a) for a in (args or [])]
        res = sandbox.run(cmd, workdir=os.path.dirname(target), timeout=timeout)
        return {
            "ok": bool(res.ok), "stdout": res.content, "error": res.error,
            "backend": getattr(res, "backend", ""),
            "containment": getattr(res, "containment", ""),
            "duration": getattr(res, "duration", 0.0),
            "script": rel, "sandboxed": True,
        }

    def get(self, name: str) -> Optional[StandardSkillSpec]:
        return self._specs.get(name)

    def stats(self) -> Dict:
        ok = [s for s in self._specs.values() if s.ok]
        installed = [s for s in self._specs.values() if s.installed_ok]
        return {"total": len(self._specs), "valid": len(ok),
                "installed": len(installed),
                "broken": len(self._specs) - len(ok),
                "roots": self.roots,
                "l1_loaded": True, "l2_activated": [s.name for s in self._specs.values()
                                                    if s.body]}
