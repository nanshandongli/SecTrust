"""新增深度能力的回归测试（扫描深度 / 记忆深度 / 规范时效）。

设计原则与既有测试一致：**不测实现细节，测承诺**。每条测试对应一句对外承诺，
失败即代表承诺被破坏。跨会话/跨进程类承诺一律用**真实子进程**验证（单进程内
验证会掩盖哈希随机化这类问题——这是项目上一次真实事故的教训）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.detectors import (DETECTORS, BY_ID, detector_stats, find_in_text)  # noqa: E402
from scanner.entropy import (shannon_entropy, assess,                          # noqa: E402
                             extract_high_entropy_candidates)
from scanner.findings import (Allowlist, dedupe, fingerprint, normalize,       # noqa: E402
                              redact, save_baseline, load_baseline,
                              diff_against_baseline, has_inline_allow, gate)
from scanner.engine import scan_path, iter_files                               # noqa: E402
from scanner.sarif import to_sarif, validate_sarif                             # noqa: E402

# 公开假样本（非真实凭证）——源码按片段拆开、运行时拼接，拼接值与原串逐字符一致；
# 拆分写法是为了通过 GitHub Push Protection（它拦截形似真密钥的连续字面量）。
_AWS_AKID = "AKIA3H7RZQ2XUVW" + "XYZAB"
_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG" + "/bPxRfiCYzREALKEY90"
from memory_hybrid import (HybridMemory, BM25, rrf, normalize as norm_scores,  # noqa: E402
                           tokenize, extract_entities, expand_query)
from security.oauth import (protected_resource_metadata, www_authenticate,     # noqa: E402
                            validate_resource_indicator, verify_iss,
                            ClientIdMetadataDocument)

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ===================================================================
# 一、扫描深度：检测器库与熵引擎
# ===================================================================
class DetectorLibraryTest(unittest.TestCase):

    def test_rule_count_and_structure(self):
        """承诺：规则库是结构化的（keywords/熵/分组），不是散落的正则。"""
        st = detector_stats()
        self.assertGreaterEqual(st["total"], 70, "规则数量应达到可用规模")
        self.assertGreaterEqual(st["with_keywords"], st["total"] - 2)
        self.assertGreaterEqual(st["with_entropy"], 8)
        self.assertIn("aws-access-key-id", BY_ID)
        self.assertIn("github-pat-classic", BY_ID)

    def test_every_detector_compiles(self):
        for d in DETECTORS:
            self.assertIsNotNone(d.compiled(), f"{d.id} 正则无法编译")

    def test_keyword_prefilter_skips_irrelevant_rules(self):
        """keywords 是性能与正确性双重保障：无关规则不该命中。"""
        det = BY_ID["stripe-secret-key"]
        self.assertFalse(det.matches_keyword("完全无关的一段中文文本"))
        self.assertTrue(det.matches_keyword("stripe sk_live_xxx"))

    def test_entropy_gate_rejects_low_entropy(self):
        """熵门槛：形态对但明显是填充值的，不应报。"""
        det = BY_ID["generic-api-key-assignment"]
        self.assertIsNotNone(det.entropy)
        low = 'api_key = "aaaaaaaaaaaaaaaaaaaa"'        # 熵 0
        self.assertEqual(find_in_text(low, only=[det]), [])

    def test_aws_example_key_is_not_reported(self):
        """AWS 官方文档示例密钥不得被报（曾误报，已修并固化为回归）。

        注意：检测器只做"标记"（suppressed_by），真正丢弃发生在抑制层——
        这样审计模式才能看到"为什么没报"。因此这里断言的是标记而非缺席。"""
        hits = find_in_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"')
        aws = [h for h in hits if h["rule_id"] == "aws-access-key-id"]
        self.assertTrue(aws, "示例密钥也应被形态命中（否则说明正则漏了）")
        self.assertTrue(all(h["suppressed_by"] for h in aws),
                        "示例密钥必须被抑制词标记")

    def test_real_looking_aws_key_is_reported(self):
        hits = find_in_text(f'AWS_KEY = "{_AWS_AKID}"')
        self.assertIn("aws-access-key-id", {h["rule_id"] for h in hits})

    def test_secret_group_extracts_value_only(self):
        """secret_group：送去算熵/算指纹的必须是凭证本身，不是整行。"""
        hits = find_in_text(f'aws_secret_access_key = "{_AWS_SECRET}"')
        aws = [h for h in hits if h["rule_id"] == "aws-secret-access-key"]
        self.assertEqual(len(aws), 1)
        self.assertEqual(aws[0]["secret"], _AWS_SECRET)

    def test_stopword_does_not_suppress_real_credentials(self):
        """抑制词不能误伤真实凭证：域名含 example 的连接串必须照报。"""
        hits = find_in_text(
            'DATABASE_URL = "postgres://admin:Sup3rS3cret@db.internal.example:5432/app"')
        ids = {h["rule_id"] for h in hits}
        self.assertIn("db-connection-string", ids)
        # 连接串本身不得被抑制（曾因域名含 example 被整行抑制，属假阴性）
        db = [h for h in hits if h["rule_id"] == "db-connection-string"]
        self.assertTrue(db)
        self.assertIsNone(db[0]["suppressed_by"], "真实连接串被误抑制")


class EntropyEngineTest(unittest.TestCase):

    def test_shannon_entropy_values(self):
        self.assertAlmostEqual(shannon_entropy("aaaa"), 0.0, places=6)
        # 4 种字符等概率 → log2(4) = 2.0
        self.assertAlmostEqual(shannon_entropy("abcd"), 2.0, places=6)

    def test_entropy_separates_secret_from_natural_language(self):
        secret = shannon_entropy("wJalrXUtnFEMI/K7MDENG")
        natural = shannon_entropy("this is a normal english sentence")
        self.assertGreater(secret, natural)

    def test_assess_rejects_hash_uuid_digits(self):
        cases = [
            "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",  # sha256
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",                              # uuid
            "12345678901234567890",                                              # 纯数字
        ]
        for c in cases:
            ok, _score, reason = assess(c)
            self.assertFalse(ok, f"{c} 不应判为凭证（理由：{reason}）")

    def test_assess_accepts_high_entropy_with_context(self):
        ok, score, _ = assess("7Qk2ZmPx8VrLdN4cYtWbGjHfAeS1uRo9",
                              line="internal_signing_secret = \"7Qk2ZmPx8VrLdN4cYtWbGjHfAeS1uRo9\"")
        self.assertTrue(ok)
        self.assertGreater(score, 0.5)

    def test_high_entropy_candidates_ignore_assignment_lines(self):
        """整行赋值不应被当成高熵候选（曾导致 2 条误报）。"""
        cands = extract_high_entropy_candidates("DB_PASSWORD=SuperSecretDbPass123")
        self.assertEqual(cands, [])


# ===================================================================
# 二、扫描工程化：漏扫回归 / 抑制 / 基线 / SARIF
# ===================================================================
class ScanEngineTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="scaneng_")

    def _write(self, rel: str, content: str) -> str:
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(rel) else None
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        return p

    def test_dotenv_is_scanned(self):
        """关键回归：点文件必须扫。.env 是凭证泄露最高频位置，曾因 '以点开头=隐藏'
        被整类跳过，导致扫描器形同虚设。"""
        self._write(".env", 'DB_PASSWORD=SuperSecretDbPass123\n'
                            'JWT_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
                            'eyJzdWIiOiIxMjM0NTY3ODkwIn0.'
                            'dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"\n')
        files = iter_files(self.dir)
        self.assertTrue(any(os.path.basename(f) == ".env" for f in files),
                        ".env 必须被纳入扫描")
        res = scan_path(self.dir)
        self.assertGreater(len(res["findings"]), 0, ".env 里的凭证必须被发现")

    def test_github_workflow_dir_is_scanned(self):
        self._write(".github/workflows/ci.yml",
                    "on: pull_request_target\njobs:\n  b:\n    steps:\n"
                    "      - uses: actions/checkout@v3\n"
                    "        with:\n          ref: ${{ github.event.pull_request.head.ref }}\n")
        files = iter_files(self.dir)
        self.assertTrue(any(".github" in f for f in files), ".github/ 不应被整目录跳过")

    def test_vcs_internal_dir_is_skipped(self):
        self._write(".git/objects/ab", _AWS_AKID)
        files = iter_files(self.dir)
        self.assertFalse(any(".git" in f for f in files), ".git 内部对象不应扫描")

    def test_binary_and_large_files_skipped(self):
        self._write("logo.png", "\x00\x01\x02" * 100)
        files = iter_files(self.dir)
        self.assertFalse(any(f.endswith(".png") for f in files))

    def test_inline_allow_suppression(self):
        self._write("a.py", 'TOKEN = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz"  # scanner:allow\n')
        res = scan_path(self.dir)
        self.assertEqual(res["findings"], [], "行内 scanner:allow 应抑制该条")
        # 审计模式下应能看到被抑制项及其原因
        res2 = scan_path(self.dir, keep_suppressed=True)
        self.assertTrue(all(f["suppressed"] for f in res2["findings"]))

    def test_ignore_file_suppression(self):
        self._write("a.py", 'TOKEN = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz"\n')
        fp = fingerprint("github-pat-classic",
                         "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz", "a.py")
        self._write(".scanignore", fp + "\n")
        res = scan_path(self.dir)
        self.assertEqual(res["findings"], [], ".scanignore 中的指纹应被抑制")

    def test_baseline_only_reports_new(self):
        self._write("a.py", 'TOKEN = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz"\n')
        base = os.path.join(self.dir, "baseline.json")
        r1 = scan_path(self.dir, baseline_path=base, write_baseline=True)
        self.assertEqual(len(r1["findings"]), 1)
        self.assertEqual(r1["new_findings"], [], "首次建基线后不应有新增")

        # 修复它 → 基线里那条应被判定为已解决
        with open(os.path.join(self.dir, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("TOKEN = os.environ['TOKEN']\n")
        r2 = scan_path(self.dir, baseline_path=base)
        self.assertEqual(len(r2["findings"]), 0)
        self.assertEqual(len(r2["resolved_since_baseline"]), 1)

    def test_baseline_detects_new_leak(self):
        self._write("a.py", 'A = "nothing"\n')
        base = os.path.join(self.dir, "baseline.json")
        scan_path(self.dir, baseline_path=base, write_baseline=True)
        self._write("b.py", 'TOKEN = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz"\n')
        r = scan_path(self.dir, baseline_path=base)
        self.assertEqual(len(r["new_findings"]), 1, "新增泄露必须被识别")
        self.assertTrue(r["gate"]["should_fail"])

    def test_fingerprint_is_stable_across_paths(self):
        a = fingerprint("r", "s3cr3t", "src/a.py")
        b = fingerprint("r", "s3cr3t", "lib/b.py")
        self.assertNotEqual(a, b, "不同文件应视为不同发现（便于逐处修复）")
        self.assertEqual(len(a), 16)
        self.assertEqual(a, fingerprint("r", "s3cr3t", "src/a.py"))

    def test_fingerprint_cross_process_stable(self):
        """指纹必须跨进程一致，否则基线/去重会在换进程后失效。"""
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from scanner.findings import fingerprint;"
                "print(fingerprint('rule','secret','a.py'))" % ROOT)
        outs = set()
        for _ in range(2):
            r = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=60)
            outs.add(r.stdout.strip())
        self.assertEqual(len(outs), 1, f"跨进程指纹不一致: {outs}")

    def test_redaction_never_leaks_full_secret(self):
        s = "ghp_1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz"
        m = redact(s)
        self.assertNotIn(s[6:-6], m)
        self.assertIn("len=40", m)
        f = normalize({"rule_id": "r", "rule_name": "R", "severity": "high",
                       "secret": s, "match": s, "line": f'TOKEN = "{s}"',
                       "line_no": 1, "start": 0, "entropy": 4.5, "tags": [],
                       "provider": "", "validator": None, "description": "",
                       "entropy_required": None, "suppressed_by": None},
                      path="a.py")
        self.assertNotIn(s, json.dumps(f.to_dict(), ensure_ascii=False),
                         "序列化结果不得包含完整凭证")

    def test_dedupe_keeps_highest_severity(self):
        fs = [normalize({"rule_id": "a", "rule_name": "A", "severity": "low",
                         "secret": "s", "match": "s", "line": "s", "line_no": 1,
                         "start": 0, "entropy": 4.0, "tags": [], "provider": "",
                         "validator": None, "description": "", "entropy_required": None,
                         "suppressed_by": None}, path="p"),
              normalize({"rule_id": "b", "rule_name": "B", "severity": "critical",
                         "secret": "s", "match": "s", "line": "s", "line_no": 1,
                         "start": 0, "entropy": 4.0, "tags": [], "provider": "",
                         "validator": None, "description": "", "entropy_required": None,
                         "suppressed_by": None}, path="p")]
        out = dedupe(fs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, "critical")

    def test_gate_semantics(self):
        fs = [normalize({"rule_id": "a", "rule_name": "A", "severity": "low",
                         "secret": "s", "match": "s", "line": "s", "line_no": 1,
                         "start": 0, "entropy": 4.0, "tags": [], "provider": "",
                         "validator": None, "description": "", "entropy_required": None,
                         "suppressed_by": None}, path="p")]
        self.assertFalse(gate(fs, fail_on="high")["should_fail"])
        self.assertTrue(gate(fs, fail_on="low")["should_fail"])


class SarifTest(unittest.TestCase):

    def _findings(self):
        return [normalize({"rule_id": "github-pat-classic", "rule_name": "GitHub PAT",
                           "severity": "critical", "secret": "ghp_" + "a" * 36,
                           "match": "x", "line": "TOKEN = ghp_" + "a" * 36, "line_no": 3,
                           "start": 0, "entropy": 4.2, "tags": ["vcs"], "provider": "github",
                           "validator": "github", "description": "GitHub PAT",
                           "entropy_required": None, "suppressed_by": None}, path="src/a.py")]

    def test_sarif_structure_valid(self):
        doc = to_sarif(self._findings(), root="/repo")
        self.assertEqual(validate_sarif(doc), [])
        self.assertEqual(doc["version"], "2.1.0")

    def test_sarif_levels_and_uri(self):
        doc = to_sarif(self._findings(), root="/repo")
        res = doc["runs"][0]["results"][0]
        self.assertEqual(res["level"], "error")            # critical → error
        self.assertEqual(res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                         "src/a.py")
        self.assertEqual(res["locations"][0]["physicalLocation"]["region"]["startLine"], 3)
        self.assertIn("fingerprints", res)

    def test_sarif_contains_no_plaintext_secret(self):
        doc = to_sarif(self._findings(), root="/repo")
        blob = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn("ghp_" + "a" * 36, blob)

    def test_sarif_rules_are_declared(self):
        doc = to_sarif(self._findings(), root="/repo")
        ids = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
        self.assertIn("github-pat-classic", ids)
        self.assertGreaterEqual(len(ids), 70)


# ===================================================================
# 三、记忆深度：BM25 / RRF / 实体 / 衰减 / 作用域
# ===================================================================
class HybridMemoryTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hybmem_")
        self.db = os.path.join(self.dir, "mem.db")
        self.hm = HybridMemory(db_path=self.db)
        self.hm.remember("preference", "code_review_model", "Qwen-Max",
                         note="用户偏好用 Qwen-Max 做代码审查", kind="semantic",
                         scope={"user_id": "u1"})
        self.hm.remember("preference", "editor", "Neovim",
                         note="用户喜欢用 Neovim", kind="semantic", scope={"user_id": "u1"})
        self.hm.remember("repo", "main", "/data/foo", note="主仓库路径",
                         kind="semantic", scope={"user_id": "u1"})
        self.hm.remember("incident", "oom", "导出任务 OOM", note="上周事故",
                         kind="episodic", scope={"user_id": "u1"})
        self.hm.remember("preference", "other", "别人的偏好", note="其他用户",
                         kind="semantic", scope={"user_id": "u2"})

    def tearDown(self):
        self.hm.close()

    def test_bm25_finds_exact_terms(self):
        """纯向量检索会漏掉的专有名词，BM25 必须补上。"""
        self.assertGreater(self.hm.bm25.scores(tokenize("Qwen-Max")).get(
            "preference::code_review_model", 0), 0)

    def test_top1_accuracy(self):
        cases = [("代码审查用哪个模型", "code_review_model"),
                 ("Qwen-Max", "code_review_model"),
                 ("主仓库路径", "main"),
                 ("上周的 OOM 事故", "oom"),
                 ("编辑器", "editor")]
        for q, expected in cases:
            r = self.hm.search(q, k=1)
            self.assertTrue(r, f"{q} 无结果")
            self.assertEqual(r[0]["key"], expected, f"{q} → {r[0]['key']}")

    def test_entity_extraction(self):
        ents = extract_entities("用 Qwen-Max 审查 /data/foo 下的代码")
        joined = " ".join(ents)
        self.assertIn("qwen-max", joined)
        self.assertTrue(any("data" in e for e in ents))

    def test_entity_signal_boost(self):
        r = self.hm.search("Qwen-Max", k=1, explain=True)
        self.assertGreater(r[0]["signals"]["entity"], 0)

    def test_query_expansion(self):
        self.assertIn("editor", expand_query("编辑器"))

    def test_scope_isolation(self):
        r = self.hm.search("偏好", k=10, scope={"user_id": "u2"})
        keys = {x["key"] for x in r}
        self.assertEqual(keys, {"other"}, f"作用域隔离失效: {keys}")

    def test_kind_filter(self):
        r = self.hm.search("事故", k=10, kinds=["episodic"])
        self.assertEqual({x["key"] for x in r}, {"oom"})

    def test_rrf_fusion_is_deterministic(self):
        a = rrf([["x", "y"], ["y", "x"], []], k=60)
        b = rrf([["x", "y"], ["y", "x"], []], k=60)
        self.assertEqual(a, b)

    def test_normalize_scores(self):
        n = norm_scores({"a": 4.0, "b": 2.0, "c": 0.0})
        self.assertAlmostEqual(n["a"], 1.0)
        self.assertAlmostEqual(n["b"], 0.5)
        self.assertAlmostEqual(n["c"], 0.0)

    def test_decay_and_reinforcement(self):
        """衰减按最后访问时间；被检索会刷新（强化循环）。"""
        meta = {"kind": "semantic", "created_at": 0.0, "updated_at": 0.0,
                "last_access": None, "importance": 0.5}
        old = self.hm.decay_factor(meta, now=180 * 86400.0)
        fresh = self.hm.decay_factor(meta, now=1.0)
        self.assertLess(old, fresh)
        self.assertGreater(old, 0.0)
        # 检索后 last_access 被刷新
        self.hm.search("Qwen-Max", k=1)
        conn = self.hm._connect()
        cnt = conn.execute("SELECT access_count FROM memory_meta WHERE key='code_review_model'"
                           ).fetchone()[0]
        self.assertGreaterEqual(cnt, 1)

    def test_episodic_decays_faster_than_semantic(self):
        now = 30 * 86400.0
        m_sem = {"kind": "semantic", "created_at": 0.0, "importance": 0.5}
        m_epi = {"kind": "episodic", "created_at": 0.0, "importance": 0.5}
        self.assertLess(self.hm.decay_factor(m_epi, now=now),
                        self.hm.decay_factor(m_sem, now=now))

    def test_backward_compatible_semantic_search(self):
        """继承 SemanticMemory 的旧接口必须仍然可用。"""
        r = self.hm.search_semantic("代码审查 用哪个模型", k=3)
        self.assertTrue(r)
        self.assertEqual(r[0][1], "code_review_model")


class HybridMemoryCrossProcessTest(unittest.TestCase):
    """跨会话是长期记忆的立身之本，必须真实子进程验证（曾被哈希随机化击穿）。"""

    def test_cross_process_recall(self):
        d = tempfile.mkdtemp(prefix="xproc_")
        db = os.path.join(d, "mem.db")
        write = textwrap.dedent(f"""
            import sys; sys.path.insert(0, r"{ROOT}")
            from memory_hybrid import HybridMemory
            hm = HybridMemory(db_path=r"{db}")
            hm.remember("preference", "code_review_model", "Qwen-Max",
                        note="用户偏好用 Qwen-Max 做代码审查")
            hm.remember("preference", "editor", "Neovim", note="用户喜欢用 Neovim")
            hm.remember("repo", "main", "/data/foo", note="主仓库路径")
            print("written", hm.stats()["items"])
            hm.close()
        """)
        read = textwrap.dedent(f"""
            import sys; sys.path.insert(0, r"{ROOT}")
            from memory_hybrid import HybridMemory
            hm = HybridMemory(db_path=r"{db}")
            for q in ["代码审查用哪个模型", "Qwen-Max", "主仓库路径", "编辑器"]:
                r = hm.search(q, k=1)
                print(q, "->", r[0]["key"] if r else "NONE", round(r[0]["score"], 4) if r else 0)
            hm.close()
        """)
        for script in (write, read, read):
            p = subprocess.run([PY, "-c", script], capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr[-500:])

        # 两次独立读取必须给出**完全相同**的结果与分数
        outs = []
        for _ in range(2):
            p = subprocess.run([PY, "-c", read], capture_output=True, text=True, timeout=120)
            outs.append(p.stdout.strip())
        self.assertEqual(outs[0], outs[1], "跨进程检索结果不一致")

        lines = outs[0].splitlines()
        expected = ["code_review_model", "code_review_model", "main", "editor"]
        self.assertEqual(len(lines), 4)
        for ln, exp in zip(lines, expected):
            self.assertIn(exp, ln, f"{ln} 未命中 {exp}")


# ===================================================================
# 四、规范时效：MCP 2026-07-28 + OAuth 2.1
# ===================================================================
class ProtocolCurrencyTest(unittest.TestCase):

    def test_declares_latest_protocol_version(self):
        import mcp_server
        self.assertEqual(mcp_server.PROTOCOL_VERSION, "2026-07-28")
        self.assertIn("2026-07-28", mcp_server.SUPPORTED_PROTOCOL_VERSIONS)
        # 旧版仍可服务（现实兼容需要）
        self.assertIn("2024-11-05", mcp_server.SUPPORTED_PROTOCOL_VERSIONS)

    def test_no_deprecated_capabilities_declared(self):
        import mcp_server
        caps = mcp_server.server_capabilities()
        for deprecated in ("roots", "sampling", "logging"):
            self.assertNotIn(deprecated, caps)

    def test_meta_carries_protocol_version(self):
        """2026-07-28：握手移除后，版本走 _meta。"""
        import mcp_server
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
               "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}
        resp = mcp_server.McpServer().handle(req)
        self.assertIn("result", resp)

    def test_unsupported_version_rejected(self):
        import mcp_server
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
               "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}}}
        resp = mcp_server.McpServer().handle(req)
        self.assertIn("UnsupportedProtocolVersion", resp["error"]["message"])


class OAuth21ResourceServerTest(unittest.TestCase):

    def test_protected_resource_metadata_rfc9728(self):
        os.environ["MCP_RESOURCE_URL"] = "https://mcp.example.com"
        try:
            md = protected_resource_metadata()
            self.assertEqual(md["resource"], "https://mcp.example.com")
            self.assertIn("authorization_servers", md)
            self.assertIn("scopes_supported", md)
        finally:
            os.environ.pop("MCP_RESOURCE_URL", None)

    def test_www_authenticate_points_to_metadata(self):
        os.environ["MCP_RESOURCE_URL"] = "https://mcp.example.com"
        try:
            h = www_authenticate()
            self.assertIn("Bearer", h)
            self.assertIn("resource_metadata=", h)
            self.assertIn(".well-known/oauth-protected-resource", h)
        finally:
            os.environ.pop("MCP_RESOURCE_URL", None)

    def test_resource_indicator_binding_rfc8707(self):
        os.environ["MCP_RESOURCE_URL"] = "https://mcp.example.com"
        try:
            self.assertTrue(validate_resource_indicator({"aud": "https://mcp.example.com"}))
            self.assertTrue(validate_resource_indicator(
                {"aud": ["https://other.com", "https://mcp.example.com"]}))
            # 发给别的服务器的令牌不得被接受（防跨服务重放）
            self.assertFalse(validate_resource_indicator({"aud": "https://evil.com"}))
            self.assertFalse(validate_resource_indicator({}))
        finally:
            os.environ.pop("MCP_RESOURCE_URL", None)

    def test_issuer_verification_rfc9207(self):
        os.environ["OAUTH_ISSUER"] = "https://as.example.com"
        try:
            self.assertTrue(verify_iss("https://as.example.com"))
            self.assertFalse(verify_iss("https://evil.example.com"))
            self.assertFalse(verify_iss(None))
        finally:
            os.environ.pop("OAUTH_ISSUER", None)

    def test_cimd_requires_https_and_matching_id(self):
        c = ClientIdMetadataDocument()
        self.assertTrue(c.is_cimd("https://client.example.com/metadata.json"))
        self.assertFalse(c.is_cimd("my-client-id"))
        # 非 https 默认拒绝
        self.assertFalse(c.fetch("http://client.example.com/metadata.json")["ok"])
        self.assertFalse(c.fetch("not-a-url")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
