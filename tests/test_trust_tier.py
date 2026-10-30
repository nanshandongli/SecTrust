"""边界平衡④：SKILL.md 新增 trust_tier 字段，映射到安全分级（仅作意图，不绕过安全）。"""
import os
import shutil
import tempfile
import unittest

from agentskills.skill_md import StandardSkillLibrary, _validate_meta, parse_skill_md
from agentskills.tier import classify


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


class TrustTierParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-trust-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_low_maps_to_white(self):
        _write_skill(self.tmp, "s1", "纯提示词技能。", "trust_tier: low\n")
        spec = parse_skill_md(os.path.join(self.tmp, "s1", "SKILL.md"))
        self.assertEqual(spec.trust_tier, "low")
        # security_tier 未显式声明 → 由 trust_tier 映射为 white
        self.assertEqual(spec.security_tier, "white")
        dec = classify(spec)
        self.assertEqual(dec.tier, "white")
        self.assertEqual(dec.trust_tier, "low")

    def test_medium_maps_to_gray(self):
        _write_skill(self.tmp, "s2", "只读脚本技能。", "trust_tier: medium\n")
        spec = parse_skill_md(os.path.join(self.tmp, "s2", "SKILL.md"))
        self.assertEqual(spec.security_tier, "gray")
        dec = classify(spec)
        self.assertEqual(dec.tier, "gray")  # 显式 gray 收紧，覆盖 auto white
        self.assertEqual(dec.trust_tier, "medium")

    def test_high_maps_to_black(self):
        _write_skill(self.tmp, "s3", "写操作技能。", "trust_tier: high\n")
        spec = parse_skill_md(os.path.join(self.tmp, "s3", "SKILL.md"))
        self.assertEqual(spec.security_tier, "black")
        dec = classify(spec)
        self.assertEqual(dec.tier, "black")
        self.assertEqual(dec.trust_tier, "high")

    def test_explicit_security_tier_wins_over_trust(self):
        _write_skill(self.tmp, "s4", "纯提示词技能。",
                     "trust_tier: high\nsecurity_tier: white\n")
        spec = parse_skill_md(os.path.join(self.tmp, "s4", "SKILL.md"))
        self.assertEqual(spec.security_tier, "white")  # 显式声明不被 trust 覆盖
        self.assertEqual(spec.trust_tier, "high")

    def test_invalid_trust_tier_is_problem(self):
        _write_skill(self.tmp, "s5", "技能。", "trust_tier: ultra\n")
        problems = _validate_meta(
            {"name": "s5", "description": "技能。", "trust_tier": "ultra"})
        self.assertTrue(any("trust_tier" in p for p in problems))

    def test_l1_metadata_carries_trust_tier(self):
        _write_skill(self.tmp, "s6", "纯提示词技能。", "trust_tier: low\n")
        lib = StandardSkillLibrary(roots=[self.tmp])
        # 无 manifest 的技能 installed_ok=False，需 include_broken 才入 L1 元数据
        # （router 实际调用即传 include_broken=True）
        meta = lib.load_l1_metadata(include_broken=True)
        m = next(x for x in meta if x["name"] == "s6")
        self.assertEqual(m["trust_tier"], "low")


if __name__ == "__main__":
    unittest.main()
