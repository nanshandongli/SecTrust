"""
跨进程任务登记处 —— 最小契约实现

存储后端：本地 SQLite 文件（同机共享）
设计前提：runner 与 task_center 在同一台机器，通过共享 SQLite 文件交换任务元数据，
          不共享内存、不暴露网络端口。

关键约束（对应你的并发安全条款）：
- 每个进程各自持有独立连接，绝不跨进程共享连接对象。
- 打开连接时 timeout=30，写操作走短事务（连接上下文管理器自动提交）。
- 启用 WAL 日志模式 + busy_timeout，允许多个读进程与单个写进程并发，
  把「database is locked」降到极低概率。
- 任一连接建立时都执行 CREATE TABLE IF NOT EXISTS，删库后首个写入方自动重建。

注意（部署提示）：
- WAL 在本地磁盘上表现最佳；若 data 目录挂在 NFS/SMB 等网络共享上，WAL 可能失效，
  此时应改回默认 DELETE 日志并依赖 busy_timeout（本模块已设 30s，足够）。
- WAL 备份：绝不可只复制主库文件。未 checkpoint 的数据在 -wal/-shm 里，
  请用本模块的 backup()（sqlite3.backup API，自动合并主库+WAL 成单文件），
  或先 wal_checkpoint(TRUNCATE) 再整目录复制。详见 backup() / checkpoint()。
- synchronous 默认 FULL（极端断电也不丢）；若可接受丢最近几秒、追求 ~3x 写入吞吐，
  可 synchronous="NORMAL"（WAL 下安全）。通过构造参数或 CLI 设置。
"""

import argparse
import json
import os
import socket
import sqlite3
import threading
import time
import uuid


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "task_registry.db"
)

# 连接打开时的重试次数：Windows 上多进程同时首次打开同一个新库时，
# SQLite 可能给出"connect 成功但实际只读"的半残连接（后续所有写操作报
# attempt to write a readonly database 且不自愈）。重开即可恢复。
_CONN_OPEN_RETRIES = 5


def make_runner_id():
    """runner_id 生成：hostname + pid。可读且能区分同一台机器上的多个 runner 进程。"""
    return f"{socket.gethostname()}-{os.getpid()}"


class TaskRegistry:
    """对登记处（SQLite）的封装。runner 与 task_center 共用同一类，
    只读/只写由调用方业务约束，本类不强制。"""

    def __init__(self, db_path=DEFAULT_DB_PATH, runner_id=None, timeout=30, synchronous="FULL"):
        if synchronous not in ("FULL", "NORMAL"):
            raise ValueError("synchronous 仅支持 'FULL' 或 'NORMAL'")
        self.db_path = db_path
        self.runner_id = runner_id or make_runner_id()
        self._timeout = timeout
        self._synchronous = synchronous
        # 确保目录存在；表在 _ensure_schema 中按需创建（删库后可自动重建）
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        # 每条线程一条独立连接，进程级天然隔离 -> 满足「不跨进程共享连接」
        self._local = threading.local()
        # 建表也走重试：多进程首开竞争时，坏连接可能在建表这一步才暴露
        self._retry_write(self._ensure_schema)

    # ---- 连接管理 ----
    def _open_conn(self):
        """建立一条"确认可写"的连接。

        为什么需要探针：`sqlite3.connect()` 本身不验证可写性。在 Windows 上
        多个进程同时首次打开同一个新库时，个别进程会拿到一条半残连接——
        它通过了 connect、`PRAGMA journal_mode=WAL` 也返回 wal（因为库文件
        头确实是 WAL），但第一条 INSERT/CREATE TABLE 就报
        `attempt to write a readonly database`，且**该连接之后永不恢复**。

        对策：新建连接时用 `BEGIN IMMEDIATE` + `ROLLBACK` 做一次廉价写探针，
        确认这条连接真能拿写锁；不健康就关掉重开，重试上限内仍失败则明确
        抛错——绝不把半残连接返回给调用方（静默的半残比崩溃更难查）。
        """
        last_err = None
        for attempt in range(_CONN_OPEN_RETRIES):
            conn = sqlite3.connect(
                self.db_path, timeout=self._timeout, check_same_thread=False
            )
            try:
                conn.execute("PRAGMA busy_timeout=30000;")   # 重试而非立即报错
                mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise sqlite3.OperationalError(
                        f"journal_mode={mode!r}，期望 wal"
                    )
                # 探针：WAL 下拿一次写锁再立刻释放，验证连接真的可写
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                conn.execute(f"PRAGMA synchronous={self._synchronous};")
                return conn
            except sqlite3.Error as e:
                last_err = e
                try:
                    conn.close()
                except Exception:  # noqa: BLE001  关闭失败不影响重试
                    pass
                time.sleep(0.05 * (attempt + 1))
        raise RuntimeError(
            f"无法建立可写连接（重试 {_CONN_OPEN_RETRIES} 次后仍失败）: "
            f"{type(last_err).__name__}: {last_err}"
        )

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_conn()
            self._local.conn = conn
        return conn

    def _drop_conn(self):
        """丢掉当前线程的连接（下次 _connect 会重建）。

        用于"连接已损坏"的恢复：Windows 多进程首开竞争下，一条连接可能
        在建立后、使用中变成实际只读；唯一可靠的恢复是丢掉重开。
        """
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001  关闭失败不阻碍重建
                pass

    def _retry_write(self, fn):
        """执行一次写操作；若失败则重建连接再试一次，仍失败才向上抛。

        为什么需要：`_open_conn` 的探针能挡住"打开时就是坏的"连接，但挡不住
        "用着用着变坏"的连接（多进程首开竞争窗口比探针长）。此类失败重开
        连接即可恢复（实测有效）。

        安全性：本类所有写操作都是幂等的——
          register_task      : INSERT OR REPLACE（同 id 覆盖）
          update_task_status : UPDATE（同值重复写无副作用）
          set_progress       : UPDATE（payload 内子键覆盖）
          claim_task         : CAS（比较 status 后才更新，重试最多多比一次）
        因此"重试一次"不会产生重复副作用。
        """
        try:
            with self._connect() as conn:
                return fn(conn)
        except sqlite3.Error:
            self._drop_conn()
            try:
                with self._connect() as conn:
                    return fn(conn)
            except sqlite3.Error:
                self._drop_conn()
                raise

    def _ensure_schema(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id   TEXT PRIMARY KEY,
                runner_id TEXT NOT NULL,
                status    TEXT NOT NULL DEFAULT 'registered',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload   TEXT,
                depends_on TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        # 旧库迁移：补 depends_on 列（DAG 调度用；无依赖任务等同 '[]'）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "depends_on" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runners (
                runner_id      TEXT PRIMARY KEY,
                last_heartbeat REAL NOT NULL,
                status         TEXT DEFAULT 'alive'
            )
            """
        )
        conn.commit()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    # ---- runner 侧写入 ----
    def register_task(self, task_id, payload=None, depends_on=None):
        """登记任务。depends_on：上游 task_id 列表（DAG 调度用；省略等同无依赖）。
        注意 INSERT OR REPLACE 语义：重复登记同一 id 会把状态重置为 registered。"""
        if not task_id:
            task_id = f"task-{uuid.uuid4().hex[:12]}"
        now = time.time()
        deps = json.dumps(list(depends_on) if depends_on else [], ensure_ascii=False)

        def op(conn):
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(task_id, runner_id, status, created_at, updated_at, payload, depends_on) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    self.runner_id,
                    "registered",
                    now,
                    now,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    deps,
                ),
            )

        self._retry_write(op)
        return task_id

    def get_task(self, task_id):
        """按 task_id 查单条任务（task_center 只读查询）。不存在返回 None。"""
        with self._connect() as conn:
            r = conn.execute(
                "SELECT task_id, runner_id, status, created_at, updated_at, payload, depends_on "
                "FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if r is None:
            return None
        return {
            "task_id": r[0],
            "runner_id": r[1],
            "status": r[2],
            "created_at": r[3],
            "updated_at": r[4],
            "payload": json.loads(r[5]) if r[5] else None,
            "depends_on": json.loads(r[6]) if r[6] else [],
        }

    def update_task_status(self, task_id, status):
        def op(conn):
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (status, time.time(), task_id),
            )

        self._retry_write(op)

    def set_progress(self, task_id, progress=None, log=None):
        """更新任务进度（向后兼容：仅改 payload 内的 progress/log 子键，不动 status 主字段）。
        - progress: 数字(0-100) 或阶段文本，供 live_view 实时展示进度条/阶段。
        - log     : 追加一行执行日志（如沙箱 stdout 片段），供 live_view 展示。
        任务不存在时静默跳过。"""
        def op(conn):
            cur = conn.execute("SELECT payload FROM tasks WHERE task_id=?", (task_id,))
            row = cur.fetchone()
            if row is None:
                return False
            payload = json.loads(row[0]) if row[0] else {}
            if progress is not None:
                payload["progress"] = progress
            if log is not None:
                payload.setdefault("log", []).append(log)
            conn.execute(
                "UPDATE tasks SET payload=?, updated_at=? WHERE task_id=?",
                (json.dumps(payload, ensure_ascii=False), time.time(), task_id),
            )
            return True

        return self._retry_write(op)

    # ---- task_center 侧读取（只读查询）----
    def list_tasks(self, runner_id=None, status=None, limit=100):
        q = (
            "SELECT task_id, runner_id, status, created_at, updated_at, payload, depends_on "
            "FROM tasks WHERE 1=1"
        )
        params = []
        if runner_id:
            q += " AND runner_id=?"
            params.append(runner_id)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [
            {
                "task_id": r[0],
                "runner_id": r[1],
                "status": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "payload": json.loads(r[5]) if r[5] else None,
                "depends_on": json.loads(r[6]) if r[6] else [],
            }
            for r in rows
        ]

    # ---- 心跳（可选）----
    def beat(self):
        """runner 声明存活：每 30 秒调用一次。"""

        def op(conn):
            conn.execute(
                "INSERT OR REPLACE INTO runners VALUES (?, ?, 'alive')",
                (self.runner_id, time.time()),
            )

        self._retry_write(op)

    def list_runners(self, stale_after=90):
        """task_center 拉取 runner 存活状态；超过 stale_after 秒无心跳标记为 stale。"""
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT runner_id, last_heartbeat, status FROM runners"
            ).fetchall()
        return [
            {
                "runner_id": r[0],
                "last_heartbeat": r[1],
                "status": "stale" if (now - r[1]) > stale_after else r[2],
            }
            for r in rows
        ]

    # ---- 备份 / 检查点（WAL 运维）----
    def backup(self, dest_path):
        """用 sqlite3 内置 backup API 把『主库 + 未 checkpoint 的 WAL』合并进单一目标文件。
        无需手动复制 -wal/-shm，避免只拷主文件丢数据。dest_path 会被整体覆盖。"""
        dest = sqlite3.connect(dest_path)
        try:
            self._connect().backup(dest)
            dest.commit()
        finally:
            dest.close()
        return dest_path

    def checkpoint(self, mode="PASSIVE"):
        """执行 WAL 检查点，返回 (total_frames, ckpt_frames, status) 三元组：
        - status == 0：检查点已完成；
        - status != 0（通常 1）：被阻塞，最大嫌疑是『长读事务』挂着没释放，
          此时 WAL 不会收缩，需排查 reader 是否及时结束事务（见模块文档）。
        日常监控用 PASSIVE（不阻塞）；备份/拷贝前用 TRUNCATE（回收 -wal 文件）。"""
        cur = self._connect().execute(f"PRAGMA wal_checkpoint({mode})")
        return tuple(cur.fetchone())

    # ---- 任务认领（CAS，未来多 worker 抢单用）----
    def claim_task(self, task_id, runner_id=None):
        """CAS 认领：仅当任务仍为 'registered' 时才改为 'running'，靠影响行数判断谁抢到。
        返回 True 表示本次认领成功；False 表示已被别人认领或不存在——无需分布式锁。
        runner_id 省略则保持原登记 runner 不变；传入则改写为认领方。"""
        now = time.time()
        if runner_id is None:
            sql = (
                "UPDATE tasks SET status='running', updated_at=? "
                "WHERE task_id=? AND status='registered'"
            )
            params = (now, task_id)
        else:
            sql = (
                "UPDATE tasks SET status='running', runner_id=?, updated_at=? "
                "WHERE task_id=? AND status='registered'"
            )
            params = (runner_id, now, task_id)
        def op(conn):
            cur = conn.execute(sql, params)
            return cur.rowcount == 1

        # CAS 重试安全：条件 status='registered' 保证只有第一次生效，
        # 重试时任务已被改为 running，自然返回 False，不会重复认领。
        return self._retry_write(op)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="task_registry 工具 / 冒烟")
    ap.add_argument("--db-path", default=DEFAULT_DB_PATH)
    ap.add_argument("--synchronous", default="FULL", choices=["FULL", "NORMAL"])
    ap.add_argument("--backup-to", help="备份到目标文件（sqlite backup API，自动含 WAL）")
    ap.add_argument("--checkpoint", action="store_true", help="执行 wal_checkpoint(TRUNCATE) 后退出")
    args = ap.parse_args()

    if args.backup_to:
        TaskRegistry(db_path=args.db_path).backup(args.backup_to)
        print("已备份到:", args.backup_to)
    elif args.checkpoint:
        reg = TaskRegistry(db_path=args.db_path)
        print("wal_checkpoint(TRUNCATE):", reg.checkpoint("TRUNCATE"))
        reg.close()
    else:
        reg = TaskRegistry(db_path=args.db_path, synchronous=args.synchronous)
        reg.register_task("demo-1", payload={"hello": "world"})
        reg.update_task_status("demo-1", "running")
        print(reg.list_tasks())
        reg.close()
