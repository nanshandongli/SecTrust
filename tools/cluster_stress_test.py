"""
真实多机压测（tools/cluster_stress_test.py）—— 路线图第 4 步「先支持 2–3 台机器，再逐步压测」。

本机没有官方 Redis，但 transport.fake_redis.FakeRedisServer 是**真实 socket 级
TCP 服务**（逐连接处理 RESP 协议）。本工具把它起成真实协调服务器，再拉起 N 个
**真实 OS 进程**（multiprocessing，Windows 上 spawn = 真隔离）模拟 N 台机器，
所有机器通过 TCP 连同一个协调后端争用跨机锁 / 心跳 / 死信——这正是「多机」的本质：
进程边界 = 机器边界，共享协调后端走网络，真实锁竞争。

两项基准：
  A. 跨机原语争用（直驱 ClusterCoordinator）：N 台机器对一个小锁池高频抢锁/放锁，
     校验「同一把锁任意时刻只有一个持有者」（防双抢），并采集锁获取延迟
     P50/P95/P99/Max、心跳在线检测、死信阈值行为。
  B. 真实 daemon 集群：N 台机器各跑一个**真实 ExecutionDaemon**，共享同一 SQLite
     （WAL，对应「共享库落网络盘」拓扑），seed M 个 noop 任务，校验 total 完成 == M、
     跨机 0 重复执行（每个任务只被一台机器认领+完成）、吞吐。

⚠️ 诚实边界（必须如实记录）：
  - 这是**单宿主 + 同机多进程**压测，不是分散在多台物理机。机器隔离靠进程隔离模拟，
    网络靠本机 TCP 回环模拟；真实跨机房还会叠加延迟、分区、WAL 落 NFS/SMB 失效等风险。
  - 协调后端是本项目自带的协议兼容 FakeRedisServer，**不是官方 Redis**。生产跨机应接
    官方 Redis（ClusterCoordinator 的开关纪律：SECTRUST_REDIS=1 不可达即抛错）。
    本工具的价值是：在单机上真实压出「多进程 + 网络协调 + 锁竞争」的正确性数据与延迟分布。
  - 留下数据：每次运行写 data/cluster_stress_<时间戳>.json（机器可读）+ .md（人读）。

用法：
    python tools/cluster_stress_test.py --machines 3 --tasks 200 --duration 6
    python tools/cluster_stress_test.py --out-dir data            # 指定落盘目录
    python tools/cluster_stress_test.py --no-bench-b             # 只跑原语争用
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from transport.fake_redis import FakeRedisServer          # noqa: E402
from transport.distributed import ClusterCoordinator      # noqa: E402
from orchestration import REGISTERED                      # noqa: E402
from task_registry import TaskRegistry                    # noqa: E402
from execution.daemon import ExecutionDaemon              # noqa: E402


# ---------------------------------------------------------------- 工具
def _pct(sorted_vals, p):
    """分位数（sorted_vals 已升序）。p 取 0–100。空返回 None。"""
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1,
                   int(math.ceil(p / 100.0 * len(sorted_vals))) - 1))
    return sorted_vals[k]


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ================================================================ 基准 A
# 跨机原语争用：N 台机器对共享锁池高频抢/放，校验互斥 + 延迟分布。
def _worker_coordination(args):
    idx, port, n_machines, duration, pool_size, out_file = args
    owner = "machine-%d" % idx
    cluster = ClusterCoordinator(url="redis://127.0.0.1:%d/0" % port, enabled=True,
                                 lock_ttl=5, heartbeat_ttl=5, dead_letter_after=3)
    pool = ["lock-%d" % i for i in range(pool_size)]
    latencies: list[float] = []
    events: list[tuple] = []          # (name, owner, kind, ts)
    rng = random.Random(idx * 7919 + 1)
    deadline = time.time() + duration
    last_beat = 0.0
    dlq_driven = 0
    max_held = 0
    while time.time() < deadline:
        name = rng.choice(pool)
        t0 = time.perf_counter()
        tok = cluster.acquire(name, owner, ttl=5)
        dt = time.perf_counter() - t0
        latencies.append(dt)
        if tok:
            events.append((name, owner, "ACQUIRE", time.time()))
            time.sleep(rng.uniform(0.0005, 0.002))   # 模拟极小工作量
            events.append((name, owner, "RELEASE", time.time()))
            ok = cluster.release(name, tok)
            max_held = max(max_held, 1)
        if time.time() - last_beat > 1.0:
            cluster.beat(owner, meta={"idx": idx})
            last_beat = time.time()
    # machine-0 驱动死信：对同一任务连续 note_failure，超过阈值应入死信
    if idx == 0:
        for _ in range(5):
            if cluster.note_failure("dlq-probe-task", "boom",
                                    payload={"from": "bench-a"}):
                dlq_driven += 1
    cluster.close()
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"owner": owner, "latencies": latencies, "events": events,
                   "dlq_driven": dlq_driven, "max_held": max_held}, f)


def _check_lock_exclusion(events_all: list[tuple]) -> dict:
    """校验「同一把锁任意时刻只有一个持有者」。

    events_all: 所有机器的 (name, owner, kind, ts)。为每把锁构造持有区间
    (acquire_ts, release_ts, owner)，两两比较：若两个区间重叠且 owner 不同
    => 互斥被破坏（双抢）。返回 {violations, max_concurrent_per_lock, n_intervals}。
    """
    by_name: dict[str, list] = {}
    for name, owner, kind, ts in events_all:
        if kind == "ACQUIRE":
            by_name.setdefault(name, []).append((ts, owner, "A"))
        elif kind == "RELEASE":
            by_name.setdefault(name, []).append((ts, owner, "R"))
    violations = 0
    max_concurrent = 0
    n_intervals = 0
    for name, evs in by_name.items():
        evs.sort(key=lambda e: e[0])
        # 构造区间：A 与随后最近的同 owner R 配对
        open_by_owner: dict[str, float] = {}
        current_owners: set[str] = set()
        for ts, owner, kind in evs:
            if kind == "A":
                # 与任一当前持有者（不同 owner）重叠 => 违规
                for o in current_owners:
                    if o != owner:
                        violations += 1
                open_by_owner[owner] = ts
                current_owners.add(owner)
                n_intervals += 1
                max_concurrent = max(max_concurrent, len(current_owners))
            else:  # R
                if owner in open_by_owner:
                    del open_by_owner[owner]
                    current_owners.discard(owner)
    return {"violations": violations,
            "max_concurrent_per_lock": max_concurrent,
            "n_intervals": n_intervals}


def bench_coordination(port: int, n_machines: int, duration: float,
                       pool_size: int, results_dir: str) -> dict:
    tuples = [(i, port, n_machines, duration, pool_size,
               os.path.join(results_dir, "coord_%d.json" % i))
              for i in range(n_machines)]
    procs = [mp.Process(target=_worker_coordination, args=(t,)) for t in tuples]
    for p in procs:
        p.start()
    # 运行期间采样「在线机器数」（真实跨机心跳在线检测）
    alive_samples: set[str] = set()
    sampler = ClusterCoordinator(url="redis://127.0.0.1:%d/0" % port, enabled=True,
                                 heartbeat_ttl=5)
    t0 = time.time()
    while time.time() - t0 < duration + 1.0:
        try:
            alive_samples.update(sampler.alive_runners())
        except Exception:     # noqa: BLE001 —— 采样失败不挡压测
            pass
        time.sleep(0.4)
    for p in procs:
        p.join()
    sampler.close()

    merged = [_read_json(os.path.join(results_dir, "coord_%d.json" % i))
              for i in range(n_machines)]
    all_lat = [x for m in merged for x in m["latencies"]]
    all_events = [e for m in merged for e in m["events"]]
    all_lat.sort()
    exclusion = _check_lock_exclusion(all_events)

    # 死信校验：machine-0 已驱动 5 次失败，应已入队；recover 后应消失
    verifier = ClusterCoordinator(url="redis://127.0.0.1:%d/0" % port, enabled=True,
                                  heartbeat_ttl=5, dead_letter_after=3)
    dlq = verifier.dead_letter_list(limit=50)
    dlq_ids = {d.get("task_id") for d in dlq}
    dlq_present = "dlq-probe-task" in dlq_ids
    recovered = verifier.recover_dead_letter("dlq-probe-task")
    dlq_after_recover = any(
        d.get("task_id") == "dlq-probe-task" for d in verifier.dead_letter_list(50))
    verifier.close()

    return {
        "scenario": "coordination_primitives",
        "machines": n_machines,
        "lock_pool_size": pool_size,
        "duration_seconds": duration,
        "total_lock_ops": len(all_lat),
        "lock_ops_per_sec": round(len(all_lat) / duration, 1) if duration > 0 else None,
        "lock_acquire_latency_ms": {
            "p50": round(_pct(all_lat, 50) * 1000, 3) if all_lat else None,
            "p95": round(_pct(all_lat, 95) * 1000, 3) if all_lat else None,
            "p99": round(_pct(all_lat, 99) * 1000, 3) if all_lat else None,
            "max": round(max(all_lat) * 1000, 3) if all_lat else None,
        },
        "lock_exclusion": exclusion,
        "alive_runners_observed": sorted(alive_samples),
        "dead_letter": {
            "driven_failures": sum(m["dlq_driven"] for m in merged),
            "present_after_threshold": dlq_present,
            "recovered_entry": recovered is not None,
            "gone_after_recover": not dlq_after_recover,
        },
    }


# ================================================================ 基准 B
# 真实 daemon 集群：N 台机器各跑真实 ExecutionDaemon，共享 SQLite + TCP 协调。
def _worker_pipeline(args):
    idx, port, db_path, duration, out_file = args
    cluster = ClusterCoordinator(url="redis://127.0.0.1:%d/0" % port, enabled=True,
                                 lock_ttl=60, heartbeat_ttl=60, dead_letter_after=3)
    daemon = ExecutionDaemon(
        db_path=db_path, bot_id="machine-%d" % idx,
        heartbeat_interval=2.0, poll_interval=0.02, cluster=cluster,
    )
    daemon.run(duration=duration)
    done = [(tid, st) for (tid, st) in daemon._processed if st == "done"]
    requeued = sum(1 for (_, st) in daemon._processed if st == "requeued")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"bot_id": daemon.bot_id, "processed": daemon._processed,
                   "done": done, "requeued": requeued}, f)


def bench_pipeline(port: int, n_machines: int, n_tasks: int,
                   duration: float, db_path: str, results_dir: str) -> dict:
    # seed：共享库里登记 N 个 noop 任务（对应「共享库落网络盘」拓扑）
    seed = TaskRegistry(db_path=db_path, runner_id="seeder")
    seeded = 0
    for i in range(n_tasks):
        tid = "task-%04d" % i
        if seed.get_task(tid) is None:
            seed.register_task(tid, payload={"op": "noop", "name": "noop-%d" % i})
            seeded += 1
    seed.close()

    tuples = [(i, port, db_path, duration,
               os.path.join(results_dir, "pipe_%d.json" % i))
              for i in range(n_machines)]
    procs = [mp.Process(target=_worker_pipeline, args=(t,)) for t in tuples]
    t0 = time.perf_counter()
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    wall = time.perf_counter() - t0

    merged = [_read_json(os.path.join(results_dir, "pipe_%d.json" % i))
              for i in range(n_machines)]
    # 跨机观察：聚合所有机器完成的任务，校验「同一任务只被一台完成」
    done_by_task: dict[str, list[str]] = {}
    per_machine_done = {}
    total_requeued = 0
    for m in merged:
        per_machine_done[m["bot_id"]] = len(m["done"])
        total_requeued += m["requeued"]
        for tid, _st in m["done"]:
            done_by_task.setdefault(tid, []).append(m["bot_id"])
    duplicates = {tid: bots for tid, bots in done_by_task.items()
                  if len(bots) > 1}
    unique_done = len(done_by_task)

    return {
        "scenario": "real_daemon_fleet",
        "machines": n_machines,
        "tasks_seeded": seeded,
        "tasks_completed_unique": unique_done,
        "tasks_completed_total": sum(len(m["done"]) for m in merged),
        "double_executions": duplicates,        # 必须为空：跨机零重复执行
        "double_execution_count": len(duplicates),
        "requeued_total": total_requeued,
        "wall_seconds": round(wall, 3),
        "throughput_tasks_per_sec": round(unique_done / wall, 2) if wall > 0 else None,
        "per_machine_done": per_machine_done,
        "correct": (len(duplicates) == 0 and unique_done == seeded),
    }


# ================================================================ 主流程
def run(machines: int = 3, tasks: int = 200, duration: float = 6.0,
        pool_size: int = 16, out_dir: str = "data", bench_b: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    results_dir = tempfile.mkdtemp(prefix="cluster_stress_results_")
    srv = FakeRedisServer().start()
    port = srv.port
    started = time.time()
    report = {
        "tool": "cluster_stress_test",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "backend": "FakeRedisServer (wire-compatible RESP, in-process TCP server)",
        "topology": "single-host / multi-process; shared SQLite (WAL) + TCP coordination",
        "machines": machines,
    }
    try:
        report["bench_a_coordination"] = bench_coordination(
            port, machines, duration, pool_size, results_dir)
        if bench_b:
            db_path = os.path.join(results_dir, "shared_fleet.db")
            report["bench_b_pipeline"] = bench_pipeline(
                port, machines, tasks, duration, db_path, results_dir)
    finally:
        srv.stop()

    # 落盘：JSON（机器可读）+ MD（人读）
    ts = time.strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(out_dir, "cluster_stress_%s.json" % ts)
    md_path = os.path.join(out_dir, "cluster_stress_%s.md" % ts)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    _write_markdown(report, md_path)
    report["_artifact_json"] = json_path
    report["_artifact_md"] = md_path
    return report


def _write_markdown(report: dict, path: str) -> None:
    a = report["bench_a_coordination"]
    lines = []
    lines.append("# 真实多机压测报告（同机多进程 + 真实 TCP 协调）\n")
    lines.append("- 时间：%s" % report["timestamp"])
    lines.append("- 机器数（进程模拟）：%d" % report["machines"])
    lines.append("- 协调后端：%s" % report["backend"])
    lines.append("- 拓扑：%s" % report["topology"])
    lines.append("")
    lines.append("> 诚实边界：单宿主 + 同机多进程，进程隔离模拟机器、本机 TCP 回环模拟")
    lines.append("> 网络。协调后端是项目自带的协议兼容 FakeRedisServer，非官方 Redis。")
    lines.append("> 真实跨机房还会叠加延迟/分区/WAL 落 NFS 失效等风险。\n")

    lines.append("## 基准 A · 跨机原语争用（锁/心跳/死信）\n")
    lines.append("- 锁池大小：%d（高争用）" % a["lock_pool_size"])
    lines.append("- 锁操作总数：%d，吞吐：%.1f ops/s"
                 % (a["total_lock_ops"], a["lock_ops_per_sec"] or 0))
    lat = a["lock_acquire_latency_ms"]
    lines.append("- 锁获取延迟（ms）：P50=%.3f  P95=%.3f  P99=%.3f  Max=%.3f"
                 % (lat["p50"] or 0, lat["p95"] or 0, lat["p99"] or 0, lat["max"] or 0))
    ex = a["lock_exclusion"]
    ok = ex["violations"] == 0
    lines.append("- 锁互斥校验：同一锁最大并发持有者=%d，违规（双抢）=%d  %s"
                 % (ex["max_concurrent_per_lock"], ex["violations"],
                    "✓ 正确" if ok else "✗ 有双抢！"))
    lines.append("- 在线机器观测（心跳）：%s" % (", ".join(a["alive_runners_observed"]) or "无"))
    d = a["dead_letter"]
    lines.append("- 死信：驱动失败 %d 次，达阈值后入队=%s，recover 后消失=%s"
                 % (d["driven_failures"], d["present_after_threshold"],
                    d["gone_after_recover"]))
    lines.append("")

    if "bench_b_pipeline" in report:
        b = report["bench_b_pipeline"]
        lines.append("## 基准 B · 真实 daemon 集群（共享 SQLite + TCP 协调）\n")
        lines.append("- 任务 seed：%d" % b["tasks_seeded"])
        lines.append("- 跨机完成（去重）：%d / %d" % (b["tasks_completed_unique"],
                                                    b["tasks_seeded"]))
        lines.append("- 重复执行（双机同跑同一任务）：%d  %s"
                     % (b["double_execution_count"],
                        "✓ 零重复" if b["double_execution_count"] == 0 else "✗ 有重复！"))
        if b["double_executions"]:
            lines.append("  - 违规任务：%s" % list(b["double_executions"].items())[:10])
        lines.append("- 跨机放回（requeue）总计：%d" % b["requeued_total"])
        lines.append("- 墙钟：%.3fs，吞吐：%.2f 任务/s"
                     % (b["wall_seconds"], b["throughput_tasks_per_sec"] or 0))
        lines.append("- 各机完成分布：%s" % b["per_machine_done"])
        lines.append("- 正确性：%s" % ("✓ 通过" if b["correct"] else "✗ 未通过"))

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="真实多机压测（同机多进程 + 真实 TCP 协调）")
    ap.add_argument("--machines", type=int, default=3, help="模拟机器数（进程数）")
    ap.add_argument("--tasks", type=int, default=200, help="基准 B seed 任务数")
    ap.add_argument("--duration", type=float, default=6.0, help="每项基准运行时长（秒）")
    ap.add_argument("--pool-size", type=int, default=16, help="基准 A 锁池大小（争用强度）")
    ap.add_argument("--out-dir", default="data", help="报告落盘目录")
    ap.add_argument("--no-bench-b", action="store_true", help="只跑基准 A")
    args = ap.parse_args()

    mp.freeze_support()   # Windows spawn 必须
    report = run(machines=args.machines, tasks=args.tasks, duration=args.duration,
                 pool_size=args.pool_size, out_dir=args.out_dir,
                 bench_b=not args.no_bench_b)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print("\n报告已落盘：")
    print("  JSON: %s" % report["_artifact_json"])
    print("  MD:   %s" % report["_artifact_md"])
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
