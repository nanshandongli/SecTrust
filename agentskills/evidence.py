"""
证据门禁（Evidence Gate）—— 把 superpowers 的软约束做成硬约束。

## 借鉴来源

obra/superpowers（303K stars）里最有价值的一条技能是 `verification-before-completion`：

    The Iron Law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
    核心原则: Evidence before claims, always.

但它有一个结构性弱点：**它是 prompt，不是系统**。SKILL.md 正文里写着
"Mandatory workflows, not suggestions"，模型照旧可以写一句 "Done!" 就收工——
没有任何东西会拦它。superpowers 自己也承认这点，只能靠"Red Flags"段落
劝模型自我审查。

## 本项目能做得更硬

本仓库天生有两件 superpowers 没有的东西：
  1. 一个**跨进程任务登记处**（task_registry.py）——任务状态是共享事实，不是会话内变量；
  2. 一个**审批网关**（approval.py）——默认拒绝，能真正阻断动作。

于是把"证据优于声明"从**劝告**升级为**拦截**：

    任务标记 completed 之前，必须提交可核验的验证证据；
    证据不合规 → 状态转换被拒绝，并把拒因写进审计表。

## 核验什么（全部可判定，不做模糊判断）

| 检查 | 判据 | 为什么 |
|------|------|--------|
| 命令非空 | ≥4 字符且不是自然语言句子 | 防止把 claim 原样当 command 提交 |
| 退出码 | exit_code == 0 | "Linter passed" ≠ 编译通过 |
| 输出非空 | strip 后 >0 字符 | 静默成功不算证据 |
| 新鲜度 | now - ran_at ≤ ttl（默认 300s） | 上一次跑通 ≠ 这次跑通 |
| 期望匹配 | 可选 expect 正则命中 stdout | 防止"跑了个不相关的命令" |
| 禁止匹配 | 可选 forbid 正则不命中 stdout | 如 traceback / FAILED |
| 措辞扫描 | claim 不含 rationalization 词 | 直接搬 superpowers 的 Red Flags |

## 真跑模式（run_and_record）

最有说服力的证据不是 Agent 自报，而是**系统自己跑一遍**。开启后由本模块
用 subprocess 实测命令，捕获退出码与输出——Agent 无法伪造。
出于安全，真跑默认关闭，且开启时受命令前缀白名单约束（只允许测试/构建类）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Sequence

# ---- superpowers `verification-before-completion` 的 Red Flags（原样搬运）----
# 原文：Using "should", "probably", "seems" / Expressing satisfaction before
#       verification ("Great!", "Perfect!", "Done!")
RATIONALIZATION_PATTERNS: Sequence[tuple] = (
    (r"\bshould\s+(?:work|pass|be)\b", "should work/pass/be —— 置信度不是证据，RUN the verification"),
    (r"\bprobably\b", "probably —— 不确定性不足以支撑完成声明"),
    (r"\bseems?\s+(?:to|ok|fine|correct)\b", "seems —— 需要实测输出，不是观感"),
    (r"\bI'?m confident\b", "I'm confident —— Confidence ≠ evidence"),
    (r"\bjust this once\b", "just this once —— No exceptions"),
    (r"\bmust (?:have )?(?:worked|passed)\b", "must have worked —— 需要实际输出"),
    (r"应该(?:能|会|可以)?(?:通过|成功|没问题)", "应该能通过 —— 需实测"),
    (r"大概(?:能|会|可以)?(?:通过|成功|没问题)", "大概能过 —— 需实测"),
    (r"似乎(?:已经)?(?:修复|通过|成功)", "似乎已修复 —— 需实测"),
    (r"应该没?问题", "应该没问题 —— 需实测"),
)

# 完成前的"自我庆祝"——superpowers 明确列为 STOP 信号
SELF_CONGRATULATION = re.compile(
    r"^\s*(great|perfect|done!|excellent|awesome|搞定|完成啦|完美)[!\s]*$", re.I)

# 真跑模式的命令白名单前缀（只允许只读/测试/构建类，禁止任意命令执行）
RUN_ALLOWLIST: Sequence[str] = (
    "python -m unittest", "python -m pytest", "python -m py_compile",
    "pytest", "python -m mypy", "python -m ruff",
    "npm test", "npm run build", "npm run lint",
    "go test", "go build", "go vet",
    "cargo test", "cargo build", "cargo clippy",
    "make test", "make build", "make lint",
    "git diff --stat", "git status --short",
)

DEFAULT_TTL = 300.0     # 证据新鲜度窗口（秒）
MAX_EVIDENCE_TTL = 3600.0

# 解释器别名：命令首 token 的 basename 若命中，统一归一化为 "python"，
# 否则 `C:\...\python.exe -m unittest` 会因前缀不匹配被白名单误拦。
_PYTHON_ALIASES = {"python", "python3", "python.exe", "python3.exe", "py", "py.exe"}


def _normalize_cmd(cmd: str) -> str:
    parts = cmd.strip().split()
    if not parts:
        return cmd.strip()
    head = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head in _PYTHON_ALIASES:
        parts[0] = "python"
    return " ".join(parts)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def _head_tail(s: str, n: int = 400) -> str:
    if len(s) <= n * 2:
        return s
    return f"{s[:n]}\n...[truncated {len(s) - 2 * n} chars]...\n{s[-n:]}"


# 自然语言检测：命令里若大量出现中文词或长空格串，多半是把 claim 当成了 command
_CJK = re.compile(r"[\u4e00-\u9fff]")


def looks_like_command(cmd: str) -> bool:
    if not cmd or len(cmd.strip()) < 4:
        return False
    if _CJK.search(cmd):
        return False
    if len(cmd.split()) < 1:
        return False
    # 纯英文句子（如 "all tests pass"）也不算命令：至少要有个可执行 token
    first = cmd.strip().split()[0]
    return bool(re.match(r"^[A-Za-z0-9_./\-]+$", first)) and (
        len(cmd.split()) >= 2 or first in {"make", "pytest", "go", "cargo", "npm", "git"}
    )


@dataclass
class Evidence:
    """一次验证运行的证据。刻意要求 command + exit_code + stdout 三件套。"""
    command: str
    exit_code: int
    stdout: str
    claim: str = ""                  # Agent 想做的完成声明
    task_id: str = ""
    ran_at: float = field(default_factory=time.time)
    stderr: str = ""
    expect: str = ""                 # 可选：stdout 必须匹配的正则
    forbid: str = ""                 # 可选：stdout 不得匹配的正则
    ttl: float = DEFAULT_TTL
    cwd: str = ""

    @property
    def output(self) -> str:
        """用于核验的“完整输出”= stdout + stderr。

        为什么合并：unittest / pytest / go test 的结果行写在 **stderr** 而不是
        stdout。只看 stdout 会把“Ran 136 tests ... OK”判成“输出为空”
        （实测踩到：真跑 `python -m unittest ...` 得到 exit_code=0 但 stdout
        为 0 字节，于是被误判为静默成功而驳回——典型的假阴性）。
        """
        return (self.stdout or "") + (self.stderr or "")

    @property
    def stdout_sha256(self) -> str:
        return _sha256(self.output)

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.ran_at)

    def redacted(self) -> dict:
        """对外输出：只给出脱敏摘要，避免证据本身成为泄密载体。"""
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_sha256": self.stdout_sha256[:16],
            "stdout_bytes": len(self.stdout or ""),
            "stderr_bytes": len(self.stderr or ""),
            "stdout_head": _head_tail(self.output, 200),
            "stdout_tail": _head_tail(self.output, 200) if len(self.output) > 400 else "",
            "age_seconds": round(self.age, 1),
            "ran_at": self.ran_at,
            "claim": self.claim,
            "task_id": self.task_id,
        }


@dataclass
class Verdict:
    accepted: bool
    checks: dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    rationalizations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"accepted": self.accepted, "checks": self.checks,
                "reasons": self.reasons,
                "rationalizations": self.rationalizations}


class EvidenceGate:
    """验证门禁。默认只核验"自报证据"的形态；开启 allow_run 后可系统自跑。"""

    def __init__(self, db_path: Optional[str] = None, allow_run: bool = False,
                 run_timeout: float = 120.0):
        self.db_path = db_path
        self.allow_run = allow_run
        self.run_timeout = run_timeout
        if db_path:
            self._ensure_schema()

    # ---------------- 持久化（审计可回放） ----------------
    def _conn(self):
        if not self.db_path:
            return None
        c = sqlite3.connect(self.db_path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _ensure_schema(self):
        c = self._conn()
        if not c:
            return
        with c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id   TEXT PRIMARY KEY,
                    task_id       TEXT NOT NULL DEFAULT '',
                    claim         TEXT NOT NULL DEFAULT '',
                    command       TEXT NOT NULL,
                    exit_code     INTEGER,
                    stdout_sha256 TEXT,
                    stdout_head   TEXT,
                    ran_at        REAL,
                    accepted      INTEGER NOT NULL,
                    reasons       TEXT,
                    created_at    REAL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_evidence_task "
                      "ON evidence(task_id)")
        c.close()

    def record(self, ev: Evidence, v: Verdict) -> str:
        c = self._conn()
        if not c:
            return ""
        eid = _sha256(f"{ev.task_id}|{ev.command}|{ev.ran_at}")[:32]
        with c:
            c.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (eid, ev.task_id, ev.claim, ev.command, ev.exit_code,
                       ev.stdout_sha256, _head_tail(ev.output, 300), ev.ran_at,
                       1 if v.accepted else 0, json.dumps(v.reasons,
                                                          ensure_ascii=False),
                       time.time()))
        c.close()
        return eid

    def history(self, task_id: str, limit: int = 20) -> List[dict]:
        c = self._conn()
        if not c:
            return []
        rows = c.execute(
            "SELECT evidence_id, command, exit_code, stdout_sha256, ran_at, "
            "accepted, reasons FROM evidence WHERE task_id=? "
            "ORDER BY created_at DESC LIMIT ?", (task_id, limit)).fetchall()
        c.close()
        return [{"evidence_id": r[0], "command": r[1], "exit_code": r[2],
                 "stdout_sha256": r[3][:16], "ran_at": r[4],
                 "accepted": bool(r[5]), "reasons": json.loads(r[6] or "[]")}
                for r in rows]

    # ---------------- 核验 ----------------
    def verify(self, ev: Evidence) -> Verdict:
        checks: dict = {}
        reasons: List[str] = []

        # 1) 命令形态
        ok_cmd = looks_like_command(ev.command)
        checks["command_is_command"] = ok_cmd
        if not ok_cmd:
            reasons.append(
                f"提交的 command 不像可执行命令: {ev.command[:80]!r}。"
                "必须给出真实验证命令（如 `python -m unittest discover -s tests`），"
                "而非自然语言描述。")

        # 2) 退出码
        checks["exit_code_zero"] = (ev.exit_code == 0)
        if ev.exit_code != 0:
            reasons.append(f"验证命令退出码 {ev.exit_code} ≠ 0，不能据此声明完成。")

        # 3) 输出非空（用 stdout+stderr 合并，见 Evidence.output）
        out = ev.output
        has_out = bool(out and out.strip())
        checks["stdout_nonempty"] = has_out
        if not has_out:
            reasons.append("验证命令输出为空（stdout 与 stderr 均无内容）——静默成功不构成证据。")

        # 4) 新鲜度
        ttl = min(max(1.0, float(ev.ttl or DEFAULT_TTL)), MAX_EVIDENCE_TTL)
        fresh = ev.age <= ttl
        checks["fresh"] = fresh
        checks["age_seconds"] = round(ev.age, 1)
        checks["ttl_seconds"] = ttl
        if not fresh:
            reasons.append(
                f"证据已过期（{ev.age:.0f}s > ttl {ttl:.0f}s）。"
                "上一次跑通不等于这次跑通——请重新运行验证命令。")

        # 5) 期望匹配（可选）
        if ev.expect:
            try:
                m = re.search(ev.expect, out, re.M)
            except re.error as e:
                m, reasons = None, reasons + [f"expect 正则非法: {e}"]
            checks["expect_matched"] = bool(m)
            if not m:
                reasons.append(f"输出未匹配 expect 正则 {ev.expect!r}——"
                               "跑的命令可能和声明的事无关。")

        # 6) 禁止匹配（可选）
        if ev.forbid:
            try:
                f = re.search(ev.forbid, out, re.I | re.M)
            except re.error as e:
                f, reasons = None, reasons + [f"forbid 正则非法: {e}"]
            checks["forbid_absent"] = not f
            if f:
                reasons.append(f"输出命中 forbid 正则 {ev.forbid!r}（命中 {f.group(0)!r}）。")

        # 7) 措辞扫描（搬 superpowers 的 Red Flags）
        rats = scan_rationalizations(ev.claim)
        checks["no_rationalization"] = not rats
        if rats:
            reasons.extend(rats)

        accepted = all([
            checks["command_is_command"], checks["exit_code_zero"],
            checks["stdout_nonempty"], checks["fresh"],
            checks.get("expect_matched", True), checks.get("forbid_absent", True),
            checks["no_rationalization"],
        ])
        return Verdict(accepted=accepted, checks=checks, reasons=reasons,
                       rationalizations=rats)

    # ---------------- 真跑模式 ----------------
    def run(self, command: str, cwd: str = "", timeout: float = 0.0) -> Evidence:
        """由系统亲自跑一遍——Agent 无法伪造的证据。

        开启条件：allow_run=True 且命令命中白名单前缀。否则抛 PermissionError。
        """
        if not self.allow_run:
            raise PermissionError(
                "真跑模式未开启（EvidenceGate(allow_run=True)）。"
                "默认关闭是因为它会执行 shell 命令。")
        cmd = _normalize_cmd(command)
        if not any(cmd.startswith(p) for p in RUN_ALLOWLIST):
            raise PermissionError(
                f"命令不在白名单内: {cmd[:60]!r}。"
                f"只允许测试/构建/只读类前缀，例如: {', '.join(list(RUN_ALLOWLIST)[:6])} …")
        t0 = time.time()
        try:
            p = subprocess.run(cmd, shell=True, cwd=cwd or None,
                               capture_output=True, text=True,
                               timeout=timeout or self.run_timeout)
            out, err, rc = p.stdout or "", p.stderr or "", p.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", f"timeout after {timeout or self.run_timeout}s", 124
        return Evidence(command=cmd, exit_code=rc, stdout=out, stderr=err,
                        ran_at=time.time(), cwd=cwd,
                        claim=f"ran at t+{time.time() - t0:.1f}s")

    def run_and_gate(self, command: str, claim: str, task_id: str = "",
                     cwd: str = "", expect: str = "", forbid: str = "") -> dict:
        """真跑 + 核验 + 落库。一条龙，Agent 无从插手。"""
        ev = self.run(command, cwd=cwd)
        ev.claim, ev.task_id, ev.expect, ev.forbid = claim, task_id, expect, forbid
        v = self.verify(ev)
        eid = self.record(ev, v)
        return {"evidence_id": eid, "verdict": v.to_dict(),
                "evidence": ev.redacted()}

    # ---------------- 门禁：与任务状态绑定 ----------------
    def gate_complete(self, registry, task_id: str, claim: str,
                      ev: Evidence) -> dict:
        """核验通过才允许把任务标记为 completed。这是"拦截"发生的地方。"""
        ev.task_id, ev.claim = task_id, claim
        v = self.verify(ev)
        eid = self.record(ev, v)
        if not v.accepted:
            return {
                "completed": False, "blocked": True, "evidence_id": eid,
                "reason": "证据未通过核验，任务状态未变更",
                "verdict": v.to_dict(),
                "hint": "补齐真实的验证命令输出（退出码 0 + 非空输出 + 新鲜），"
                        "并把声明里的 'should/probably/seems/应该/大概' 换成实测结果。",
            }
        registry.update_task_status(task_id, "completed")
        return {"completed": True, "blocked": False, "evidence_id": eid,
                "task_id": task_id, "verdict": v.to_dict(),
                "evidence": ev.redacted()}


def scan_rationalizations(claim: str) -> List[str]:
    """扫 superpowers 列的 rationalization 措辞，返回带反驳的提示列表。"""
    if not claim:
        return []
    out = []
    for pat, why in RATIONALIZATION_PATTERNS:
        if re.search(pat, claim, re.I):
            out.append(f"措辞告警：{why}")
    if SELF_CONGRATULATION.match(claim or ""):
        out.append("措辞告警：先表达满意（Done!/Great!）——验证之前不能宣告成功")
    return out
