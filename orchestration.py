"""
编排层 · DAG 调度器（orchestration.py）

这是"多 AI 同事协同"从**底座**走向**应用**的第一块砖：
task_registry 提供"共享任务表 + CAS 认领"（单个任务怎么被领走），
本模块提供"任务之间怎么排队"——依赖、就绪、并行波次、失败级联。

设计原则：
- **零新依赖**：只用标准库 + task_registry 的共享 SQLite；不引入消息中间件。
- **向后兼容**：tasks 表只新增 depends_on 列（见 task_registry 的迁移逻辑），
  不使用 DAG 的旧任务一切行为不变。
- **就绪性是算出来的，不是存出来的**：ready = status='registered' 且所有依赖
  都已 completed。依赖只会从"未完成"变"完成"，不会反向变化，
  因此"先算就绪集、再 CAS 认领"是安全的（认领的原子性由 UPDATE 影响行数保证）。
- **失败级联**：上游 failed => 所有传递下游 registered -> blocked（永不可执行），
  已 running 的下游不受影响（正常情况下不可能出现：它当年领不到这个任务）。

状态机：
    registered --claim--> running --complete--> completed
                              +---------------> failed
    registered --(上游失败)-------> blocked

与 transport.Broker 的关系：任务 completed/failed/blocked 时发布事件（可选，
MQ_DB_PATH 未设置或 Broker 不可用时自动静默跳过）——事件只做"实时观战/通知"，
绝不参与关键路径，失败不影响调度正确性。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from task_registry import TaskRegistry, DEFAULT_DB_PATH  # noqa: E402

try:
    from transport.broker import Broker  # 复用底座事件总线（可选）
except Exception:  # 单独运行 / Broker 不可用时降级为无事件
    Broker = None

try:
    from trace import trace_id_of  # 轨迹层：trace_id 只走 payload 约定
except Exception:  # 单独运行时降级：无 trace 模块就不记轨迹
    trace_id_of = None

# 调度器使用的状态常量（与 task_registry 默认值对齐）
REGISTERED = "registered"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
BLOCKED = "blocked"


class DagError(ValueError):
    """DAG 结构错误（重复任务 / 未知依赖 / 成环）的基类。"""


class DagScheduler:
    """在共享 SQLite 任务表之上的依赖调度器。

    用法（串行链）：
        s = DagScheduler(db_path)
        s.add_task("a"); s.add_task("b", depends_on=["a"]); s.add_task("c", depends_on=["b"])
        t = s.claim_ready(runner_id="worker-1")   # 只会领到 a
        s.complete("a", result={...})             # b 就此就绪
        t = s.claim_ready(runner_id="worker-2")   # 领到 b
    """

    def __init__(self, db_path=DEFAULT_DB_PATH, registry: TaskRegistry | None = None,
                 broker: Broker | None = None, events: bool = True,
                 tracer=None, cost=None):
        self.registry = registry if registry is not None else TaskRegistry(db_path=db_path)
        self.db_path = self.registry.db_path
        # tracer：可选的 TraceStore。记轨迹是**旁路**——缺失或异常都不挡调度主链路
        self.tracer = tracer
        # cost：可选的 CostStore。成本打点同样是旁路——记账失败绝不挡任务完成
        self.cost = cost
        # events=False：只读视图（如 Web 看板）不建事件总线，避免每请求开队列句柄
        if broker is not None:
            self.broker = broker
        else:
            self.broker = self._default_broker() if events else None

    # ------------------------------------------------------------------
    # 轨迹旁路：trace_event（best-effort，绝不挡主链路）
    # ------------------------------------------------------------------
    def _trace_id_of(self, task_id: str):
        """从任务 payload 取 trace_id（读失败/未标记 → None，跳过留痕）。"""
        if trace_id_of is None:
            return None
        try:
            t = self.registry.get_task(task_id)
            return trace_id_of(t.get("payload") if t else None)
        except Exception:  # noqa: BLE001
            return None

    def trace_event(self, kind: str, task_id: str = None, trace_id: str = None,
                    **kw):
        """记一条轨迹事件。tracer 未注入或留痕失败都静默跳过——
        留痕是观测，不是调度前置条件。"""
        if self.tracer is None:
            return
        try:
            tid = trace_id
            if tid is None and task_id:
                tid = self._trace_id_of(task_id)
            if tid:
                self.tracer.record(tid, kind, task_id=task_id, **kw)
        except Exception:  # noqa: BLE001 —— 旁路故障不上抛
            pass

    # ------------------------------------------------------------------
    # 成本旁路：finalize_cost（best-effort，绝不挡调度主链路）
    # ------------------------------------------------------------------
    def _attribution(self, payload: dict) -> tuple:
        """从 payload 取归因三元组 (role, template, tier)——缺则空串。"""
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        return (str(payload.get("skill") or ""),
                str(plan.get("template") or ""),
                str(plan.get("tier") or ""))

    def finalize_cost(self, task_id: str, payload: dict = None,
                      outcome: str = "") -> dict:
        """任务结束：聚合该任务的成本事件成快照，并补记 cost_snapshot 轨迹事件。

        返回快照（cost 未注入时返回 {}）。与 trace_event 同语义：
        记账是观测，不是完成的前置条件。
        """
        if self.cost is None:
            return {}
        try:
            payload = payload if isinstance(payload, dict) else {}
            tid = trace_id_of(payload) if trace_id_of is not None else None
            role, template, tier = self._attribution(payload)
            snap = self.cost.finalize_task(task_id, trace_id=tid, role=role,
                                           template=template, tier=tier,
                                           outcome=outcome)
            # 成本快照也进轨迹（数字非敏感；detail 有界由 cost 层保证）
            self.trace_event("cost_snapshot", task_id=task_id, trace_id=tid,
                             summary=f"成本快照 {task_id}",
                             detail={k: snap.get(k) for k in
                                     ("model_calls", "input_tokens",
                                      "output_tokens", "tool_calls", "retries",
                                      "sandbox_seconds", "cost_estimate",
                                      "unpriced_calls")})
            return snap
        except Exception:  # noqa: BLE001 —— 旁路故障不上抛
            return {}

    def record_retry_cost(self, task_id: str, reason: str = "") -> None:
        """记一次重试/打回成本（best-effort）。"""
        if self.cost is None:
            return
        try:
            tid = None
            try:
                t = self.registry.get_task(task_id)
                tid = trace_id_of(t.get("payload") if t else None) \
                    if trace_id_of is not None else None
            except Exception:  # noqa: BLE001
                pass
            self.cost.record_retry(task_id=task_id, trace_id=tid, reason=reason)
        except Exception:  # noqa: BLE001 —— 旁路故障不上抛
            pass

    def _default_broker(self):
        """有 MQ_DB_PATH 才启用事件总线（与 player.py 同约定）；否则 None。"""
        if Broker is None or not os.environ.get("MQ_DB_PATH"):
            return None
        try:
            return Broker()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 建图：add_task（结构校验）
    # ------------------------------------------------------------------
    def add_task(self, task_id, payload=None, depends_on=()):
        """登记一个带依赖的任务。依赖必须**已经存在**（先添上游再添下游）。
        拒绝：重复 task_id / 未知依赖 / 成环（含自依赖）。"""
        deps = list(depends_on) if depends_on else []
        if not task_id:
            raise DagError("task_id 不能为空")
        if task_id in deps:
            raise DagError(f"自依赖不被允许: {task_id}")
        if self.registry.get_task(task_id) is not None:
            raise DagError(f"任务已存在: {task_id}（DAG 建图要求先添上游再添下游）")
        for d in deps:
            if self.registry.get_task(d) is None:
                raise DagError(f"未知依赖: {d}（尚未登记；请先添加上游任务）")
        # 环检测：从每个上游沿 depends_on 向上走，若能回到 task_id 即成环
        graph = self.graph()
        for d in deps:
            seen, stack = set(), [d]
            while stack:
                cur = stack.pop()
                if cur == task_id:
                    raise DagError(f"成环: {task_id} -> {d} -> ... -> {task_id}")
                if cur in seen:
                    continue
                seen.add(cur)
                stack.extend(graph.get(cur, []))
        self.registry.register_task(task_id, payload=payload, depends_on=deps)
        self.trace_event("task_registered", task_id=task_id,
                         summary=f"登记 {task_id}",
                         detail={"depends_on": list(deps)})
        return task_id

    # ------------------------------------------------------------------
    # 查询：就绪集 / 等待原因 / 波次 / 全图
    # ------------------------------------------------------------------
    def _all(self):
        return self.registry.list_tasks(limit=100000)

    def ready_tasks(self):
        """当前可被认领的任务：registered 且所有依赖已 completed。按登记先后（FIFO）排序。"""
        tasks = self._all()
        status = {t["task_id"]: t["status"] for t in tasks}
        out = []
        for t in tasks:
            if t["status"] != REGISTERED:
                continue
            if all(status.get(d) == COMPLETED for d in t["depends_on"]):
                out.append(t)
        out.sort(key=lambda t: t["created_at"])  # 与 claim_ready 的挑选顺序一致
        return out

    def waiting_on(self, task_id):
        """该任务还在等哪些上游（未 completed 的依赖列表）。"""
        t = self.registry.get_task(task_id)
        if t is None:
            return []
        status = {x["task_id"]: x["status"] for x in self._all()}
        return [d for d in t["depends_on"] if status.get(d) != COMPLETED]

    def graph(self):
        """{task_id: [依赖...]} 全图快照。"""
        return {t["task_id"]: list(t["depends_on"]) for t in self._all()}

    def waves(self):
        """并行波次分层：第 0 波 = 无依赖任务；第 n 波 = 依赖全部落在前 n 波的任务。
        同一波内的任务互不依赖，可并行执行——这就是"并行+串行"的调度视图。"""
        g = self.graph()
        depth: dict[str, int] = {}

        def d(tid, guard):
            if tid in depth:
                return depth[tid]
            if tid in guard:  # 理论上 add_task 已挡环；防御性返回
                return 0
            guard.add(tid)
            deps = g.get(tid, [])
            depth[tid] = 0 if not deps else 1 + max(d(x, guard) for x in deps)
            guard.discard(tid)
            return depth[tid]

        for tid in g:
            d(tid, set())
        if not depth:
            return []
        out = [[] for _ in range(max(depth.values()) + 1)]
        for tid, w in depth.items():
            out[w].append(tid)
        return [sorted(w) for w in out]

    def stats(self):
        """按状态计数 + 就绪数（供看板/自检）。"""
        tasks = self._all()
        by = {}
        for t in tasks:
            by[t["status"]] = by.get(t["status"], 0) + 1
        return {"total": len(tasks), "by_status": by,
                "ready": len(self.ready_tasks())}

    # ------------------------------------------------------------------
    # 认领：claim_ready（BEGIN IMMEDIATE 内"选最老就绪任务 + CAS"，防双抢）
    # ------------------------------------------------------------------
    def claim_ready(self, task_id=None, runner_id=None):
        """认领一个就绪任务（registered -> running）。
        - task_id 指定：只尝试该任务（仍要求其就绪）；
        - 不指定：在就绪集里取登记最早的一个。
        整个"选+认领"在一个写事务内完成，两个 worker 并发调用不会拿到同一个任务。
        无就绪任务返回 None。"""
        rid = runner_id or self.registry.runner_id
        now = time.time()
        with self.registry._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # 写锁：与 claim_task 同一串行化保证
            try:
                rows = conn.execute(
                    "SELECT task_id, status, depends_on FROM tasks "
                    "WHERE status=? ORDER BY created_at ASC",
                    (REGISTERED,),
                ).fetchall()
                status = {r[0]: r[1] for r in conn.execute(
                    "SELECT task_id, status FROM tasks").fetchall()}

                def _deps_ok(deps_j):
                    deps = json.loads(deps_j) if deps_j else []
                    return all(status.get(d) == COMPLETED for d in deps)

                picked = None
                if task_id is not None:
                    for r in rows:
                        if r[0] == task_id:
                            if _deps_ok(r[2]):
                                picked = task_id
                            break
                else:
                    for r in rows:
                        if _deps_ok(r[2]):
                            picked = r[0]
                            break
                if picked is None:
                    conn.execute("COMMIT")
                    return None
                cur = conn.execute(
                    "UPDATE tasks SET status=?, runner_id=?, updated_at=? "
                    "WHERE task_id=? AND status=?",
                    (RUNNING, rid, now, picked, REGISTERED),
                )
                if cur.rowcount != 1:  # 理论上写锁内不会发生；防御性保留
                    conn.execute("COMMIT")
                    return None
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        task = self.registry.get_task(picked)
        tid = trace_id_of(task.get("payload")) if (task and trace_id_of) else None
        self.trace_event("task_claimed", task_id=picked, actor=rid,
                         trace_id=tid, summary=f"{rid} 认领 {picked}")
        return task

    # ------------------------------------------------------------------
    # 完成 / 失败：complete / fail（含级联）
    # ------------------------------------------------------------------
    def complete(self, task_id, result=None):
        """标记任务完成（running/registered -> completed），可附带 result 存入 payload。
        前置条件：该任务所有依赖已 completed（否则拒绝——保证图的一致性）。
        幂等：重复调用返回 False。完成后发布 Broker 事件。"""
        now = time.time()
        t = self.registry.get_task(task_id)
        if t is None or t["status"] not in (REGISTERED, RUNNING):
            return False
        if self.waiting_on(task_id):
            return False  # 上游未完成，不允许完成
        payload = t["payload"] if isinstance(t["payload"], dict) else {}
        if result is not None:
            payload["result"] = result
        # 清掉上一轮的失败原因：重试成功后还挂着旧 error，会让人以为这次也失败了
        payload.pop("error", None)
        payload.pop("failed_at", None)
        payload["completed_at"] = now
        with self.registry._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status=?, payload=?, updated_at=? "
                "WHERE task_id=? AND status IN (?, ?)",
                (COMPLETED, json.dumps(payload, ensure_ascii=False), now,
                 task_id, REGISTERED, RUNNING),
            )
            ok = cur.rowcount == 1
        if ok:
            self._emit("task", "completed",
                       {"task_id": task_id, "result": result})
            self.trace_event("task_completed", task_id=task_id,
                             summary=f"完成 {task_id}",
                             detail={"result_keys": sorted(result)
                                     if isinstance(result, dict) else None},
                             content=json.dumps(result, ensure_ascii=False,
                                                default=str)
                             if result is not None else None)
            # 成本收尾：聚合本任务全部成本事件成快照（旁路，失败静默）
            self.finalize_cost(task_id, payload=payload, outcome="completed")
        return ok

    def fail(self, task_id, error=None):
        """标记任务失败（running -> failed），并级联阻塞所有传递下游。
        返回被新阻塞的 task_id 列表（已 blocked/running/completed 的不动）。"""
        now = time.time()
        t = self.registry.get_task(task_id)
        if t is None or t["status"] != RUNNING:
            return []
        payload = t["payload"] if isinstance(t["payload"], dict) else {}
        payload["error"] = str(error) if error is not None else "failed"
        payload["failed_at"] = now
        blocked_now = []
        with self.registry._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    "UPDATE tasks SET status=?, payload=?, updated_at=? "
                    "WHERE task_id=? AND status=?",
                    (FAILED, json.dumps(payload, ensure_ascii=False), now,
                     task_id, RUNNING),
                )
                if cur.rowcount != 1:
                    conn.execute("COMMIT")
                    return []
                # 级联：沿反向依赖 BFS，把仍 registered 的下游置为 blocked
                rows = conn.execute(
                    "SELECT task_id, status, depends_on FROM tasks").fetchall()
                dependents = {r[0]: [] for r in rows}
                for r in rows:
                    for d in (json.loads(r[2]) if r[2] else []):
                        dependents.setdefault(d, []).append(r[0])
                queue = [task_id]
                seen = {task_id}
                while queue:
                    cur_tid = queue.pop(0)
                    for down in dependents.get(cur_tid, []):
                        if down in seen:
                            continue
                        seen.add(down)
                        row = conn.execute(
                            "SELECT status FROM tasks WHERE task_id=?",
                            (down,)).fetchone()
                        if row and row[0] == REGISTERED:
                            conn.execute(
                                "UPDATE tasks SET status=?, updated_at=? "
                                "WHERE task_id=? AND status=?",
                                (BLOCKED, now, down, REGISTERED),
                            )
                            blocked_now.append(down)
                            queue.append(down)  # 继续向下游传播
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._emit("task", "failed",
                   {"task_id": task_id, "error": str(error) if error else None,
                    "blocked": blocked_now})
        self.trace_event("task_failed", task_id=task_id,
                         summary=f"失败 {task_id}",
                         detail={"error": str(error) if error is not None else "failed",
                                 "blocked_downstream": blocked_now})
        # 成本收尾：失败的一次尝试也是成本（记一笔重试 + 聚合快照）
        self.record_retry_cost(task_id, reason=str(error) if error else "failed")
        self.finalize_cost(task_id, payload=payload, outcome="failed")
        return blocked_now

    # ------------------------------------------------------------------
    # 事件（可选，失败静默）
    # ------------------------------------------------------------------
    def _emit(self, topic, event_type, data):
        if self.broker is None:
            return
        try:
            self.broker.publish_event(topic, event_type, data)
        except Exception:
            pass  # 观战/通知类事件绝不拖垮调度关键路径

    def close(self):
        self.registry.close()


if __name__ == "__main__":
    import tempfile

    db = os.path.join(tempfile.mkdtemp(prefix="dag_demo_"), "dag.db")
    s = DagScheduler(db)
    # 菱形：a -> {b, c} -> d
    s.add_task("a", payload={"name": "拉取数据"})
    s.add_task("b", payload={"name": "文案初稿"}, depends_on=["a"])
    s.add_task("c", payload={"name": "分镜设计"}, depends_on=["a"])
    s.add_task("d", payload={"name": "合成"}, depends_on=["b", "c"])
    print("波次（同波可并行）:", s.waves())
    print("初始就绪:", [t["task_id"] for t in s.ready_tasks()])
    t = s.claim_ready(runner_id="worker-1")
    print("worker-1 领到:", t["task_id"] if t else None)
    print("complete a:", s.complete("a", result={"rows": 100}))
    print("a 后就绪:", [x["task_id"] for x in s.ready_tasks()])
    print("d 在等:", s.waiting_on("d"))
    s.claim_ready(runner_id="worker-2"); s.claim_ready(runner_id="worker-3")
    print("双 worker 分别领到 b/c 后，d 就绪?:", [x["task_id"] for x in s.ready_tasks()])
    print("stats:", s.stats())
    s.close()
