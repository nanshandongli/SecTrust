"""
编排层 · 素材库（assets.py）—— 二进制资产的统一存储与版本管理。

用户描述里的共享工作区三件套：共享知识库（memory_hybrid）+ 任务看板
（task_center_web）+ **素材库**（本模块）。前两者管文本与状态，
这里管图/视频/文件等二进制产出——多 AI 协同里"设计 AI 出图、剪辑 AI 取片"
必须有同一个地方存放，且要能回答三个问题：

  1. 这个素材是哪来的、关联哪个任务？（元数据 + 关联）
  2. 内容重复的要不要存两份？（不要——内容寻址天然去重）
  3. 同一名字改了三版，哪版是哪版？（版本链，按内容而非文件名区分）

## 设计

- **内容寻址**：asset_id = blake2b(data) 十六进制。同内容同 id，落盘路径
  `root/ab/abcdef...` 与文件名无关——名字只是元数据，永远碰不到路径穿越。
- **版本链**：`versions(name)` 按 created_at 列同名素材的不同内容版本；
  每版带 task_id / meta / by，可追溯"谁在哪个任务里产出了哪一版"。
- **软删除**：deleted 标记 + 保留字节（审计优先）；`gc(retention_days)` 负责
  物理回收过期软删素材与孤儿 blob，删除前写 `asset_gc_log` 审计——删字节不删证据。
- **配额**：`max_bytes` 是**总额度**（活素材总和上限，put 时强校验，超限抛
  QuotaExceeded）；`max_file_bytes` 是单文件上限；`usage()` 看用量与待回收量。
- **零依赖**：标准库（sqlite3 + hashlib + pathlib + shutil）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import weakref
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_ROOT = os.path.join("data", "assets")
DEFAULT_DB = os.path.join("data", "assets.db")
DEFAULT_MAX_BYTES = 100 * 1024 * 1024        # 总额度 100 MiB（防爆盘）
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024    # 单文件上限 64 MiB

# 常见素材类型（不强制校验，仅作筛选维度）
KINDS = ("image", "video", "audio", "document", "archive", "file")


def _safe_close(conn) -> None:
    """finalize 回调：关连接；已关过或句柄失效都静默通过。"""
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


class AssetError(ValueError):
    pass


class QuotaExceeded(AssetError):
    """存储总额度超限（区别于单文件超限的 AssetError）。"""


def _digest(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()


class AssetStore:
    """内容寻址的素材库。put 去重、get 取字节、versions 看版本链。"""

    def __init__(self, root: str = DEFAULT_ROOT, db_path: str = DEFAULT_DB,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES):
        self.root = root
        self.db_path = db_path
        self.max_bytes = max_bytes            # 总额度（活素材字节总和上限）
        self.max_file_bytes = max_file_bytes  # 单文件上限
        self._local = threading.local()
        os.makedirs(root, exist_ok=True)
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._ensure_schema()

    def close(self):
        """关闭本线程的连接（`with self._conn()` 是事务上下文，不关连接）。"""
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.conn = None

    def _conn(self):
        # thread-local 缓存：`with conn` 只提供事务语义（commit/rollback），
        # 不关闭连接——不缓存的话每次调用泄漏一个 sqlite3.Connection。
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
            # 实例被回收时显式关连接：否则连接靠 GC 释放，
            # 解释器退出时会刷一屏 ResourceWarning（unclosed database）。
            weakref.finalize(self, _safe_close, c)
        return c

    def _ensure_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id    TEXT PRIMARY KEY,   -- blake2b hex
                    name        TEXT NOT NULL,      -- 显示名（可重名 = 多版本）
                    kind        TEXT NOT NULL DEFAULT 'file',
                    bytes       INTEGER NOT NULL,
                    sha256      TEXT,               -- 兼容常见指纹口径
                    task_id     TEXT,               -- 关联任务（可空）
                    meta        TEXT,               -- JSON：producer/prompt/...
                    by          TEXT,               -- 提交方
                    created_at  REAL NOT NULL,
                    deleted     INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_assets_task ON assets(task_id)")
            # GC 审计日志：物理删除了谁、为什么、何时——删字节不删证据
            c.execute("""
                CREATE TABLE IF NOT EXISTS asset_gc_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id   TEXT NOT NULL,
                    name       TEXT,
                    bytes      INTEGER,
                    reason     TEXT NOT NULL,       -- soft_deleted_expired / orphan_blob
                    gc_at      REAL NOT NULL
                )
            """)

    # ------------------------------------------------------------------
    # 存取
    # ------------------------------------------------------------------
    def _store_path(self, asset_id: str) -> str:
        # 只由内容指纹决定路径：name 永远不参与拼路径，无穿越可能
        return os.path.join(self.root, asset_id[:2], asset_id)

    def put(self, data: bytes, name: str, kind: str = "file",
            task_id: str = "", meta: dict = None, by: str = "") -> dict:
        """存入素材。同内容重复 put 只更新元数据关联，不重复占盘（去重）。"""
        if not isinstance(data, (bytes, bytearray)):
            raise AssetError("data 必须是 bytes")
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise AssetError(f"非法的素材名: {name!r}（仅接受单纯文件名）")
        data = bytes(data)
        if len(data) > self.max_file_bytes:
            raise AssetError(
                f"单文件超限: {len(data)} > {self.max_file_bytes} bytes")
        if kind not in KINDS:
            kind = "file"
        aid = _digest(data)
        path = self._store_path(aid)
        now = time.time()
        with self._conn() as c:
            row = c.execute("SELECT asset_id FROM assets WHERE asset_id=?",
                            (aid,)).fetchone()
            if row is None:
                # 总额度检查（去重 put 不重复占盘，也不重复计额度）
                live = c.execute("SELECT COALESCE(SUM(bytes),0) FROM assets "
                                 "WHERE deleted=0").fetchone()[0]
                if live + len(data) > self.max_bytes:
                    raise QuotaExceeded(
                        f"存储总额度超限: 现有 {live} + 新增 {len(data)} > "
                        f"{self.max_bytes} bytes（先 GC 或调大 max_bytes）")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)  # 原子落盘，防半截文件
            c.execute(
                "INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?,?,?)",
                (aid, os.path.basename(name), kind, len(data),
                 hashlib.sha256(data).hexdigest(), task_id or None,
                 json.dumps(meta or {}, ensure_ascii=False), by, now, 0))
        return {"asset_id": aid, "name": os.path.basename(name),
                "kind": kind, "bytes": len(data), "deduped": row is not None,
                "task_id": task_id, "created_at": now}

    def get(self, asset_id: str) -> bytes:
        with self._conn() as c:
            r = c.execute("SELECT deleted FROM assets WHERE asset_id=?",
                          (asset_id,)).fetchone()
        if r is None:
            raise AssetError(f"素材不存在: {asset_id}")
        if r[0]:
            raise AssetError(f"素材已删除: {asset_id}")
        path = self._store_path(asset_id)
        if not os.path.exists(path):
            raise AssetError(f"元数据在、字节不在（存储被外部清掉?）: {asset_id}")
        with open(path, "rb") as f:
            return f.read()

    def info(self, asset_id: str) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute(
                "SELECT asset_id, name, kind, bytes, sha256, task_id, meta, "
                "by, created_at, deleted FROM assets WHERE asset_id=?",
                (asset_id,)).fetchone()
        if r is None:
            return None
        return {"asset_id": r[0], "name": r[1], "kind": r[2], "bytes": r[3],
                "sha256": r[4], "task_id": r[5], "meta": json.loads(r[6] or "{}"),
                "by": r[7], "created_at": r[8], "deleted": bool(r[9]),
                "path": self._store_path(r[0])}

    def list(self, task_id: str = None, kind: str = None,
             limit: int = 100) -> list[dict]:
        q = ("SELECT asset_id, name, kind, bytes, task_id, by, created_at "
             "FROM assets WHERE deleted=0")
        params = []
        if task_id:
            q += " AND task_id=?"
            params.append(task_id)
        if kind:
            q += " AND kind=?"
            params.append(kind)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(q, params).fetchall()
        return [{"asset_id": r[0], "name": r[1], "kind": r[2], "bytes": r[3],
                 "task_id": r[4], "by": r[5], "created_at": r[6]} for r in rows]

    def versions(self, name: str) -> list[dict]:
        """同名素材的版本链（不同内容 = 不同版本），按时间正序。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT asset_id, kind, bytes, task_id, by, created_at, deleted "
                "FROM assets WHERE name=? ORDER BY created_at ASC",
                (os.path.basename(name),)).fetchall()
        return [{"asset_id": r[0], "kind": r[1], "bytes": r[2],
                 "task_id": r[3], "by": r[4], "created_at": r[5],
                 "deleted": bool(r[6]), "version": i + 1}
                for i, r in enumerate(rows)]

    def link(self, asset_id: str, task_id: str) -> bool:
        """把素材关联到任务（设计 AI 出图后挂到 p1/design_brief 上）。"""
        with self._conn() as c:
            cur = c.execute("UPDATE assets SET task_id=? WHERE asset_id=?",
                            (task_id, asset_id))
            return cur.rowcount == 1

    def delete(self, asset_id: str) -> bool:
        """软删除：标记 + 保留字节（审计优先，不做物理删除）。"""
        with self._conn() as c:
            cur = c.execute("UPDATE assets SET deleted=1 WHERE asset_id=? "
                            "AND deleted=0", (asset_id,))
            return cur.rowcount == 1

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) "
                              "FROM assets WHERE deleted=0").fetchone()
            by_kind = c.execute(
                "SELECT kind, COUNT(*) FROM assets WHERE deleted=0 "
                "GROUP BY kind").fetchall()
        return {"assets": total[0], "bytes": total[1],
                "by_kind": {k: n for k, n in by_kind}}

    # ------------------------------------------------------------------
    # 配额与回收（GC）
    # ------------------------------------------------------------------
    def usage(self) -> dict:
        """存储用量：活素材数/字节/总额度/使用率/待回收量（软删 + 孤儿预估）。"""
        with self._conn() as c:
            live = c.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) "
                             "FROM assets WHERE deleted=0").fetchone()
            soft = c.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) "
                             "FROM assets WHERE deleted=1").fetchone()
        used = live[1] or 0
        return {
            "assets": live[0], "bytes": used,
            "max_bytes": self.max_bytes,
            "utilization": round(used / self.max_bytes, 4) if self.max_bytes else None,
            "soft_deleted_assets": soft[0], "soft_deleted_bytes": soft[1] or 0,
            "reclaimable_bytes": soft[1] or 0,  # + 孤儿 blob（gc 时才算）
        }

    def _orphan_blobs(self, c) -> list[str]:
        """root 下存在但数据库无记录的 blob（崩溃的半截 tmp 或外部塞入的文件）。"""
        known = {r[0] for r in c.execute("SELECT asset_id FROM assets")}
        orphans = []
        if not os.path.isdir(self.root):
            return orphans
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                stem = fn[:-4] if fn.endswith(".tmp") else fn
                if stem not in known:
                    orphans.append(full)
        return orphans

    def gc(self, retention_days: float = 7.0, include_orphans: bool = True,
           dry_run: bool = False) -> dict:
        """物理回收：软删超过保留期的素材 + 孤儿 blob。

        审计优先：删除前进 asset_gc_log 留一行（asset_id/name/bytes/原因/时间），
        删字节不删证据。活素材（deleted=0）永远不动。
        """
        cutoff = time.time() - retention_days * 86400
        removed, freed = [], 0
        with self._conn() as c:
            expired = c.execute(
                "SELECT asset_id, name, bytes FROM assets "
                "WHERE deleted=1 AND created_at<?", (cutoff,)).fetchall()
            orphans = self._orphan_blobs(c) if include_orphans else []
            if dry_run:
                for aid, name, b in expired:
                    removed.append({"asset_id": aid, "name": name, "bytes": b,
                                    "reason": "soft_deleted_expired"})
                    freed += b or 0
                for full in orphans:
                    try:
                        sz = os.path.getsize(full)
                    except OSError:
                        sz = 0
                    removed.append({"asset_id": os.path.basename(full),
                                    "name": None, "bytes": sz,
                                    "reason": "orphan_blob"})
                    freed += sz
                return {"dry_run": True, "removed": removed, "freed_bytes": freed,
                        "count": len(removed)}

            for aid, name, b in expired:
                path = self._store_path(aid)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        continue  # 文件删不掉就保留记录，下轮再试
                c.execute("DELETE FROM assets WHERE asset_id=?", (aid,))
                c.execute(
                    "INSERT INTO asset_gc_log(asset_id, name, bytes, reason, gc_at) "
                    "VALUES (?,?,?,?,?)",
                    (aid, name, b, "soft_deleted_expired", time.time()))
                removed.append({"asset_id": aid, "name": name, "bytes": b,
                                "reason": "soft_deleted_expired"})
                freed += b or 0
            for full in orphans:
                try:
                    sz = os.path.getsize(full)
                    os.remove(full)
                except OSError:
                    continue
                c.execute(
                    "INSERT INTO asset_gc_log(asset_id, name, bytes, reason, gc_at) "
                    "VALUES (?,?,?,?,?)",
                    (os.path.basename(full), None, sz, "orphan_blob", time.time()))
                removed.append({"asset_id": os.path.basename(full), "name": None,
                                "bytes": sz, "reason": "orphan_blob"})
                freed += sz
        return {"dry_run": False, "removed": removed, "freed_bytes": freed,
                "count": len(removed)}

    def gc_log(self, limit: int = 50) -> list[dict]:
        """GC 审计日志（删字节不删证据）。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT asset_id, name, bytes, reason, gc_at FROM asset_gc_log "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"asset_id": r[0], "name": r[1], "bytes": r[2],
                 "reason": r[3], "gc_at": r[4]} for r in rows]


if __name__ == "__main__":
    import tempfile

    root = tempfile.mkdtemp(prefix="assets_demo_")
    store = AssetStore(root=root, db_path=os.path.join(root, "a.db"))
    # 设计 AI 出了两版主视觉 + 一版文案图（内容不同 = 版本链）
    v1 = store.put(b"POSTER-BYTES-v1-red", "主视觉.png", kind="image",
                   task_id="p1/design_brief", meta={"producer": "design-ai",
                                                    "prompt": "双十一红金"},
                   by="design-ai")
    v2 = store.put(b"POSTER-BYTES-v2-blue", "主视觉.png", kind="image",
                   task_id="p1/design_brief", by="design-ai")
    dup = store.put(b"POSTER-BYTES-v1-red", "主视觉.png", kind="image",
                    task_id="p1/assembly", by="design-ai")
    doc = store.put("# 投放排期\n...".encode(), "排期.md", kind="document",
                    task_id="p1/roi", by="strategy-ai")
    print("v1:", v1["asset_id"][:12], "| v2 去重?", v2["deduped"],
          "| 重复 put 去重?", dup["deduped"], "| 同 id?", dup["asset_id"] == v1["asset_id"])
    print("版本链:", [(x["version"], x["bytes"], x["task_id"]) for x in store.versions("主视觉.png")])
    print("get 往返一致:", store.get(v1["asset_id"]) == b"POSTER-BYTES-v1-red")
    print("按任务列:", [a["name"] for a in store.list(task_id="p1/roi")])
    print("stats:", store.stats())
    print("软删:", store.delete(v2["asset_id"]), "| 删后 get 抛错?",
          end=" ")
    try:
        store.get(v2["asset_id"])
        print("否（BUG）")
    except AssetError:
        print("是")
