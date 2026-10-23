"""
最小原型驱动（games/run_demo.py）

验证"多 AI 同事底座能否撑起两方博弈"：
  - 起两个**独立进程**（= 两个 AI 同事），只通过共享 SQLite 棋局表协调；
  - 复用 transport.Broker（MQ_DB_PATH 共享）做"实时观战"事件流；
  - 跑通 约局 -> 轮流落子 -> 判负 闭环。

用法：python games/run_demo.py [depth]
"""

import os
import sys
import subprocess
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from games.xiangqi_table import GameTable  # noqa: E402
from transport.broker import Broker        # noqa: E402


def main():
    depth = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    # 用项目 data/ 下的固定路径：避免 Windows 8.3 短名（如 ADMINI~1）导致两个进程把
    # "同一文件"当成两个文件、跨进程读不到对方写入。
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "xiangqi_demo.db")
    q_path = os.path.join(data_dir, "xiangqi_demo_mq.db")
    for f in (db_path, q_path):
        if os.path.exists(f):
            os.remove(f)
    os.environ["MQ_DB_PATH"] = q_path  # 对局与观战同享此队列文件

    gid = "demo-2p"
    broker = Broker()
    table = GameTable(db_path, broker=broker)
    table.propose(gid, red_id="red", black_id="black")

    # 观战：订阅落子事件
    events = []

    def handler(ev):
        events.append(ev)
        d = ev.get("data", {})
        print(f"  [观战] {d.get('player')} {d.get('uci')} -> "
              f"{d.get('status')}{(' 胜方=' + d['winner']) if d.get('winner') else ''}",
              flush=True)

    worker = broker.spawn_worker("xiangqi", handler, poll_interval=0.05)

    print(f"== 启动两个独立进程对弈（depth={depth}），共享 DB: {db_path} ==")
    env = dict(os.environ)
    p_red = subprocess.Popen(
        [sys.executable, "games/player_proc.py", "red", db_path, gid, str(depth)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    p_black = subprocess.Popen(
        [sys.executable, "games/player_proc.py", "black", db_path, gid, str(depth)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    # 实时打印两个玩家的 stdout
    def pump(proc, label):
        for line in proc.stdout:
            print(f"  ({label}) {line.rstrip()}", flush=True)

    import threading
    t1 = threading.Thread(target=pump, args=(p_red, "红"), daemon=True)
    t2 = threading.Thread(target=pump, args=(p_black, "黑"), daemon=True)
    t1.start(); t2.start()
    p_red.wait(); p_black.wait()
    t1.join(timeout=1); t2.join(timeout=1)
    broker.stop_worker(worker)

    st = table.get_state(gid)
    print("\n== 终局 ==")
    print(f"状态: {st['status']} | 胜方: {st['winner']} | 原因: {st['reason']} "
          f"| 总步数: {len(st['history'])}")
    print(f"观战事件数: {len(events)}")
    print("最近 6 步:", [h['uci'] for h in st['history'][-6:]])


if __name__ == "__main__":
    main()
