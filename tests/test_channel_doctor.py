"""渠道联调工具测试（tools/channel_doctor.py）。

验证联调自检工具本身的正确性——它是"帮用户排除是代码错还是凭据错"的工具，
自身出错会误导排查方向，所以每一步都要有测试兜底。
"""

import base64
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from config import ChannelConfig  # noqa: E402
from tools.channel_doctor import (  # noqa: E402
    _wecom_echo_plain, check_approval_push_loop, check_credentials,
    loopback_feishu, loopback_wecom, run_doctor)


def _aes_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()[:43]


class TestCredentialCheck(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_missing_credentials_reported(self):
        for k in ("WECHAT_TOKEN", "WECHAT_AES_KEY", "WECHAT_CORP_ID",
                  "WECHAT_AGENT_ID", "WECHAT_SECRET"):
            os.environ.pop(k, None)
        res = check_credentials("wecom")
        self.assertFalse(res["ready"])
        self.assertIn("WECHAT_SECRET", res["missing_required"])

    def test_wrong_aes_key_length_detected(self):
        os.environ["WECHAT_AES_KEY"] = "tooshort"
        os.environ["WECHAT_TOKEN"] = "tok"
        os.environ["WECHAT_CORP_ID"] = "ww1234567890"
        os.environ["WECHAT_AGENT_ID"] = "1000002"
        os.environ["WECHAT_SECRET"] = "x" * 30
        res = check_credentials("wecom")
        item = {i["name"]: i for i in res["items"]}["WECHAT_AES_KEY"]
        self.assertFalse(item["ok"])
        self.assertIn("43", item["detail"])

    def test_never_prints_secret_values(self):
        """自检输出绝不能包含凭据明文——否则会进日志/截图泄露。"""
        secret = "SUPERSECRETVALUE123456"
        os.environ["WECHAT_SECRET"] = secret
        os.environ["WECHAT_TOKEN"] = "tok"
        os.environ["WECHAT_AES_KEY"] = _aes_key()
        os.environ["WECHAT_CORP_ID"] = "ww1234567890"
        os.environ["WECHAT_AGENT_ID"] = "1000002"
        res = check_credentials("wecom")
        blob = json.dumps(res, ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertNotIn(os.environ["WECHAT_AES_KEY"], blob)

    def test_optional_feishu_encrypt_key_not_required(self):
        """Encrypt Key / Verification Token 是可选项；未设置不应判为不就绪。

        但 APP_ID / APP_SECRET 是必填——必填项齐全才算 ready。
        """
        os.environ["FEISHU_APP_ID"] = "cli_test123456789"
        os.environ["FEISHU_APP_SECRET"] = "cli_test_secret_abcdefg"
        os.environ.pop("FEISHU_VERIFICATION_TOKEN", None)
        os.environ.pop("FEISHU_ENCRYPT_KEY", None)
        res = check_credentials("feishu")
        self.assertTrue(res["ready"], "可选项未设置不应判为不就绪")
        opt = {i["name"]: i for i in res["items"]}
        self.assertTrue(opt["FEISHU_ENCRYPT_KEY"]["ok"])
        self.assertFalse(opt["FEISHU_ENCRYPT_KEY"]["required"])


class TestLoopback(unittest.TestCase):
    """本地回环：按官方协议造请求打自己的回调端点。"""

    def test_wecom_loopback_all_steps_pass(self):
        cfg = ChannelConfig()
        cfg.wecom_token = "testtoken123"
        cfg.wecom_aes_key = _aes_key()
        cfg.wecom_corp_id = "ww1234567890abcdef"
        res = loopback_wecom(cfg)
        self.assertTrue(res["ok"], f"企微回环未通过: {res['steps']}")
        names = [s["step"] for s in res["steps"]]
        self.assertIn("URL 验证(GET)", names)
        self.assertIn("加密消息(POST)", names)

    def test_feishu_loopback_all_steps_pass(self):
        cfg = ChannelConfig()
        cfg.feishu_app_secret = "cli_test_secret_abcdefg"
        res = loopback_feishu(cfg)
        self.assertTrue(res["ok"], f"飞书回环未通过: {res['steps']}")

    def test_wecom_rejects_bad_signature(self):
        """验签必须真的拒绝伪造请求——这是渠道安全的第一道闸。"""
        cfg = ChannelConfig()
        cfg.wecom_token = "testtoken123"
        cfg.wecom_aes_key = _aes_key()
        cfg.wecom_corp_id = "ww1234567890abcdef"
        res = loopback_wecom(cfg)
        bad = [s for s in res["steps"] if "错误签名" in s["step"]]
        self.assertTrue(bad, "缺少错误签名用例")
        self.assertTrue(bad[0]["ok"], "错误签名未被正确拒绝")

    def test_feishu_rejects_bad_signature(self):
        cfg = ChannelConfig()
        cfg.feishu_app_secret = "cli_test_secret_abcdefg"
        res = loopback_feishu(cfg)
        bad = [s for s in res["steps"] if "错误签名" in s["step"]]
        self.assertTrue(bad and bad[0]["ok"], "飞书错误签名未被正确拒绝")

    def test_wecom_echo_plain_is_decryptable(self):
        """构造的密文必须能被自家解密还原（IV=key[:16]，非随机 IV）。"""
        from security.crypto import wecom_decrypt
        key = _aes_key()
        corp = "ww1234567890abcdef"
        enc = _wecom_echo_plain("tok", corp, key, "u1", "hello")
        plain = wecom_decrypt(key, enc, receive_id=corp)
        self.assertIn("hello", plain)

    def test_wecom_echo_plain_wrong_receive_id_rejected(self):
        from security.crypto import wecom_decrypt
        key = _aes_key()
        enc = _wecom_echo_plain("tok", "ww_real_corp", key, "u1", "hello")
        with self.assertRaises(ValueError):
            wecom_decrypt(key, enc, receive_id="ww_other_corp")


class TestApprovalLoop(unittest.TestCase):
    def test_approval_request_decide_cycle(self):
        db = os.path.join(tempfile.mkdtemp(), "ap.db")
        res = check_approval_push_loop(db, "wecom")
        self.assertTrue(res["ok"], res["detail"])
        self.assertIn("pending", res["detail"])
        self.assertIn("approved", res["detail"])

    def test_approval_reject_path(self):
        from approval import ApprovalCenter
        db = os.path.join(tempfile.mkdtemp(), "ap2.db")
        ac = ApprovalCenter(db_path=db)
        aid = ac.request("scan", "detail", channel="feishu")
        ac.decide(aid, "reject", by="t")
        self.assertEqual(ac.status(aid), "rejected")
        ac.close()


class TestRunDoctor(unittest.TestCase):
    def test_doctor_reports_not_ready_without_credentials(self):
        saved = dict(os.environ)
        try:
            for k in ("WECHAT_TOKEN", "WECHAT_AES_KEY", "WECHAT_CORP_ID",
                      "WECHAT_AGENT_ID", "WECHAT_SECRET"):
                os.environ.pop(k, None)
            db = os.path.join(tempfile.mkdtemp(), "d.db")
            res = run_doctor("wecom", db_path=db)
            self.assertFalse(res["ready"])
            self.assertIn("credentials", res)
            self.assertIn("loopback", res)
            self.assertIn("approval_loop", res)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_doctor_json_serializable(self):
        saved = dict(os.environ)
        try:
            for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET",
                      "FEISHU_VERIFICATION_TOKEN", "FEISHU_ENCRYPT_KEY"):
                os.environ.pop(k, None)
            db = os.path.join(tempfile.mkdtemp(), "d2.db")
            res = run_doctor("feishu", db_path=db)
            json.dumps(res)  # 不抛异常即可
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
