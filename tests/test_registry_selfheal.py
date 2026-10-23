"""连接自愈回归测试：Windows 多进程首开竞争导致的"半残连接"必须被检测并重建。

背景（实测复现过的真实缺陷）：
  Windows 上多个进程同时首次打开同一个新 SQLite 库时，个别进程会拿到一条
  "connect 成功、journal_mode 也返回 wal、但第一条写操作就报
  attempt to write a readonly database" 的半残连接，且该连接之后**永不恢复**。
  压测 8 进程时约 1/3 概率命中，表现为某些 worker 全程 0 成功。

修复：`_open_conn` 建连时做写探针（BEGIN IMMEDIATE + ROLLBACK），
  所有写操作走 `_retry_write`（失败→丢弃连接→重建→重试一次）。

这些测试用"假连接"精确注入故障，因此 100% 稳定复现，不靠运气。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from task_registry import TaskRegistry  # noqa: E402


_WRITE_PREFIXES = ("BEGIN", "INSERT", "UPDATE", "CREATE", "DELETE",
                   "ALTER", "REPLACE", "DROP", "COMMIT", "SAVEPOINT")


def _is_write_sql(sql: str) -> bool:
    head = sql.split("(", 1)[0].strip().upper()
    return head.startswith(_WRITE_PREFIXES)


class FakeBadConn:
    """模拟半残连接：connect 成功、PRAGMA 正常，但一写就报 readonly。

    fail_times：允许失败这么多次，耗尽后恢复正常
    （用于测试"失败一次后重试成功"与"一直失败"两种情形）。
    """

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.closed = False

    def execute(self, sql, *args):
        s = " ".join(str(sql).split())
        if self.fail_times > 0 and _is_write_sql(s):
            self.fail_times -= 1
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return _NullCursor()

    # Python sqlite3 连接支持 with 协议（异常时 rollback、正常时 commit）。
    # 假连接只需放行，不吞异常。
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        self.closed = True


class _NullCursor:
    def fetchone(self):
        return ("wal",)


def _fresh_db():
    return os.path.join(tempfile.mkdtemp(prefix="reg_selfheal_"), "t.db")


class OpenConnProbeTest(unittest.TestCase):
    """建连探针：挡住"打开时就是坏的"连接。"""

    def test_probe_rejects_bad_connection_and_retries(self):
        """第一次 connect 返回半残连接 -> 探针发现 -> 关掉重开 -> 拿到好连接。"""
        real_connect = sqlite3.connect
        state = {"n": 0, "bad_closed": []}

        def fake_connect(*a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                bad = FakeBadConn()
                state["bad_closed"].append(bad)
                return bad
            return real_connect(*a, **kw)

        sqlite3.connect = fake_connect
        try:
            reg = TaskRegistry(db_path=_fresh_db(), runner_id="probe-1")
            # 能正常构造并写入，说明重开后拿到的是好连接
            reg.register_task("t1", payload={"a": 1})
            got = reg.get_task("t1")
            reg.close()
        finally:
            sqlite3.connect = real_connect

        self.assertGreaterEqual(state["n"], 2, "应当至少连接两次（坏+好）")
        self.assertTrue(state["bad_closed"][0].closed,
                        "半残连接必须被关闭，不能 leak")
        self.assertIsNotNone(got)
        self.assertEqual(got["payload"], {"a": 1})

    def test_probe_raises_when_all_attempts_bad(self):
        """始终坏 -> 明确抛 RuntimeError，绝不静默返回半残连接。"""
        real_connect = sqlite3.connect

        def fake_connect(*a, **kw):
            return FakeBadConn(fail_times=10 ** 9)

        sqlite3.connect = fake_connect
        try:
            with self.assertRaises(RuntimeError) as ctx:
                TaskRegistry(db_path=_fresh_db(), runner_id="probe-2")
        finally:
            sqlite3.connect = real_connect
        self.assertIn("无法建立可写连接", str(ctx.exception))

    def test_probe_passes_for_healthy_connection(self):
        """正常库只连接一次（探针不增加正常路径开销）。"""
        real_connect = sqlite3.connect
        state = {"n": 0}

        def counting_connect(*a, **kw):
            state["n"] += 1
            return real_connect(*a, **kw)

        sqlite3.connect = counting_connect
        try:
            reg = TaskRegistry(db_path=_fresh_db(), runner_id="probe-3")
            reg.register_task("t1")
            reg.close()
        finally:
            sqlite3.connect = real_connect
        self.assertEqual(state["n"], 1)


class RetryWriteTest(unittest.TestCase):
    """写操作自愈：连接中途损坏时自动重建并重试。"""

    def test_write_recovers_from_mid_flight_bad_connection(self):
        """连接建立后变坏（探针已通过、之后才坏）-> 写操作仍应成功。"""
        reg = TaskRegistry(db_path=_fresh_db(), runner_id="heal-1")
        try:
            # 人为把坏连接塞进去，模拟"用着用着变坏"
            reg._local.conn = FakeBadConn(fail_times=1)
            reg.register_task("t1", payload={"k": "v"})
            got = reg.get_task("t1")
            self.assertIsNotNone(got, "损坏连接上的写入应自愈成功")
            self.assertEqual(got["payload"], {"k": "v"})
        finally:
            reg.close()

    def test_second_failure_still_raises(self):
        """重试后仍失败 -> 向上抛（连接重建耗尽后报 RuntimeError），不吞错。"""
        db = _fresh_db()
        reg = TaskRegistry(db_path=db, runner_id="heal-2")
        real_connect = sqlite3.connect
        # 让"重建"也拿到坏连接：模拟连接始终建不健康
        sqlite3.connect = lambda *a, **kw: FakeBadConn(fail_times=10 ** 9)
        try:
            reg._local.conn = FakeBadConn(fail_times=1)
            with self.assertRaises(RuntimeError) as ctx:
                reg.register_task("t1")
        finally:
            sqlite3.connect = real_connect
            reg.close()
        self.assertIn("无法建立可写连接", str(ctx.exception))

    def test_claim_retry_does_not_double_claim(self):
        """CAS 重试安全性：重试不会让同一任务被认领两次。"""
        reg = TaskRegistry(db_path=_fresh_db(), runner_id="heal-3")
        try:
            reg.register_task("task-x")
            # 第一次 claim 成功；此后即使连接损坏，任务已是 running，
            # CAS 条件不满足 -> 重试也返回 False，不会二次认领
            first = reg.claim_task("task-x", runner_id="heal-3")
            reg._local.conn = FakeBadConn(fail_times=10 ** 9)
            second = reg.claim_task("task-x", runner_id="other")
            self.assertTrue(first)
            self.assertFalse(second, "已被认领的任务不能被二次认领")
        finally:
            reg.close()

    def test_register_is_idempotent_after_retry(self):
        """幂等性：register 重试后不会产生两行。"""
        reg = TaskRegistry(db_path=_fresh_db(), runner_id="heal-4")
        try:
            reg.register_task("same-id", payload={"n": 1})
            reg._local.conn = FakeBadConn(fail_times=1)
            reg.register_task("same-id", payload={"n": 2})
            rows = [t for t in reg.list_tasks(limit=100) if t["task_id"] == "same-id"]
            reg.close()
        finally:
            pass
        self.assertEqual(len(rows), 1, "INSERT OR REPLACE 语义：重试不得产生重复行")
        self.assertEqual(rows[0]["payload"], {"n": 2})


def _mp_first_open_worker(i, db_path, out_file):
    """多进程首开工原子进程（必须是模块级函数：Windows spawn 无法 pickle 闭包）。"""
    import json
    res = {"ok": 0, "err": None}
    try:
        reg = TaskRegistry(db_path=db_path, runner_id=f"mp{i}", synchronous="FULL")
        for j in range(60):
            reg.register_task(f"mp{i}-{j}", payload={"j": j})
            reg.update_task_status(f"mp{i}-{j}", "done")
            res["ok"] += 1
        reg.close()
    except Exception as e:  # noqa: BLE001
        res["err"] = f"{type(e).__name__}: {e}"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(res, f)


class MultiProcessSmokeTest(unittest.TestCase):
    """轻量多进程冒烟（比完整压测快，用于回归时持续验证）。"""

    def test_parallel_first_open_no_readonly(self):
        """多进程同时首次打开同一个新库：不应出现 readonly 类失败。"""
        import multiprocessing as mp

        db = _fresh_db()
        tmpdir = tempfile.mkdtemp(prefix="reg_mp_")
        procs = []
        for i in range(6):
            out = os.path.join(tmpdir, f"o{i}.json")
            p = mp.Process(target=_mp_first_open_worker, args=(i, db, out))
            p.start()
            procs.append((p, out))
        errs = []
        total = 0
        import json
        for p, out in procs:
            p.join()
            try:
                with open(out, encoding="utf-8") as f:
                    d = json.load(f)
                total += d["ok"]
                if d["err"]:
                    errs.append(f"w{d['err'][:80]}")
            except OSError:
                errs.append("worker 无结果")
        self.assertEqual(errs, [], f"多进程首开仍有失败: {errs}")
        self.assertEqual(total, 6 * 60)


if __name__ == "__main__":
    unittest.main()
