"""审批链强化测试（approval.py × trace.py，2026-10-25 #91）。

锁定的承诺：
1. 审计证据挂 Trace：请求/决策/过期三种迁移各留一条 approval_* 事件，
   detail 只存脱敏结构、原文只留 blake2b 指纹（可验真、不囤敏感原文）；
2. 高危动作（HIGH_RISK_ACTIONS：外发/发布/购买/支付/删除/扫描）事件带 risk=high；
3. trace_id 解析优先级：显式传入 > 任务 payload 内嵌 > 无上下文则不记
   （不硬造孤儿轨迹，audit 如实标注"无轨迹上下文"）；
4. 旁路纪律：轨迹写入失败（坏 tracer）绝不挡审批主链路；
5. 过期清理 sweep_expired：没人 wait 的审批不会永远 pending；
6. 一次性语义：重复 decide 只有一条 decided 事件；
7. tracer=False 完全关闭轨迹（纯审批表模式）；
8. CLI：--audit / --sweep 可用（main(argv) 可测）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approval import ApprovalCenter, HIGH_RISK_ACTIONS, main          # noqa: E402
from task_registry import TaskRegistry                                # noqa: E402
from trace import TraceStore                                          # noqa: E402


def _fp(text: str) -> str:
    """独立重算 trace 的文本指纹（不借被测代码的私有函数，避免自己验自己）。"""
    import hashlib
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


class _TmpDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sectrust-approval-test-")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_task(self, task_id, trace_id="tr-audit-1"):
        reg = TaskRegistry(db_path=self.db)
        try:
            reg.register_task(task_id, payload={"op": "noop", "trace_id": trace_id})
        finally:
            reg.close()

    def _backdate(self, approval_id, seconds):
        """把审批的 created_at 改到过去（模拟挂起超时，不真等）。"""
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE approvals SET created_at=? WHERE approval_id=?",
                         (time.time() - seconds, approval_id))
            conn.commit()
        finally:
            conn.close()


class ApprovalTraceTest(_TmpDB):
    def _center(self, **kw):
        ac = ApprovalCenter(db_path=self.db, **kw)
        self.addCleanup(ac.close)
        return ac

    # ---- 1. 三迁移各留一条事件 ----
    def test_request_decide_expire_leave_full_chain(self):
        self._register_task("t1")
        ac = self._center()
        aid = ac.request("scan_repo", "https://github.com/foo/bar", task_id="t1")
        chain = ac.audit(aid)
        self.assertEqual(chain["trace_id"], "tr-audit-1")
        self.assertEqual([e["kind"] for e in chain["events"]],
                         ["approval_requested"])
        req = chain["events"][0]
        self.assertEqual(req["detail"]["action"], "scan_repo")
        self.assertEqual(req["detail"]["risk"], "high")       # 扫描=高危
        self.assertEqual(req["detail"]["approval_id"], aid)
        # 原文只留指纹：能验真，但不落敏感原文
        self.assertEqual(req["content_hash"], _fp("https://github.com/foo/bar"))
        self.assertNotIn("github.com/foo/bar", json.dumps(req, ensure_ascii=False))

        self.assertTrue(ac.decide(aid, "approve", by="alice"))
        chain = ac.audit(aid)
        kinds = [e["kind"] for e in chain["events"]]
        self.assertEqual(kinds, ["approval_requested", "approval_decided"])
        dec = chain["events"][1]
        self.assertEqual(dec["actor"], "alice")
        self.assertEqual(dec["detail"]["decision"] if "decision" in dec["detail"]
                         else dec["detail"]["status"], "approved")
        self.assertIn("latency_s", dec["detail"])             # 决策耗时可审计
        self.assertEqual(dec["content_hash"], _fp("https://github.com/foo/bar"))

    def test_reject_event_recorded(self):
        self._register_task("t1")
        ac = self._center()
        aid = ac.request("send", "给客户发周报", task_id="t1")
        self.assertTrue(ac.decide(aid, "reject", by="bob"))
        dec = ac.audit(aid)["events"][-1]
        self.assertEqual(dec["kind"], "approval_decided")
        self.assertEqual(dec["detail"]["status"], "rejected")
        self.assertEqual(dec["detail"]["risk"], "high")       # 外发=高危

    def test_timeout_expire_event_recorded(self):
        self._register_task("t1")
        ac = self._center()
        aid = ac.request("publish", "发布文章", task_id="t1")
        self.assertEqual(ac.wait_for_decision(aid, timeout=0.2, poll=0.05), "expired")
        kinds = [e["kind"] for e in ac.audit(aid)["events"]]
        self.assertEqual(kinds, ["approval_requested", "approval_expired"])

    # ---- 2. 风险分级 ----
    def test_risk_classification(self):
        self.assertIn("send", HIGH_RISK_ACTIONS)
        self.assertIn("scan_repo", HIGH_RISK_ACTIONS)
        self._register_task("t1")
        ac = self._center()
        aid_low = ac.request("read_file", "读个文件", task_id="t1")
        aid_high = ac.request("pay", "付款 100 元", task_id="t1")
        self.assertEqual(ac.audit(aid_low)["events"][0]["detail"]["risk"], "normal")
        self.assertEqual(ac.audit(aid_high)["events"][0]["detail"]["risk"], "high")

    # ---- 3. trace_id 解析 ----
    def test_explicit_trace_id_wins_without_task(self):
        ac = self._center()
        aid = ac.request("send", "x", task_id="")             # 无任务
        self.assertEqual(ac.audit(aid)["events"], [])          # 无上下文不记
        aid2 = ac.request("send", "x", task_id="", trace_id="tr-explicit")
        chain = ac.audit(aid2)
        self.assertEqual(chain["trace_id"], "tr-explicit")
        self.assertEqual(len(chain["events"]), 1)

    def test_no_trace_context_is_honest(self):
        ac = self._center()
        aid = ac.request("send", "给幽灵任务", task_id="ghost-task")
        chain = ac.audit(aid)
        self.assertIsNotNone(chain["approval"])               # 审批行仍在
        self.assertIsNone(chain["trace_id"])                  # 如实：没挂上 trace
        self.assertEqual(chain["events"], [])
        # 决策也不炸（无轨迹上下文只是不记）
        self.assertTrue(ac.decide(aid, "approve", by="human"))
        self.assertEqual(ac.audit(aid)["events"], [])

    # ---- 4. 旁路纪律 ----
    def test_broken_tracer_never_blocks_approval(self):
        class BrokenTracer:
            def record(self, *a, **kw):
                raise RuntimeError("trace store exploded")

            def events(self, *a, **kw):
                raise RuntimeError("trace store exploded")

        self._register_task("t1")
        ac = self._center(tracer=BrokenTracer())
        aid = ac.request("send", "x", task_id="t1")
        self.assertTrue(aid.startswith("ap-"))
        self.assertTrue(ac.decide(aid, "approve", by="human"))  # 主链路不受影响
        self.assertEqual(ac.status(aid), "approved")

    def test_tracer_false_disables_tracing(self):
        self._register_task("t1")
        ac = self._center(tracer=False)
        aid = ac.request("send", "x", task_id="t1")
        ac.decide(aid, "approve", by="human")
        chain = ac.audit(aid)
        self.assertEqual(chain["events"], [])
        self.assertIsNone(chain["trace_id"])

    # ---- 5. 过期清理 ----
    def test_sweep_expires_stale_pending_only(self):
        self._register_task("t1")
        ac = self._center()
        stale = ac.request("send", "没人看的审批", task_id="t1")
        self._backdate(stale, 3600)
        fresh = ac.request("send", "刚提的审批", task_id="t1")
        ids = ac.sweep_expired(max_age=600)
        self.assertEqual(ids, [stale])
        self.assertEqual(ac.status(stale), "expired")
        self.assertEqual(ac.status(fresh), "pending")          # 新的不动
        # 过期的也留了轨迹
        kinds = [e["kind"] for e in ac.audit(stale)["events"]]
        self.assertEqual(kinds, ["approval_requested", "approval_expired"])

    def test_sweep_is_idempotent(self):
        self._register_task("t1")
        ac = self._center()
        aid = ac.request("send", "x", task_id="t1")
        self._backdate(aid, 3600)
        self.assertEqual(ac.sweep_expired(max_age=600), [aid])
        self.assertEqual(ac.sweep_expired(max_age=600), [])    # 第二次扫空
        kinds = [e["kind"] for e in ac.audit(aid)["events"]]
        self.assertEqual(kinds.count("approval_expired"), 1)   # 也只记一条

    # ---- 6. 一次性语义 ----
    def test_double_decide_leaves_one_event(self):
        self._register_task("t1")
        ac = self._center()
        aid = ac.request("send", "x", task_id="t1")
        self.assertTrue(ac.decide(aid, "approve", by="alice"))
        self.assertFalse(ac.decide(aid, "reject", by="mallory"))  # 二次决策无效
        kinds = [e["kind"] for e in ac.audit(aid)["events"]]
        self.assertEqual(kinds.count("approval_decided"), 1)
        self.assertEqual(ac.audit(aid)["events"][-1]["actor"], "alice")  # 第一次的才算数

    # ---- 7. 与既有语义兼容 ----
    def test_existing_semantics_unchanged(self):
        ac = self._center()
        aid = ac.request("noop-ish", "x")
        self.assertTrue(ac.is_approved(aid) is False)
        with self.assertRaises(ValueError):
            ac.decide(aid, "maybe")
        self.assertTrue(ac.decide(aid, "approve"))
        self.assertTrue(ac.is_approved(aid))
        rows = ac.list_approvals(status="approved")
        self.assertEqual(len(rows), 1)

    # ---- 7b. 旧库迁移（无 trace_id 列的 approvals 表）----
    def test_old_db_without_trace_id_column_migrates(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "CREATE TABLE approvals ("
                " approval_id TEXT PRIMARY KEY, task_id TEXT, channel TEXT,"
                " user_id TEXT, action TEXT NOT NULL, detail TEXT,"
                " status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,"
                " decided_at REAL, decided_by TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        self._register_task("t1")
        ac = self._center()                       # 构造即迁移（ALTER 补列）
        aid = ac.request("send", "迁移后的审批", task_id="t1")
        row = ac._row(aid)
        self.assertEqual(row["trace_id"], "tr-audit-1")   # 新审批自描述
        ac.decide(aid, "approve", by="alice")
        kinds = [e["kind"] for e in ac.audit(aid)["events"]]
        self.assertEqual(kinds, ["approval_requested", "approval_decided"])

    def test_concurrent_migration_is_race_safe(self):
        """旧库 + 多线程同时首开：PRAGMA 与 ALTER 之间的窗口真实存在，
        "同时看到无列 → 同时 ALTER" 曾炸 duplicate column name（多测试文件
        合跑才偶发复现）。修法=吞掉重复列错误；这里用 barrier + 多轮把
        竞态稳定复现，锁死修法不回归。"""
        import sqlite3
        import threading

        errors = []
        rounds = 5

        for rnd in range(rounds):
            # 每轮重建"旧 schema"库（无 trace_id 列）
            db = os.path.join(self.tmp, "race-%d.db" % rnd)
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE approvals ("
                    " approval_id TEXT PRIMARY KEY, task_id TEXT, channel TEXT,"
                    " user_id TEXT, action TEXT NOT NULL, detail TEXT,"
                    " status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,"
                    " decided_at REAL, decided_by TEXT)"
                )
                conn.commit()
            finally:
                conn.close()

            barrier = threading.Barrier(8)

            def worker(db=db, barrier=barrier):
                try:
                    barrier.wait(timeout=10)
                    ac = ApprovalCenter(db_path=db)
                    ac.request("send", "x", task_id="")
                    ac.close()
                except Exception as e:  # noqa: BLE001 —— 收集全部线程的失败
                    errors.append("%s: %r" % (db, e))

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            # 列确实在，且只有一列
            conn = sqlite3.connect(db)
            try:
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(approvals)").fetchall()]
            finally:
                conn.close()
            self.assertEqual(cols.count("trace_id"), 1)

        self.assertEqual(errors, [])              # 8 线程 × 5 轮全部成功

    # ---- 8. CLI ----
    def test_cli_audit_and_sweep(self):
        self._register_task("t1")
        ac = ApprovalCenter(db_path=self.db)
        aid = ac.request("send", "周报", task_id="t1")
        ac.decide(aid, "approve", by="cli-tester")
        ac.close()

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--db-path", self.db, "--audit", aid])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("approval_requested", out)
        self.assertIn("approval_decided", out)
        self.assertIn("cli-tester", out)

        # sweep：新审批不该被扫（max-age 默认 1 天）
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = main(["--db-path", self.db, "--sweep"])
        self.assertEqual(rc2, 0)
        self.assertIn("0 条", buf2.getvalue())

    def test_cli_audit_unknown_id(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--db-path", self.db, "--audit", "ap-nope"])
        self.assertEqual(rc, 1)
        self.assertIn("未找到审批", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
