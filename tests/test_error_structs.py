"""反馈学习③：统一结构化错误 ErrorInfo + 自我反思提示。"""
import unittest

from agentskills.error_structs import ErrorInfo, self_correction_prompt


class ErrorInfoTest(unittest.TestCase):
    def test_guard_stage_mapping(self):
        i = ErrorInfo.build("approval", "审批单 x 挂起", "", kind="guard")
        self.assertEqual(i.error_code, "APPROVAL_PENDING")
        self.assertIn("审批", i.human_readable_message)
        self.assertIn("审批单 x 挂起", i.human_readable_message)

    def test_guard_stage_evidence(self):
        i = ErrorInfo.build("evidence", "缺少实跑产出", "evidence.missing_or_stale",
                            kind="guard")
        self.assertEqual(i.error_code, "EVIDENCE_MISSING")
        self.assertEqual(i.rule_id, "evidence.missing_or_stale")

    def test_proxy_stage_mapping(self):
        i = ErrorInfo.build("secret_scan_in", "命中 2 条密钥规则", "", kind="proxy")
        self.assertEqual(i.error_code, "SECRET_DETECTED")
        self.assertEqual(i.stage, "secret_scan_in")

    def test_unknown_stage_falls_back(self):
        i = ErrorInfo.build("weird_stage", "reason text", "", kind="guard")
        self.assertEqual(i.error_code, "UNKNOWN")
        self.assertIn("reason text", i.human_readable_message)

    def test_to_dict_shape(self):
        d = ErrorInfo.build("quality", "含 TODO 占位符", "", kind="guard").to_dict()
        self.assertIn("error_code", d)
        self.assertIn("human_readable_message", d)
        self.assertIn("technical_detail", d)
        self.assertIn("stage", d)
        self.assertIn("rule_id", d)

    def test_self_correction_prompt_on_failure(self):
        info = ErrorInfo.build("approval", "审批单 x 挂起", "", kind="guard").to_dict()
        prompt = self_correction_prompt(info)
        self.assertIn("APPROVAL_PENDING", prompt)
        self.assertIn("下一步", prompt)

    def test_self_correction_prompt_empty_for_none(self):
        self.assertEqual(self_correction_prompt(None), "")
        self.assertEqual(self_correction_prompt({}), "")

    def test_explicit_error(self):
        i = ErrorInfo.explicit("MISSING_PARAM", "我需要收件人邮箱",
                               technical="field recipient empty", stage="sandbox")
        self.assertEqual(i.error_code, "MISSING_PARAM")
        self.assertEqual(i.human_readable_message, "我需要收件人邮箱")
        self.assertEqual(i.technical_detail, "field recipient empty")


if __name__ == "__main__":
    unittest.main()
