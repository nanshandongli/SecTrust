"""
plan_service.py —— 「一句话需求 → 拆解 → 建图」的应用层服务

**为什么有这个模块（产品缺口的补法）**

`planner.py` 里的 `Planner.decompose()` / `Plan.build()` 写得很完整（5 套模板、
成本路由、并行波次、投票展开），但**生产链路里没有任何调用点**——只有测试在用。
控制台「新建任务」走的是 `console_api.create_task()`，它只登记**一条**任务，
不拆解。于是「写一句需求 → 自动拆成 5–7 道工序 → 自动建图 → 自动执行」
这条主链路在产品里是断的。

本模块就是那个缺口上的桥：**把拆解能力接进控制台**，并遵守三条纪律：

1. **拆解是纯函数、落库是副作用**——`decompose()` 不碰 IO（可单测、可回放），
   `build()` 才落库。本模块是唯一的组合点，两步都从这里走。
2. **缺槽位不猜**——模板里的 `<待补:key>` 会原样留在 instruction 里，
   并在 `needs_input` 里列出来交给人补；绝不自动编一个值填进去。
3. **建图失败不留半张图**——`Plan.build()` 逐个 `add_task`，中途失败会留下
   已建的几个任务。本模块在建图前**先校验**（`plan.validate()`），并在
   失败时把已建的任务登记为失败状态，避免留下"看起来能跑、实际跑不通"的图。

诚实边界：
- 规则模板版只认 5 类需求（内容营销 / 软件研发 / 研究报告 / 客服工单 / 通用兜底），
  匹配不上就走通用兜底，**不会**凭空造工序；
- 拆解结果**不等于**产出真的做完了——工序建好还得有 runner 来认领执行
  （见 `execution/daemon.py`）；
- 本模块只写 tasks 表 + trace 事件，不碰审批、成本、素材等其他表。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from planner import Plan, PlanError, Planner

# 需求长度上限（与控制台任务标题同量级；过长的需求多半不是"一件事"）
MAX_REQUIREMENT_LEN = 2000


class PlanServiceError(Exception):
    """需求拆解/建图的业务错误（带可直接展示给用户的中文原因）。"""

    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class PlanService:
    """一句话 → Plan → DAG。

    依赖注入：`registry` / `tracer` / `scheduler` 都可传入，便于测试。
    默认自建：`DagScheduler`（共享同一份 tasks 表）+ `TraceStore`。
    """

    def __init__(self, registry=None, tracer=None, scheduler=None):
        self._registry = registry
        self._tracer = tracer
        self._scheduler = scheduler
        self._planner = Planner()

    # ---------------- 内部装配（懒加载，避免 import 期副作用） ----------------
    @property
    def registry(self):
        if self._registry is None:
            from task_registry import TaskRegistry
            self._registry = TaskRegistry()
        return self._registry

    @property
    def tracer(self):
        if self._tracer is None:
            from trace import TraceStore
            self._tracer = TraceStore()
        return self._tracer

    @property
    def scheduler(self):
        if self._scheduler is None:
            from orchestration import DagScheduler
            self._scheduler = DagScheduler(registry=self.registry,
                                           tracer=self.tracer)
        return self._scheduler

    # ---------------- 1) 只拆解，不落库（预览） ----------------
    def preview(self, requirement: str, params: Optional[Dict[str, Any]] = None,
                tier: Optional[str] = None) -> Dict[str, Any]:
        """一句需求 → 拆解结果预览（**不写库**）。

        返回：模板命中情况 / 工序清单 / 并行波次 / 成本档位 / 待补槽位。
        """
        requirement = (requirement or "").strip()
        if not requirement:
            raise PlanServiceError("需求不能为空")
        if len(requirement) > MAX_REQUIREMENT_LEN:
            raise PlanServiceError("需求过长（上限 %d 字）" % MAX_REQUIREMENT_LEN)
        try:
            plan = self._planner.decompose(requirement, params=params or {},
                                           tier=tier)
        except PlanError as e:
            raise PlanServiceError("拆解失败：%s" % e)
        except Exception as e:  # noqa: BLE001
            raise PlanServiceError("拆解失败：%s" % e)
        return self._plan_to_view(plan, plan_id=None, created=None)

    # ---------------- 2) 拆解 + 建图（落库） ----------------
    def build(self, requirement: str, params: Optional[Dict[str, Any]] = None,
              tier: Optional[str] = None, plan_id: Optional[str] = None,
              operator: str = "operator") -> Dict[str, Any]:
        """一句需求 → 拆解 → 在共享任务表上建图。

        返回：plan_id / 创建的 task_id 列表 / 波次 / 工序清单。
        建图过程记一条 trace（plan_built），可回放。
        """
        requirement = (requirement or "").strip()
        if not requirement:
            raise PlanServiceError("需求不能为空")
        if len(requirement) > MAX_REQUIREMENT_LEN:
            raise PlanServiceError("需求过长（上限 %d 字）" % MAX_REQUIREMENT_LEN)
        try:
            plan = self._planner.decompose(requirement, params=params or {},
                                           tier=tier)
        except Exception as e:  # noqa: BLE001
            raise PlanServiceError("拆解失败：%s" % e)

        # 搜索意图打标：与 console_api.create_task 的单任务路径保持一致——
        # 走 planner 建图路径的搜索类需求（"今天重要国际新闻"等）也要接真实
        # 联网搜索，否则退化成模型凭记忆编造。console_chat 引 model_profiles
        # 等重模块，这里懒加载避免模块加载顺序/循环导入问题。
        try:
            from console_chat import detect_search_intent
            search_intent = bool(detect_search_intent(requirement))
        except Exception:  # noqa: BLE001
            search_intent = False

        # 建图前先自检：依赖缺失/成环在这里就会发现，不要留半张图
        try:
            plan.validate()
        except PlanError as e:
            raise PlanServiceError("拆解结果不合法（未建图）：%s" % e)

        pid = plan_id or ("plan-%d" % int(time.time() * 1000))
        trace_id = None
        try:
            trace_id = self.tracer.start_trace(
                title="拆解需求：%s" % (plan.title or requirement[:40]),
                kind="plan_build", started_by=operator)
        except Exception:  # noqa: BLE001 —— 留痕是旁路，不挡建图
            trace_id = None

        created: List[str] = []
        try:
            created = plan.build(self.scheduler, plan_id=pid, trace_id=trace_id,
                                web_search=search_intent)
        except Exception as e:  # noqa: BLE001
            # 建图中途失败：把已建的任务标记为失败，别留"看着能跑实际跑不通"的图
            self._abort(created, e)
            raise PlanServiceError("建图失败：%s" % e, detail={"created": created})

        if trace_id:
            try:
                self.tracer.record(trace_id, "plan_built", actor=operator,
                                   summary="拆解 %d 道工序" % len(created),
                                   detail={"plan_id": pid,
                                           "template": plan.template,
                                           "tier": plan.tier,
                                           "requirement": requirement,
                                           "task_ids": created})
                self.tracer.finish(trace_id, status="completed")
            except Exception:  # noqa: BLE001
                pass

        return self._plan_to_view(plan, plan_id=pid, created=created)

    # ---------------- 内部：失败清理 ----------------
    def _abort(self, created: List[str], err: Exception) -> None:
        """建图中途失败 → 已建任务转 failed，并记下原因。"""
        for tid in created or []:
            try:
                self.scheduler.fail(tid, error="建图失败：%s" % err)
            except Exception:  # noqa: BLE001 —— 清理是尽力而为
                pass

    # ---------------- 内部：视图序列化 ----------------
    def _plan_to_view(self, plan: Plan, plan_id: Optional[str],
                      created: Optional[List[str]]) -> Dict[str, Any]:
        waves = plan.waves()
        by_key = {s["key"]: s for s in plan.subtasks}
        # 波次里的 key → 显示名，前端直接渲染
        wave_view = [[by_key.get(k, {}).get("name", k) for k in w] for w in waves]
        subtasks = []
        for s in plan.subtasks:
            subtasks.append({
                "key": s["key"],
                "name": s["name"],
                "instruction": s["instruction"],
                "depends_on": list(s.get("depends_on") or []),
                "skill": s.get("skill", ""),
                "quality": s.get("quality", {}) or {},
                "needs_input": sorted(set(__import__("re").findall(
                    r"<待补:(\w+)>", s["instruction"]))),
                "task_id": ("%s/%s" % (plan_id, s["key"])) if plan_id else None,
            })
        needs_input = sorted({x for st in subtasks for x in st["needs_input"]})
        return {
            "ok": True,
            "requirement": plan.requirement,
            "template": plan.template,
            "title": plan.title,
            "tier": plan.tier,
            "cost": plan.cost or {},
            "slots": plan.slots or {},
            "waves": wave_view,
            "wave_keys": waves,
            "subtasks": subtasks,
            "needs_input": needs_input,
            "plan_id": plan_id,
            "created": created or [],
            "note": ("已建图 %d 个任务，等 runner 认领执行；"
                     "未配置算力模型时任务会如实失败，不会伪造产出。"
                     % len(created or []) if created else
                     "仅预览，未建图。"),
        }

    # ---------------- 模板清单（前端下拉） ----------------
    def templates(self) -> List[Dict[str, Any]]:
        """可用的拆解模板清单（前端展示"这类需求会被拆成什么样"）。"""
        out = []
        for t in getattr(self._planner, "templates", []) or []:
            out.append({
                "name": t.get("name"),
                "title": t.get("title"),
                "keywords": list(t.get("keywords") or ()),
                "stages": [{"key": s.get("key"), "name": s.get("name"),
                            "depends_on": list(s.get("depends_on") or [])}
                           for s in t.get("stages", [])],
            })
        return out
