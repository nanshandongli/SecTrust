"""task_center Web 测试：/api/dag 依赖视图 + 页面渲染（零依赖，临时端口真起服务）。"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from task_center_web import Handler, dag_view  # noqa: E402
from orchestration import DagScheduler  # noqa: E402


class TaskCenterWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "tc.db")
        self.s = DagScheduler(self.db)
        # 菱形：a -> {b, c} -> d
        self.s.add_task("a", payload={"name": "拉取数据"})
        self.s.add_task("b", payload={"name": "文案初稿"}, depends_on=["a"])
        self.s.add_task("c", payload={"name": "分镜设计"}, depends_on=["a"])
        self.s.add_task("d", payload={"name": "合成"}, depends_on=["b", "c"])
        from task_registry import TaskRegistry
        self.registry = TaskRegistry(db_path=self.db, runner_id="web-test")
        Handler.registry = self.registry
        from approval import ApprovalCenter
        self.approval = ApprovalCenter(db_path=self.db)
        Handler.approval = self.approval
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.registry.close()
        self.approval.close()
        self.s.close()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}",
                                    timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_api_dag_waves_and_ready(self):
        dag = self._get("/api/dag")
        self.assertEqual(dag["waves"], [["a"], ["b", "c"], ["d"]])
        self.assertEqual(dag["ready"], ["a"])
        self.assertEqual(dag["graph"]["d"], ["b", "c"])
        self.assertEqual(dag["stats"]["total"], 4)
        t = dag["tasks"]["d"]
        self.assertEqual(sorted(t["waiting_on"]), ["b", "c"])
        self.assertEqual(t["name"], "合成")
        self.assertEqual(t["dead_upstream"], [])

    def test_api_dag_dead_upstream_propagation(self):
        self.s.claim_ready(runner_id="w")
        self.s.fail("a", error="模型超时")  # b/c/d 全部级联 blocked
        dag = self._get("/api/dag")
        self.assertEqual(dag["ready"], [])
        # 直接上游已死：b/c 的死上游是 a；d 的死上游是 b/c（阻塞逐层传播）
        self.assertEqual(dag["tasks"]["b"]["dead_upstream"], ["a"])
        self.assertEqual(dag["tasks"]["c"]["dead_upstream"], ["a"])
        self.assertEqual(sorted(dag["tasks"]["d"]["dead_upstream"]), ["b", "c"])
        for tid in ("b", "c", "d"):
            self.assertEqual(dag["tasks"][tid]["status"], "blocked", tid)

    def test_index_page_contains_dag_section(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/",
                                    timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("依赖视图 (DAG)", html)
        self.assertIn("/api/dag", html)
        self.assertIn("dagwaves", html)

    def test_dag_view_function_directly(self):
        v = dag_view(self.registry)
        self.assertEqual(v["waves"], [["a"], ["b", "c"], ["d"]])
        self.assertIn("a", v["ready"])


if __name__ == "__main__":
    unittest.main()
