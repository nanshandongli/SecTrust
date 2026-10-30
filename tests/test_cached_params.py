"""状态进化②：会话记忆 cached_params 参数缓存（避免 AI 重复询问）。"""
import os
import shutil
import tempfile
import unittest

from agentskills.router import SkillRouter
from agentskills.skill_md import StandardSkillLibrary
from agentskills.state import SkillStateStore


def _write_skill(root, name, description, extra=""):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    text = ("---\nname: %s\ndescription: %s\nversion: 1.0.0\nlicense: MIT\n"
            "allowed-tools: [web_search, file_read]\nrisk_level: low\n"
            "cost_tier: low\n%s---\n\n# %s\n\nBODY_MARKER\n"
            % (name, description, extra, name))
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return d


def _fixture_library(tmp):
    root = os.path.join(tmp, "lib")
    _write_skill(root, "disk-cleanup",
                 "磁盘释放工具：清理电脑垃圾文件、释放磁盘空间、系统优化瘦身。")
    _write_skill(root, "competitor-report",
                 "竞品分析报告：梳理竞争对手功能对比、输出竞品矩阵。")
    return StandardSkillLibrary(roots=[root])


class CachedParamsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-cache-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_redacts_sensitive_values(self):
        store = SkillStateStore()
        store.cache_params("s1", {"project_path": "/x", "api_key": "sk-123",
                                  "token": "abc"})
        got = store.get_cached_params("s1")
        self.assertEqual(got["project_path"], "/x")
        # 敏感键只记名不记值
        self.assertEqual(got["api_key"], "<redacted>")
        self.assertEqual(got["token"], "<redacted>")

    def test_get_missing_returns_empty(self):
        store = SkillStateStore()
        self.assertEqual(store.get_cached_params("nope"), {})

    def test_cache_none_is_noop(self):
        store = SkillStateStore()
        store.cache_params("s1", None)
        self.assertEqual(store.get_cached_params("s1"), {})

    def test_router_surfaces_cached_params_and_feedback(self):
        lib = _fixture_library(self.tmp)
        store = SkillStateStore()
        # 先缓存 disk-cleanup 的「已获取环境变量」
        store.cache_params("disk-cleanup",
                          {"project_path": "/data", "db_token": "secret"})
        router = SkillRouter(library=lib, state=store)
        out = router.select("清理电脑释放磁盘", k=3)
        self.assertTrue(out["ok"])
        # 选中的恰好是 disk-cleanup 时应带出缓存
        if out["chosen"] == "disk-cleanup":
            self.assertIn("project_path", out["cached_params"])
            self.assertEqual(out["cached_params"]["db_token"], "<redacted>")
            self.assertIn("已缓存环境变量", out["feedback"])
            self.assertIn("无需重复", out["feedback"])

    def test_router_no_cached_params_when_empty(self):
        lib = _fixture_library(self.tmp)
        store = SkillStateStore()
        router = SkillRouter(library=lib, state=store)
        out = router.select("清理电脑释放磁盘", k=3)
        self.assertEqual(out["cached_params"], {})


class ErrorSignatureTest(unittest.TestCase):
    def test_signature_threshold_emits_suggestion(self):
        store = SkillStateStore()
        # 前两次不应返回建议
        self.assertEqual(store.record_error_signature("s1", "boom"), {})
        self.assertEqual(store.record_error_signature("s1", "boom"), {})
        # 第三次达阈值 → 返回优化建议
        sug = store.record_error_signature("s1", "boom")
        self.assertEqual(sug["skill"], "s1")
        self.assertEqual(sug["technical_detail"], "boom")
        self.assertEqual(sug["count"], 3)
        # 第四次不再重复返回（只触发一次）
        self.assertEqual(store.record_error_signature("s1", "boom"), {})

    def test_different_detail_no_cross_count(self):
        store = SkillStateStore()
        store.record_error_signature("s1", "a")
        store.record_error_signature("s1", "b")
        self.assertEqual(store.record_error_signature("s1", "a"), {})


if __name__ == "__main__":
    unittest.main()
