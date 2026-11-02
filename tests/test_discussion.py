"""同事圆桌（discussion.py）单元测试。

用 FakeLlm / FakeProfiles 替代真实算力，覆盖：存储纪律、建室、发言轮、
诚实缺席（无算力不替身）、综合共识抽取、落任务、参与者解析去重。
"""

import os
import tempfile
import unittest

from discussion import DiscussionService, DiscussionStore, DiscussionError


class FakeProfile:
    def __init__(self, pid, configured=True):
        self.id = pid
        self.configured = configured
        self.name = pid
        self.provider = "openai"
        self.model = "fake"


class FakeProfiles:
    def __init__(self, active_configured=True):
        self._active = FakeProfile("active", active_configured)
        self._by = {
            "active": self._active,
            "p1": FakeProfile("p1", True),
            "p2": FakeProfile("p2", False),  # 已配但 configured=False → 不可用
        }

    def get(self, pid):
        return self._by.get(pid)

    def get_active(self):
        return self._active

    def get_active_id(self):
        return self._active.id


class FakeLlm:
    def __init__(self):
        self.calls = []

    def speak(self, system_prompt, user_prompt, model=None, timeout=90):
        self.calls.append({"model": model,
                           "is_moderator": "综合主持人" in system_prompt})
        if "综合主持人" in system_prompt:
            return {"answer": "【共识点】方向一致。\n【分歧】无。\n"
                              "【未决问题】无。\n"
                              "【建议落地方案】写一份关于该主题的分析报告。",
                    "model": "fake/%s" % (model or "active")}
        return {"answer": "（模拟发言）%s" % user_prompt[-24:],
                "model": "fake/%s" % (model or "active")}


class FakePlan:
    def __init__(self):
        self.calls = []

    def build(self, requirement, operator="operator"):
        self.calls.append((requirement, operator))
        return {"ok": True, "plan_id": "plan-test",
                "created": ["plan-test/x"], "requirement": requirement}


class DiscussionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="discussion_test_")
        self.db = os.path.join(self.tmp, "discussion.db")
        self.profiles = FakeProfiles(active_configured=True)
        self.llm = FakeLlm()
        self.plan = FakePlan()
        self.svc = DiscussionService(self.profiles, self.llm,
                                    plan_service=self.plan,
                                    store=DiscussionStore(db_path=self.db))

    def tearDown(self):
        try:
            self.svc._store.close()
        except Exception:
            pass


class TestStoreSchema(DiscussionBase):
    def test_rooms_turns_syntheses_tables_exist(self):
        store = self.svc._store
        rid = store.create_room("t", "目标", [
            {"colleague_id": "role:writer", "name": "内容撰写",
             "role_label": "内容撰写", "role_brief": "x", "profile_id": "",
             "is_expert": False}])["room_id"]
        room = store.get_room(rid)
        self.assertEqual(room["status"], "open")
        self.assertEqual(len(room["participants"]), 1)
        # 发言 / 综合落库
        store.add_turn(rid, 1, room["participants"][0], "hi", True, model="m")
        self.assertEqual(len(store.turns(rid)), 1)
        synth = store.save_synthesis(rid, room["participants"][0], "纪要", "建议", True)
        self.assertEqual(store.latest_synthesis(rid)["version"], 1)
        self.assertEqual(synth["recommended_approach"], "建议")
        # 列表可见
        self.assertEqual(len(store.list_rooms()), 1)


class TestCreateAndResolve(DiscussionBase):
    def test_create_room_default_trio(self):
        room = self.svc.create_room("", "把季度复盘做好",
                                    ["designer", "writer", "auditor"])
        self.assertEqual(len(room["participants"]), 3)
        self.assertEqual(room["status"], "open")

    def test_empty_objective_rejected(self):
        with self.assertRaises(DiscussionError):
            self.svc.create_room("", "", ["writer"])

    def test_resolve_dedup_and_unknown_ignored(self):
        parts = self.svc.resolve_participants(
            ["writer", "writer", "nope", "exp-auditor"])
        ids = [p["colleague_id"] for p in parts]
        self.assertEqual(ids, ["role:writer", "exp-auditor"])

    def test_catalog_lists_roles_and_experts(self):
        cat = self.svc.catalog()
        self.assertTrue(any(r["id"] == "writer" for r in cat["roles"]))
        self.assertTrue(any(e["id"] == "exp-auditor" for e in cat["experts"]))


class TestRunRound(DiscussionBase):
    def test_round_produces_one_turn_per_participant(self):
        room = self.svc.create_room("", "主题X",
                                    ["designer", "writer", "auditor"])
        res = self.svc.run_round(room["room_id"], rounds=1)
        self.assertEqual(len(res["turns"]), 3)
        self.assertTrue(all(t["available"] for t in res["turns"]))
        self.assertEqual(res["turns"][0]["round"], 1)
        # 第二轮应能看到第一轮内容（作为 prior 注入）
        res2 = self.svc.run_round(room["room_id"], rounds=1)
        self.assertEqual(res2["round"], 2)

    def test_honest_absence_when_no_llm(self):
        # active 不可用 → 所有未绑定同事如实缺席，绝不替身
        svc = DiscussionService(FakeProfiles(active_configured=False), self.llm,
                                store=self.svc._store)
        room = svc.create_room("", "主题Y", ["designer", "writer"])
        res = svc.run_round(room["room_id"], rounds=1)
        self.assertTrue(all(not t["available"] for t in res["turns"]))
        self.assertTrue(all("缺席" in t["content"] for t in res["turns"]))


class TestSynthesize(DiscussionBase):
    def test_synthesize_extracts_recommendation(self):
        room = self.svc.create_room("", "主题Z",
                                    ["designer", "writer", "auditor"])
        self.svc.run_round(room["room_id"], rounds=1)
        res = self.svc.synthesize(room["room_id"])
        self.assertTrue(res["available"])
        self.assertIn("分析报告", res["recommended_approach"])
        self.assertEqual(self.svc._store.get_room(room["room_id"])["status"],
                         "synthesized")

    def test_synthesize_requires_turns(self):
        room = self.svc.create_room("", "主题W", ["writer"])
        with self.assertRaises(DiscussionError):
            self.svc.synthesize(room["room_id"])


class TestToTask(DiscussionBase):
    def test_to_task_feeds_plan_service(self):
        room = self.svc.create_room("", "做一场产品发布",
                                    ["designer", "writer", "auditor"])
        self.svc.run_round(room["room_id"], rounds=1)
        self.svc.synthesize(room["room_id"])
        view = self.svc.to_task(room["room_id"], operator="alice")
        self.assertEqual(view["plan_id"], "plan-test")
        # requirement 应包含原始目标
        self.assertIn("产品发布", self.plan.calls[0][0])
        # 讨论室记回 plan 链接
        self.assertEqual(self.svc._store.get_room(room["room_id"])["task_link"],
                         "plan-test")

    def test_to_task_requires_synthesis(self):
        room = self.svc.create_room("", "主题V", ["writer"])
        self.svc.run_round(room["room_id"], rounds=1)
        with self.assertRaises(DiscussionError):
            self.svc.to_task(room["room_id"])


if __name__ == "__main__":
    unittest.main()
