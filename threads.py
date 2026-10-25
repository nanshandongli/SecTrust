"""
threads.py —— 会话线程持久化（多端接续的最小底座）。

职责：把"一次对话"存成 SQLite 里的 thread + messages，让同一 thread_id
可以在不同渠道/不同进程间接续——这是多端连续（手机发起、桌面继续）的
存储底座。线程本身**不含业务逻辑**（不含模型调用、不含渠道收发），
与 channels / models 的边界保持清晰。

数据模型：
  threads (thread_id PK, title, channel, created_at, updated_at)
  messages(msg_id PK, thread_id, role, content, created_at, meta JSON)

API 约定：
  - append() 只往已存在的线程追加；线程不存在 → ThreadError（显式失败）。
    聊天入口请先 ensure_thread()/create_thread()——"往不存在的会话里
    塞消息"是隐性 bug，不许自动兜；
  - history() 按时间正序；before 可游标分页；
  - search() 对 content 做 LIKE（本地规模够用；不加谎称的全文索引）；
  - delete_thread() 级联删消息（会话是用户的，删就删干净）。

纪律（与项目既有模块一致）：
  - SQLite 连接 thread-local 缓存 + 显式 close()；
  - WAL + busy_timeout，删库自动重建（CREATE TABLE IF NOT EXISTS）；
  - 缺任何输入时明确报错，不静默造假。

运行：
    python threads.py --new --title "双十一复盘"     # 建线程
    python threads.py --list                        # 看线程
    python threads.py --thread <id>                 # 看历史
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import uuid

DEFAULT_DB = os.path.join("data", "threads.db")

#: 允许的消息角色（与 models.ChatMessage 对齐，外加 tool 记录工具回参）
ROLES = ("user", "assistant", "system", "tool")


class ThreadError(ValueError):
    """线程操作错误（线程不存在 / 参数非法）。"""


def _new_id(prefix: str) -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


class ThreadStore:
    """会话线程存储。db_path 可注入；连接管理遵循项目纪律。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.environ.get("SECTRUST_THREADS_DB", DEFAULT_DB)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._local = threading.local()
        self._closed = False
        with self._conn() as conn:
            self._ensure_schema(conn)

    # ---- 连接：thread-local + 显式 close ----
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA busy_timeout=30000;")
            self._local.conn = c
        return c

    def _ensure_schema(self, conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads ("
            " thread_id TEXT PRIMARY KEY,"
            " title TEXT NOT NULL DEFAULT '',"
            " channel TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            " msg_id TEXT PRIMARY KEY,"
            " thread_id TEXT NOT NULL,"
            " role TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " created_at REAL NOT NULL,"
            " meta TEXT NOT NULL DEFAULT '{}',"
            " FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_thread"
            " ON messages(thread_id, created_at)"
        )
        conn.commit()

    def close(self):
        if self._closed:
            return
        self._closed = True
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            finally:
                self._local.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 线程管理 ----
    def create_thread(self, title: str = "", channel: str = "",
                      thread_id: str = None) -> dict:
        now = time.time()
        tid = thread_id or _new_id("th")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO threads"
                " (thread_id, title, channel, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (tid, title or tid, channel, now, now))
        return {"thread_id": tid, "title": title or tid, "channel": channel,
                "created_at": now, "updated_at": now}

    def ensure_thread(self, thread_id: str = None, title: str = "",
                      channel: str = "") -> dict:
        """有则返回，无则创建（聊天入口用）。"""
        if thread_id:
            t = self.get_thread(thread_id)
            if t is not None:
                return t
        return self.create_thread(title=title, channel=channel,
                                  thread_id=thread_id)

    def get_thread(self, thread_id: str) -> dict:
        if not thread_id:
            raise ThreadError("thread_id 不能为空")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT thread_id, title, channel, created_at, updated_at"
                " FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        if row is None:
            return None
        return {"thread_id": row[0], "title": row[1], "channel": row[2],
                "created_at": row[3], "updated_at": row[4]}

    def list_threads(self, limit: int = 50) -> list:
        """按最近活跃倒序；带最后一条消息预览（子查询，不 N+1）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT t.thread_id, t.title, t.channel, t.created_at, t.updated_at,"
                "       (SELECT content FROM messages m WHERE m.thread_id=t.thread_id"
                "         ORDER BY m.created_at DESC LIMIT 1) AS preview"
                " FROM threads t ORDER BY t.updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"thread_id": r[0], "title": r[1], "channel": r[2],
                 "created_at": r[3], "updated_at": r[4],
                 "preview": (r[5] or "")[:80]} for r in rows]

    def rename_thread(self, thread_id: str, title: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE threads SET title=? WHERE thread_id=?",
                               (title, thread_id))
            return cur.rowcount == 1

    def delete_thread(self, thread_id: str) -> bool:
        """级联删消息（FK ON DELETE CASCADE + 显式删，双保险）。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
            cur = conn.execute("DELETE FROM threads WHERE thread_id=?", (thread_id,))
            return cur.rowcount == 1

    # ---- 消息 ----
    def append(self, thread_id: str, role: str, content: str,
               meta: dict = None) -> dict:
        if not thread_id:
            raise ThreadError("thread_id 不能为空")
        if role not in ROLES:
            raise ThreadError("role 必须是 %s 之一（收到 %r）"
                              % ("/".join(ROLES), role))
        if not isinstance(content, str) or not content.strip():
            raise ThreadError("content 必须是非空字符串")
        if self.get_thread(thread_id) is None:
            raise ThreadError("线程不存在: %s（先 create_thread/ensure_thread）"
                              % thread_id)
        now = time.time()
        msg = {"msg_id": _new_id("msg"), "thread_id": thread_id, "role": role,
               "content": content, "created_at": now,
               "meta": meta or {}}
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (msg_id, thread_id, role, content,"
                " created_at, meta) VALUES (?,?,?,?,?,?)",
                (msg["msg_id"], thread_id, role, content, now,
                 json.dumps(msg["meta"], ensure_ascii=False)))
            conn.execute("UPDATE threads SET updated_at=? WHERE thread_id=?",
                         (now, thread_id))
        return msg

    def history(self, thread_id: str, limit: int = 50,
                before: float = None) -> list:
        """最近 limit 条，按时间**正序**返回；before=游标（只取该时刻之前的）。

        语义对齐聊天窗口：要的是"最近 N 条"（不是"最早 N 条"），
        因此先倒序取 limit 再翻正序；before 配合上一页最早一条做翻页。
        """
        q = ("SELECT msg_id, thread_id, role, content, created_at, meta"
             " FROM messages WHERE thread_id=?")
        params = [thread_id]
        if before is not None:
            q += " AND created_at < ?"
            params.append(before)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        rows.reverse()                               # 倒序取 → 正序呈现
        return [{"msg_id": r[0], "thread_id": r[1], "role": r[2],
                 "content": r[3], "created_at": r[4],
                 "meta": json.loads(r[5] or "{}")} for r in rows]

    def search(self, keyword: str, limit: int = 20) -> list:
        if not keyword or not keyword.strip():
            return []
        like = "%" + keyword.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT msg_id, thread_id, role, content, created_at"
                " FROM messages WHERE content LIKE ? ESCAPE '\\'"
                " ORDER BY created_at DESC LIMIT ?",
                (like, limit)).fetchall()
        return [{"msg_id": r[0], "thread_id": r[1], "role": r[2],
                 "content": r[3], "created_at": r[4]} for r in rows]

    def stats(self) -> dict:
        with self._conn() as conn:
            threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"threads": threads, "messages": msgs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="会话线程存储 CLI")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--new", action="store_true", help="新建线程")
    ap.add_argument("--title", default="")
    ap.add_argument("--channel", default="")
    ap.add_argument("--thread", help="线程 id")
    ap.add_argument("--list", action="store_true", help="列出线程")
    ap.add_argument("--append", metavar="TEXT", help="向 --thread 追加消息")
    ap.add_argument("--role", default="user", choices=ROLES)
    ap.add_argument("--search", metavar="关键词")
    args = ap.parse_args(argv)

    store = ThreadStore(db_path=args.db)
    try:
        if args.new:
            t = store.create_thread(title=args.title, channel=args.channel)
            print("已创建线程 %s（title=%s）" % (t["thread_id"], t["title"]))
        if args.append:
            if not args.thread:
                print("错误: --append 需要 --thread", file=sys.stderr)
                return 2
            m = store.append(args.thread, args.role, args.append)
            print("已追加 %s [%s] %s" % (m["msg_id"], m["role"], m["content"][:60]))
        if args.thread and not args.append:
            t = store.get_thread(args.thread)
            if t is None:
                print("线程不存在: %s" % args.thread)
                return 1
            print("# %s（channel=%s）" % (t["title"], t["channel"] or "-"))
            for m in store.history(args.thread):
                print("[%s] %s: %s" % (
                    time.strftime("%m-%d %H:%M", time.localtime(m["created_at"])),
                    m["role"], m["content"]))
        if args.list:
            for t in store.list_threads():
                print("%-16s %-20s %-8s %s" % (
                    t["thread_id"], t["title"][:20], t["channel"] or "-",
                    t["preview"]))
        if args.search:
            for m in store.search(args.search):
                print("%s [%s/%s] %s" % (m["msg_id"], m["thread_id"],
                                         m["role"], m["content"][:60]))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
