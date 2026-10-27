"""tests/test_model_providers.py —— 供应商目录的单元测试。

纪律：
  - 端点真实性：目录里每条 base_url 必须是 https（本地 Ollama 除外），
    且**不得出现**占位符（example.com / your-key / …），杜绝编造。
  - 包月/按量必须分开：同厂商两种计费方案在目录里是两条不同条目，
    且 base_url 不得相同（相同就说明没认真核实）。
  - 分组完整：每个条目必须落在已声明的分组里，且所有分组都被用到。
"""

import unittest

import model_providers as mp


class TestCatalogIntegrity(unittest.TestCase):
    def test_every_entry_has_required_fields(self):
        for p in mp.PROVIDERS:
            self.assertTrue(p.id, "条目缺 id")
            self.assertTrue(p.name, "%s 缺 name" % p.id)
            self.assertTrue(p.base_url, "%s 缺 base_url" % p.id)
            self.assertIn(p.protocol, ("openai", "anthropic", "ollama"),
                          "%s 协议非法" % p.id)
            self.assertIn(p.billing, (mp.BILLING_SUBSCRIPTION, mp.BILLING_PAYG,
                                      mp.BILLING_LOCAL), "%s 计费属性非法" % p.id)
            self.assertTrue(p.models, "%s 无模型候选" % p.id)
            self.assertTrue(p.note, "%s 无说明" % p.id)

    def test_base_urls_are_real_not_placeholders(self):
        banned = ("example.com", "your-key", "yourdomain", "<", ">", "{", "}")
        for p in mp.PROVIDERS:
            if p.protocol == "ollama":
                self.assertTrue(p.base_url.startswith("http://127.0.0.1"),
                                "%s 本地端点异常" % p.id)
                continue
            self.assertTrue(p.base_url.startswith("https://"),
                            "%s 云端端点必须是 https" % p.id)
            for b in banned:
                self.assertNotIn(b, p.base_url,
                                 "%s 的 base_url 含占位符 %s" % (p.id, b))

    def test_ids_unique(self):
        ids = [p.id for p in mp.PROVIDERS]
        self.assertEqual(len(ids), len(set(ids)), "目录 id 重复")

    def test_base_url_unique_per_billing(self):
        """包月与按量是不同端点：同厂商两条不得共用 base_url。"""
        by_url = {}
        for p in mp.PROVIDERS:
            by_url.setdefault(p.base_url, []).append(p)
        for url, items in by_url.items():
            if len(items) > 1:
                self.fail("同一端点出现多条目录项：%s → %s" % (
                    url, [i.id for i in items]))

    def test_groups_are_declared_and_used(self):
        for p in mp.PROVIDERS:
            self.assertIn(p.group, mp.GROUP_ORDER,
                          "%s 的分组未声明：%s" % (p.id, p.group))
        used = set(p.group for p in mp.PROVIDERS)
        for g in mp.GROUP_ORDER:
            self.assertIn(g, used, "分组 %s 没有任何条目" % g)

    def test_subscription_and_payg_both_present(self):
        billings = set(p.billing for p in mp.PROVIDERS)
        self.assertIn(mp.BILLING_SUBSCRIPTION, billings)
        self.assertIn(mp.BILLING_PAYG, billings)
        self.assertIn(mp.BILLING_LOCAL, billings)

    def test_tencent_token_plan_has_compliance_notice(self):
        """腾讯云 Token Plan 条款禁止自动化脚本/应用后端 —— 本产品正属此类，必须提示。"""
        hits = [p for p in mp.PROVIDERS if "token" in p.id and "tencent" in p.id]
        self.assertTrue(hits, "目录缺少腾讯云 Token Plan 条目")
        for p in hits:
            self.assertTrue(p.compliance, "%s 缺合规提示" % p.id)
            self.assertTrue("自动化" in p.compliance,
                            "%s 合规提示未覆盖自动化调用限制" % p.id)

    def test_ollama_requires_no_key(self):
        o = mp.get("ollama-local")
        self.assertIsNotNone(o)
        self.assertEqual(o.protocol, "ollama")
        self.assertEqual(o.billing, mp.BILLING_LOCAL)


class TestCatalogApi(unittest.TestCase):
    def test_list_providers_sorted_by_group(self):
        items = mp.list_providers()
        order = {g: i for i, g in enumerate(mp.GROUP_ORDER)}
        idx = [order[i["group"]] for i in items]
        self.assertEqual(idx, sorted(idx), "条目未按分组顺序排列")

    def test_catalog_shape(self):
        c = mp.catalog()
        self.assertEqual(c["groups"], mp.GROUP_ORDER)
        self.assertTrue(c["notice"], "缺少全局提示")
        self.assertEqual(len(c["providers"]), len(mp.PROVIDERS))
        for i in c["providers"]:
            self.assertIn("label", i)
            self.assertNotIn("api_key", i)

    def test_get_and_base_url_for(self):
        p = mp.get("deepseek")
        self.assertIsNotNone(p)
        self.assertEqual(mp.base_url_for("deepseek"), p.base_url)
        self.assertIsNone(mp.get("no-such-provider"))
        self.assertEqual(mp.base_url_for("no-such-provider"), "")

    def test_by_protocol(self):
        for e in mp.by_protocol("anthropic"):
            self.assertEqual(e.protocol, "anthropic")
        self.assertTrue(mp.by_protocol("ollama"))

    def test_validate_entry_accepts_catalog_item(self):
        p = mp.get("deepseek")
        out = mp.validate_entry({
            "provider": p.protocol, "provider_id": p.id, "base_url": p.base_url})
        self.assertEqual(out["base_url"], p.base_url)

    def test_validate_entry_rejects_bad_protocol(self):
        with self.assertRaises(ValueError):
            mp.validate_entry({"provider": "bogus", "base_url": "https://x.example.com"})

    def test_validate_entry_rejects_protocol_mismatch(self):
        with self.assertRaises(ValueError):
            mp.validate_entry({"provider": "anthropic", "provider_id": "deepseek",
                               "base_url": "https://api.deepseek.com/v1"})

    def test_validate_entry_rejects_bad_url(self):
        with self.assertRaises(ValueError):
            mp.validate_entry({"provider": "openai", "base_url": "ftp://x"})
        with self.assertRaises(ValueError):
            mp.validate_entry({"provider": "openai", "base_url": ""})


if __name__ == "__main__":
    unittest.main()
