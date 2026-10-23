"""
常驻 runner 守护示例（含 30s 心跳循环）

职责：
- 模拟接收消息：每隔 task_interval 秒"收到"一条消息并登记为任务；
- 任务生命周期：registered -> running -> done/failed（模拟执行成败）；
- 心跳：独立线程每 heartbeat_interval 秒向 runners 表声明存活（默认 30s）；
- 优雅退出：捕获 SIGINT/SIGTERM，停止所有循环并关闭连接。

运行（常驻）：
    python runner_daemon.py --db-path data/task_registry.db

测试（短跑，便于验证）：
    python runner_daemon.py --db-path data/_demo.db --task-interval 1 --heartbeat 2 --duration 7
"""

import argparse
import random
import signal
import threading
import time

from task_registry import TaskRegistry


class RunnerDaemon:
    def __init__(
        self,
        db_path,
        task_interval=5.0,
        heartbeat_interval=30.0,
        fail_rate=0.15,
    ):
        self.reg = TaskRegistry(db_path=db_path)
        self.task_interval = task_interval
        self.heartbeat_interval = heartbeat_interval
        self.fail_rate = fail_rate
        self.runner_id = self.reg.runner_id
        self._stop = threading.Event()
        self._counter = 0
        self._lock = threading.Lock()

    # ---- 心跳线程：每 heartbeat_interval 秒声明一次存活 ----
    def _heartbeat_loop(self):
        self.reg.beat()  # 启动即声明一次
        while not self._stop.wait(self.heartbeat_interval):
            self.reg.beat()

    def _next_task_id(self):
        with self._lock:
            self._counter += 1
            return f"task-{int(time.time())}-{self._counter:04d}"

    # ---- 模拟"收到消息 -> 登记 -> 执行 -> 变更状态" ----
    def _process_message(self, message):
        tid = self._next_task_id()
        self.reg.register_task(tid, payload=message)      # registered
        self.reg.update_task_status(tid, "running")       # running
        time.sleep(min(self.task_interval * 0.5, 2.0))    # 模拟执行耗时
        status = "failed" if random.random() < self.fail_rate else "done"
        self.reg.update_task_status(tid, status)          # done / failed
        return tid, status

    def run(self, duration=None):
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        print(
            f"[runner:{self.runner_id}] 启动 | 心跳间隔 {self.heartbeat_interval}s | "
            f"任务间隔 {self.task_interval}s"
        )
        deadline = (time.time() + duration) if duration else None
        try:
            while not self._stop.is_set():
                if deadline and time.time() >= deadline:
                    break
                msg = {"seq": self._counter + 1, "msg": "mock message"}
                tid, status = self._process_message(msg)
                print(f"[runner:{self.runner_id}] 处理 {tid} -> {status}")
                # 下一个任务间隔内可被 stop 立即唤醒
                self._stop.wait(self.task_interval)
        finally:
            self._stop.set()
            hb.join(timeout=1)
            self.reg.close()
            print(f"[runner:{self.runner_id}] 已停止")

    def stop(self):
        self._stop.set()


def _install_signal_handlers(daemon):
    def _handler(signum, _frame):
        print(f"收到信号 {signum}，准备退出…")
        daemon.stop()

    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):  # 某些平台不支持 SIGTERM
        pass


def main():
    ap = argparse.ArgumentParser(description="常驻 runner 守护（含心跳）")
    ap.add_argument("--db-path", default="data/task_registry.db")
    ap.add_argument("--task-interval", type=float, default=5.0, help="模拟消息间隔(秒)")
    ap.add_argument("--heartbeat", type=float, default=30.0, help="心跳间隔(秒)")
    ap.add_argument("--duration", type=float, default=None, help="仅测试：运行 N 秒后自动退出")
    args = ap.parse_args()

    daemon = RunnerDaemon(
        db_path=args.db_path,
        task_interval=args.task_interval,
        heartbeat_interval=args.heartbeat,
    )
    _install_signal_handlers(daemon)
    daemon.run(duration=args.duration)


if __name__ == "__main__":
    main()
