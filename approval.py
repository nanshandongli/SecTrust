"""
审批中心（ApprovalCenter）—— Human-in-the-loop 的核心。

任何「高权限 / 副作用 / 触碰外部资源」的动作（如 /scan repo、调用外部 API、
写文件）在执行前先进入审批中心挂起为 pending，由人工（或自动策略）批准/拒绝后才放行。
超时未决自动 expire，绝不默认放行——这是「保守默认拒绝」原则的落地。

存储：与 task_registry 共用同一 SQLite 文件（同机共享），但独立维护 approvals 表，
      不污染最小契约。连接管理同样遵循：每线程独立连接、WAL、busy_timeout、
      CREATE TABLE IF NOT EXISTS（删库自动重建）。

用法（代码）：
  ac = ApprovalCenter(db_path="data/task_registry.db")
  aid = ac.request("scan_repo", "https://github.com/foo/bar", task_id="t1")
  # 阻塞等待真人/CLI 决策
  decision = ac.wait_for_decision(aid, timeout=120)
  if decision == "approved":
      ... 放行执行 ...

用法（CLI，真人审批入口）：
  python approval.py --list                      # 列出待审批
  python approval.py --approve <approval_id>      # 批准
  python approval.py --reject <approval_id>       # 拒绝
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import uuid


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "task_registry.db"
)


class ApprovalCenter:
    """审批中心：pending / approved / rejected / expired 四态。默认拒绝。"""

    def __init__(self, db_path=DEFAULT_DB_PATH, timeout=30):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._timeout = timeout
        self._local = threading.local()
        with self._connect() as conn:
            self._ensure_schema(conn)

    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=self._timeout, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            self._local.conn = conn
        return conn

    def _ensure_schema(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                task_id      TEXT,
                channel      TEXT,
                user_id      TEXT,
                action       TEXT NOT NULL,
                detail       TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   REAL NOT NULL,
                decided_at   REAL,
                decided_by   TEXT
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

    # ---- 写：发起审批 ----
    def request(self, action: str, detail: str, task_id: str = "",
                channel: str = "", user_id: str = "") -> str:
        aid = f"ap-{uuid.uuid4().hex[:10]}"
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, task_id, channel, user_id, action, detail,
                 "pending", now, None, None),
            )
        return aid

    # ---- 写：决策 ----
    def decide(self, approval_id: str, decision: str, by: str = "human") -> bool:
        """decision: 'approve' / 'reject'。返回是否成功（id 存在且当时为 pending）。"""
        decision = decision.lower()
        if decision not in ("approve", "reject"):
            raise ValueError("decision 仅支持 approve / reject")
        status = "approved" if decision == "approve" else "rejected"
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status=?, decided_at=?, decided_by=? "
                "WHERE approval_id=? AND status='pending'",
                (status, time.time(), by, approval_id),
            )
            return cur.rowcount == 1

    def _mark_expired(self, approval_id: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET status='expired' "
                "WHERE approval_id=? AND status='pending'",
                (approval_id,),
            )

    # ---- 读：等待决策（供 runner 阻塞等待真人/CLI）----
    def wait_for_decision(self, approval_id: str, timeout: float = 120, poll: float = 1.0) -> str:
        """阻塞直到该审批被决定或超时。超时返回 'expired'（绝不返回 approved）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.status(approval_id)
            if st in ("approved", "rejected", "expired"):
                if st == "expired":
                    self._mark_expired(approval_id)
                return st
            time.sleep(poll)
        self._mark_expired(approval_id)
        return "expired"

    def status(self, approval_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return row[0] if row else "unknown"

    def is_approved(self, approval_id: str) -> bool:
        return self.status(approval_id) == "approved"

    # ---- 只读查询（task_center / CLI 用）----
    def list_approvals(self, status: str = None, limit: int = 100) -> list:
        q = ("SELECT approval_id, task_id, channel, user_id, action, detail, status, "
             "created_at, decided_at, decided_by FROM approvals WHERE 1=1")
        params = []
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [
            {
                "approval_id": r[0], "task_id": r[1], "channel": r[2], "user_id": r[3],
                "action": r[4], "detail": r[5], "status": r[6],
                "created_at": r[7], "decided_at": r[8], "decided_by": r[9],
            }
            for r in rows
        ]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="审批中心 CLI（真人审批入口）")
    ap.add_argument("--db-path", default=DEFAULT_DB_PATH)
    ap.add_argument("--list", action="store_true", help="列出待审批(及最近)")
    ap.add_argument("--approve", metavar="APPROVAL_ID", help="批准指定审批")
    ap.add_argument("--reject", metavar="APPROVAL_ID", help="拒绝指定审批")
    ap.add_argument("--by", default="human", help="决策人标识")
    args = ap.parse_args()

    ac = ApprovalCenter(db_path=args.db_path)
    if args.approve:
        ok = ac.decide(args.approve, "approve", by=args.by)
        print("批准" if ok else "失败(不存在或已决策):", args.approve)
    elif args.reject:
        ok = ac.decide(args.reject, "reject", by=args.by)
        print("拒绝" if ok else "失败(不存在或已决策):", args.reject)
    else:
        rows = ac.list_approvals(limit=50)
        for r in rows:
            print(f"{r['approval_id']}  [{r['status']}]  {r['action']}  {r['detail']}  "
                  f"(task={r['task_id']}, by={r['decided_by']})")
    ac.close()
