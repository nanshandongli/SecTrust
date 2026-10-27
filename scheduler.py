"""
scheduler.py —— 定时/周期调度器（Grok Bot "Routine 定时执行"的本地最小版）。

职责：把"每隔 N 秒"或"每天 9 点（cron）"这类时间规则，变成向
TaskRegistry 登记的真实任务。调度器自己**不执行**任务——执行归
execution daemon / AgentRunner，边界清晰。

支持两种规则：
  - interval：每 N 秒（简单周期，适合演示/轮询类任务）；
  - cron：标准五段式 `分 时 日 月 周`（*、*/n、a、a-b、a-b/n、列表），
    纯标准库实现，按分钟步进扫描（上限 366 天，找不到返回 None 明说）。

纪律：
- 连接管理遵循项目既有模式：thread-local 缓存 + 显式 close()，
  **不用 `with sqlite3.connect()`**（那是事务上下文不是关闭，会泄漏）；
- last_run / next_run 落库，重启不重复触发（fire_due 只认 next_run <= now）；
- 到期即登记任务并推进 next_run；登记失败（如 task_id 冲突）不静默吞——
  返回错误信息，last_run 不推进，下次重试。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from task_registry import TaskRegistry

DEFAULT_DB = os.path.join("data", "scheduler.db")

_CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),   # 0=周一 … 6=周日（与 cron 的 0=周日不同，见 _weekday_ok）
)

# cron 语义的星期：0=周日。我们内部用 0=周一，转换一次。
_CRON_WD = {"minute": None, "hour": None, "day": None, "month": None, "weekday": "sun0"}


class CronError(ValueError):
    """cron 表达式非法。"""


def _parse_field(spec: str, lo: int, hi: int, name: str) -> frozenset:
    """解析单个 cron 字段为允许值集合。"""
    values = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError("cron 字段 %s 有空片段: %r" % (name, spec))
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise CronError("cron 步长非整数: %r" % part)
            if step < 1:
                raise CronError("cron 步长必须 ≥1: %r" % part)
            part = base
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise CronError("cron 区间非整数: %r" % part)
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise CronError("cron 值非整数: %r" % part)
        if start < lo or end > hi or start > end:
            raise CronError("cron 值越界或区间反向: %r（允许 %d-%d）" % (part, lo, hi))
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError("cron 字段解析为空: %r" % spec)
    return frozenset(values)


def parse_cron(expr: str) -> dict:
    """解析五段式 cron。返回 {字段名: frozenset(允许值)}。

    星期按 cron 习惯 0/7=周日；内部统一转成 0=周一 的集合。
    """
    if not isinstance(expr, str):
        raise CronError("cron 表达式必须是字符串")
    parts = expr.split()
    if len(parts) != 5:
        raise CronError("cron 必须五段（分 时 日 月 周）: %r" % expr)
    out = {}
    for (name, lo, hi), spec in zip(_CRON_FIELDS, parts):
        # cron 星期字段特殊：0-7 均合法（0 和 7 都是周日）
        if name == "weekday":
            lo, hi = 0, 7
        vals = _parse_field(spec, lo, hi, name)
        if name == "weekday":
            # cron: 0=周日,7=周日；内部: 0=周一
            conv = set()
            for v in vals:
                v = 0 if v == 7 else v
                conv.add((v + 6) % 7)   # 周日0 → 6；周一1 → 0
            out[name] = frozenset(conv)
        else:
            out[name] = vals
    return out


def _weekday_ok(fields: dict, tm) -> bool:
    # cron 通配语义：日与周都是 * 时按"与"；否则任一命中即命中（Vixie cron 规则）
    return tm.tm_wday in fields["weekday"]


def next_after(fields: dict, after_ts: float, horizon_days: int = 366):
    """fields 之后最近一次命中时间（epoch 秒）。找不到返回 None。"""
    # 从下一分钟整点开始按分钟步进
    t = time.localtime(after_ts)
    cur = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, 0,
                      0, 0, -1)) + 60
    limit = after_ts + horizon_days * 86400
    day_star = len(fields["day"]) == 31 and len(fields["month"]) == 12
    wd_star = len(fields["weekday"]) == 7
    while cur <= limit:
        tm = time.localtime(cur)
        if (tm.tm_min in fields["minute"] and tm.tm_hour in fields["hour"]
                and tm.tm_mon in fields["month"]):
            day_ok = tm.tm_mday in fields["day"]
            wd_ok = tm.tm_wday in fields["weekday"]
            if day_star and wd_star:
                hit = True
            elif day_star:
                hit = wd_ok
            elif wd_star:
                hit = day_ok
            else:
                hit = day_ok or wd_ok     # Vixie cron：日月均非*时取或
            if hit:
                return cur
        cur += 60
    return None


class Scheduler:
    """调度器：规则落库，到期向 TaskRegistry 登记任务。"""

    def __init__(self, db_path: str = None, registry: TaskRegistry = None):
        self.db_path = db_path or os.environ.get("SECTRUST_SCHEDULER_DB", DEFAULT_DB)
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._own_registry = registry is None
        self.registry = registry or TaskRegistry(db_path=_sibling(self.db_path))
        self._local = threading.local()
        self._ensure_schema()

    # ---- 连接：thread-local 缓存 + 显式 close（项目纪律）----
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
        # 自己创建的 registry 也要关（TaskRegistry 有 thread-local 连接，
        # 不关会泄漏——这正是本项目反复踩过的坑）
        if self._own_registry:
            try:
                self.registry.close()
            except Exception:
                pass

    def _ensure_schema(self):
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                " job_id TEXT PRIMARY KEY,"
                " name TEXT NOT NULL DEFAULT '',"
                " kind TEXT NOT NULL,"                 # cron | interval
                " spec TEXT NOT NULL,"                 # cron 表达式或秒数
                " payload TEXT NOT NULL DEFAULT '{}',"
                " enabled INTEGER NOT NULL DEFAULT 1,"
                " last_run REAL,"
                " next_run REAL NOT NULL,"
                " created_at REAL NOT NULL)"
            )

    # ---- 规则管理 ----
    def add_job(self, job_id: str, cron: str = None, interval: float = None,
                payload: dict = None, name: str = "") -> dict:
        """新增/覆盖（幂等 INSERT OR REPLACE）一个调度规则。"""
        if bool(cron) == bool(interval):
            raise CronError("必须且只能给出 cron 或 interval 之一")
        now = time.time()
        if cron:
            fields = parse_cron(cron)
            kind, spec = "cron", cron
            nxt = next_after(fields, now)
            if nxt is None:
                raise CronError("cron %r 在 %d 天内无命中时间" % (cron, 366))
        else:
            if interval <= 0:
                raise CronError("interval 必须 > 0")
            kind, spec = "interval", repr(float(interval))
            nxt = now + float(interval)
        job = {
            "job_id": job_id, "name": name or job_id, "kind": kind, "spec": spec,
            "payload": payload or {}, "enabled": 1,
            "last_run": None, "next_run": nxt, "created_at": now,
        }
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs"
                " (job_id,name,kind,spec,payload,enabled,last_run,next_run,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, job["name"], kind, spec,
                 json.dumps(job["payload"], ensure_ascii=False), 1,
                 None, nxt, now),
            )
        return job

    def remove_job(self, job_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            return cur.rowcount == 1

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._conn() as conn:
            cur = conn.execute("UPDATE jobs SET enabled=? WHERE job_id=?",
                               (1 if enabled else 0, job_id))
            return cur.rowcount == 1

    def list_jobs(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT job_id,name,kind,spec,payload,enabled,last_run,next_run,"
                "created_at FROM jobs ORDER BY created_at").fetchall()
        out = []
        for r in rows:
            out.append({
                "job_id": r[0], "name": r[1], "kind": r[2], "spec": r[3],
                "payload": json.loads(r[4]), "enabled": bool(r[5]),
                "last_run": r[6], "next_run": r[7], "created_at": r[8],
            })
        return out

    # ---- 到期判定与触发 ----
    def due_jobs(self, now: float = None) -> list:
        now = now if now is not None else time.time()
        return [j for j in self.list_jobs() if j["enabled"] and j["next_run"] <= now]

    def _advance(self, job: dict, now: float) -> float:
        """推进 next_run；返回新值。"""
        if job["kind"] == "interval":
            nxt = now + float(job["spec"])
        else:
            nxt = next_after(parse_cron(job["spec"]), now)
            if nxt is None:               # 理论上 add 时已校验；防御
                nxt = now + 86400
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET last_run=?, next_run=? WHERE job_id=?",
                         (now, nxt, job["job_id"]))
        return nxt

    def fire_due(self, now: float = None, executor=None) -> list:
        """触发所有到期任务。

        两种模式（向后兼容）：
          - executor 为 None（默认）：把到期任务登记进 TaskRegistry，返回 [(job_id, task_id)]；
          - executor 为一可调用对象 executor(job, payload, task_id) -> dict：调用它**真实执行**
            动作（如自扫/成本快照/结案），返回的执行结果随 fired 一并返回：[(job_id, result)]。

        无论哪种模式，执行失败都**不推进** last_run/next_run（下次重试）；
        错误通过返回值体现。
        """
        now = now if now is not None else time.time()
        fired = []
        for job in self.due_jobs(now):
            task_id = "%s-%d" % (job["job_id"], int(now * 1000))
            payload = dict(job["payload"])
            payload["scheduled_from"] = job["job_id"]
            if executor is not None:
                try:
                    result = executor(job, payload, task_id)
                    self._advance(job, now)
                    fired.append((job["job_id"], result))
                except Exception as e:  # noqa: BLE001
                    fired.append((job["job_id"], {"ok": False, "error": str(e)}))
                continue
            # 兼容旧行为：登记任务
            try:
                self.registry.register_task(task_id, payload=payload)
            except Exception as e:
                fired.append((job["job_id"], "ERROR: %s" % e))
                continue                    # 不推进，下次重试
            self._advance(job, now)
            fired.append((job["job_id"], task_id))
        return fired


def _sibling(db_path: str) -> str:
    """scheduler.db 旁的 task_registry.db（同目录约定）。"""
    d = os.path.dirname(os.path.abspath(db_path))
    for cand in ("task_registry.db", "tasks.db"):
        p = os.path.join(d, cand)
        if os.path.exists(p):
            return p
    return os.path.join(d, "task_registry.db")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="调度器 CLI")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fire", action="store_true", help="立即触发一次到期任务")
    args = ap.parse_args()
    s = Scheduler(db_path=args.db)
    if args.list:
        for j in s.list_jobs():
            print("%-20s %-9s %-12s next=%s enabled=%s"
                  % (j["job_id"], j["kind"], j["spec"],
                     time.strftime("%H:%M:%S", time.localtime(j["next_run"])), j["enabled"]))
    if args.fire:
        for job_id, tid in s.fire_due():
            print("fired %s -> %s" % (job_id, tid))
    s.close()
