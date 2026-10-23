"""
共享棋局表（games/xiangqi_table.py）

这是原型的"协调底座"层——它**故意只复用项目已有的原语**，不另造轮子：

1. 共享 SQLite（WAL + 事务）：与 task_registry 同一份保证。两个独立进程（= 两个 AI 同事）
   只通过这一个文件交换棋局状态。
2. 回合锁 = 事务化"读-校验-落子"：用 `BEGIN IMMEDIATE` 拿到写锁，在锁内校验
   "是否轮到你 + 落子是否合法 + 落子后自己不被将"，再一次性提交并翻转轮次。
   这与 task_registry.claim_task 的 CAS 思想一致——靠"共享 DB + 串行化写"保证
   不会有两个进程同时成功落子、也不会有人连走两步。
3. 落子后通过 transport.Broker（SQLite 队列 / 可切 Redis）发布事件，供"实时观战"。

零依赖（仅 sqlite3 + 标准库）；Broker 可选，缺失则只跳过事件发布。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

try:
    from transport.broker import Broker  # 复用底座事件总线
except Exception:  # 单独运行 games/ 时包不在路径
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from transport.broker import Broker

try:
    from games import xiangqi_core as X
except ImportError:  # 直接 `python games/xiangqi_table.py` 运行时
    import xiangqi_core as X


class GameTable:
    def __init__(self, db_path: str = "data/xiangqi.db", broker: Broker | None = None):
        # 规范化为绝对/真实路径：避免不同进程拿到 "ADMINI~1" 这类 8.3 短名或相对路径，
        # 导致"同一文件"被当成两个文件（跨进程读不到对方写入）。
        self.db_path = os.path.realpath(db_path)
        self.broker = broker
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._connect().close()
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        # 注：本原型要支持两个独立进程高频读写同一文件，Windows 下 WAL 的跨进程
        # 读偶有可见性问题；改用默认 DELETE 日志 + 较高 busy_timeout，SQLite 靠锁
        # 串行化写（恰与"同一时刻只有轮到的一方在写"一致），跨进程读稳定。
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS games (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,           -- proposed | playing | ended
                    turn TEXT NOT NULL,             -- 'r' | 'b'
                    red_id TEXT NOT NULL,
                    black_id TEXT NOT NULL,
                    board TEXT NOT NULL,            -- JSON(二维列表)
                    history TEXT NOT NULL,         -- JSON(落子列表)
                    keys TEXT NOT NULL DEFAULT '[]',  -- JSON(局面指纹序列，用于三次重复判和)
                    winner TEXT,                   -- 'r' | 'b' | NULL
                    reason TEXT,                   -- ended 时的终局原因
                    created_at REAL, updated_at REAL
                )"""
            )
            # 旧库迁移：补 keys 列（老对局从空列表起算，仅影响重复计数精度）
            cols = {r[1] for r in conn.execute("PRAGMA table_info(games)").fetchall()}
            if "keys" not in cols:
                conn.execute("ALTER TABLE games ADD COLUMN keys TEXT NOT NULL DEFAULT '[]'")

    # ---- 约局（对应 channels 的"约好一块下"；这里用共享 DB 做权威握手）----
    def propose(self, game_id: str, red_id: str = "red", black_id: str = "black") -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO games(id,status,turn,red_id,black_id,board,history,"
                    "keys,winner,reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (game_id, "proposed", "r", red_id, black_id,
                     json.dumps(X.board_to_json(X.initial_board())),
                     json.dumps([]),
                     json.dumps([X.position_key(X.initial_board(), "r")]),
                     None, None, time.time(), time.time()),
                )
                return True
            except sqlite3.IntegrityError:
                return False  # 已存在

    def accept(self, game_id: str) -> bool:
        """黑方接受约局：CAS 式翻转 proposed -> playing。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE games SET status='playing', updated_at=? "
                "WHERE id=? AND status='proposed'",
                (time.time(), game_id),
            )
            return cur.rowcount == 1

    # ---- 查询 ----
    def get_state(self, game_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if not row:
            return None
        cols = ["id", "status", "turn", "red_id", "black_id", "board",
                "history", "keys", "winner", "reason", "created_at", "updated_at"]
        st = dict(zip(cols, row))
        st["board"] = X.board_from_json(json.loads(st["board"]))
        st["history"] = json.loads(st["history"])
        st["keys"] = json.loads(st["keys"]) if st.get("keys") else []
        return st

    # ---- 落子（核心：事务化回合锁 + 合法性 + 翻面）----
    def apply_move(self, game_id: str, player_id: str, move: tuple) -> dict:
        fr, fc, tr, tc = move
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # 写锁：与 claim_task 同一串行化保证
            try:
                row = conn.execute(
                    "SELECT status,turn,red_id,black_id,board,history,keys,winner,reason "
                    "FROM games WHERE id=?", (game_id,)
                ).fetchone()
                if not row:
                    return {"ok": False, "reason": "no_such_game"}
                status, turn, red_id, black_id, board_j, hist_j, keys_j, winner, reason = row
                if status != "playing":
                    return {"ok": False, "reason": f"status={status}",
                            "winner": winner, "status": status}
                # 玩家身份 -> 颜色（轮次锁的第一步：不是你别下）
                if player_id == red_id:
                    color = "r"
                elif player_id == black_id:
                    color = "b"
                else:
                    return {"ok": False, "reason": "not_a_player"}
                if turn != color:
                    return {"ok": False, "reason": "not_your_turn", "turn": turn}
                board = X.board_from_json(json.loads(board_j))
                piece = board[fr][fc]
                if piece is None or piece[0] != color:
                    return {"ok": False, "reason": "no_own_piece"}
                # 合法性：目标在伪走法内，且落子后自己不被将
                if (tr, tc) not in X.pseudo_moves(board, fr, fc):
                    return {"ok": False, "reason": "illegal_target"}
                nb = X.make_move(board, fr, fc, tr, tc)
                if X.in_check(nb, color):
                    return {"ok": False, "reason": "leaves_self_in_check"}
                # 落子合法 -> 写盘 + 翻面
                history = json.loads(hist_j)
                history.append({"by": color, "move": [fr, fc, tr, tc],
                                "uci": X.move_to_str((fr, fc, tr, tc))})
                keys = json.loads(keys_j) if keys_j else []
                opp = X.opponent(color)
                new_key = X.position_key(nb, opp)
                keys.append(new_key)
                # 终局判定优先级：将死/困毙 > 三次重复判和 > 步数上限判和
                over, w = X.is_game_over(nb, opp)
                new_status = "ended" if over else "playing"
                new_turn = opp
                new_reason = None
                if over:
                    new_reason = "checkmate" if X.in_check(nb, opp) else "stalemate"
                elif keys.count(new_key) >= X.REPETITION_LIMIT:
                    over, new_status, new_reason = True, "ended", "threefold_repetition"
                elif len(history) >= X.MAX_PLIES:
                    over, new_status, new_reason = True, "ended", "max_plies"
                conn.execute(
                    "UPDATE games SET status=?, turn=?, board=?, history=?, keys=?, "
                    "winner=?, reason=?, updated_at=? WHERE id=?",
                    (new_status, new_turn, json.dumps(X.board_to_json(nb)),
                     json.dumps(history), json.dumps(keys),
                     w if over and new_reason in ("checkmate", "stalemate") else None,
                     new_reason, time.time(), game_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        result = {"ok": True, "status": new_status, "turn": new_turn,
                  "winner": w if over else None, "reason": new_reason,
                  "uci": X.move_to_str((fr, fc, tr, tc))}
        # 事件发布是"观战/通知"用途，绝不影响落子关键路径
        if self.broker is not None:
            try:
                self.broker.publish_event(
                    "xiangqi", "move",
                    {"game_id": game_id, "player": color, "uci": result["uci"],
                     "status": new_status, "turn": new_turn,
                     "winner": result["winner"]},
                )
            except Exception:
                pass
        return result

    # 便于测试的便捷方法：直接用颜色落子（id 默认 red/black）
    def apply_move_by_color(self, game_id: str, color: str, move: tuple) -> dict:
        pid = "red" if color == "r" else "black"
        return self.apply_move(game_id, pid, move)


if __name__ == "__main__":
    import tempfile
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    t = GameTable(db)
    t.propose("g1")
    t.accept("g1")
    st = t.get_state("g1")
    print("开局轮次:", st["turn"], "| 状态:", st["status"])
    # 红走一步，再让红走（应被回合锁拒绝）
    mv = X.choose_move(st["board"], "r", depth=2)
    print("红落子:", t.apply_move_by_color("g1", "r", mv))
    print("红再走(应拒):", t.apply_move_by_color("g1", "r", mv))
    print("黑落子:", t.apply_move_by_color("g1", "b",
          X.choose_move(t.get_state("g1")["board"], "b", depth=2)))
