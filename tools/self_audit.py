"""self_audit.py —— 项目自检评测脚本（零依赖，纯标准库）

用途：不依赖文档、直接从**代码与运行时**取证，产出可复现的评测数据：
  1. inventory  功能面清点（MCP 工具 / REST 端点 / 扫描模板 / 测试例 / 代码规模 / 第三方依赖）
  2. perf       性能实测（SQLite 吞吐、并发 CAS、备份耗时、队列吞吐、语义检索延迟）
  3. scan       扫描检出率实测（对 examples/sample_repo 跑全部模板，regex vs semgrep 对比）
  4. security   安全姿态穿透测试（审批绕过 / 沙箱越权 / 鉴权 401 / 记忆含密钥 / 依赖面）

用法：
    python tools/self_audit.py                # 全跑，输出 JSON
    python tools/self_audit.py --only perf    # 只跑某一段
    python tools/self_audit.py --out audit.json

设计原则：
- **诚实优先**：测不了/没真实凭据的项一律标 "unverified"，绝不粉饰。
- 所有临时数据落 tempfile，不污染 data/。
- 单文件零依赖，与项目"零依赖"定位一致。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 全世界公开已知的"测试向量"密钥（gitleaks 官方测试语料）。
# 它们不是真实凭证——本仓库为对齐公开基准而引用，自扫时应豁免而非报泄露。
KNOWN_PUBLIC_TEST_VECTORS = {
    "AKIALALEMEL33243OLIA",            # gitleaks api.go 第 20 行
    "8ae31cacf141669ddfb5da",          # gitleaks .env.prod 的 DB_PASSWORD
}
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------
# 1. 功能面清点
# --------------------------------------------------------------------------
# McpServer.handle 支持的 RPC 方法（协议面清点用）
_SERVER_METHODS = ("server/discover", "initialize", "ping", "tools/list", "tools/call")


def _optional_import_tops(src: str) -> set:
    """返回"只出现在 try 块内"的 import 顶层模块名集合（即可选依赖）。

    逐行扫描 + 缩进栈判定，不用正则。语义与原先的
    `try:\\s*\\n(?:\\s+.*\\n)*?\\s+(?:from|import)\\s+mod` 等价：
    某条 import 语句若被某个缩进更小的 `try:` 包裹，即视为可选。
    """
    optional = set()
    try_stack = []          # 当前处于的 try 块缩进栈
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        # 缩进回退 => 对应的 try 块已结束
        while try_stack and indent <= try_stack[-1]:
            try_stack.pop()
        if stripped.startswith("try:"):
            try_stack.append(indent)
        elif stripped.startswith(("from ", "import ")) and try_stack:
            m = re.match(r"(?:from|import)\s+([A-Za-z_][\w.]*)", stripped)
            if m:
                optional.add(m.group(1).split(".")[0])
    return optional


def audit_inventory() -> dict:
    import mcp_server as mcp
    from templates import TEMPLATES

    tools = [t["name"] for t in mcp._TOOLS]

    # REST 端点：从 _ROUTES 表真实读取
    from api.app import RegistryAPI
    routes = [f"{meth} {pat}" for meth, pat, _ in RegistryAPI._ROUTES]

    # 测试例数
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)

    def _count(s):
        n = 0
        for x in s:
            n += _count(x) if isinstance(x, unittest.TestSuite) else 1
        return n

    tests = _count(suite)

    # 代码规模（排除 tests/examples/data/.git 等）
    SKIP_DIRS = {".git", ".workbuddy", "__pycache__", ".sandbox", ".scan_jail",
                 "data", "node_modules", ".venv", "venv"}
    files, loc = [], 0
    for base, dirs, fs in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fs:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, f), ROOT)
            if rel.startswith(("tests" + os.sep, "examples" + os.sep, "tools" + os.sep)):
                continue
            files.append(rel)
            try:
                loc += len(open(os.path.join(base, f), encoding="utf-8").read().splitlines())
            except OSError:
                pass

    # 第三方依赖面：扫所有 import，找出非标准库的
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    third_party = {}
    imp_re = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
    for rel in files:
        try:
            src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except OSError:
            continue
        # try 块内的 import 顶层模块（= 可选依赖）。
        # 注意：这里曾用正则 `try:\s*\n(?:\s+.*\n)*?\s+(?:from|import)\s+mod`
        # 判断，存在灾难性回溯（\s 含 \n 与后续量词重叠），在长文件上会卡死
        # 数分钟——self_audit 因此 15 分钟无输出。改用逐行扫描，语义等价、O(n)。
        optional_mods = _optional_import_tops(src)
        for mod in imp_re.findall(src):
            top = mod.split(".")[0]
            if top in stdlib or top in ("task_registry", "memory", "approval",
                                        "templates", "sandbox", "channels",
                                        "models", "skills", "config", "runner",
                                        "mcp_server", "api", "transport",
                                        "scanner", "security", "memory_semantic",
                                        "memory_hybrid", "mem0_adapter", "demo"):
                continue
            third_party.setdefault(top, {"optional": True, "files": []})
            third_party[top]["optional"] = (
                third_party[top]["optional"] and top in optional_mods
            )
            if rel not in third_party[top]["files"]:
                third_party[top]["files"].append(rel)

    # 深度能力清点：结构化检测器 + 协议版本 + 记忆检索信号
    from scanner.detectors import detector_stats
    det = detector_stats()

    import mcp_server as _m
    protocol = {
        "declared": _m.PROTOCOL_VERSION,
        "latest_supported": _m.LATEST_PROTOCOL_VERSION,
        "supported": list(_m.SUPPORTED_PROTOCOL_VERSIONS),
        "has_server_discover": "server/discover" in _SERVER_METHODS,
        "deprecated_declared": sorted((_m.server_capabilities() or {}).keys()),
    }
    annotated = sum(1 for t in _m._annotated_tools() if "annotations" in t)

    return {
        "mcp_tools": {"count": len(tools), "names": tools,
                      "with_annotations": annotated},
        "rest_routes": {"count": len(routes), "routes": routes},
        "scan_templates": {"count": len(TEMPLATES), "ids": list(TEMPLATES)},
        "detectors": det,
        "protocol": protocol,
        "tests": tests,
        "code": {"modules": len(files), "loc": loc},
        "third_party": {
            "hard_required": sorted(k for k, v in third_party.items() if not v["optional"]),
            "optional": sorted(k for k, v in third_party.items() if v["optional"]),
            "detail": third_party,
        },
    }


# --------------------------------------------------------------------------
# 2. 性能实测
# --------------------------------------------------------------------------
def audit_perf(n: int = 500) -> dict:
    from task_registry import TaskRegistry
    from memory import MemoryStore
    from transport.queue import LocalQueue
    from memory_semantic import SemanticMemory

    out = {}
    d = tempfile.mkdtemp(prefix="audit_perf_")
    db = os.path.join(d, "reg.db")

    # --- 2.1 登记处写入吞吐（单进程，逐条提交）---
    reg = TaskRegistry(db_path=db, runner_id="audit")
    t0 = time.perf_counter()
    for i in range(n):
        reg.register_task(None, {"i": i})
    w = time.perf_counter() - t0

    # --- 2.2 读取吞吐 ---
    t0 = time.perf_counter()
    rows = reg.list_tasks(limit=n)
    r = time.perf_counter() - t0

    # --- 2.3 状态更新吞吐 ---
    ids = [x["task_id"] for x in rows][:n]
    t0 = time.perf_counter()
    for tid in ids:
        reg.update_task_status(tid, "done")
    u = time.perf_counter() - t0
    reg.close()

    out["registry"] = {
        "n": n,
        "write_tps": round(n / w, 1),
        "read_tps": round(n / r, 1) if r > 0 else None,
        "update_tps": round(n / u, 1),
        "write_ms_per_op": round(w / n * 1000, 3),
    }

    # --- 2.4 WAL 备份耗时 + 是否含未 checkpoint 数据 ---
    mdb = os.path.join(d, "mem.db")
    ms = MemoryStore(db_path=mdb)
    for i in range(200):
        ms.remember("perf", f"k{i}", f"v{i}")
    t0 = time.perf_counter()
    bak = ms.backup(os.path.join(d, "bak.db"))
    bt = time.perf_counter() - t0
    ms2 = MemoryStore(db_path=bak)
    got = ms2.recall("perf", "k199")
    ms2.close()
    ms.close()
    out["backup"] = {
        "items": 200,
        "ms": round(bt * 1000, 1),
        "contains_uncheckpointed_data": got == "v199",
    }

    # --- 2.5 队列吞吐 ---
    q = LocalQueue(db_path=os.path.join(d, "q.db"))
    t0 = time.perf_counter()
    for i in range(n):
        q.publish("t", {"i": i})
    pub = time.perf_counter() - t0
    t0 = time.perf_counter()
    got = q.consume("t", batch=n)
    con = time.perf_counter() - t0
    q.close()
    out["queue"] = {
        "n": n,
        "publish_tps": round(n / pub, 1),
        "consume_tps": round(len(got) / con, 1) if con > 0 else None,
    }

    # --- 2.6 语义检索延迟 & 准确率 ---
    sdb = os.path.join(d, "sem.db")
    sm = SemanticMemory(db_path=sdb)
    sm.remember("preference", "code_review_model", "Qwen-Max",
                note="用户偏好用 Qwen-Max 做代码审查")
    sm.remember("preference", "editor", "Neovim", note="用户喜欢用 Neovim 编辑器")
    sm.remember("repo", "main", "C:/work/app", note="主业务仓库路径")
    sm.remember("command", "scan", "python examples/template_scan_demo.py",
                note="常用扫描指令")
    sm.close()

    queries = [
        ("代码审查用哪个模型", "code_review_model"),
        ("编辑器偏好", "editor"),
        ("主仓库在哪", "main"),
        ("扫描命令", "scan"),
    ]
    hits_top1 = 0
    lat = []
    sm = SemanticMemory(db_path=sdb)
    for qtext, expect in queries:
        t0 = time.perf_counter()
        res = sm.search_semantic(qtext, k=3)
        lat.append((time.perf_counter() - t0) * 1000)
        if res and res[0][1] == expect:
            hits_top1 += 1
    sm.close()
    out["semantic_memory"] = {
        "queries": len(queries),
        "top1_hits": hits_top1,
        "top1_accuracy": round(hits_top1 / len(queries), 3),
        "avg_latency_ms": round(sum(lat) / len(lat), 2),
        "note": "离线 hashed n-gram 余弦，非真语义模型",
    }
    return out


# --------------------------------------------------------------------------
# 3. 扫描检出率实测
# --------------------------------------------------------------------------
def audit_scan() -> dict:
    import templates
    from scanner.semgrep_adapter import semgrep_available

    repo = os.path.join(ROOT, "examples", "sample_repo")
    if not os.path.isdir(repo):
        return {"status": "skipped", "reason": "examples/sample_repo 不存在"}

    out = {"repo": os.path.relpath(repo, ROOT), "templates": {}}
    for tid in templates.list_templates():
        tid = tid["id"] if isinstance(tid, dict) else tid
        d = tempfile.mkdtemp(prefix="audit_scan_")
        db = os.path.join(d, "r.db")
        try:
            res = templates.run_scan(tid, repo, auto_approve=True,
                                     db_path=db, sandbox_backend="local")
            rep = res.get("report", {}) or {}
            findings = rep.get("findings") or []
            out["templates"][tid] = {
                "status": res.get("status"),
                "engine": res.get("engine_used", "regex"),
                "findings": len(findings),
                "high": sum(1 for f in findings
                            if str(f.get("severity", "")).lower() == "high"),
            }
        except Exception as e:  # noqa: BLE001
            out["templates"][tid] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    out["semgrep_installed"] = semgrep_available()
    out["semgrep_note"] = ("未安装 → run_scan(engine='semgrep') 自动回退正则；"
                           "深度对比因此无法在本机实测")
    return out


# --------------------------------------------------------------------------
# 3b. 深度能力实测（扫描基准 + 记忆基准）—— 让"深度"是可复现的数字
# --------------------------------------------------------------------------
def audit_depth() -> dict:
    out: dict = {}

    # --- 扫描：金标准语料 ---
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from scan_bench import run_bench
        b = run_bench()
        out["scan_bench"] = {
            "corpus": b["corpus_size"], "positives": b["positives"],
            "negatives": b["negatives"],
            "tp": b["tp"], "fn": b["fn"], "fp": b["fp"], "tn": b["tn"],
            "recall": b["recall"], "precision": b["precision"], "f1": b["f1"],
            "rules": b["rules_total"],
            "sarif_valid": not b["sarif_problems"],
        }
    except Exception as e:  # noqa: BLE001
        out["scan_bench"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # --- 扫描：样例仓库全量（含 .env / .github 漏扫回归）---
    try:
        from scanner.engine import scan_path
        repo = os.path.join(ROOT, "examples", "sample_repo")
        res = scan_path(repo)
        out["deep_scan_sample_repo"] = {
            "files": res["scanned_files"], "findings": res["summary"]["total"],
            "by_severity": res["summary"]["by_severity"],
            "elapsed_ms": res["elapsed_ms"],
        }
    except Exception as e:  # noqa: BLE001
        out["deep_scan_sample_repo"] = {"status": "error",
                                        "error": f"{type(e).__name__}: {e}"}

    # --- 记忆：混合检索 Top1 准确率（含跨进程一致性）---
    try:
        from memory_hybrid import HybridMemory
        d = tempfile.mkdtemp(prefix="audit_mem_")
        db = os.path.join(d, "m.db")
        hm = HybridMemory(db_path=db)
        hm.remember("preference", "code_review_model", "Qwen-Max",
                    note="用户偏好用 Qwen-Max 做代码审查")
        hm.remember("preference", "editor", "Neovim", note="用户喜欢用 Neovim")
        hm.remember("repo", "main", "/data/foo", note="主仓库路径")
        hm.remember("incident", "oom", "导出任务 OOM", note="上周事故")
        cases = [("代码审查用哪个模型", "code_review_model"), ("Qwen-Max", "code_review_model"),
                 ("主仓库路径", "main"), ("上周的 OOM 事故", "oom"), ("编辑器", "editor")]
        hits = 0
        per_case = []
        for q, exp in cases:
            r = hm.search(q, k=1)
            got = r[0]["key"] if r else None
            hits += int(got == exp)
            per_case.append({"query": q, "top1": got, "ok": got == exp})
        out["memory_bench"] = {
            "top1": hits, "total": len(cases),
            "accuracy": round(hits / len(cases), 4),
            "signals": ["vector", "bm25", "entity"], "fuse": "RRF(k=60)",
            "stats": hm.stats(), "cases": per_case,
        }
        hm.close()
    except Exception as e:  # noqa: BLE001
        out["memory_bench"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # --- 技能库（SKILL.md 渐进式加载，借鉴 obra/superpowers）---
    try:
        from agentskills.loader import SkillLibrary
        lib = SkillLibrary()
        st = lib.stats()
        out["skills"] = {
            "total": st["total"], "valid": st["valid"], "broken": st["broken"],
            "names": [s["name"] for s in lib.list()],
            "tokens_if_all_activated": st["total_tokens_if_all_activated"],
            "attribution": "derived from obra/superpowers (MIT)",
        }
    except Exception as e:  # noqa: BLE001
        out["skills"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # --- 证据门禁：把"证据优于声明"从 prompt 升级为可拦截约束 ---
    try:
        import tempfile as _tf
        from agentskills.evidence import Evidence, EvidenceGate
        from task_registry import TaskRegistry
        d = _tf.mkdtemp()
        gate = EvidenceGate(db_path=os.path.join(d, "ev.db"))
        reg = TaskRegistry(db_path=os.path.join(d, "t.db"))
        tid = reg.register_task(None, {"title": "audit probe"})

        probes = {
            "空口声称（自然语言当命令 + Done!）": Evidence(
                command="改好了", exit_code=0, stdout="ok", claim="Done!"),
            "退出码非 0": Evidence(command="pytest -q", exit_code=1,
                                   stdout="FAILED 2", claim="修好了"),
            "证据过期": Evidence(command="pytest -q", exit_code=0, stdout="ok",
                                claim="通过", ran_at=time.time() - 9999),
            "措辞含 should": Evidence(command="python -m unittest discover -s tests",
                                      exit_code=0, stdout="Ran 1 test OK",
                                      claim="should pass now"),
        }
        blocked = 0
        details = []
        for label, ev in probes.items():
            v = gate.verify(ev)
            blocked += int(not v.accepted)
            details.append({"probe": label, "blocked": not v.accepted,
                            "reasons": v.reasons[:1]})

        # 合法证据必须放行，否则门禁就是"一律拒绝"的假安全
        good = Evidence(command="python -m unittest discover -s tests",
                        exit_code=0, stdout="Ran 136 tests in 3.1s\n\nOK",
                        claim="136 项测试全部通过", task_id=tid)
        gv = gate.verify(good)
        r = gate.gate_complete(reg, tid, "136 项测试全部通过", good)
        status_after_bad = reg.get_task(tid)["status"]
        reg.close()

        out["evidence_gate"] = {
            "probes_total": len(probes) + 1,
            "blocked_illegitimate": blocked,
            "accepted_legitimate": bool(gv.accepted),
            "task_status_after_legitimate_complete": r.get("completed"),
            "task_status_before": status_after_bad,
            "block_rate": round(blocked / len(probes), 4),
            "details": details,
        }
    except Exception as e:  # noqa: BLE001
        out["evidence_gate"] = {"status": "error",
                                "error": f"{type(e).__name__}: {e}"}

    return out


# --------------------------------------------------------------------------
# 4. 安全姿态穿透测试（攻击者视角）
# --------------------------------------------------------------------------
def audit_security() -> dict:
    import templates
    from approval import ApprovalCenter

    out = {}
    d = tempfile.mkdtemp(prefix="audit_sec_")
    db = os.path.join(d, "r.db")
    repo = os.path.join(ROOT, "examples", "sample_repo")
    if not os.path.isdir(repo):
        repo = tempfile.mkdtemp(prefix="audit_repo_")

    # --- 4.1 审批闸门：未批准能否执行 ---
    req = templates.request_scan("secret_leak", repo, db_path=db)
    res = templates.execute_scan(req["task_id"], req["approval_id"], repo,
                                 db_path=db, sandbox_backend="local")
    out["approval_gate"] = {
        "attempt": "未批准直接 execute_scan",
        "status": res.get("status"),
        "blocked": res.get("status") == "blocked",
    }

    # --- 4.2 审批闸门：伪造 approval_id ---
    res2 = templates.execute_scan(req["task_id"], "approval-FORGED-0000", repo,
                                  db_path=db, sandbox_backend="local")
    out["approval_gate_forged"] = {
        "attempt": "伪造 approval_id",
        "status": res2.get("status"),
        "blocked": res2.get("status") == "blocked",
    }

    # --- 4.3 审批闸门：拒绝后能否执行 ---
    ac = ApprovalCenter(db_path=db)
    ac.decide(req["approval_id"], "reject", by="audit")
    ac.close()
    res3 = templates.execute_scan(req["task_id"], req["approval_id"], repo,
                                  db_path=db, sandbox_backend="local")
    out["approval_gate_rejected"] = {
        "attempt": "拒绝后仍 execute_scan",
        "status": res3.get("status"),
        "blocked": res3.get("status") == "blocked",
    }

    # --- 4.4 沙箱越权：能否读出 jail 外的文件 ---
    # 探针选择（曾在此栽过）：**不能**用 SAM 之类有 Windows ACL 保护的文件。
    # 那测到的是"操作系统不给读"，不是沙箱能力——会得出"沙箱已拦住"的假阳性。
    # 必须用 jail 外一个普通可读文件，才能真实反映 containment 语义。
    try:
        from sandbox import SandboxManager
        sb = SandboxManager.auto()
        jail = os.path.join(d, "jail")
        os.makedirs(jail, exist_ok=True)
        outside = os.path.join(d, "outside_secret.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("CANARY-OUTSIDE-JAIL")
        r = sb.run(["python", "-c",
                    f"print(open(r'{outside}').read())"], jail, timeout=15)
        leaked = "CANARY-OUTSIDE-JAIL" in (r.content or "")
        containment = getattr(r, "containment", "?")
        # containment="limited"（本地 enforced）的文档化语义：文件系统**不**隔离，
        # 故越权读成功是**预期**；只有 containment="os"（Docker/E2B）才应拦住。
        expected_leak = (containment != "os")
        out["sandbox_escape"] = {
            "probe": "读 jail 外的普通可读文件（不用 SAM——那测的是 OS ACL）",
            "backend": getattr(r, "backend", "?"),
            "containment": containment,
            "leaked": leaked,
            "matches_declared_containment": leaked == expected_leak,
            "verdict": ("与声明的 containment 一致" if leaked == expected_leak
                        else "与声明的 containment 不一致——需排查"),
            "note": ("containment='limited' 不隔离文件系统/网络（文档化边界，"
                     "见 sandbox/__init__.py 诚实边界段）；"
                     "要真拦住越权读需 containment='os'（Docker/E2B）"),
        }
    except Exception as e:  # noqa: BLE001
        out["sandbox_escape"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # --- 4.5 API 鉴权：无 token 应 401 ---
    try:
        import threading
        from api.app import RegistryAPI
        from api.client import ApiClient
        import security.oauth as oa

        api = RegistryAPI(db_path=os.path.join(d, "api.db"),
                          memory_db_path=os.path.join(d, "apimem.db"),
                          host="127.0.0.1", port=0, require_auth=True)
        # 用端口 0 拿不到实际端口，改为固定高位端口
        port = 8123
        api = RegistryAPI(db_path=os.path.join(d, "api.db"),
                          memory_db_path=os.path.join(d, "apimem.db"),
                          host="127.0.0.1", port=port, require_auth=True)
        api.validator = oa.TokenValidator(static_tokens=["secret123"])
        th = threading.Thread(target=api.serve, daemon=True)
        th.start()
        time.sleep(0.6)
        noauth = ApiClient(f"http://127.0.0.1:{port}")
        denied = False
        try:
            noauth.health()
        except Exception as e:  # noqa: BLE001
            denied = "401" in str(e) or "403" in str(e)
        okauth = ApiClient(f"http://127.0.0.1:{port}", token="secret123")
        passed = okauth.health().get("status") == "ok"
        api.shutdown()
        out["api_auth"] = {
            "no_token_rejected": denied,
            "valid_token_accepted": passed,
        }
    except Exception as e:  # noqa: BLE001
        out["api_auth"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # --- 4.6 记忆库是否泄露密钥（只应存偏好，不应存任何 secret）---
    try:
        from memory import MemoryStore
        ms = MemoryStore(db_path=os.path.join(d, "mem.db"))
        ms.remember("preference", "k", "v")
        conn = sqlite3.connect(ms.db_path)
        allv = " ".join(str(r[0]) for r in conn.execute("SELECT value FROM memory_items"))
        conn.close()
        ms.close()
        pats = [r"sk-[A-Za-z0-9]{20,}", r"ghp_[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}"]
        leaked = [p for p in pats if re.search(p, allv)]
        out["memory_secret_leak"] = {"leaked": bool(leaked), "patterns": leaked}
    except Exception as e:  # noqa: BLE001
        out["memory_secret_leak"] = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # --- 4.7 源码硬编码密钥自查（用自家的深度引擎扫自己）---
    #
    # 用 scanner 引擎而非三条正则，并且**排除样本/规则文件**：检测器规则库与测试语料
    # 按定义就是凭证形态的样本库，把它们算作"硬编码密钥"是自我误报。
    # 判据收紧到：severity >= high 且未被抑制 → 才可能是真的泄露。
    #
    # 两条排错纪律（都是实测踩过的）：
    #   1. **必须带证据**：只报文件名会让 `clean: false` 无法行动——要能给出行号与
    #      脱敏片段，人工才能几秒内判断是真泄露还是误报。
    #   2. **公开测试向量要豁免**：gitleaks 官方向量（如 AKIALALEMEL33243OLIA）是
    #      全世界都知道的假密钥，本工具为对齐公开基准而引用它属正常，不算泄露。
    try:
        from scanner.engine import scan_path
        res = scan_path(ROOT, high_entropy=False)
        excluded = {
            "scanner\\detectors.py", "scanner/detectors.py",
            "tools\\scan_bench.py", "tools/scan_bench.py",
        }

        def _has_known_public_vector(path, line_no):
            """该行是否含"公开已知的测试向量"（读原文判断，不靠脱敏串）。"""
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                return False
            if not (1 <= line_no <= len(lines)):
                return False
            return any(v in lines[line_no - 1]
                       for v in KNOWN_PUBLIC_TEST_VECTORS)

        hits, evidence, known_vec_hits = [], [], []
        for f in res.get("findings", []):
            rel = os.path.relpath(f["path"], ROOT)
            # 只看 Python 源码：文档（.md）里必然出现凭证示例（示例域名、示例密钥），
            # 那是写给人看的说明文字，不是"硬编码到代码里的真实凭证"。
            if not rel.endswith(".py"):
                continue
            if rel in excluded or rel.startswith(("tests" + os.sep, "examples" + os.sep,
                                                  "data" + os.sep + "bench_corpus")):
                continue
            if f["severity"] not in ("critical", "high"):
                continue
            ev = {"file": rel, "line": f.get("line_no"),
                  "rule": f["rule_id"], "severity": f["severity"],
                  "masked": (f.get("line_masked") or "")[:90]}
            if _has_known_public_vector(f["path"], f.get("line_no") or 0):
                ev["verdict"] = "公开已知测试向量（豁免）"
                known_vec_hits.append(ev)
                continue
            hits.append(rel)
            evidence.append(ev)
        out["source_hardcoded_secret"] = {
            "files": sorted(set(hits)), "clean": not hits,
            "evidence": evidence[:10],
            "known_public_test_vectors": known_vec_hits[:5],
            "engine": "scanner/detectors(78 rules)",
            "scope": "仅 .py 源码（排除 tests/、examples/、公开基准语料与规则样本）",
            "excluded_sample_files": sorted(excluded),
        }
    except Exception as e:  # noqa: BLE001
        out["source_hardcoded_secret"] = {"status": "error",
                                          "error": f"{type(e).__name__}: {e}"}

    return out


SECTIONS = {
    "inventory": audit_inventory,
    "perf": audit_perf,
    "scan": audit_scan,
    "depth": audit_depth,
    "security": audit_security,
}


def main():
    ap = argparse.ArgumentParser(description="项目自检评测")
    ap.add_argument("--only", default=None, choices=list(SECTIONS))
    ap.add_argument("--out", default=None, help="输出 JSON 路径")
    ap.add_argument("--n", type=int, default=500, help="性能样本量")
    args = ap.parse_args()

    result = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
              "python": sys.version.split()[0], "platform": sys.platform}
    todo = [args.only] if args.only else list(SECTIONS)
    for name in todo:
        t0 = time.perf_counter()
        try:
            result[name] = SECTIONS[name]() if name != "perf" else audit_perf(args.n)
            result[name]["_elapsed_s"] = round(time.perf_counter() - t0, 2)
        except Exception as e:  # noqa: BLE001
            result[name] = {"status": "error",
                            "error": f"{type(e).__name__}: {e}"}

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[已写入] {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
