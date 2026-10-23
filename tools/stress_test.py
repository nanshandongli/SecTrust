"""
多进程并发压测（tools/stress_test.py）—— ⑫ 跨机压测的同机基线。

为什么需要它：此前报告里的性能数字（写 1.6k TPS / 读 199k TPS）都是**单进程
线程内**测的，跨机部署后写竞争会剧增（WAL 只允许一个写者，其余靠 busy_timeout
排队）。本工具用**真实多进程**压出同机基线，并暴露：
  - 写吞吐随进程数的变化曲线（找出 SQLite 写竞争的拐点）；
  - CAS 认领的正确性（同一任务只能被一个进程抢到）——并发下的数据正确性；
  - database is locked 等错误的实际发生率；
  - 队列（本地 SQLite / Redis）在多进程下的吞吐。

⚠️ 诚实边界：这是**同机**多进程压测，不是跨机。跨机还叠加网络延迟与
分区风险（尤其 WAL 挂在 NFS/SMB 上会失效——见 task_registry 模块文档）。
跨机压测需要多台机器，本工具无法替代。

用法：
    python tools/stress_test.py --processes 1 4 8 16 --ops 2000
    python tools/stress_test.py --scenario claim --processes 8   # CAS 正确性
    python tools/stress_test.py --scenario queue --processes 4   # 队列吞吐
    python tools/stress_test.py --json                           # 机器可读
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import statistics
import sqlite3
import sys
import tempfile
import time
import uuid
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- 工作进程
def _wal_probe(db_path: str) -> str:
    """采集 WAL 现场（排错用）：三件套文件状态 + 库自报的 journal_mode。"""
    info = [f"wal={os.path.exists(db_path + '-wal')}",
            f"shm={os.path.exists(db_path + '-shm')}"]
    try:
        info.append(f"db_size={os.path.getsize(db_path)}")
    except OSError:
        pass
    try:
        c = sqlite3.connect(db_path, timeout=1)
        info.append(f"journal={c.execute('PRAGMA journal_mode;').fetchone()[0]}")
        c.close()
    except sqlite3.Error as e:
        info.append(f"journal=<{type(e).__name__}>")
    return " ".join(info)


def _worker_write(args) -> Dict:
    """写压测：多进程并发 register_task + update_task_status。"""
    db_path, n_ops, prefix, sync_mode = args
    from task_registry import TaskRegistry

    errors: List[str] = []
    n_ok = 0
    t0 = time.perf_counter()
    try:
        reg = TaskRegistry(db_path=db_path, runner_id=f"{prefix}-w",
                           synchronous=sync_mode)
    except Exception as e:  # noqa: BLE001
        # 构造期失败：留现场（目录可写性 + 文件状态），否则只剩一句难懂的错误
        errors.append(f"INIT_FAIL {type(e).__name__}: {e} | "
                      f"dir_writable={os.access(os.path.dirname(db_path), os.W_OK)} | "
                      f"{_wal_probe(db_path)}")
        return {"ok": 0, "errors": errors, "elapsed": time.perf_counter() - t0}

    try:
        for i in range(n_ops):
            tid = f"{prefix}-{i}"
            try:
                reg.register_task(tid, payload={"i": i, "w": prefix})
                reg.update_task_status(tid, "done")
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                if not errors:
                    # 首个错误附 WAL 现场（后续同质错误不再重复采集，避免刷屏）
                    msg += f" | {_wal_probe(db_path)}"
                errors.append(msg)
    finally:
        reg.close()
    return {"ok": n_ok, "errors": errors, "elapsed": time.perf_counter() - t0}


def _worker_read(args) -> Dict:
    """读压测：多进程并发 list_tasks。"""
    db_path, n_ops, prefix = args
    from task_registry import TaskRegistry

    reg = TaskRegistry(db_path=db_path, runner_id=f"{prefix}-r")
    errors: List[str] = []
    n_ok = 0
    t0 = time.perf_counter()
    try:
        for _ in range(n_ops):
            try:
                reg.list_tasks(limit=50)
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
    finally:
        reg.close()
    elapsed = time.perf_counter() - t0
    return {"ok": n_ok, "errors": errors, "elapsed": elapsed}


def _worker_claim(args) -> Dict:
    """CAS 认领正确性压测：N 个进程抢同一批任务，每个任务只能被抢到一次。"""
    db_path, task_ids, prefix = args
    from task_registry import TaskRegistry

    reg = TaskRegistry(db_path=db_path, runner_id=f"{prefix}-c")
    claimed: List[str] = []
    errors: List[str] = []
    try:
        for tid in task_ids:
            try:
                if reg.claim_task(tid, runner_id=prefix):
                    claimed.append(tid)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
    finally:
        reg.close()
    return {"claimed": claimed, "errors": errors}


def _worker_queue(args) -> Dict:
    """队列吞吐压测：多进程 publish + consume。"""
    db_path, n_ops, prefix = args
    from transport.queue import build_queue

    q = build_queue(backend="local", db_path=db_path)
    errors: List[str] = []
    n_ok = 0
    t0 = time.perf_counter()
    try:
        for i in range(n_ops):
            try:
                q.publish("stress", {"w": prefix, "i": i})
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
    finally:
        close = getattr(q, "close", None)
        if callable(close):
            close()
    elapsed = time.perf_counter() - t0
    return {"ok": n_ok, "errors": errors, "elapsed": elapsed}


# ---------------------------------------------------------------- 场景
def _err_summary(results: List[Dict]) -> Dict:
    """汇总各 worker 的错误：统计类型分布（locked 错误最需要关注）。"""
    counter: Dict[str, int] = {}
    total = 0
    sample: List[str] = []
    for r in results:
        for e in r.get("errors", []):
            total += 1
            kind = e.split(":")[0]
            counter[kind] = counter.get(kind, 0) + 1
            if len(sample) < 3:
                sample.append(e[:400])
    return {"total": total, "by_kind": counter, "sample": sample}


def run_write(db_path: str, n_procs: int, n_ops: int,
              sync_mode: str = "FULL") -> Dict:
    prefix = uuid.uuid4().hex[:8]
    procs = []
    t0 = time.perf_counter()
    for i in range(n_procs):
        p = mp.Process(target=_worker_write,
                       args=((db_path, n_ops, f"{prefix}-p{i}", sync_mode),))
        p.start()
        procs.append(p)
    results = []
    for p in procs:
        p.join()
        # 子进程结果通过 Queue 回传更稳，这里用返回值不可行——改为落文件
    elapsed = time.perf_counter() - t0
    return {"elapsed": elapsed, "n_procs": n_procs, "n_ops": n_ops,
            "sync": sync_mode, "_procs": procs}


def _worker_write_file(db_path, n_ops, prefix, sync_mode, out_file) -> None:
    """写 worker：结果落临时文件（跨进程传结果用文件比 Queue 更省事可靠）。"""
    res = _worker_write((db_path, n_ops, prefix, sync_mode))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(res, f)


def _worker_read_file(db_path, n_ops, prefix, out_file) -> None:
    res = _worker_read((db_path, n_ops, prefix))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(res, f)


def _worker_claim_file(db_path, task_ids, prefix, out_file) -> None:
    res = _worker_claim((db_path, task_ids, prefix))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(res, f)


def _worker_queue_file(db_path, n_ops, prefix, out_file) -> None:
    res = _worker_queue((db_path, n_ops, prefix))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(res, f)


def _run_parallel(worker, arg_tuples: List[tuple]) -> List[Dict]:
    """跑一批 worker 进程并收集结果（结果走文件，避免跨进程 Queue 的坑）。"""
    tmpdir = tempfile.mkdtemp(prefix="stress_")
    procs = []
    for i, t in enumerate(arg_tuples):
        out_file = os.path.join(tmpdir, f"out_{i}.json")
        # 注意：worker 的位置参数是散装的，这里要展开（*t），不能整体传一个 tuple
        p = mp.Process(target=worker, args=(*t, out_file))
        p.start()
        procs.append((p, out_file))
    results = []
    for p, out_file in procs:
        p.join()
        try:
            with open(out_file, encoding="utf-8") as f:
                results.append(json.load(f))
        except (OSError, ValueError):
            # worker 崩了（连结果都没写出）：记录退出码，便于区分
            # 「worker 自己捕获的失败」与「worker 进程级崩溃」
            results.append({"ok": 0,
                            "errors": [f"worker 进程崩溃 exitcode={p.exitcode}"],
                            "elapsed": 0.0})
    return results


# ---------------------------------------------------------------- 场景实现
def scenario_write(db_path: str, n_procs: int, n_ops: int,
                   sync_mode: str = "FULL") -> Dict:
    prefix = uuid.uuid4().hex[:8]
    tuples = [(db_path, n_ops, f"{prefix}-p{i}", sync_mode)
              for i in range(n_procs)]
    t0 = time.perf_counter()
    results = _run_parallel(_worker_write_file, tuples)
    wall = time.perf_counter() - t0
    total_ok = sum(r.get("ok", 0) for r in results)
    total_ops = n_procs * n_ops
    lat = [r["elapsed"] for r in results if r.get("elapsed")]
    errs = _err_summary(results)
    return {
        "scenario": "write",
        "processes": n_procs,
        "ops_per_process": n_ops,
        "total_ops": total_ops,
        "succeeded": total_ok,
        "failed": total_ops - total_ok,
        "wall_seconds": round(wall, 3),
        "tps": round(total_ok / wall, 1) if wall > 0 else None,
        "per_process_elapsed_mean": round(statistics.mean(lat), 3) if lat else None,
        "per_process_elapsed_max": round(max(lat), 3) if lat else None,
        "errors": errs,
    }


def scenario_read(db_path: str, n_procs: int, n_ops: int) -> Dict:
    prefix = uuid.uuid4().hex[:8]
    tuples = [(db_path, n_ops, f"{prefix}-p{i}") for i in range(n_procs)]
    t0 = time.perf_counter()
    results = _run_parallel(_worker_read_file, tuples)
    wall = time.perf_counter() - t0
    total_ok = sum(r.get("ok", 0) for r in results)
    total_ops = n_procs * n_ops
    errs = _err_summary(results)
    return {
        "scenario": "read",
        "processes": n_procs,
        "ops_per_process": n_ops,
        "total_ops": total_ops,
        "succeeded": total_ok,
        "failed": total_ops - total_ok,
        "wall_seconds": round(wall, 3),
        "tps": round(total_ok / wall, 1) if wall > 0 else None,
        "errors": errs,
    }


def scenario_claim(db_path: str, n_procs: int, n_tasks: int) -> Dict:
    """并发 CAS 认领：验证同一任务不会被两个进程同时抢到（数据正确性）。"""
    from task_registry import TaskRegistry

    reg = TaskRegistry(db_path=db_path, runner_id="setup")
    task_ids = [f"claim-{uuid.uuid4().hex[:10]}-{i}" for i in range(n_tasks)]
    for tid in task_ids:
        reg.register_task(tid, payload={"n": tid})
    reg.close()

    prefix = uuid.uuid4().hex[:8]
    tuples = [(db_path, task_ids, f"{prefix}-p{i}") for i in range(n_procs)]
    t0 = time.perf_counter()
    results = _run_parallel(_worker_claim_file, tuples)
    wall = time.perf_counter() - t0

    all_claimed = [t for r in results for t in r.get("claimed", [])]
    unique = set(all_claimed)
    dup = [t for t in unique if all_claimed.count(t) > 1]

    reg2 = TaskRegistry(db_path=db_path, runner_id="verify")
    running = [t for t in task_ids
               if (reg2.get_task(t) or {}).get("status") == "running"]
    reg2.close()

    return {
        "scenario": "claim",
        "processes": n_procs,
        "tasks": n_tasks,
        "wall_seconds": round(wall, 3),
        "claimed_total": len(all_claimed),
        "claimed_unique": len(unique),
        "duplicated": dup,          # 必须为空：同一任务被多个进程抢到 = CAS 失效
        "db_running_count": len(running),
        "correct": not dup and len(unique) == len(task_ids),
        "errors": _err_summary(results),
    }


def scenario_queue(db_path: str, n_procs: int, n_ops: int) -> Dict:
    prefix = uuid.uuid4().hex[:8]
    tuples = [(db_path, n_ops, f"{prefix}-p{i}") for i in range(n_procs)]
    t0 = time.perf_counter()
    results = _run_parallel(_worker_queue_file, tuples)
    wall = time.perf_counter() - t0
    total_ok = sum(r.get("ok", 0) for r in results)
    total_ops = n_procs * n_ops
    return {
        "scenario": "queue",
        "processes": n_procs,
        "ops_per_process": n_ops,
        "total_ops": total_ops,
        "succeeded": total_ok,
        "failed": total_ops - total_ok,
        "wall_seconds": round(wall, 3),
        "tps": round(total_ok / wall, 1) if wall > 0 else None,
        "errors": _err_summary(results),
    }


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="多进程并发压测（同机基线）")
    ap.add_argument("--scenario", default="write",
                    choices=["write", "read", "claim", "queue", "all"])
    ap.add_argument("--processes", type=int, nargs="+", default=[1, 4, 8])
    ap.add_argument("--ops", type=int, default=1000, help="每进程操作数")
    ap.add_argument("--sync", default="FULL", choices=["FULL", "NORMAL"],
                    help="SQLite synchronous 模式")
    ap.add_argument("--db-path", default=None, help="默认用临时目录")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    tmpdir = None
    if args.db_path:
        db_path = args.db_path
    else:
        tmpdir = tempfile.mkdtemp(prefix="stress_db_")
        db_path = os.path.join(tmpdir, "stress.db")

    results = []
    scenarios = (["write", "read", "claim", "queue"]
                 if args.scenario == "all" else [args.scenario])
    for sc in scenarios:
        for n in args.processes:
            if sc == "write":
                r = scenario_write(db_path, n, args.ops, args.sync)
            elif sc == "read":
                r = scenario_read(db_path, n, args.ops)
            elif sc == "claim":
                r = scenario_claim(db_path, n, max(10, args.ops // 10))
            else:
                r = scenario_queue(db_path, n, args.ops)
            results.append(r)
            if not args.json:
                _print_row(r)

    if args.json:
        print(json.dumps({"results": results,
                          "note": "同机多进程基线，非跨机压测"},
                         ensure_ascii=False, indent=2))
    else:
        _print_summary(results)
    return 0


def _print_row(r: Dict) -> None:
    sc = r["scenario"]
    if sc == "claim":
        print(f"[claim] 进程={r['processes']:>3} 任务={r['tasks']:>5} "
              f"抢到={r['claimed_unique']:>5} 重复={len(r['duplicated']):>3} "
              f"正确={'✓' if r['correct'] else '✗'}")
        return
    err = r.get("errors", {}).get("total", 0)
    print(f"[{sc:5s}] 进程={r['processes']:>3} 操作={r['total_ops']:>6} "
          f"成功={r['succeeded']:>6} 失败={r['failed']:>4} "
          f"耗时={r['wall_seconds']:>7.3f}s TPS={r['tps'] or 0:>9} "
          f"错误={err}")


def _print_summary(results: List[Dict]) -> None:
    print("\n" + "=" * 72)
    print("多进程并发压测汇总（同机基线）")
    print("=" * 72)
    writes = [r for r in results if r["scenario"] == "write"]
    if writes:
        print("\n写吞吐随进程数变化（找写竞争拐点）：")
        for r in writes:
            print(f"  {r['processes']:>3} 进程: {r['tps']:>9} TPS   "
                  f"失败 {r['failed']:>5}  错误 {r['errors']['total']:>5}"
                  + (f"  {r['errors']['by_kind']}" if r["errors"]["by_kind"] else ""))
        best = max(writes, key=lambda r: (r["tps"] or 0))
        print(f"  → 峰值在 {best['processes']} 进程（{best['tps']} TPS）")
    claims = [r for r in results if r["scenario"] == "claim"]
    if claims:
        print("\nCAS 认领正确性（同一任务只能被一个进程抢到）：")
        for r in claims:
            print(f"  {r['processes']:>3} 进程: 抢到 {r['claimed_unique']}/{r['tasks']} "
                  f"重复 {len(r['duplicated'])}  {'✓ 正确' if r['correct'] else '✗ 有重复！'}")
    print("\n说明：以上为**同机**多进程基线。跨机部署还需实测网络延迟与")
    print("     WAL 落在 NFS/SMB 上失效的风险（见 task_registry 模块文档）。")


if __name__ == "__main__":
    # Windows 上 multiprocessing 默认 spawn，子进程会重新导入本模块。
    # 必须用 freeze_support 保护，否则子进程会再次执行 main() 造成无限 fork。
    mp.freeze_support()
    sys.exit(main())
