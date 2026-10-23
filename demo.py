"""
最小验收自测：覆盖你列的 5 条验收标准。

运行：python demo.py
- 标准 4 用 multiprocessing 真正拉起多个独立进程并发读写同一 SQLite 文件，
  验证「不跨进程共享连接 + 不报错」。
- 标准 5 先删除登记处文件（含 WAL 副文件），再让 runner 写入，验证自动重建表。
"""

import multiprocessing as mp
import os
import shutil
import tempfile

from task_registry import TaskRegistry


def _writer_worker(db_path, base, n, err_q):
    try:
        reg = TaskRegistry(db_path=db_path, runner_id=f"writer-{base}")
        for i in range(n):
            tid = f"{base}-{i}"
            reg.register_task(tid, payload={"idx": i})
            if i % 3 == 0:
                reg.update_task_status(tid, "running")
            if i % 5 == 0:
                reg.update_task_status(tid, "done")
        reg.close()
    except Exception as e:  # noqa: BLE001
        err_q.put(f"writer-{base}: {type(e).__name__}: {e}")


def _reader_worker(db_path, iterations, err_q):
    try:
        reg = TaskRegistry(db_path=db_path, runner_id="reader")
        for _ in range(iterations):
            reg.list_tasks(limit=50)
        reg.close()
    except Exception as e:  # noqa: BLE001
        err_q.put(f"reader: {type(e).__name__}: {e}")


def run_acceptance():
    tmp = tempfile.mkdtemp(prefix="taskreg_")
    db_path = os.path.join(tmp, "task_registry.db")
    results = []

    # 1. runner 收到 mock 消息后，登记处出现对应任务记录
    runner = TaskRegistry(db_path=db_path, runner_id="runner-A")
    mock_messages = [{"id": "m1", "text": "do x"}, {"id": "m2", "text": "do y"}]
    for m in mock_messages:
        runner.register_task(m["id"], payload=m)
    runner.close()

    center = TaskRegistry(db_path=db_path, runner_id="center")
    rows = center.list_tasks()
    center.close()
    ids = {r["task_id"] for r in rows}
    ok1 = ids == {"m1", "m2"}
    results.append(("1. runner 收到 mock 消息后登记出现记录", ok1, f"ids={ids}"))

    # 2. task_center 能查到该任务
    center = TaskRegistry(db_path=db_path, runner_id="center")
    found = center.list_tasks(runner_id="runner-A")
    center.close()
    ok2 = len(found) == 2
    results.append(("2. task_center 查到该任务", ok2, f"count={len(found)}"))

    # 3. 任务状态变更后，task_center 能看到最新状态
    runner = TaskRegistry(db_path=db_path, runner_id="runner-A")
    runner.update_task_status("m1", "done")
    runner.close()

    center = TaskRegistry(db_path=db_path, runner_id="center")
    m1 = next(r for r in center.list_tasks() if r["task_id"] == "m1")
    center.close()
    ok3 = m1["status"] == "done"
    results.append(("3. 状态变更后可见最新状态", ok3, f"m1.status={m1['status']}"))

    # 4. 两个进程同时读写不报错（真·跨进程）
    err_q = mp.Queue()
    procs = [
        mp.Process(target=_writer_worker, args=(db_path, b, 50, err_q))
        for b in range(4)
    ]
    procs.append(mp.Process(target=_reader_worker, args=(db_path, 200, err_q)))
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    errs = []
    while not err_q.empty():
        errs.append(err_q.get())

    center = TaskRegistry(db_path=db_path, runner_id="center")
    all_rows = center.list_tasks(limit=10000)
    center.close()
    # 只统计本步骤 4 个 writer 进程写入的任务（排除前序步骤遗留的 m1/m2）
    writer_count = sum(1 for r in all_rows if r["runner_id"].startswith("writer-"))
    ok4 = (not errs) and writer_count == 200
    results.append(
        ("4. 多进程并发读写不报错", ok4, f"errs={errs} writer_count={writer_count}")
    )

    # 5. 删除登记处文件后，runner 能重建表
    for f in (db_path, db_path + "-wal", db_path + "-shm"):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
    runner = TaskRegistry(db_path=db_path, runner_id="runner-rebuild")
    runner.register_task("rebuilt-1", payload={"note": "after delete"})
    runner.close()

    center = TaskRegistry(db_path=db_path, runner_id="center")
    after = center.list_tasks()
    center.close()
    ok5 = len(after) == 1 and after[0]["task_id"] == "rebuilt-1"
    results.append(("5. 删库后 runner 自动重建", ok5, f"count={len(after)}"))

    shutil.rmtree(tmp, ignore_errors=True)
    return results


if __name__ == "__main__":
    print("=" * 60)
    print("跨进程任务登记处 —— 最小验收")
    print("=" * 60)
    all_ok = True
    for name, ok, detail in run_acceptance():
        all_ok = all_ok and ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}  ({detail})")
    print("=" * 60)
    print("结论：全部通过" if all_ok else "结论：存在未通过项，请检查上方 FAIL")
    print("=" * 60)
