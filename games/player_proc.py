"""
双人进程入口（games/player_proc.py）

被 run_demo.py 以子进程方式拉起，模拟"两个独立的 AI 同事进程"。
用法：python games/player_proc.py <color> <db_path> <game_id> [depth]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from games.player import Player  # noqa: E402


def main():
    color = sys.argv[1]
    db_path = sys.argv[2]
    game_id = sys.argv[3]
    depth = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    Player(color, game_id, db_path, depth=depth,
           log=lambda m: print(m, flush=True)).run()


if __name__ == "__main__":
    main()
