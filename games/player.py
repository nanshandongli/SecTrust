"""
AI 玩家（games/player.py）

代表"一个 AI 同事"：只通过共享 SQLite 棋局表协调，循环做四件事：
  1. 约局：黑方接受（红方等待）
  2. 等待：不是我的回合就睡一小会儿（轮询，对应 runner 的 heartbeat/claim 模式）
  3. 落子：轮到我时用 negamax 选一步，交给 GameTable.apply_move（事务化回合锁把关）
  4. 判终：对局结束 / 达到步数上限就退出

不持有任何"对手状态"——它只知道共享棋盘，这正是多方博弈对底座的要求：
**每个参与者都是无状态的观察者 + 仅当轮到自己的行动者**。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from games import xiangqi_core as X
    from games.xiangqi_table import GameTable
except ImportError:  # 直接运行 games/ 内脚本时
    import xiangqi_core as X
    from xiangqi_table import GameTable

try:
    from transport.broker import Broker  # 复用底座事件总线（可选）
except Exception:
    Broker = None


class Player:
    def __init__(self, color: str, game_id: str, db_path: str,
                 depth: int = 3, time_budget: float = 2.0,
                 poll: float = 0.03, max_plies: int = 400, log=print):
        # 归一化：外部可传 'red'/'black' 或 'r'/'b'；内部统一用 'r'/'b' 与棋局表/轮次对齐
        self.color = 'r' if color in ('r', 'red') else 'b'
        self.game_id = game_id
        self.max_plies = max_plies
        self.depth = depth
        self.time_budget = time_budget
        self.poll = poll
        self.log = log
        broker = None
        if Broker is not None and os.environ.get("MQ_DB_PATH"):
            try:
                broker = Broker()  # 与对局同享 MQ_DB_PATH -> 可跨进程收事件
            except Exception:
                broker = None
        self.table = GameTable(db_path, broker=broker)

    def run(self) -> dict:
        self.log(f"[{self.color}] 启动 gid={self.game_id} db={self.table.db_path}")
        while True:
            st = self.table.get_state(self.game_id)
            if st is None:
                self.log(f"[{self.color}] 未读到棋局，重试")
                time.sleep(self.poll)
                continue

            if st["status"] == "proposed":
                if self.color == "b":
                    ok = self.table.accept(self.game_id)  # 黑方接受约局（CAS）
                    self.log(f"[{self.color}] 尝试接受约局 -> {ok}")
                time.sleep(self.poll)
                continue

            if st["status"] == "ended":
                self.log(f"[{self.color}] 终局：胜方={st['winner']} "
                         f"原因={st['reason']} 总步数={len(st['history'])}")
                return st

            # status == playing
            if st["turn"] != self.color:
                time.sleep(self.poll)  # 不是我的回合：让出 CPU，轮询等待
                continue

            # 轮到我：选步并落子
            self.log(f"[{self.color}] 轮到我，选步中…")
            mv = X.choose_move(st["board"], self.color,
                               depth=self.depth, time_budget=self.time_budget)
            if mv is None:
                self.log(f"[{self.color}] 无合法步（理论应已被判终局）")
                return st
            res = self.table.apply_move_by_color(self.game_id, self.color, mv)
            if res.get("ok"):
                tag = f"胜方={res['winner']}" if res.get("winner") else res["status"]
                self.log(f"[{self.color}] {res['uci']} -> {tag}")
                if res["status"] == "ended":
                    return st
            # 若 ok=False（极偶发的并发竞态或非法），重读状态再试
            if len(st["history"]) >= self.max_plies:
                self.log(f"[{self.color}] 已达步数上限 {self.max_plies}，停止")
                return st
