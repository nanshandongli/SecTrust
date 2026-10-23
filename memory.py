"""
轻量长期记忆 —— SQLite 存储（零依赖、零运维）

设计要点（与 task_registry 一致，天然复用 WAL 模式）：
- 每个进程 / 每线程持有独立连接（threading.local），绝不跨进程共享连接对象。
- WAL 日志 + busy_timeout(30s) + synchronous(FULL/NORMAL)，并发读 + 单写，事务可靠。
- 不引入向量库：用户偏好 / 常用指令模板 / 常用 repo 路径 都是「少量、结构化、可精确查」的数据，
  SQLite 的 (category, key) 主键 + 子串检索 + 访问频次排序已足够，且备份 / 迁移 / 排查都简单。

数据模型（单表 memory_items）：
- category     分类（内置三类：preference / command / repo；也允许任意自定义分类）
- key          分类内唯一标识
- value        JSON 文本（可存字符串、数字、对象、数组）
- note         人类可读说明（可选）
- created_at / updated_at  时间戳
- access_count / last_accessed 访问统计，支撑「常用」排名
主键：(category, key)

典型用法（跨会话记忆「这个用户喜欢用 Qwen-Max 做代码审查」）：
    mem = MemoryStore()
    mem.remember_preference("code_review_model", "Qwen-Max", note="用户偏好的代码审查模型")
    # 下一次会话重新打开同一文件即可读到：
    mem2 = MemoryStore()                      # 同一 data/memory.db
    model = mem2.recall_preference("code_review_model")
"""

import argparse
import json
import os
import sqlite3
import threading
import time


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "memory.db"
)

# 三个语义化分类常量（仅作约定，代码不强约束）
CAT_PREFERENCE = "preference"
CAT_COMMAND = "command"
CAT_REPO = "repo"

# 内置分类的中文名，用于展示/文档
CATEGORY_LABELS = {
    CAT_PREFERENCE: "用户偏好",
    CAT_COMMAND: "常用指令模板",
    CAT_REPO: "常用仓库路径",
}


def _memory_db_path() -> str:
    """记忆库路径：环境变量 MEMORY_DB_PATH 优先，否则默认 data/memory.db。"""
    return os.getenv("MEMORY_DB_PATH") or DEFAULT_DB_PATH


class MemoryStore:
    """对长期记忆（SQLite）的封装。同一类既可在本地进程内调用，
    也可由 mcp_server 通过同一文件跨会话读写——因为数据落在磁盘 SQLite 上。"""

    def __init__(self, db_path=DEFAULT_DB_PATH, synchronous="FULL"):
        if synchronous not in ("FULL", "NORMAL"):
            raise ValueError("synchronous 仅支持 'FULL' 或 'NORMAL'")
        self.db_path = db_path
        self._synchronous = synchronous
        # 确保目录存在；表在 _ensure_schema 中按需创建（删库后可自动重建）
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        # 每条线程一条独立连接，进程级天然隔离 -> 满足「不跨进程共享连接」
        self._local = threading.local()
        with self._connect() as conn:
            self._ensure_schema(conn)

    # ---- 连接管理（与 TaskRegistry 完全同款）----
    def _connect(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path, timeout=30, check_same_thread=False
            )
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute(f"PRAGMA synchronous={self._synchronous};")
            self._local.conn = conn
        return conn

    def _ensure_schema(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                category      TEXT NOT NULL,
                key           TEXT NOT NULL,
                value         TEXT NOT NULL,
                note          TEXT,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                access_count  INTEGER NOT NULL DEFAULT 0,
                last_accessed REAL,
                PRIMARY KEY (category, key)
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

    # ---- 内部工具 ----
    @staticmethod
    def _row_to_dict(r):
        return {
            "category": r[0],
            "key": r[1],
            "value": json.loads(r[2]) if r[2] is not None else None,
            "note": r[3],
            "created_at": r[4],
            "updated_at": r[5],
            "access_count": r[6],
            "last_accessed": r[7],
        }

    @staticmethod
    def _dump_value(value):
        """把任意可 JSON 序列化的值落盘为文本；None 也允许（极少用）。"""
        return json.dumps(value, ensure_ascii=False)

    # ---- 核心读写 ----
    def remember(self, category, key, value, note=None, touch=True):
        """写入 / 覆盖一条记忆。已存在则原地更新 value/note/updated_at。
        value 可以是任意 JSON 可序列化对象（字符串 / 数字 / 列表 / 字典）。"""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM memory_items WHERE category=? AND key=?",
                (category, key),
            )
            exists = cur.fetchone() is not None
            dumped = self._dump_value(value)
            if exists:
                conn.execute(
                    "UPDATE memory_items SET value=?, note=?, updated_at=? "
                    "WHERE category=? AND key=?",
                    (dumped, note, now, category, key),
                )
            else:
                conn.execute(
                    "INSERT INTO memory_items "
                    "(category,key,value,note,created_at,updated_at,access_count,last_accessed) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (category, key, dumped, note, now, now, 0, None),
                )
        return value

    def recall(self, category, key, default=None, touch=True):
        """读取一条记忆；不存在返回 default。touch=True 时累加访问计数（支撑「常用」排名）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT value FROM memory_items WHERE category=? AND key=?",
                (category, key),
            )
            row = cur.fetchone()
            if row is None:
                return default
            value = json.loads(row[0]) if row[0] is not None else None
            if touch:
                now = time.time()
                conn.execute(
                    "UPDATE memory_items SET access_count=access_count+1, last_accessed=? "
                    "WHERE category=? AND key=?",
                    (now, category, key),
                )
        return value

    def get_record(self, category, key):
        """读取一条记忆的完整记录（字典）；不存在返回 None。供 API / 远程客户端使用。"""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items WHERE category=? AND key=?",
                (category, key),
            )
            row = cur.fetchone()
        return self._row_to_dict(row) if row else None

    def forget(self, category, key):
        """删除一条记忆；返回是否真的删除到了。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memory_items WHERE category=? AND key=?",
                (category, key),
            )
            removed = cur.rowcount == 1
        return removed

    # ---- 查询 ----
    def list_category(self, category, limit=1000):
        """列出某分类下的全部记忆（按更新时间倒序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items WHERE category=? ORDER BY updated_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def search(self, query, category=None, limit=50):
        """在 key / note / value 文本里做子串检索；可按分类过滤。
        跨会话「这个用户喜欢什么」这类模糊回忆时很有用。"""
        like = f"%{query}%"
        if category:
            q = (
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items WHERE category=? AND (key LIKE ? OR note LIKE ? OR value LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ?"
            )
            params = (category, like, like, like, limit)
        else:
            q = (
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items WHERE (key LIKE ? OR note LIKE ? OR value LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ?"
            )
            params = (like, like, like, limit)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def top_used(self, category=None, n=5):
        """访问频次最高的 N 条记忆（支撑「常用指令模板 / 常用 repo 路径」）。
        touch=True 的 recall 会持续累积 access_count。"""
        if category:
            q = (
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items WHERE category=? ORDER BY access_count DESC, updated_at DESC LIMIT ?"
            )
            params = (category, n)
        else:
            q = (
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items ORDER BY access_count DESC, updated_at DESC LIMIT ?"
            )
            params = (n,)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def recent(self, category=None, n=5):
        """最近更新的 N 条记忆。"""
        if category:
            q = (
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items WHERE category=? ORDER BY updated_at DESC LIMIT ?"
            )
            params = (category, n)
        else:
            q = (
                "SELECT category,key,value,note,created_at,updated_at,access_count,last_accessed "
                "FROM memory_items ORDER BY updated_at DESC LIMIT ?"
            )
            params = (n,)
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def categories(self):
        """列出所有出现过的分类。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT category FROM memory_items ORDER BY category"
            ).fetchall()
        return [r[0] for r in rows]

    def stats(self):
        """各分类的记忆条数。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) FROM memory_items GROUP BY category ORDER BY category"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ---- 语义化便捷方法（三类内置分类）----
    def remember_preference(self, key, value, note=None):
        return self.remember(CAT_PREFERENCE, key, value, note=note)

    def recall_preference(self, key, default=None):
        return self.recall(CAT_PREFERENCE, key, default=default)

    def remember_command(self, name, template, note=None):
        """template 为指令模板字符串，可用 {repo} / {branch} 之类占位符。"""
        return self.remember(CAT_COMMAND, name, template, note=note)

    def recall_command(self, name, default=None):
        return self.recall(CAT_COMMAND, name, default=default)

    def remember_repo(self, name, path, note=None):
        return self.remember(CAT_REPO, name, path, note=note)

    def recall_repo(self, name, default=None):
        return self.recall(CAT_REPO, name, default=default)

    # ---- 运维（与 TaskRegistry 同款 WAL 工具）----
    def backup(self, dest_path):
        """用 sqlite3 内置 backup API 把『主库 + 未 checkpoint 的 WAL』合并进单一目标文件。"""
        dest = sqlite3.connect(dest_path)
        try:
            self._connect().backup(dest)
            dest.commit()
        finally:
            dest.close()
        return dest_path

    def checkpoint(self, mode="PASSIVE"):
        """执行 WAL 检查点，返回 (total_frames, ckpt_frames, status) 三元组。"""
        cur = self._connect().execute(f"PRAGMA wal_checkpoint({mode})")
        return tuple(cur.fetchone())

    def seed_example(self):
        """写入一份示例记忆（用于演示 / 首次体验），覆盖写入不会报错。"""
        self.remember_preference(
            "code_review_model", "Qwen-Max", note="用户偏好的代码审查模型"
        )
        self.remember_preference(
            "preferred_lang", "Python", note="用户主力语言"
        )
        self.remember_command(
            "scan_repo", "python mcp_server.py && python examples/template_scan_demo.py --backend local --auto-approve",
            note="一键合规扫描本地仓库",
        )
        self.remember_command(
            "run_tests", "python -m unittest discover -s tests -v",
            note="跑全套回归测试",
        )
        self.remember_repo(
            "this_project", os.path.dirname(os.path.abspath(__file__)),
            note="当前 task_registry 项目根",
        )
        return self.stats()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="memory 工具 / 冒烟")
    ap.add_argument("--db-path", default=_memory_db_path())
    ap.add_argument("--synchronous", default="FULL", choices=["FULL", "NORMAL"])
    ap.add_argument("--set", nargs=3, metavar=("CATEGORY", "KEY", "VALUE"),
                    help="写入一条记忆（VALUE 会尝试按 JSON 解析，失败则存原文）")
    ap.add_argument("--get", nargs=2, metavar=("CATEGORY", "KEY"), help="读取一条记忆")
    ap.add_argument("--forget", nargs=2, metavar=("CATEGORY", "KEY"), help="删除一条记忆")
    ap.add_argument("--note", default=None, help="配合 --set 的备注")
    ap.add_argument("--search", default=None, help="子串检索关键词")
    ap.add_argument("--category", default=None, help="--search / --list 的分类过滤")
    ap.add_argument("--list", action="store_true", help="列出某分类全部（需 --category）")
    ap.add_argument("--top", type=int, default=None, help="输出访问频次 Top N")
    ap.add_argument("--stats", action="store_true", help="各分类条数")
    ap.add_argument("--seed", action="store_true", help="写入示例记忆")
    ap.add_argument("--backup-to", help="备份到目标文件（sqlite backup API，自动含 WAL）")
    ap.add_argument("--checkpoint", action="store_true", help="执行 wal_checkpoint(TRUNCATE) 后退出")
    args = ap.parse_args()

    mem = MemoryStore(db_path=args.db_path, synchronous=args.synchronous)

    if args.backup_to:
        mem.backup(args.backup_to)
        print("已备份到:", args.backup_to)
    elif args.checkpoint:
        print("wal_checkpoint(TRUNCATE):", mem.checkpoint("TRUNCATE"))
    elif args.seed:
        print("示例记忆已写入, 统计:", mem.seed_example())
    elif args.stats:
        print(json.dumps(mem.stats(), ensure_ascii=False, indent=2))
    elif args.top is not None:
        print(json.dumps(mem.top_used(args.category, args.top), ensure_ascii=False, indent=2))
    elif args.list:
        if not args.category:
            print("ERROR: --list 需要配合 --category")
        else:
            print(json.dumps(mem.list_category(args.category), ensure_ascii=False, indent=2))
    elif args.search is not None:
        print(json.dumps(mem.search(args.search, args.category), ensure_ascii=False, indent=2))
    elif args.forget:
        ok = mem.forget(args.forget[0], args.forget[1])
        print({"category": args.forget[0], "key": args.forget[1], "removed": ok})
    elif args.get:
        val = mem.recall(args.get[0], args.get[1], default=None)
        print(json.dumps({"category": args.get[0], "key": args.get[1], "value": val}, ensure_ascii=False))
    elif args.set:
        cat, key, raw = args.set
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        mem.remember(cat, key, value, note=args.note)
        print(json.dumps({"category": cat, "key": key, "value": value, "stored": True}, ensure_ascii=False))
    else:
        # 默认冒烟：写读一条
        mem.remember_preference("smoke_test", "ok")
        print("smoke_test =>", mem.recall_preference("smoke_test"))

    mem.close()
