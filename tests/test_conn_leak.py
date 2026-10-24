"""SQLite 连接泄漏回归测试（2026-10-25 巡检发现并修复）。

背景：`with self._conn() as c:` 是**事务上下文**（commit/rollback），**不是关闭**。
ballot.py 曾因这个模式泄漏连接（已修）；本次巡检发现 arbiter.py / assets.py /
games/xiangqi_table.py 存在完全相同的模式——每调一次方法泄漏一个
sqlite3.Connection（Windows 上表现为句柄累积 + ResourceWarning）。

设计原则与既有测试一致：**不测实现细节，测承诺**。
本测试锁定的承诺：**同一实例、同一线程内，无论调用多少次方法，
底层 sqlite3.connect 只发生一次**；close() 后可以重新建连。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arbiter                                                        # noqa: E402
import assets                                                         # noqa: E402
from games.xiangqi_table import GameTable                               # noqa: E402


class _ConnectCounter:
    """包一层 sqlite3.connect，数真实建连次数。"""

    def __init__(self):
        self.count = 0
        self._orig = sqlite3.connect

    def __enter__(self):
        outer = self

        def counting_connect(*a, **kw):
            outer.count += 1
            return outer._orig(*a, **kw)

        sqlite3.connect = counting_connect
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self._orig


class TestNoConnectionLeak(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _db(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    # ---------------------------------------------------------------- 仲裁
    def test_arbiter_connects_once(self):
        with _ConnectCounter() as cc:
            a = arbiter.Arbiter(db_path=self._db("arb.db"))
            for _ in range(50):
                a.positions("task-x")            # 读路径，50 次调用
        self.assertEqual(cc.count, 1,
                         "Arbiter 每调一次方法就新建连接（连接泄漏）")
        a.close()

    # ---------------------------------------------------------------- 素材库
    def test_assets_connects_once(self):
        with _ConnectCounter() as cc:
            s = assets.AssetStore(root=self.tmp, db_path=self._db("assets.db"))
            for i in range(50):
                s.versions("no-such-name")
        self.assertEqual(cc.count, 1,
                         "AssetStore 每调一次方法就新建连接（连接泄漏）")
        s.close()

    # ---------------------------------------------------------------- 象棋
    def test_xiangqi_table_connects_once(self):
        with _ConnectCounter() as cc:
            t = GameTable(db_path=self._db("xq.db"))
            for _ in range(50):
                t.get_state("no-such-game")
        self.assertEqual(cc.count, 1,
                         "GameTable 每调一次方法就新建连接（连接泄漏）")
        t.close()

    # ---------------------------------------------------------------- 通用契约
    def test_close_then_reconnect_makes_new_connection(self):
        """close() 必须真正关闭并清缓存，之后能重新建连（不能返回已关闭连接）。"""
        with _ConnectCounter() as cc:
            a = arbiter.Arbiter(db_path=self._db("arb2.db"))
            a.close()
            a.positions("task-y")                 # close 之后必须还能用
        self.assertEqual(cc.count, 2,
                         "close() 后应重新建连；若仍为 1 说明缓存没清，"
                         "或 >2 说明 close 后又泄漏了连接")
        a.close()

    def test_close_is_idempotent(self):
        a = arbiter.Arbiter(db_path=self._db("arb3.db"))
        a.close()
        a.close()                                 # 二次 close 不应抛异常
        a.positions("task-z")                     # 且之后仍可用


if __name__ == "__main__":
    unittest.main()
