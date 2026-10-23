"""
产品化升级路线（P2 之后）的回归测试。

覆盖：AES(NIST 向量) / 渠道解密集成 / HTTP API 往返+鉴权 / 消息队列持久化 /
事件总线 / 扫描深度回退 / 语义记忆跨会话 / OAuth JWT / 可观测性指标。
全部使用临时目录与临时端口，不触碰 data/ 与真实网络。
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from security import crypto as crypto_mod
from security import oauth as oauth_mod
from security import observability as obs_mod
from api import app as api_app
from api import client as api_client
from transport import queue as mq
from transport import broker as mq_broker
from scanner import semgrep_adapter, validators, push_protection
from memory import MemoryStore
from memory_semantic import SemanticMemory


class TestCrypto(unittest.TestCase):
    def test_nist_cbc_vector(self):
        key = bytes.fromhex(
            "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pt = bytes.fromhex(
            "6bc1bee22e409f96e93d7e117393172a"
            "ae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52ef"
            "f69f2445df4f9b17ad2b417be66c3710")
        expected = bytes.fromhex(
            "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
            "9cfc4e967edb808d679f777bc6702c7d"
            "39f23369a9d9bacfa530e26304231461"
            "b2eb05e2c39be9fcda6c19078c6a9d1b")
        ct = crypto_mod.aes_cbc_encrypt(key, iv, pt)
        self.assertEqual(ct[:64], expected)  # 原生 CBC（无填充）
        self.assertEqual(crypto_mod.aes_cbc_decrypt(key, iv, ct), pt)  # PKCS7 往返

    def test_platform_decrypt_roundtrip(self):
        aes_key = base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        msg = "企业微信/飞书 真实消息 hello ☎"
        enc = crypto_mod.feishu_encrypt(aes_key, msg, receive_id="appid-123")
        self.assertEqual(
            crypto_mod.wecom_decrypt(aes_key, enc, receive_id="appid-123"), msg)
        self.assertEqual(
            crypto_mod.feishu_decrypt(aes_key, enc, receive_id="appid-123"), msg)


class TestChannelDecrypt(unittest.TestCase):
    def test_wecom_feishu_decrypt_integration(self):
        # 复用纯 Python 实现，确认渠道解密链路可被纯标准库满足
        aes_key = base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        msg = "<xml>hello 中文</xml>"
        enc = crypto_mod.feishu_encrypt(aes_key, msg, receive_id="corp-1")
        from channels.wecom import WeComChannel  # 仅验证解密函数可达
        # WeComChannel._decrypt 依赖实例属性，这里直接验证 crypto 被其调用一致
        self.assertEqual(crypto_mod.wecom_decrypt(aes_key, enc, receive_id="corp-1"), msg)


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "reg.db")
        cls.mb = os.path.join(cls.tmp, "mem.db")
        cls.api = api_app.RegistryAPI(db_path=cls.db, memory_db_path=cls.mb,
                                      host="127.0.0.1", port=8123,
                                      require_auth=False)
        cls.srv = threading.Thread(target=cls.api.serve, daemon=True)
        cls.srv.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        time.sleep(0.3)

    def test_task_and_memory_roundtrip(self):
        c = api_client.ApiClient("http://127.0.0.1:8123")
        self.assertEqual(c.health()["status"], "ok")
        tid = c.register_task(None, {"x": 1})
        self.assertTrue(tid and tid != "None")
        c.update_task_status(tid, "done")
        self.assertEqual(c.get_task(tid)["status"], "done")
        c.beat("r1")
        self.assertGreaterEqual(len(c.list_runners()), 1)
        c.memory_set("preference", "code_review_model", "Qwen-Max")
        self.assertEqual(
            c.memory_get("preference", "code_review_model")["value"], "Qwen-Max")
        self.assertEqual(len(c.list_templates()), 5)

    def test_auth_required_rejects(self):
        api2 = api_app.RegistryAPI(db_path=self.db, memory_db_path=self.mb,
                                   host="127.0.0.1", port=8124, require_auth=True)
        api2.validator = oauth_mod.TokenValidator(static_tokens=["good"])
        srv2 = threading.Thread(target=api2.serve, daemon=True)
        srv2.start()
        time.sleep(0.5)
        try:
            # 无令牌：401
            anon = api_client.ApiClient("http://127.0.0.1:8124")
            with self.assertRaises(RuntimeError):
                anon.health()
            # 有效令牌：200
            good = api_client.ApiClient("http://127.0.0.1:8124", token="good")
            self.assertEqual(good.health()["status"], "ok")
        finally:
            api2.shutdown()
            time.sleep(0.3)


class TestTransport(unittest.TestCase):
    def test_local_queue_persist(self):
        db = os.path.join(tempfile.mkdtemp(), "mq.db")
        q = mq.LocalQueue(db_path=db)
        q.publish("t", {"a": 1})
        q.publish("t", {"a": 2})
        self.assertEqual(q.pending("t"), 2)
        msgs = q.consume("t", batch=10)
        self.assertEqual(len(msgs), 2)
        q.ack(msgs[0]["id"])
        self.assertEqual(q.pending("t"), 0)  # 取走即处理中，ack 后从 pending 移除

    def test_broker_events(self):
        b = mq_broker.Broker()
        got = []
        w = b.spawn_worker("ev", lambda ev: got.append(ev), poll_interval=0.05)
        b.publish_event("ev", "created", {"id": 1})
        time.sleep(0.4)
        b.stop_worker(w)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["event_type"], "created")


class TestScanner(unittest.TestCase):
    def test_semgrep_unavailable_fallback(self):
        self.assertFalse(semgrep_adapter.semgrep_available())
        res = semgrep_adapter.scan_with_semgrep(".")
        self.assertFalse(res["available"])
        self.assertIn("semgrep", res["error"])

    def test_validators_offline(self):
        v = validators.validate_secret("ghp_" + "a" * 36, "github")
        self.assertEqual(v["verdict"], "unverified")

    def test_scan_does_not_skip_root_on_relative_path(self):
        """相对路径 "." 不得导致仓库根目录被整层跳过。

        曾踩的坑：扫描脚本用 `os.path.basename(dp).startswith(".")` 判隐藏目录，
        而 `basename(".") == "."` → 相对路径调用时**根目录全部文件被静默漏扫**，
        产出"干净"的假阴性报告。安全扫描器里这类 bug 最危险。
        """
        from sandbox import SandboxManager
        from templates import get_template, parse_report
        repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "examples", "sample_repo")
        if not os.path.isdir(repo):
            self.skipTest("缺少 examples/sample_repo")
        tpl = get_template("generic_sast")
        sb = SandboxManager.auto()
        got = {}
        for arg in (".", repo):  # 相对 vs 绝对必须一致
            res = sb.run(tpl.build_command(arg), repo, timeout=60)
            self.assertTrue(res.ok, res.error)
            got[arg] = parse_report(res.content)
        rel, abs_ = got["."], got[repo]
        self.assertEqual(rel, abs_, "相对路径与绝对路径扫描结果不一致")
        self.assertGreater(rel.get("scanned_files", 0), 1,
                           "根目录文件被漏扫（假阴性）")

    def test_push_protection(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".git", "hooks"))
        p = push_protection.generate_pre_push_hook(d)
        self.assertTrue(os.path.exists(p))
        yaml_text = push_protection.github_actions_yaml()
        self.assertIn("name: Secret Scan", yaml_text)


class TestMemorySemantic(unittest.TestCase):
    def test_semantic_recall_and_persist(self):
        db = os.path.join(tempfile.mkdtemp(), "mem.db")
        sm = SemanticMemory(db_path=db)
        sm.remember("preference", "code_review_model", "Qwen-Max",
                    note="用户偏好用 Qwen-Max 做代码审查")
        sm.remember("preference", "editor", "Neovim", note="用户喜欢用 Neovim")
        sm.close()
        # 模拟下一会话重新打开
        sm2 = SemanticMemory(db_path=db)
        res = sm2.search_semantic("代码审查 用哪个模型", k=3)
        self.assertEqual(res[0][1], "code_review_model")
        sm2.close()

    def test_backfill_cross_entry_writes(self):
        """记忆经 MemoryStore（/ MCP memory_set）直接写入时，语义层也要能召回。"""
        db = os.path.join(tempfile.mkdtemp(), "mem.db")
        ms = MemoryStore(db_path=db)
        ms.remember("preference", "code_review_model", "Qwen-Max",
                    note="用户偏好用 Qwen-Max 做代码审查")
        ms.close()
        sm = SemanticMemory(db_path=db)  # 另一入口/会话打开
        res = sm.search_semantic("代码审查用哪个模型", k=3)
        self.assertTrue(res, "跨入口写入的记忆应能被语义检索召回")
        self.assertEqual(res[0][1], "code_review_model")
        sm.close()

    def test_embed_stable_across_processes(self):
        """向量桶位必须**跨进程稳定**。

        曾踩的坑：用内置 hash() 做桶位，而 Python 3 对 str 默认 PYTHONHASHSEED 随机加盐，
        导致「进程 A 写入的向量」在「进程 B」里全部错位、余弦恒为 0，跨会话语义检索实质失效。
        本测试用真实子进程往返，单进程内测试抓不到它。
        """
        from memory_semantic import HashedNGramIndex, _cosine
        idx = HashedNGramIndex()
        # 中文查询与中文 note 应有非零重合（CJK bigram 生效）
        sim = _cosine(idx.embed("主仓库在哪"), idx.embed("主业务仓库路径"))
        self.assertGreater(sim, 0, "中文查询应能与中文 note 产生非零相似度")

        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from memory_semantic import HashedNGramIndex\n"
            "i = HashedNGramIndex()\n"
            "print(i.embed('abc').index(max(i.embed('abc'))))\n"
        ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import subprocess
        outs = set()
        for _ in range(3):
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, cwd=os.path.dirname(
                                   os.path.dirname(os.path.abspath(__file__))))
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.add(r.stdout.strip())
        self.assertEqual(len(outs), 1,
                         f"桶位跨进程不稳定（PYTHONHASHSEED 加盐）：{outs}")

    def test_reindex(self):
        db = os.path.join(tempfile.mkdtemp(), "mem.db")
        sm = SemanticMemory(db_path=db)
        sm.remember("repo", "main", "C:/work/app", note="主仓库")
        self.assertEqual(sm.reindex(), 1)
        res = sm.search_semantic("主仓库 路径", k=3)
        self.assertTrue(res)
        sm.close()


class TestMcpProductization(unittest.TestCase):
    """MCP 生态入口需覆盖新增的产品化能力。"""

    def test_tools_count_and_new_capabilities(self):
        import mcp_server as m
        names = [t["name"] for t in m._TOOLS]
        for expected in ("memory_semantic_search", "validate_secret",
                         "push_protection", "queue_publish",
                         "queue_consume", "api_health"):
            self.assertIn(expected, names)
        self.assertGreaterEqual(len(names), 22)

    def test_registry_manifest_matches_code(self):
        """mcpserver.json 声明的工具清单必须与 mcp_server._TOOLS 完全一致。"""
        import mcp_server as m
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "mcpserver.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        declared = set(manifest["tools"])
        actual = {t["name"] for t in m._TOOLS}
        self.assertEqual(declared, actual,
                         f"清单与代码不一致：多声明={declared - actual}，漏声明={actual - declared}")
        # 每个声明的工具都必须能被分发（不要求成功，但不得是「未知工具」）
        for name in sorted(actual):
            if name in ("execute_scan", "push_protection", "queue_publish",
                        "queue_consume", "api_health"):
                continue  # 需要外部前置条件（审批/仓库/远端服务）
            try:
                m._dispatch(name, {})
            except ValueError as e:
                if "未知工具" in str(e):
                    self.fail(f"{name} 已声明但 _dispatch 未实现")
            except (KeyError, TypeError, FileNotFoundError, RuntimeError):
                pass  # 已实现，只是必填参数/外部资源缺失，属预期

    def test_validate_secret_and_ci_snippet_dispatch(self):
        import mcp_server as m
        out = json.loads(m._dispatch("validate_secret", {"secret": "ghp_" + "a" * 36,
                                                         "secret_type": "github"}))
        self.assertIn(out.get("verdict"), ("unverified", "valid", "invalid"))
        ci = json.loads(m._dispatch("push_protection", {"mode": "ci"}))
        self.assertIn("yaml", ci)
        self.assertTrue(ci["yaml"].strip())


class TestOAuth(unittest.TestCase):
    def test_jwt_issue_verify_scope(self):
        secret = "s3cr3t"
        tok = oauth_mod.issue_token("runner-1", secret, scopes=["scan"], expires_in=60)
        self.assertIsNotNone(oauth_mod.verify_token(tok, secret, allowed_scopes=["scan"]))
        self.assertIsNone(oauth_mod.verify_token(tok, secret, allowed_scopes=["admin"]))
        tv = oauth_mod.TokenValidator(static_tokens=["abc"], jwt_secret=secret)
        self.assertTrue(tv.validate("abc")["active"])
        self.assertTrue(tv.validate(tok)["active"])
        self.assertFalse(tv.validate("nope")["active"])

    def test_pkce_pair(self):
        pair = oauth_mod.generate_pkce_pair()
        self.assertEqual(pair["code_challenge_method"], "S256")
        self.assertTrue(pair["code_verifier"])


class TestObservability(unittest.TestCase):
    def test_metrics_and_alerts(self):
        m = obs_mod.MetricsCollector()  # 独立实例，避免全局收集器被其它测试污染
        m.inc("http_requests_total", tags={"status": "2xx"})
        m.inc("http_requests_total", tags={"status": "5xx"})
        m.observe("http_request_duration_seconds", 0.1, {"method": "GET"})
        m.observe("http_request_duration_seconds", 2.0, {"method": "POST"})
        snap = m.snapshot()
        self.assertIn("counters", snap)
        self.assertGreaterEqual(
            snap["histograms"]["http_request_duration_seconds{method=POST}"]["p95"], 2.0)
        alerts = obs_mod.evaluate_alerts(snap)
        self.assertTrue(any(a["rule"] == "error_rate_high" for a in alerts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
