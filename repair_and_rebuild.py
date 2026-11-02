#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复「计划永不产出」缺陷的收尾动作（配合 planner.extract_slots 的 topic 兜底修复）：

1. 把历史遗留的 104 个卡在 running 的任务重置回 registered，让 daemon 重新认领
   （这些任务是因为 daemon 进程被回收而卡住的，不是真的在跑）。
2. 删除 4 个被 <待补:topic> 缺陷拖垮的旧计划任务（1793604xxx 系列）。
3. 用修复后的逻辑重建用户的 5 个计划（阿朱×3、情人×2），使其能端到端跑通。
"""
import os
import sys
import time
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "task_registry.db")
sys.path.insert(0, ROOT)

OLD_PLANS = [
    "plan-1793604305904",
    "plan-1793607752628",
    "plan-1793607783532",
    "plan-1793607969595",
]

# 用户原始需求（重建用）。阿朱走通用/单任务；情人用杜拉斯《情人》独白。
AZHU = "天龙八部里阿朱之死这一段，帮我生成一段视频"
QINGREN = ("我已经老了，有一天，在一处公共场所的大厅里，一个男人向我走来。"
           "他主动介绍自己，对我说：我认识你，永远记得你。那时候，你还很年轻，"
           "人人都说你美，现在，我是特地来告诉你，对我来说，我觉得现在的你比"
           "年轻时的你更美，与你那时的容貌相比，我更爱你现在备受摧残的面容。"
           "让他产出一段视频")

NEW_PLANS = [AZHU, AZHU, AZHU, QINGREN, QINGREN]


def main():
    c = sqlite3.connect(DB)

    # 1) 重置卡住的 running
    n_running = c.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
    now = time.time()
    c.execute("UPDATE tasks SET status='registered', updated_at=? WHERE status='running'",
              (now,))
    print("[repair] 重置 running 任务：%d -> registered" % n_running)

    # 2) 删除旧 broken 计划
    for pid in OLD_PLANS:
        n = c.execute("DELETE FROM tasks WHERE task_id LIKE ?", (pid + "/%",)).rowcount
        print("[repair] 删除旧计划 %s：%d 个任务" % (pid, n))
    c.commit()

    # 3) 重建 5 个计划（修复后逻辑）
    from plan_service import PlanService
    from task_registry import TaskRegistry

    reg = TaskRegistry(db_path=DB, runner_id="repair_script")
    svc = PlanService(registry=reg)
    built = []
    for i, req in enumerate(NEW_PLANS, 1):
        try:
            out = svc.build(req, operator="repair")
            built.append((i, out["plan_id"], out["template"], len(out.get("created", []))))
            print("[rebuild] #%d plan=%s template=%s 工序=%d" %
                  (i, out["plan_id"], out["template"], len(out.get("created", []))))
        except Exception as e:  # noqa: BLE001
            print("[rebuild] #%d 失败：%s" % (i, e))

    c.commit()
    c.close()
    print("[repair] 完成。新计划：")
    for b in built:
        print("   ", b)


if __name__ == "__main__":
    main()
