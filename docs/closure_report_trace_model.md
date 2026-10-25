# 结案报告 · 两级 Trace 视图配比规则

> 适用代码：`summarizer.py` 的 `ResultSummarizer.closure_collect` / `closure_markdown`。
> 这是「任务结案报告」可复用的核心设计——改配比前先读本文。

## 1. 一句话原则

结案报告对**同一条 trace** 给出两层视角，配比固定：

- **逐任务切片**（第二节「逐任务明细 · Trace 回放」）= 按 `task_id` 对整条 trace **分区（partition）**，每个任务只看属于自己的事件。
- **全局回放**（第三节「全局轨迹回放」）= 整条 trace 按 `seq` **全量呈现一次**，用于跨任务因果排序。

**不是「复制」**：每条事件恰好归属一个任务，因此「各任务切片事件数之和 == 全局回放事件数」。既零冗余、又零信息丢失。

## 2. 配比实证（本仓库 5 任务流水线）

| 视图 | 事件数 | 说明 |
|------|--------|------|
| 逐任务切片 · research | 2 | plan_built + task_completed |
| 逐任务切片 · lint | 2 | plan_built + task_completed |
| 逐任务切片 · fetch | 2 | plan_built + task_completed |
| 逐任务切片 · analyze | 2 | plan_built + task_completed |
| 逐任务切片 · publish | 4 | 上述 2 条 + approval_requested + approval_decided（高危动作经 ApprovalCenter） |
| **全局回放** | **12** | = 流水线事件总数，按 seq 全量一次 |

推论：

- 普通叶子任务切片恒为 **2 条**（`plan_built` + `task_completed`）。
- 高危任务切片为 **2~4 条**——只要该任务触发了 `HIGH_RISK_ACTIONS`（send/publish/post/buy/pay/delete/scan 等），就会多 2 条审批事件。
- 全局回放条数始终 = trace 事件总数，**与任务数量无关**。

## 3. 为什么是「分区」而不是「全量塞进每个任务」

早期实现（`closure_collect` 未加 `if e.get("task_id") == t["task_id"]` 过滤时）把整条 12 事件 trace 直接塞进每个任务的 Trace 回放：

- 重复 N 倍（5 个任务 × 12 事件 = 60 行，且几乎全是别的任务的噪声）；
- 淹没单任务焦点，看不清「这个任务自己干了什么」；
- 全局因果关系被切碎到每节重复，反而更难读。

修复后的「分区 + 全局一次」模型：单任务聚焦（只看自己的 2~4 条）+ 全局俯瞰（12 条一次），且总量守恒、可交叉校验。

## 4. 扩展指引（新增事件类型 / 改配比前看这里）

- **per-task 切片条数** = 该任务自身产生的事件数。未来在 trace 层新增事件种类（如 `retry`、`escalate`），会自动进入对应任务的切片，无需改 `closure_collect`。
- **全局回放条数** 始终等于 trace 事件总数，`closure_markdown` 第三节直接 `trace.events(trace_id)` 全量渲染，与任务数解耦。
- 若某事件**不属于任何任务**（如 pipeline 级 `plan_built`），它只会出现在全局回放、不会出现在任何 per-task 切片——这是预期的「全局专属」事件，不是 bug。
- 改过滤逻辑时务必保留「per-task 只看自己 + 全局一次」的不变式，并同步更新本文件与 `closure_collect` 的 docstring。

## 5. 测试锚点

- `tests/test_closure_report.py`：验证「无存储→标注未接入」「有存储→三路统一」以及 `run_pipeline --closure` 端到端。
- 验证 per-task 切片被过滤（publish 4 条、其余 2 条）且全局回放含全部 12 条，是回归该模型的关键断言。
