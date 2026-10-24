"""
象棋最小原型的回归测试（tests/test_xiangqi.py）

覆盖三层：
1. 规则引擎 xiangqi_core：开局步数、蹩马腿/塞象眼/炮架/过河兵/九宫/飞将、
   不能送将、将死/困毙、局面指纹、重复惩罚、静态搜索消除水平线效应。
2. 共享棋局表 xiangqi_table：约局 CAS、事务化回合锁、各类非法落子拒绝、
   三次重复判和、步数上限判和、终局写盘、旧库迁移。
3. 集成：两个 Player（线程模拟两个独立 AI 同事）共享同一 DB 跑完整一局——
   落子全合法、严格交替、必有终局（胜者或和棋）。
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from games import xiangqi_core as X
from games.xiangqi_table import GameTable
from games.player import Player


def empty_board():
    return [[None] * X.COLS for _ in range(X.ROWS)]


def put(board, r, c, color, t):
    board[r][c] = (color, t)


def moves_of(board, r, c):
    return sorted(X.pseudo_moves(board, r, c))


class TestRules(unittest.TestCase):
    """规则引擎单测"""

    def test_initial_position(self):
        b = X.initial_board()
        self.assertEqual(len(X.legal_moves(b, "r")), 44)
        self.assertEqual(len(X.legal_moves(b, "b")), 44)
        self.assertFalse(X.in_check(b, "r"))
        self.assertFalse(X.in_check(b, "b"))
        self.assertEqual(X.evaluate(b, "r"), 0.0)  # 对称开局

    def test_horse_leg_blocked(self):
        b = empty_board()
        put(b, 4, 4, "r", "H")
        put(b, 5, 0, "b", "G"); put(b, 0, 4, "r", "G")
        free = moves_of(b, 4, 4)
        self.assertEqual(len(free), 8)
        put(b, 3, 4, "r", "P")  # 挡住向上的马腿
        blocked = moves_of(b, 4, 4)
        self.assertNotIn((2, 3), blocked)
        self.assertNotIn((2, 5), blocked)
        self.assertEqual(len(blocked), 6)  # 少了向上跳的两点

    def test_elephant_eye_and_river(self):
        b = empty_board()
        put(b, 0, 2, "r", "E")
        put(b, 0, 4, "r", "G"); put(b, 9, 4, "b", "G")
        free = moves_of(b, 0, 2)
        self.assertIn((2, 4), free)
        self.assertIn((2, 0), free)
        put(b, 1, 3, "r", "P")  # 塞象眼
        blocked = moves_of(b, 0, 2)
        self.assertNotIn((2, 4), blocked)
        # 象不过河：红象永远到不了 row 5+
        for (tr, tc) in X.pseudo_moves(b, 0, 2):
            self.assertLess(tr, 5)

    def test_cannon_needs_screen(self):
        b = empty_board()
        put(b, 2, 1, "r", "C")
        put(b, 7, 1, "b", "C")  # 炮架
        put(b, 9, 1, "b", "H")  # 炮架后的目标
        put(b, 0, 4, "r", "G"); put(b, 9, 4, "b", "G")
        mv = moves_of(b, 2, 1)
        self.assertIn((9, 1), mv)  # 隔一子可吃
        self.assertIn((3, 1), mv)  # 平移
        b[7][1] = None  # 撤掉炮架
        mv2 = moves_of(b, 2, 1)
        self.assertNotIn((9, 1), mv2)  # 无架不可吃（(9,1) 成为第一个子）

    def test_pawn_movement(self):
        b = empty_board()
        put(b, 3, 0, "r", "P")  # 未过河
        put(b, 0, 4, "r", "G"); put(b, 9, 4, "b", "G")
        self.assertEqual(moves_of(b, 3, 0), [(4, 0)])  # 只能向前
        put(b, 5, 2, "r", "P")  # 已过河
        self.assertEqual(sorted(moves_of(b, 5, 2)), [(5, 1), (5, 3), (6, 2)])
        # 永不后退
        self.assertNotIn((4, 2), moves_of(b, 5, 2))

    def test_general_palace_and_flying(self):
        b = empty_board()
        put(b, 0, 4, "r", "G")
        put(b, 9, 4, "b", "G")  # 同列无子 => 对脸
        self.assertTrue(X.in_check(b, "b"))
        self.assertTrue(X.in_check(b, "r"))
        # 对脸时黑将可飞过去吃红将（一步制胜）
        bmoves = moves_of(b, 9, 4)
        self.assertIn((0, 4), bmoves)
        nb = X.make_move(b, 9, 4, 0, 4)
        over, winner = X.is_game_over(nb, "r")
        self.assertTrue(over)
        self.assertEqual(winner, "b")
        # 塞入一子后不再对脸
        b[5][4] = ("r", "P")
        self.assertFalse(X.in_check(b, "r"))

    def test_general_stays_in_palace(self):
        b = empty_board()
        put(b, 0, 4, "r", "G")
        put(b, 9, 0, "b", "G")  # 不同列：排除"飞将吃将"这一特殊走法
        mv = moves_of(b, 0, 4)
        self.assertTrue(mv)  # 至少有一步
        for (tr, tc) in mv:
            self.assertTrue(3 <= tc <= 5 and tr <= 2)

    def test_cannot_leave_self_in_check(self):
        b = empty_board()
        put(b, 0, 4, "r", "G")
        put(b, 1, 4, "b", "R")  # 黑车将军
        put(b, 9, 4, "b", "G")
        legal = X.legal_moves(b, "r")
        self.assertTrue(X.in_check(b, "r"))
        for (fr, fc, tr, tc) in legal:
            nb = X.make_move(b, fr, fc, tr, tc)
            self.assertFalse(X.in_check(nb, "r"))  # 任何合法步都必须解将
        # 该局面红只能吃车或将走开，不存在"垫一步不解将"的合法步
        self.assertTrue(all(m[0] == 0 and m[1] == 4 for m in legal) or
                        any(m[2:] == (1, 4) for m in legal))

    def test_checkmate_detection(self):
        b = empty_board()
        put(b, 0, 4, "r", "G")
        put(b, 9, 4, "b", "G")
        put(b, 8, 4, "r", "R")   # 将军
        put(b, 7, 3, "r", "R")   # 锁 col 3
        put(b, 7, 5, "r", "R")   # 锁 col 5
        over, winner = X.is_game_over(b, "b")
        self.assertTrue(over)
        self.assertEqual(winner, "r")
        self.assertTrue(X.in_check(b, "b"))

    def test_stalemate_is_loss_in_xiangqi(self):
        b = empty_board()
        put(b, 0, 3, "r", "G")  # 红将在九宫内但避开 e 线：否则黑将可"飞将"逃生
        put(b, 9, 4, "b", "G")
        put(b, 8, 3, "r", "P")  # 过河兵锁死黑将三邻格
        put(b, 8, 5, "r", "P")
        over, winner = X.is_game_over(b, "b")
        self.assertTrue(over)
        self.assertEqual(winner, "r")  # 困毙：无步者负
        self.assertFalse(X.in_check(b, "b"))


class TestSearch(unittest.TestCase):
    """搜索：重复惩罚 + 静态搜索（水平线效应）"""

    def test_position_key(self):
        b = X.initial_board()
        self.assertEqual(X.position_key(b, "r"), X.position_key(X.clone(b), "r"))
        self.assertNotEqual(X.position_key(b, "r"), X.position_key(b, "b"))
        nb = X.make_move(b, 0, 1, 2, 2)
        self.assertNotEqual(X.position_key(b, "r"), X.position_key(nb, "b"))

    def test_repetition_penalty_in_search(self):
        b = X.initial_board()
        key = X.position_key(b, "r")
        val = X._negamax(b, "r", 2, -float("inf"), float("inf"),
                         time.time() + 5, frozenset({key}))
        self.assertEqual(val, 0.0)  # 重复局面按和棋计

    def test_quiescence_kills_horizon_effect(self):
        """开局炮隔子吃马后，浅层搜索必须看到反吃：估值 ≈ -50 而非 +340。"""
        b = X.initial_board()
        nb = X.make_move(b, 2, 1, 9, 1)  # 炮八进八吃马
        root = X.position_key(b, "r")
        val = -X._negamax(nb, "b", 1, -float("inf"), float("inf"),
                          time.time() + 5, frozenset({root}))
        # 反吃序列解析后：红失炮(450)得马(400) ≈ -50，绝不应是大幅正分
        self.assertLess(val, -30)
        self.assertGreater(val, -120)

    def test_choose_move_develops_not_hangs(self):
        """引擎不应选择明显亏损的炮打马（开局双边马皆被炮瞄住的陷阱）。"""
        b = X.initial_board()
        mv = X.choose_move(b, "r", depth=2, time_budget=5.0)
        self.assertIsNotNone(mv)
        # 打马走法的共同特征：目标在 (9,1) 或 (9,7)
        self.assertNotIn((mv[2], mv[3]), [(9, 1), (9, 7)])


class TestGameTable(unittest.TestCase):
    """共享棋局表：约局 CAS + 回合锁 + 终局规则"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "g.db")
        self.t = GameTable(self.db)
        self.t.propose("g1", red_id="red", black_id="black")
        self.t.accept("g1")

    def _set_board(self, board):
        # 注意：`with sqlite3.connect()` 是事务上下文，不关连接——显式 close。
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE games SET board=? WHERE id='g1'",
                         (json.dumps(X.board_to_json(board)),))
            conn.commit()
        finally:
            conn.close()

    def test_propose_cas(self):
        self.assertFalse(self.t.propose("g1"))       # 同 id 不可重复约局
        self.assertTrue(self.t.propose("g2"))
        t2 = GameTable(self.db)
        self.assertTrue(t2.accept("g2"))
        self.assertFalse(t2.accept("g2"))            # accept 只能一次

    def test_turn_lock(self):
        st = self.t.get_state("g1")
        self.assertEqual(st["turn"], "r")
        mv = X.choose_move(st["board"], "r", depth=1, time_budget=1.0)
        res = self.t.apply_move_by_color("g1", "r", mv)
        self.assertTrue(res["ok"])
        self.assertEqual(res["turn"], "b")
        # 红方连走两步 -> 第二次被回合锁拒绝
        again = self.t.apply_move_by_color("g1", "r", mv)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "not_your_turn")
        # 非玩家身份被拒
        bad = self.t.apply_move("g1", "intruder", mv)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "not_a_player")

    def test_illegal_moves_rejected(self):
        self._set_board(X.initial_board())
        # 无自己的子
        r1 = self.t.apply_move_by_color("g1", "r", (4, 4, 4, 5))
        self.assertEqual(r1["reason"], "no_own_piece")
        # 目标不在走法内（车一步到 (5,5)）
        r2 = self.t.apply_move_by_color("g1", "r", (0, 0, 5, 5))
        self.assertEqual(r2["reason"], "illegal_target")
        # 走到自己子上
        r3 = self.t.apply_move_by_color("g1", "r", (0, 0, 0, 1))
        self.assertEqual(r3["reason"], "illegal_target")

    def test_leaves_self_in_check_rejected(self):
        b = empty_board()
        put(b, 0, 4, "r", "G")
        put(b, 0, 2, "r", "E")
        put(b, 9, 4, "b", "G")
        put(b, 1, 4, "b", "R")  # 黑车将军
        self._set_board(b)
        res = self.t.apply_move_by_color("g1", "r", (0, 2, 2, 4))  # 象走田，不解将
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "leaves_self_in_check")

    def test_threefold_repetition_draw(self):
        """双方车来回走：4 步为一循环，第 8 手初始局面第 3 次出现 -> 判和。"""
        seq = [  # (颜色, from, to) —— 红车/黑车都在 a 线来回
            ("r", (0, 0), (1, 0)), ("b", (9, 0), (8, 0)),
            ("r", (1, 0), (0, 0)), ("b", (8, 0), (9, 0)),
            ("r", (0, 0), (1, 0)), ("b", (9, 0), (8, 0)),
            ("r", (1, 0), (0, 0)), ("b", (8, 0), (9, 0)),
        ]
        for i, (color, frm, to) in enumerate(seq):
            res = self.t.apply_move_by_color("g1", color, frm + to)
            self.assertTrue(res["ok"], f"第 {i+1} 手应合法: {res}")
            if i < len(seq) - 1:
                self.assertEqual(res["status"], "playing")
            else:
                self.assertEqual(res["status"], "ended")
                self.assertEqual(res["reason"], "threefold_repetition")
                self.assertIsNone(res["winner"])  # 和棋无胜者
        st = self.t.get_state("g1")
        self.assertEqual(st["status"], "ended")
        self.assertEqual(st["reason"], "threefold_repetition")
        self.assertIsNone(st["winner"])

    def test_max_plies_draw(self):
        # 把 history 伪造成 299 步，再走一步即触顶步数上限
        conn = sqlite3.connect(self.db)
        try:
            fake = [{"by": "r", "move": [0, 0, 1, 0], "uci": "x"}] * (X.MAX_PLIES - 1)
            conn.execute("UPDATE games SET history=? WHERE id='g1'",
                         (json.dumps(fake),))
            conn.commit()
        finally:
            conn.close()
        mv = X.choose_move(self.t.get_state("g1")["board"], "r",
                           depth=1, time_budget=1.0)
        res = self.t.apply_move_by_color("g1", "r", mv)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "ended")
        self.assertEqual(res["reason"], "max_plies")
        self.assertIsNone(res["winner"])

    def test_checkmate_written_to_table(self):
        b = empty_board()
        put(b, 0, 0, "r", "G")
        put(b, 9, 4, "b", "G")
        put(b, 8, 3, "r", "R")   # 锁 (9,3) 与 (8,4)
        put(b, 8, 5, "r", "R")   # 锁 (9,5) 与 (8,4)
        put(b, 1, 2, "r", "R")   # 一步平到 (1,4) 成杀
        self._set_board(b)
        res = self.t.apply_move_by_color("g1", "r", (1, 2, 1, 4))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ended")
        self.assertEqual(res["reason"], "checkmate")
        self.assertEqual(res["winner"], "r")
        st = self.t.get_state("g1")
        self.assertEqual(st["status"], "ended")
        self.assertEqual(len(st["history"]), 1)

    def test_stalemate_written_to_table(self):
        b = empty_board()
        put(b, 0, 3, "r", "G")  # 九宫内且避开 e 线，防黑将飞将逃生
        put(b, 9, 4, "b", "G")
        put(b, 8, 3, "r", "P")
        put(b, 8, 5, "r", "P")
        self._set_board(b)
        # 红方随便走一步将（不破坏困毙），黑方即无步可动
        res = self.t.apply_move_by_color("g1", "r", (0, 3, 1, 3))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "ended")
        self.assertEqual(res["reason"], "stalemate")
        self.assertEqual(res["winner"], "r")

    def test_keys_recorded_and_migrated(self):
        st = self.t.get_state("g1")
        self.assertEqual(len(st["keys"]), 1)  # 初始局面指纹
        mv = X.choose_move(st["board"], "r", depth=1, time_budget=1.0)
        self.t.apply_move_by_color("g1", "r", mv)
        self.assertEqual(len(self.t.get_state("g1")["keys"]), 2)

    def test_old_db_migration(self):
        old = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE games (id TEXT PRIMARY KEY, status TEXT NOT NULL,"
            " turn TEXT NOT NULL, red_id TEXT NOT NULL, black_id TEXT NOT NULL,"
            " board TEXT NOT NULL, history TEXT NOT NULL, winner TEXT,"
            " reason TEXT, created_at REAL, updated_at REAL)")
        conn.execute(
            "INSERT INTO games VALUES('g','proposed','r','red','black','{}','[]',"
            "NULL,NULL,0,0)")
        conn.commit(); conn.close()
        t = GameTable(old)  # 打开时应自动补 keys 列
        conn2 = sqlite3.connect(old)
        try:
            cols = {r[1] for r in conn2.execute(
                "PRAGMA table_info(games)").fetchall()}
        finally:
            conn2.close()
        self.assertIn("keys", cols)


class TestTwoPlayerIntegration(unittest.TestCase):
    """两个"AI 同事"（线程）共享同一 DB 跑完整一局"""

    def test_full_game(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "g.db")
        t = GameTable(db)
        self.assertTrue(t.propose("g"))

        results = {}

        def run_player(color):
            p = Player(color, "g", db, depth=1, time_budget=0.4,
                       poll=0.02, max_plies=X.MAX_PLIES,
                       log=lambda m: None)
            results[color] = p.run()

        threads = [threading.Thread(target=run_player, args=(c,))
                   for c in ("r", "b")]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=240)
        self.assertFalse(any(th.is_alive() for th in threads), "对局未在时限内结束")

        st = t.get_state("g")
        history = st["history"]
        self.assertGreater(len(history), 0)
        self.assertLessEqual(len(history), X.MAX_PLIES)
        self.assertEqual(st["status"], "ended")
        # 终局必须有结论：有胜者，或按规则判和
        self.assertTrue(
            st["winner"] in ("r", "b")
            or st["reason"] in ("threefold_repetition", "max_plies", "stalemate"),
            f"终局无有效结论: {st['reason']}")
        # 严格交替（红先）
        bys = [h["by"] for h in history]
        self.assertEqual(bys[0], "r")
        for i in range(len(bys) - 1):
            self.assertNotEqual(bys[i], bys[i + 1])
        # 重放验证：每一步在当时局面都是合法步
        board = X.initial_board()
        for h in history:
            fr, fc, tr, tc = h["move"]
            self.assertIn((fr, fc, tr, tc), X.legal_moves(board, h["by"]),
                          f"非法落子被写入棋局: {h}")
            board = X.make_move(board, fr, fc, tr, tc)


if __name__ == "__main__":
    unittest.main()
