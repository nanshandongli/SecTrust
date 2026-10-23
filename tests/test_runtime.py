"""
运行时回归测试（离线可跑，零依赖）：
1. 本地技能加载 + 执行（echo）
2. 模型路由器：缺 key 时自动降级 Mock；mock 模型正常应答
3. AgentRunner 端到端（mock 渠道 + mock 模型）：消息 -> 登记 -> 模型 -> 状态 done
4. AgentRunner 技能登记：/echo 文本 -> payload 标记 skill=echo
"""

import os
import sys
import tempfile
import unittest

# 让测试能从项目根目录 import 各包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import RunnerConfig, ChannelConfig, ModelConfig, SkillConfig  # noqa: E402
from models import ModelRouter, ChatMessage  # noqa: E402
from models.mock import MockModelProvider  # noqa: E402
from models.openai_like import OpenAIModelProvider  # noqa: E402
from skills.local import LocalSkillProvider  # noqa: E402
from runner import AgentRunner  # noqa: E402
from channels import InboundMessage  # noqa: E402


def _make_cfg(db_path):
    return RunnerConfig(
        db_path=db_path,
        channel=ChannelConfig(name="mock", mock_interval=0.1),
        model=ModelConfig(default_model="mock"),
        skill=SkillConfig(local_dir=os.path.join(PROJECT_ROOT, "skills", "builtin")),
    )


class TestLocalSkill(unittest.TestCase):
    def test_echo_skill(self):
        prov = LocalSkillProvider(os.path.join(PROJECT_ROOT, "skills", "builtin"))
        prov.sync()
        skill = prov.get("echo")
        self.assertIsNotNone(skill, "应加载到内置 echo 技能")
        res = skill.entry({"text": "你好"}, ctx=None)
        self.assertTrue(res.ok)
        self.assertIn("[echo] 你好", res.content)


class TestModelRouter(unittest.TestCase):
    def test_fallback_when_no_key(self):
        # 没配 key 的 OpenAI provider 应被视为未配置，路由回 Mock
        router = ModelRouter(
            providers=[OpenAIModelProvider(api_key="", base_url="x")],
            default_model="gpt-4o-mini",
        )
        self.assertIsInstance(router.route("gpt-4o-mini"), MockModelProvider)

    def test_mock_complete(self):
        router = ModelRouter(providers=[], default_model="mock")
        r = router.complete([ChatMessage(role="user", content="hi")])
        self.assertIn("hi", r.content)


class TestRunnerPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "reg.db")
        self.cfg = _make_cfg(self.db)
        self.runner = AgentRunner(self.cfg)

    def tearDown(self):
        self.runner.reg.close()
        for f in (self.db, self.db + "-wal", self.db + "-shm"):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_pipeline_registers_done_task(self):
        self.runner._on_message(
            InboundMessage(channel="mock", message_id="m1", user_id="u1", text="你好")
        )
        tasks = self.runner.reg.list_tasks()
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["payload"]["channel"], "mock")
        self.assertEqual(t["payload"]["skill"], None)

    def test_skill_registration(self):
        self.runner._on_message(
            InboundMessage(channel="mock", message_id="m2", user_id="u2", text="/echo 测试")
        )
        tasks = self.runner.reg.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["payload"]["skill"], "echo")
        self.assertEqual(tasks[0]["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
