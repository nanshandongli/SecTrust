"""
execution/daemon.py —— 常驻执行 daemon（"AI 同事真正干活"的本地最小版）。

与 runner_daemon.py 的区别：那个是**模拟演示**（随机成败、假消息）；
这个是真执行——认领就绪任务、在 Bot 工作区里真跑命令、真写状态。

职责：
1. 心跳：每 heartbeat_interval 秒向 runners 表声明存活（复用 TaskRegistry.beat）；
2. 认领：通过 DagScheduler.claim_ready 在写锁内选+认领就绪任务（CAS，安全）；
3. 执行：按 payload 的 op 派发——
     {"op":"shell",  "cmd":[...], "timeout":30}   → 工作区沙箱执行
     {"op":"skill",  "name":"x"}                  → 激活技能（元数据+正文），
                                                  模型驱动的执行归 AgentRunner
     {"op":"llm",   "instruction":"..."}           → 调真实算力模型产出文本
                                                  （无模型则如实失败，不伪造）
     {"op":"write_files", "files":[{path,content}]}→ 把文件真正落盘到工作区：
                                                  路径校验 + 原子写 + 落盘后
                                                  密钥扫描；也支持给模型产出
                                                  原文自动提取代码块
     {"op":"noop"}                                → 空转（测试/演练用）
4. 审批门：payload 带 approval_action（send/publish/buy/delete 语义）时，
   **先请求人工批准再执行**；被拒 → 任务转 needs_human 带 feedback，不执行；
5. 收尾：complete(result=...) / fail(error=...)，DAG 级联由 scheduler 负责。

跨机协同（cluster，可选）：传入/自动装配 ClusterCoordinator 后——
- 认领守卫：claim_ready 之后再抢一把 Redis 任务锁；抢不到说明别的机器正在
  干，把任务放回 registered 不占用本机（共享库拓扑下这是双保险，每机本地
  库拓扑下这是主要互斥手段）；
- 跨机心跳：本地 beat 之外向 Redis 报心跳，掉线机器可被 alive_runners 发现；
- 长任务续期：处理中的任务锁随心跳循环 renew，防锁过期后被别的机器重抢；
- 失败记账：执行失败记入死信队列，反复失败（阈值）才真正入队，可人工恢复。
未配置（SECTRUST_REDIS 未开）时全部跳过，行为与从前完全一致。

诚实边界：
- 执行强度 = 沙箱后端的 containment（本地默认 advisory，Docker/E2B 才 os 级）；
- 不自动重试失败任务（重试策略属编排层决策，不在此暗做）；
- 跨机锁是"认领防护"不是硬租约：续期失败（过期被抢）只告警，本次仍跑完，
  任务级幂等由编排层保证；
- Windows spawn 兼容：入口 run_daemon() 是模块级函数（闭包无法 pickle）。

运行：
    python -m execution.daemon --db-path data/task_registry.db
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request

from orchestration import REGISTERED, DagScheduler
from sandbox import SandboxManager
from task_registry import TaskRegistry
from transport.distributed import ClusterCoordinator

from .workspace import BotWorkspace, default_root

# payload 里声明这些 action 的，执行前必须走人工审批（HITL 红线）
_APPROVAL_ACTIONS = {"send", "publish", "buy", "delete", "pay", "post", "high_risk"}

# shell 命令命中这些模式 → 视为高风险（破坏性/不可逆），**未显式声明审批时**
# 由 daemon 自动升级为 high_risk 审批门（拦截=先问人，而非直接拒绝或放行）。
# 这是"内部工具链"场景里"高风险操作拦截"的实现：宁可多一次确认，也不让 AI
# 静默跑 rm -rf / 管道装包 / 关机 这类命令。
_HIGH_RISK_SHELL_PATTERNS = [
    re.compile(r"\brm\s+-[rR]*f\b"),                 # rm -rf / rm -fr
    re.compile(r"\brm\s+-[fF]*r\b"),
    re.compile(r"\bdd\b[^|]*\bif="),                  # dd if=...
    re.compile(r"\bmkfs\b"),                          # 格式化
    re.compile(r":\(\)\s*\{.*\}\s*;:"),               # fork bomb
    re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba)?sh\b"),  # 下载即执行
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r">\s*/dev/sd[a-z]"),                  # 直写块设备
    re.compile(r"\bchmod\s+-R\s+777\b"),
]

# daemon 执行任务时的默认审批等待上限（秒）：超时未决 → 任务挂起等人工
_APPROVAL_WAIT = float(os.environ.get("SECTRUST_APPROVAL_WAIT", "120"))


class ExecutionDaemon:
    """常驻执行 daemon。run_once() 供测试/单批使用；run() 常驻循环。"""

    def __init__(
        self,
        db_path,
        bot_id: str = None,
        workspace_root: str = None,
        heartbeat_interval: float = 30.0,
        poll_interval: float = 2.0,
        approval_wait: float = None,
        sandbox=None,
        cluster=None,
        profiles=None,
        llm_engine_factory=None,
    ):
        self.reg = TaskRegistry(db_path=db_path)
        self.sched = DagScheduler(db_path=db_path, registry=self.reg)
        self.bot_id = bot_id or ("exec-" + self.reg.runner_id[-8:])
        self.workspace = BotWorkspace(self.bot_id, root=workspace_root,
                                      sandbox=sandbox or SandboxManager.auto())
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.approval_wait = approval_wait if approval_wait is not None else _APPROVAL_WAIT
        # 算力配置来源：默认从磁盘读（与控制台同一份 model_profiles.json）；
        # 传 profiles 则用注入的（测试用，避免依赖真实磁盘配置）。
        self._profiles = profiles
        # LLM 引擎工厂（可选注入，测试用）：传它时绕过真实 LlmEngine，
        # 直接返回注入的引擎实例——这样单测能在不连真实模型的情况下
        # 覆盖"llm 产出 → 质量门 → 打回/择优"全链路。
        self._llm_engine_factory = llm_engine_factory
        # 跨机协同：未显式传入则按环境变量自动装配。TTL 取心跳间隔 3 倍
        # （下限 60s）——漏一拍心跳不该被判死/锁过期。强开不可达时这里就抛错，
        # 绝不静默降级回单机语义（transport.distributed 的开关纪律）。
        if cluster is None:
            ttl = max(60, int(heartbeat_interval * 3))
            cluster = ClusterCoordinator.from_env(lock_ttl=ttl, heartbeat_ttl=ttl)
        self.cluster = cluster
        self._current_claim = None    # (task_id, token)：处理中的跨机任务锁
        self._stop = threading.Event()
        self._processed = []          # 本 daemon 处理过的 (task_id, status)

    # ---------------- 审批门：返回 (allowed, reason) ----------------
    def _check_approval(self, task_id: str, action: str) -> tuple:
        """执行前的 HITL 检查。未配 ApprovalCenter 时默认拒绝（安全侧）。"""
        try:
            from approval import ApprovalCenter
        except ImportError:            # pragma: no cover - 仓库内恒可导入
            return False, "审批中心不可用，默认拒绝"
        center = ApprovalCenter(db_path=self.reg.db_path)
        try:
            # 同一 task 已有 pending 审批则复用，避免重复弹窗
            existing = [a for a in center.list_approvals(status="pending", limit=100)
                        if a.get("task_id") == task_id]
            if existing:
                approval_id = existing[0]["approval_id"]
            else:
                approval_id = center.request(
                    action=action, detail="执行层待审动作 task=%s" % task_id,
                    task_id=task_id,
                )
            decision = center.wait_for_decision(
                approval_id, timeout=self.approval_wait, poll=1.0,
            )
            if decision == "approved":
                return True, approval_id
            return False, "审批未通过（%s）" % decision
        finally:
            center.close()

    # ---------------- 跨机认领守卫（cluster 未启用时全部放行） ----------------
    def _claim_guard(self, task_id: str):
        """claim_ready 之后抢 Redis 任务锁。

        返回 token（str，可能为空串=未启用无锁可放）放行；
        返回 None = 别的机器正在干这个任务——已把任务放回 registered，
        调用方应跳过。放行时调用方负责在 finally 里 _release_guard。
        """
        if not (self.cluster and self.cluster.enabled):
            return ""
        token = self.cluster.claim_task(task_id, self.reg.runner_id)
        if token is None:
            holder = self.cluster.holder("claim:" + task_id) or "其他机器"
            self.reg.update_task_status(task_id, REGISTERED)
            self.reg.set_progress(
                task_id, log=["跨机锁被 %s 持有，放回重排" % holder.split(":")[0]])
            return None
        return token

    def _release_guard(self, task_id: str, token: str):
        """放锁。失败只记录：锁带 TTL，最坏过期自动放，不挡主链路。"""
        if not token:
            return
        try:
            self.cluster.release_task(task_id, token)
        except Exception as e:      # noqa: BLE001 —— 旁路纪律：记账类操作不挡执行
            print("[exec:%s] 放跨机锁失败（将靠 TTL 过期）: %s"
                  % (self.reg.runner_id, e))

    def _note_cluster_failure(self, task_id: str, reason: str):
        """执行失败记入死信队列（阈值内只计数，反复失败才真正入队）。"""
        if not (self.cluster and self.cluster.enabled):
            return
        try:
            if self.cluster.note_failure(task_id, reason,
                                         payload={"bot_id": self.bot_id}):
                print("[exec:%s] 任务 %s 反复失败，已进死信队列（可人工恢复）"
                      % (self.reg.runner_id, task_id))
        except Exception as e:      # noqa: BLE001 —— 同上，旁路不挡主链路
            print("[exec:%s] 死信记账失败: %s" % (self.reg.runner_id, e))

    # ---------------- 执行派发 ----------------
    def _execute(self, task_id: str, payload: dict):
        """按 op 派发执行。返回 (ok, result_dict, error_str)。"""
        op = payload.get("op", "")
        if op == "noop":
            return True, {"op": "noop"}, None
        if op == "llm":
            return self._execute_llm(task_id, payload)
        if op == "scan":
            return self._execute_scan(task_id, payload)
        if op == "write_files":
            return self._execute_write_files(task_id, payload)
        if op == "shell":
            cmd = payload.get("cmd")
            if not cmd:
                return False, {}, "shell  op 缺少 cmd"
            res = self.workspace.run(cmd, timeout=float(payload.get("timeout", 30)))
            return res.ok, {
                "op": "shell", "stdout": res.content, "error": res.error,
                "backend": res.backend, "containment": res.containment,
                "duration": res.duration,
            }, res.error
        if op == "skill":
            name = payload.get("name", "")
            query = str(payload.get("query") or payload.get("skill_hint") or "")
            # 兼容路径：精确 name 且无检索意图 → 旧 loader.activate（保留既有行为，
            # 不破坏已验证的按名激活；模型驱动执行归 AgentRunner，不在此越界）。
            if name and not query:
                try:
                    from agentskills.loader import SkillLibrary, default_library_dir
                except ImportError:        # pragma: no cover
                    return False, {}, "技能登记处不可用"
                roots = [os.environ.get("SECTRUST_SKILLS_DIR") or default_library_dir()]
                lib = SkillLibrary(roots=roots)
                try:
                    spec = lib.activate(name)   # 激活 = 读全文（渐进式披露第 2 段）
                except KeyError:
                    return False, {}, "未注册的技能: %s" % name
                self.workspace.write_state({"last_skill": name, "last_skill_at": time.time()})
                return True, {"op": "skill", "name": name, "description": spec.description,
                              "body_bytes": spec.bytes}, None
            # v2 P0 新路径：query / skill_hint → SkillRouter 检索选技能 →
            # SkillExecutionGuard 分级门禁执行（把标准运行时真正接进执行链）。
            if query:
                return self._execute_skill_query(task_id, payload, query)
            return False, {}, ("skill op 需要 name（按名激活）或 "
                               "query/skill_hint（检索选技能）")
        if op == "image":
            return self._execute_image(task_id, payload)
        return False, {}, ("未知 op: %r（支持 shell/skill/noop/llm/scan/write_files/"
                           "image）" % op)

    # ---------------- Skill 检索执行（v2 P0：标准运行时接通主链路）----------------
    def _execute_skill_query(self, task_id: str, payload: dict, query: str):
        """op=skill 且带 query/skill_hint：L1 检索选技能 → 分级门禁执行。

        这是把 `StandardSkillLibrary` + `SkillRouter` + `SkillExecutionGuard`
        从「未接线的能力孤岛」接进真实执行链的落点。保留 `name` 精确路径
        （旧 loader.activate）作兼容，不破坏既有调用。

        诚实边界：
          · white 走 L2 正文激活，模型驱动的下游执行归 AgentRunner，不在此越界；
          · black 需审批而未决 → 如实返回失败 + 审批单 id，绝不静默执行；
          · 缺 trace 上下文不硬造孤儿轨迹（trace_written=False 由下游标注）。
        """
        try:
            from agentskills.router import SkillRouter
            from agentskills.guard_chain import SkillExecutionGuard
            from agentskills.skill_md import StandardSkillLibrary
            from agentskills.state import SkillStateStore
        except ImportError as e:
            return False, {}, "技能运行时不可用：%s" % e

        trace_id = str(payload.get("trace_id") or "")
        tracer = None
        if trace_id:
            try:
                from trace import TraceStore
                tracer = TraceStore(db_path=os.path.join(self._profiles_dir(), "trace.db"))
            except Exception:  # noqa: BLE001 - 轨迹不可用不挡执行
                tracer = None
        state = SkillStateStore(trace_store=tracer, trace_id=trace_id)
        lib = StandardSkillLibrary()
        # 认知升级：有可用 LLM（已配模型）才注入，否则 router 纯 heuristic 重排
        llm = self._llm_engine_factory() if callable(self._llm_engine_factory) else None
        # 夜间演进（从防守到智能）：装配反思引擎 / 分层记忆 / Confidence Loop。
        # 诚实边界 + 回归安全：
        #   · 经验库落在 task_registry.db 同目录（生产 data/，测试临时目录自动隔离）；
        #   · 无 LLM 时反思走规则式，绝不 Mock；经验复用只预填 feedback 不改门禁；
        #   · Confidence Loop 需显式配 weak/strong 模型 id（平台无可靠模型强弱元数据，
        #     不猜测）；未配则 configured=False → 退回原单层 LLM 重排，行为不变。
        try:
            from agentskills.reflection import build_runtime
            weak_model = os.environ.get("SECTRUST_CONFIDENCE_WEAK_MODEL", "").strip() or None
            strong_model = os.environ.get("SECTRUST_CONFIDENCE_STRONG_MODEL", "").strip() or None
            runtime = build_runtime(
                reflection_path=os.path.join(self._profiles_dir(), "experience.json"),
                llm=llm, weak_model=weak_model, strong_model=strong_model)
        except Exception:  # noqa: BLE001 - 反思运行时不可用不挡执行（诚实降级）
            runtime = {"reflection": None, "memory": None, "confidence_loop": None}
        router = SkillRouter(library=lib, state=state, trace_store=tracer,
                             trace_id=trace_id, llm=llm,
                             reflection=runtime["reflection"],
                             confidence_loop=runtime["confidence_loop"])
        sel = router.select(
            query, role=str(payload.get("role") or ""),
            context={"task_type": str(payload.get("task_type") or ""),
                     "web_search": bool(payload.get("web_search"))},
            k=int(payload.get("skill_k") or 3),
            session_id=str(payload.get("session_id") or "default"))
        result = {"op": "skill", "query": query, "skill_match": sel}
        # 方向一/三：把经验复用命中与置信升级决策一并回写（透明，不静默）
        if sel.get("experience_hint"):
            result["experience_hint"] = sel["experience_hint"]
        if sel.get("confidence_loop", {}).get("applied"):
            result["confidence_loop"] = sel["confidence_loop"]
        if not sel["ok"]:
            result["error"] = ("L1 未召回可用技能（可能全部被角色白名单 / 安装校验 / "
                               "熔断过滤）")
            return False, result, result["error"]

        chosen = sel["chosen"]
        result["name"] = chosen
        spec = lib.get(chosen)
        rel_path = str(payload.get("script") or "")
        if not rel_path and spec is not None and spec.has_scripts:
            rel_path = next((f for f in spec.files if f.startswith("scripts/")), "")

        approvals = None
        try:
            from approval import ApprovalCenter
            approvals = ApprovalCenter(db_path=self.reg.db_path)
        except Exception:  # noqa: BLE001 - 审批中心不可用由 guard fail-closed
            approvals = None
        guard = SkillExecutionGuard(library=lib, state=state, trace_store=tracer,
                                    trace_id=trace_id, approvals=approvals)
        try:
            res = guard.execute(
                chosen, rel_path, args=payload.get("args") or None,
                approval_id=str(payload.get("approval_id") or ""),
                actor=str(payload.get("created_by") or "operator"),
                artifacts=payload.get("artifacts") or None,
                security_tier=str(payload.get("security_tier") or ""),
                timeout=float(payload.get("timeout") or 30))
        finally:
            if approvals is not None:
                try:
                    approvals.close()
                except Exception:  # noqa: BLE001
                    pass
        result["guard"] = res
        result["execution_mode"] = res.get("execution_mode", "")
        result["feedback_hint"] = res.get("feedback_hint", "")
        # 方向二·分层记忆：无论成败，先把这次执行轨迹结构化落情景记忆；
        # 成功额外提炼程序记忆（工具组合/参数模式），供后续 query 检索推荐。
        mem_ctx = {"task_type": str(payload.get("task_type") or ""),
                   "role": str(payload.get("role") or "")}
        # 工具组合（程序记忆第二维）：从选中技能的 allowed_tools 取，只记名不记值
        chosen_tools = list(spec.allowed_tools) if spec is not None else []
        episode = None
        if runtime["memory"] is not None:
            try:
                episode = runtime["memory"].remember_episode(
                    chosen, query, bool(res["ok"]),
                    error_info=res.get("error_info") or None, context=mem_ctx,
                    tools=chosen_tools)
                result["memory_episode"] = {"skill": episode["skill"],
                                            "ok": episode["ok"],
                                            "tools": episode["tools"],
                                            "procedures": runtime["memory"].recall_procedures(
                                                query=query, limit=3) if res["ok"] else []}
            except Exception:  # noqa: BLE001 - 记忆旁路，失败不挡执行
                episode = None
        # 方向一闭环有效性反馈：这次若命中并复用了历史经验，回写「复用后是否成功」，
        # 让经验库累积有效率统计（而非只存不用）。成功/失败都如实记账，绝不夸大。
        if runtime["reflection"] is not None and sel.get("experience_match"):
            try:
                runtime["reflection"].store.mark_used(
                    sel["experience_match"].get("experience_id", ""),
                    success=bool(res["ok"]))
            except Exception:  # noqa: BLE001 - 记账旁路
                pass
        # ③ 结构化错误 + 自我反思提示：把门禁失败收敛成 ErrorInfo，并生成给 AI 的
        #   反思提示（主 Agent 循环消费，触发「下一步该怎么做」），绝不伪造已纠正。
        if not res["ok"]:
            result["error_info"] = res.get("error_info", {})
            try:
                from agentskills.error_structs import self_correction_prompt
                result["self_correction_hint"] = self_correction_prompt(
                    res.get("error_info"))
            except Exception:  # noqa: BLE001
                result["self_correction_hint"] = ""
            # 方向一·自我反思闭环：失败 → 错误归因 → 修正假设（LLM 或规则式降级）
            # → 落经验库 → 下次相似 query 命中即预填 feedback（本函数顶部 router 已复用）。
            if runtime["reflection"] is not None and result["error_info"]:
                try:
                    cached_keys = list((state.get_cached_params(chosen)
                                        if state is not None else {}).keys())
                    ref = runtime["reflection"].reflect(
                        query, result["error_info"],
                        context={"cached_param_keys": cached_keys})
                    result["reflection"] = {"category": ref["category"],
                                            "generated_by": ref["generated_by"],
                                            "experience_id": ref["experience_id"],
                                            "correction": ref["correction"]}
                except Exception:  # noqa: BLE001 - 反思旁路，失败不挡执行
                    pass
        else:
            # ② 会话记忆：执行成功后把调用方显式提供的「已获取环境变量」记入状态，
            #    供后续同技能调用注入上下文，避免 AI 重复询问（敏感键只记名不记值）。
            if state is not None:
                try:
                    state.cache_params(chosen, payload.get("cached_params") or {})
                except Exception:  # noqa: BLE001 - 缓存是旁路
                    pass
        self.workspace.write_state({"last_skill": chosen, "last_skill_at": time.time()})
        if not res["ok"]:
            err = "技能门禁未通过（stage=%s）：%s" % (res.get("stage"), res.get("reason"))
            if res.get("approval_id"):
                err += ("；审批单 %s 已挂起，请在审批中心批准后带 approval_id 重试"
                        % res["approval_id"])
            return False, result, err
        return True, result, None

    # ---------------- LLM 执行（多 AI 流水线真正干活的那一步） ----------------
    def _execute_llm(self, task_id: str, payload: dict):
        """按 instruction 调真实算力模型，产出文本。

        诚实边界：
        - **没有可用模型就报错，绝不用 Mock 假装干活**（与 console_chat 的
          LlmEngine 同一纪律：调不通必须说调不通）。
        - 模型未配置时返回 error（任务转 failed），而不是悄悄生成一段假文本。
        - 只调用用户在「设置」里配好的 profile；不读环境变量里的未知密钥。
        """
        instruction = str(payload.get("instruction") or "").strip()
        if not instruction:
            return False, {}, "llm  op 缺少 instruction"
        # **待补占位符不能发给模型**（踩过坑）：planner 抽不到的槽位会留
        # "<待补:topic>"，模型会把它当正文原样吐出来（"针对主题 <待补:topic>"）。
        # 缺参数是**要人来补**的事，不是让模型猜——如实失败并说清缺什么。
        missing = sorted(set(re.findall(r"<待补:(\w+)>", instruction)))
        if missing:
            return False, {}, ("缺少必要参数：%s——请补全后重新下发"
                               "（planner 抽不到的槽位不会自动编造）"
                               % "、".join(missing))

        # ---- 规则引擎前置（A5：guard_pipeline 取代散落的 check_input）----
        # 大小模型组合的**前端**：脱敏（PII 掩码）+ 提示注入/越狱检测。
        # 高危直接拦，不送进大模型；非高危时把 **脱敏后文本** 喂给大模型
        # （findings 只有类型/计数，不含原文，绝不把被掩码的明文外泄）。
        # 与后端 check_output 一起闭环：规则引擎前端脱敏+拦截 → 大模型中段
        # 语义 → 规则引擎后端 PII 回潮/注入回显拦截。
        guard_meta = {"input_risk": "low", "desensitize_findings": [],
                      "output_safe": True, "blocked": False}
        safe_instruction = instruction
        try:
            from governance import content_guard as _cg
            gp = _cg.guard_pipeline(instruction, level="medium")
            guard_meta["input_risk"] = gp["input_risk"]
            guard_meta["desensitize_findings"] = gp["desensitize_findings"]
            if gp["block"]:
                guard_meta["blocked"] = True
                return False, {}, ("内容防护拦截：检测到提示注入/越狱意图"
                                   "（规则引擎前置校验命中 %s）"
                                   % "、".join(h["rule"] for h in gp["input_hits"]))
            safe_instruction = gp["masked"]
        except Exception:  # noqa: BLE001 - 校验异常不阻断正常任务
            pass

        # ---- 真实联网搜索（搜索意图任务）----
        # 命中 web_search 标记的任务，先跑真实 web 搜索，把结果注入模型上下文，
        # 让模型"基于资料作答、标注来源"，而不是凭记忆编（之前会编出假日期）。
        # 诚实边界：搜索失败（网络/代理不可达）**绝不阻断任务**——如实记一笔、
        # 继续让模型按自身知识答；不伪造搜索结果、不假装搜过。
        search_meta = {"requested": bool(payload.get("web_search")),
                       "ok": False, "error": None, "count": 0, "query": None}
        web_block = ""
        if payload.get("web_search"):
            try:
                from execution.web_search import web_search, build_search_query
                # **指令 ≠ 搜索词**：指令里带任务要求（"给出3条要点"），
                # 整句丢给搜索引擎会命中垃圾结果。清洗出真正的搜索词。
                # 搜索词也基于**脱敏后文本**：绝不把原文里的 PII 发到搜索引擎。
                query = build_search_query(safe_instruction)
                search_meta["query"] = query
                sres = web_search(query, max_results=5, timeout=15)
                search_meta["ok"] = bool(sres.get("ok"))
                search_meta["error"] = sres.get("error")
                results = sres.get("results") or []
                search_meta["count"] = len(results)
                # 持久化真实检索来源（标题/链接/摘要），供前端任务详情透明展示——
                # 避免用户只看到「联网产出」标签却看不到任何内容（搜索成功但答案不佳时仍能溯源）。
                search_meta["results"] = results
                if results:
                    lines = ["【真实联网检索结果（非模型记忆，请基于下列资料作答并标注来源）】"]
                    for i, r in enumerate(results, 1):
                        lines.append("%d. %s\n   %s\n   %s" % (
                            i, r.get("title", ""), r.get("url", ""),
                            r.get("snippet", "")))
                    web_block = "\n".join(lines)
            except Exception as e:  # noqa: BLE001 - 搜索异常不可阻断主任务
                search_meta["error"] = "搜索模块异常：%s" % e

        try:
            from model_profiles import ModelProfileStore
            from console_chat import LlmEngine
        except ImportError as e:      # pragma: no cover - 仓库内恒可导入
            return False, {}, "模型层不可用: %s" % e
        if self._llm_engine_factory is not None:
            # 注入路径（测试）：绕过真实模型层，直接给引擎实例
            engine = self._llm_engine_factory()
            model_id = None
            chosen = None
            intended = ""
        else:
            profiles = self._profiles or ModelProfileStore(
                data_dir=self._profiles_dir())
            engine = LlmEngine(profiles)
            # 分工路由：按岗位绑定的同事派工，绑定不可用则诚实回退平台默认同事
            chosen, intended = self._select_profile(profiles, payload, "llm")
            if chosen is None:
                return False, {"intended_colleague": intended or None}, (
                    "无可用算力模型（未配置模型或缺少 API Key）——到「设置」里"
                    "为当前岗位绑定一个已配密钥的同事，或确保存在可用的"
                    "平台默认同事")
            model_id = chosen.id
        # 把真实检索结果并入背景上下文；有资料时加"标注来源"指令，
        # 让模型引用而非凭记忆——这是接真实搜索的核心目的。
        # 注意：这段在 if/else 之外（函数级），注入引擎路径与真实路径共用，
        # 否则注入路径会跳过错过的 else 块导致 out 未绑定（踩过坑）。
        ctx = self._task_context(task_id)
        final_instruction = safe_instruction
        if web_block:
            ctx = web_block + "\n\n" + (ctx or "")
            final_instruction = (safe_instruction +
                                 "\n\n（已附真实联网检索结果于 <background>，"
                                 "请基于资料作答，关键事实处标注来源链接；"
                                 "若资料不足请说明，不要编造。）")
        elif payload.get("web_search"):
            # 搜索意图任务但**没拿到可用资料**：必须显式拦住模型编造。
            # 否则模型会退回"凭记忆编"（曾编出假日期"2025-07-11"）。
            final_instruction = (safe_instruction +
                                 "\n\n（注意：本次未能获取真实联网资料"
                                 "（网络/代理不可达或检索结果均不相关，已被诚实过滤）。"
                                 "请勿编造具体日期、事件或数据；"
                                 "若无可依据的事实，请如实说明无法检索到实时信息。）")
        try:
            out = engine.execute(final_instruction, ctx, model=model_id)
        except RuntimeError as e:
            # **必须区分两类 RuntimeError**（踩过坑）：provider 层的 HTTP 错误
            # （429 过载 / 401 鉴权失败）也是 RuntimeError，若一律说成
            # "未配置模型"，排查方向会被带偏——用户会去反复换密钥。
            # 只有 LlmEngine 的哨兵串 no_llm_available 才代表"没配模型"。
            # 即使失败也如实带回 executed_by，让前端展示"本次尝试了哪位同事"。
            _fail = {"executed_by": (chosen.name or chosen.id) if chosen else None,
                     "intended_colleague": intended or None}
            msg = str(e)
            if "no_llm_available" in msg:
                return False, _fail, ("无可用算力模型（未配置模型或缺少 API Key）"
                                      "——到「设置」里配置模型后重试")
            return False, _fail, "模型调用失败：%s" % msg
        except Exception as e:         # noqa: BLE001
            _fail = {"executed_by": (chosen.name or chosen.id) if chosen else None,
                     "intended_colleague": intended or None}
            return False, _fail, "模型调用失败: %s" % e
        answer = str(out.get("answer") or "")
        if not answer.strip():
            return False, {}, "模型返回空内容"
        # ---- 规则引擎后置校验（后端合规：多模态内容防护 + 防 PII 回潮）----
        try:
            from governance import content_guard as _cg
            _oc = _cg.check_output(answer)
            guard_meta["output_safe"] = _oc["safe"]
            if not _oc["safe"]:
                # 模型产出含未脱敏 PII 或注入指令回显：阻断外发，转人工复核。
                _res = {
                    "op": "llm", "model": out.get("model"),
                    "output": "（内容防护：产出触发合规拦截——%s，已阻断外发并转人工复核）"
                              % "、".join(i["type"] for i in _oc["issues"]),
                    # 既有 issues/safe 保留供 UI，同时并入守卫决策三键
                    "content_guard": {**_oc, **guard_meta},
                }
                if search_meta.get("requested"):
                    _res["search"] = search_meta
                return False, _res, "内容防护后置校验拦截"
        except Exception:  # noqa: BLE001 - 校验异常不阻断正常产出
            pass
        result = {
            "op": "llm",
            "model": out.get("model"),
            "output": answer,
            "usage": out.get("usage") or {},
            # 守卫决策透明展示：前端风险/脱敏计数/后端输出安全（不含被掩码原文）
            "content_guard": guard_meta,
        }
        # ---- 分工透明化：记录实际由哪位 AI 同事负责 ----
        # executed_by 给前端展示"这个任务是谁干的"；intended_colleague 是创建时
        # 按岗位预期的同事（可能已回退）；routing_note 说明是否发生了回退。
        if chosen is not None:
            result["executed_by"] = chosen.name or chosen.id
        result["intended_colleague"] = intended or None
        if intended and chosen is not None and intended != chosen.id:
            result["routing_note"] = (
                "岗位绑定同事「%s」未配置或不可用，已回退到「%s」"
                % (intended, chosen.name or chosen.id))
        # 搜索元信息回写：UI 可据 search.ok / search.count 透明展示
        # "本次是否真联网、拿到几条真实结果"，不掩盖失败。
        if search_meta.get("requested"):
            result["search"] = search_meta
        return True, result, None

    # ---------------- 文生图算子（image op：把绘图接进执行层）----------------
    def _execute_image(self, task_id: str, payload: dict):
        """op=image：调 OpenAI 兼容 /images/generations 端点生成图片。

        通用 OpenAI 兼容框架：不绑定任何特定厂商，端点/base_url/api_key/model
        全部来自用户在「设置」里配的 profile（kind=image 优先，否则回退 active）。
        落盘到 data/images/，result 含 images 列表（本地文件 + 可供前端直链的
        /api/image/ 路径）。

        诚实边界（与 _execute_llm 同一纪律）：
        - 没配绘图模型 / 端点不支持文生图 → 如实报错，绝不 Mock 一张假图；
        - 端点是外部 HTTP，失败必须带状态码与响应体摘要，不能只说"检查配置"；
        - 绘制提示先过 guard_pipeline 脱敏，绝不把 PII 原文发到图像 endpoint。
        """
        prompt = str(payload.get("instruction") or payload.get("title") or "").strip()
        if not prompt:
            return False, {}, "image op 缺少绘制指令（instruction/title 为空）"

        # ---- 规则引擎前置：脱敏 + 注入/越狱拦截（复用 LLM 主链路同一套）----
        safe_prompt = prompt
        try:
            from governance import content_guard as _cg
            gp = _cg.guard_pipeline(prompt, level="medium")
            if gp["block"]:
                return False, {}, ("内容防护拦截：绘制请求含提示注入/越狱意图"
                                   "（规则引擎前置校验命中 %s）"
                                   % "、".join(h["rule"] for h in gp["input_hits"]))
            safe_prompt = gp["masked"]
        except Exception:  # noqa: BLE001 - 校验异常不阻断正常绘图
            pass

        # ---- 解析图像 profile（分工路由：优先岗位绑定的 image 同事，
        #      否则回退到 get_image()；绑定同事不可用则诚实回退）----
        try:
            from model_profiles import ModelProfileStore
        except ImportError as e:      # pragma: no cover - 仓库内恒可导入
            return False, {}, "模型层不可用: %s" % e
        profiles = self._profiles or ModelProfileStore(data_dir=self._profiles_dir())
        chosen, intended = self._select_profile(profiles, payload, "image")
        if chosen is None:
            return False, {"intended_colleague": intended or None}, (
                "未配置可用于绘图的模型——到「设置」里把相关岗位绑定到一个"
                "「类型=图像」的同事，或新增一个支持 /images/generations 的模型")
        img = chosen

        try:
            out = self._call_image_api(task_id, img, safe_prompt, payload)
        except Exception as e:         # noqa: BLE001
            return False, {}, "图像端点调用异常: %s" % e
        if not out.get("ok"):
            return False, out, out.get("error") or "图像生成失败"

        # ---- 分工透明化：记录实际由哪位 AI 同事出图 ----
        out["executed_by"] = img.name or img.id
        out["intended_colleague"] = intended or None
        if intended and intended != img.id:
            out["routing_note"] = (
                "岗位绑定同事「%s」未配置或不可用，已回退到「%s」"
                % (intended, img.name or img.id))
        return True, out, None

    def _call_image_api(self, task_id: str, profile, prompt: str, payload: dict) -> dict:
        """对单个 profile 调 /images/generations，落盘 b64 图片。

        返回统一结构：{ok, op:"image", model, images:[{file,url,local}], count, prompt}
        或 {ok:False, op:"image", error}。绝不抛异常到上层（上层只看 ok/error）。
        """
        base = (profile.base_url or "https://api.openai.com/v1").rstrip("/")
        url = base + "/images/generations"
        size = str(payload.get("image_size") or "1024x1024")
        try:
            n = int(payload.get("image_n") or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, 4))   # 单任务最多 4 张，避免超额/滥用
        body = {
            "prompt": prompt,
            "model": profile.model or "",
            "n": n,
            "size": size,
            "response_format": "b64_json",
        }
        # 部分兼容网关（如某些图像专用端点）不接受空 model 或 response_format，
        # 但 OpenAI 官方与主流兼容端点都收；缺省照发，失败由响应体如实暴露。
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % profile.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:   # noqa: S310
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                err_body = ""
            if len(err_body) > 400:
                err_body = err_body[:400] + "…"
            return {"ok": False, "op": "image",
                    "error": "图像端点返回 HTTP %s：%s" % (e.code, err_body or e.reason)}
        except Exception as e:         # noqa: BLE001 - URLError/超时等
            return {"ok": False, "op": "image", "error": "调用图像端点失败：%s" % e}

        items = data.get("data") or []
        if not items:
            return {"ok": False, "op": "image",
                    "error": "图像端点未返回任何图像（响应 data 为空）"}

        out_dir = os.path.join(self._profiles_dir(), "images")
        os.makedirs(out_dir, exist_ok=True)
        saved = []
        for i, it in enumerate(items):
            b64 = it.get("b64_json")
            remote = it.get("url")
            if b64:
                fn = "img-%s-%d.png" % (task_id, i + 1)
                fp = os.path.join(out_dir, fn)
                try:
                    with open(fp, "wb") as fh:
                        fh.write(base64.b64decode(b64))
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "op": "image",
                            "error": "图像落盘失败：%s" % e}
                saved.append({"file": fn, "url": "/api/image/" + fn, "local": True})
            elif remote:
                # 远端 URL（部分端点返回 url 而非 b64）：直接透传给前端，不下载
                saved.append({"file": None, "url": remote, "local": False})
            # 二者皆无：跳过该条（不计入成功，也不算失败）

        if not saved:
            return {"ok": False, "op": "image",
                    "error": "图像端点返回的项均无可用图像数据（b64/json 缺失）"}

        return {
            "ok": True, "op": "image",
            "model": "%s/%s" % (profile.provider, profile.model or ""),
            "prompt": prompt,
            "count": len(saved),
            "images": saved,
        }

    # ---------------- 密钥/凭证扫描（内部工具链：密钥扫描环节） ----------------
    def _execute_scan(self, task_id: str, payload: dict):
        """op=scan：对 target（文件/目录）或 text（内联片段）跑密钥扫描。

        复用 scanner 包（77+ 结构化规则 + 熵兜底），产出结构化 findings。
        **诚实边界**：扫描是"检测"不是"修复"——只报告命中，不动代码；
        找不到 target 时如实说明，不假装扫过。
        """
        target = payload.get("target")
        text = payload.get("text")
        if not target and not text:
            return False, {}, "scan op 缺少 target（路径）或 text（内联片段）"
        try:
            from scanner import scan_path
        except ImportError:            # pragma: no cover - 仓库内恒可导入
            return False, {}, "扫描引擎不可用"
        try:
            if text:
                # 内联片段：落临时文件以复用完整检测（含熵兜底），扫完即删
                import tempfile
                tf = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8")
                try:
                    tf.write(text)
                    tf.close()
                    res = scan_path(tf.name)
                finally:
                    try:
                        os.unlink(tf.name)
                    except OSError:
                        pass
            else:
                res = scan_path(target)
        except Exception as e:         # noqa: BLE001
            return False, {}, "扫描失败：%s" % e
        findings = res.get("findings") or []
        summary = res.get("summary") or {}
        gate = res.get("gate") or {}
        return True, {
            "op": "scan",
            "target": target,
            "findings_count": len(findings),
            "severity_counts": summary.get("by_severity", {}),
            "gate_fail": bool(gate.get("should_fail")),
            "gate_severity": gate.get("fail_on"),
            "findings": findings[:200],   # 截断存储，避免超大 payload 撑爆任务表
            "summary": summary,
        }, None

    # ---------------- 文件落盘（AI 同事真正把代码写进仓库） ----------------
    def _execute_write_files(self, task_id: str, payload: dict):
        """op=write_files：把 AI 同事产出的代码真正落到磁盘工作区。

        三种喂法：
        1. files: [{"path": "src/app.py", "content": "..."}] —— 显式清单；
        2. extract: "<模型产出的 markdown 原文>" —— 自动提取 fenced 代码块；
        3. from_upstream: true —— 直接消费上游（依赖）任务的 LLM 产出原文。

        安全纪律（本产品的本分）：
        · 路径一律过 BotWorkspace._resolve()，越出工作区即拒写；
        · 拒绝覆盖 .git/、data/、密钥与证书等仓库根基；
        · 原子写（临时文件 + os.replace），不留半个文件；
        · **落盘后立刻跑密钥扫描**，命中高危由 process_task 挂起等人工——
          而不是让带凭证的代码顺畅走到下一步。
        """
        from .writer import (extract_code_blocks, normalize_files,
                             plan_from_payload, write_files)
        try:
            if payload.get("from_upstream"):
                text = self._upstream_text(task_id)
                if not text.strip():
                    return False, {}, "from_upstream 但取不到上游产出文本"
                blocks = extract_code_blocks(text)
                if not blocks:
                    return False, {}, ("上游产出里没有 fenced 代码块，无法落盘"
                                       "（模型可能只给了说明没给代码）")
                items, blocked = normalize_files(blocks)
            else:
                items, blocked = plan_from_payload(payload)
        except Exception as e:          # noqa: BLE001 —— 解析失败如实报，不猜
            return False, {}, "落盘清单解析失败：%s" % e
        if not items:
            return False, {"op": "write_files", "written": [],
                           "blocked": blocked}, \
                "没有可落盘的文件（全部被拒或清单为空）"
        try:
            res = write_files(
                self.workspace, items, blocked=blocked,
                root=payload.get("root"),
                allow_outside=bool(payload.get("allow_outside")),
                overwrite=bool(payload.get("overwrite")),
            )
        except Exception as e:         # noqa: BLE001
            return False, {}, "落盘失败：%s" % e
        if not res.get("written"):
            return False, res, "所有文件均被拒写（见 blocked 原因）"
        return True, res, None

    def _upstream_text(self, task_id: str) -> str:
        """取所有上游任务的原始产出文本（供 write_files 提取代码块）。

        取不到返回空串：落盘阶段会据此如实失败，而不是凭空造文件。
        """
        bits = []
        try:
            task = self.reg.get_task(task_id)
            for dep in (task or {}).get("depends_on") or []:
                up = self.reg.get_task(dep)
                if not up:
                    continue
                p = up.get("payload") or {}
                out = p.get("result") or p.get("output")
                if isinstance(out, dict):
                    out = out.get("output") or out.get("stdout") or ""
                if out:
                    bits.append(str(out))
        except Exception:              # noqa: BLE001 —— 上下文是旁路
            pass
        return "\n\n".join(bits)

    def _resolve_profile_id(self, profiles, payload: dict):
        """把 payload 里的模型指向解析成一个 profile id。

        **语义区分（踩过坑）**：planner 写的 payload["model"] 是「模型名」
        （如 gpt-4o），而执行层需要的是「profile id」（如 zhipu-glm-4.7-flash）——
        两者不是一回事，直接把模型名当 id 传下去会查不到 profile。

        规则：
        1. payload["profile_id"] 明确给了 profile id → 直接用；
        2. payload["model"] 恰好**就是一个已存在的 profile id** → 用它；
        3. 否则 → 留空用当前 active（不猜、不报错）。
        """
        explicit = str(payload.get("profile_id") or "").strip()
        if explicit:
            return explicit
        model = str(payload.get("model") or "").strip()
        if model and profiles.get(model) is not None:
            return model
        return None

    def _select_profile(self, profiles, payload: dict, op: str):
        """分工路由：按岗位绑定 + op 类型，选出真正执行的 AI 同事（profile）。

        返回 (chosen_profile_or_None, intended_id)。

        诚实回退纪律（单一兜底点，全平台只有这里决定"到底用谁"）：
        - **优先** payload["profile_id"]（create_task 已按岗位绑定写入）；
        - image op 只接受 kind=image 的同事；否则退化到 get_image()；
        - llm op 的绑定同事不可用时，回退到当前 active（平台默认同事）；
        - 任何链路都要求 configured（有密钥），否则继续回退；
        - 全都没有可用同事 → chosen=None，调用方如实报"无可用算力模型"。

        intended 永远回传创建时预期的同事 id（可能已回退），供前端透明说明。
        """
        intended = str(payload.get("profile_id") or "").strip()
        chosen = None
        if op == "image":
            # 画图：绑定同事必须本身就是 image 类型，否则退化到图像专用回退
            if intended:
                p = profiles.get(intended)
                if p is not None and p.configured and getattr(p, "kind", "chat") == "image":
                    chosen = p
            if chosen is None:
                img = profiles.get_image()
                if img is not None and img.configured:
                    chosen = img
        else:
            if intended:
                p = profiles.get(intended)
                if p is not None and p.configured:
                    chosen = p
            if chosen is None:
                act = profiles.get_active()
                if act is not None and act.configured:
                    chosen = act
        return chosen, intended

    def _profiles_dir(self) -> str:
        """模型配置文件所在目录（与控制台同一份 data/）。"""
        # task_registry.db 通常就在 data/ 下；用它的父目录，保证 daemon 与
        # 控制台读到**同一份** model_profiles.json。
        db = os.path.abspath(self.reg.db_path)
        parent = os.path.dirname(db)
        return parent if os.path.basename(parent) else "data"

    def _task_context(self, task_id: str) -> str:
        """喂给模型的事实上下文：上游产出 + 打回反馈。

        没有上游也不报错——上下文为空时模型照常干活，只是不知道上游写了什么。
        """
        bits = []
        try:
            task = self.reg.get_task(task_id)
            payload = (task or {}).get("payload") or {}
            fb = payload.get("last_feedback")
            if fb:
                bits.append("【上一轮被打回的反馈，请据此修订】\n%s" % fb)
            for dep in (task or {}).get("depends_on") or []:
                up = self.reg.get_task(dep)
                if not up:
                    continue
                up_payload = up.get("payload") or {}
                out = up_payload.get("result") or up_payload.get("output")
                if isinstance(out, dict):
                    out = out.get("output") or out.get("stdout") or ""
                if not out:
                    continue
                bits.append("【上游任务 %s 的产出】\n%s" % (dep, str(out)[:4000]))
        except Exception:              # noqa: BLE001 —— 上下文是旁路，取不到不挡执行
            pass
        # v2 反馈闭环：最近被熔断/降权/驳回的技能提示回灌上下文，让 AI 在
        # 后续对话能自我纠正（"上次被 X 门驳回，是否尝试备选技能"）。
        # **提示不是自动改道**：备选技能仍要过 L1 检索 + 角色白名单 + 门禁，
        # 不因「上次失败」绕过任何安全检查。
        try:
            from agentskills.state import SkillStateStore
            store = SkillStateStore()
            hints = []
            for name, snap in store.snapshot().get("skills", {}).items():
                if snap.get("status") != "ok":
                    h = store.hint_for(name)
                    if h:
                        hints.append(h)
            if hints:
                bits.append("【技能运行时反馈（自我纠正提示，非自动改道）】\n"
                            + "\n".join(hints[:3]))
        except Exception:              # noqa: BLE001 - 状态不可用不挡执行
            pass
        return "\n\n".join(bits)

    # ---------------- 出域审批门（A1：KB 分级 fail-closed）----------------
    def _check_egress(self, payload: dict) -> str:
        """返回空串=放行，非空=拒绝原因。

        只在 payload **显式声明** kb_refs / egress_destination 时触发——不破坏
        既有 web_search 链路（搜索出域由内容防护前置脱敏覆盖；未声明外发目标的
        任务不额外拦）。任何校验异常一律拒绝（fail-closed，怕泄密）。
        """
        refs = payload.get("kb_refs")
        dest = payload.get("egress_destination")
        if not refs and not dest:
            return ""
        try:
            from governance import kb_control as _kb
            res = _kb.check_egress(refs, destination=dest or "*")
        except Exception as e:  # noqa: BLE001 - fail-closed：异常即拒绝
            return "出域审批校验异常（fail-closed 拒绝）：%s" % e
        if not res.get("ok"):
            return res.get("reason") or "出域审批未通过"
        return ""

    # ---------------- 单任务处理 ----------------
    def process_task(self, task_id: str, payload: dict) -> str:
        """处理一个已认领的任务，返回最终状态。

        执行链（按场景补全后的真实闭环）：
        1. 审批门：payload 声明 approval_action（含 high_risk）必须人工批准；
        2. **投票汇合**：ballot_gather 任务跳过 LLM，直接 rank() 出确定性推荐；
        3. 执行：按 op 派发（shell/skill/llm/scan/write_files/noop）；
        3.5 落盘闸门：write_files 落盘后扫描，命中高危 → needs_human 等人工；
        4. **质量门**：payload 带 quality 规则时送 QualityGate，不合格打回重做
           （rework），超限升级 needs_human——"递归校验"真实发生；
        5. **投票登记**：带 ballot 元信息的分支任务完成后 submit_proposal 登记打分；
        6. 收尾：complete(result=...) / fail(error=...)。
        """
        # ---- 1) 审批门（HITL 红线） ----
        action = str(payload.get("approval_action", "")).strip()
        # 高风险 shell 命令且未显式声明审批 → 自动升级为 high_risk 审批门（拦截）
        if (not action and payload.get("op") == "shell"
                and self._is_high_risk_shell(payload.get("cmd"))):
            action = "high_risk"
            payload = dict(payload, approval_action="high_risk")
            # 回写任务 payload：审计/UI 应能看到"高危待审批"，而非伪装成普通任务
            self.reg.set_payload(task_id, payload)
        if action:
            if action.lower() not in _APPROVAL_ACTIONS:
                self.sched.fail(task_id, error="未知 approval_action: %s" % action)
                return "failed"
            allowed, why = self._check_approval(task_id, action)
            if not allowed:
                # 被拒/超时 → 挂起等人工（needs_human 不级联，保留现场）
                self.reg.update_task_status(task_id, "needs_human")
                self.reg.set_progress(task_id, log=["审批未通过：%s" % why])
                return "needs_human"

        # ---- 1.5) 出域审批门（A1：KB 分级 fail-closed，审批门之后、执行之前）----
        # 任务显式声明 kb_refs（引用 KB 条目）或 egress_destination（外发目标）
        # 时逐条过 EgressApproval.is_allowed；confidential 未批准 → needs_human，
        # 照抄审批门写法（不级联、保留现场），绝不静默出域。
        egress_why = self._check_egress(payload)
        if egress_why:
            self.reg.update_task_status(task_id, "needs_human")
            self.reg.set_progress(task_id, log=["出域审批门拦截：%s" % egress_why])
            return "needs_human"

        # ---- 2) 投票汇合：确定性择优，不跑 LLM ----
        if isinstance(payload.get("ballot_gather"), dict):
            return self._run_gather(task_id, payload)

        # ---- 3) 执行 ----
        ok, result, error = self._execute(task_id, payload)
        if not ok:
            self.sched.fail(task_id, error=error or "执行失败")
            self._note_cluster_failure(task_id, error or "执行失败")
            return "failed"

        # ---- 3.5) 落盘后密钥闸门 ----
        # AI 同事写进去的代码若混了真实凭证，就地挂起等人工：文件**已经写入**，
        # 不能假装没发生，也不能执行层偷偷删——由人来确认或回滚。
        if isinstance(result, dict) and result.get("op") == "write_files":
            scan = result.get("scan") or {}
            if scan.get("should_fail"):
                why = scan.get("fail_reason") or "命中高危凭证"
                self.reg.update_task_status(task_id, "needs_human")
                self.reg.set_progress(task_id, log=[
                    "落盘完成但触发密钥闸门：%s——文件已写入，等待人工确认或回滚" % why])
                return "needs_human"

        text_out = result.get("output") or result.get("stdout") or ""
        already_completed = False

        # ---- 4) 质量门：递归校验/打回 ----
        # image op 产出的是图片而非 Markdown 文本——跳过文本质量门，
        # 否则会像早期那样因"无标题行"反复打回、升级 needs_human。
        rules = payload.get("quality")
        if result.get("op") != "image" and rules and isinstance(text_out, str):
            status = self._quality_review(task_id, payload, text_out, result)
            if status != "done":
                return status          # rework（registered）/ needs_human / failed
            already_completed = True   # QualityGate 已 complete（带完整 result）

        # ---- 5) 投票登记：分支方案送审打分 ----
        if isinstance(payload.get("ballot"), dict):
            self._submit_proposal(task_id, payload, text_out)

        # ---- 6) 收尾 ----
        if not already_completed:
            self.sched.complete(task_id, result=result)
        return "done"

    # ---------------- 质量门接入（递归校验/打回） ----------------
    def _quality_gate(self) -> "QualityGate":
        """复用 agentskills.quality.QualityGate，质量库与本任务库同目录。"""
        from agentskills.quality import QualityGate
        db = os.path.join(os.path.dirname(os.path.abspath(self.reg.db_path)),
                          "quality.db")
        return QualityGate(db_path=db)

    def _quality_review(self, task_id: str, payload: dict, text_out: str,
                        result: dict) -> str:
        """送质量门；返回 done / rework / needs_human / failed。

        done 时 QualityGate 已 complete 任务（result_override 保留模型/usage）；
        调用方据此跳过二次 complete。
        """
        qg = self._quality_gate()
        try:
            v = qg.review(self.sched, task_id, text_out,
                          rules=payload.get("quality"), result_override=result)
        except Exception as e:      # noqa: BLE001 —— 质量门自身异常：如实 fail，不假装
            self.sched.fail(task_id, error="质量门异常：%s" % e)
            return "failed"
        # QualityGate 每次 _conn() 自开自关，无持久连接，无需 close()
        st = v.get("status")
        if st == "completed":
            return "done"
        if st == "needs_human":
            return "needs_human"
        if st == "registered":
            return "rework"          # 已打回，等 worker 修订后重新认领
        # not_running 等异常态：不静默放行，如实失败
        self.sched.fail(task_id, error="质量门未生效：%s" % (v.get("reason") or st))
        return "failed"

    # ---------------- 投票/择优接入（确定性，不靠 LLM 祈祷） ----------------
    def _ballot(self) -> "Ballot":
        """复用 ballot.Ballot，投票库与本任务库同目录。"""
        from ballot import Ballot
        db = os.path.join(os.path.dirname(os.path.abspath(self.reg.db_path)),
                          "ballot.db")
        return Ballot(db_path=db)

    def _submit_proposal(self, task_id: str, payload: dict, text_out: str):
        """分支任务完成后登记方案 + 按 quality 规则打分（确定性）。"""
        b = payload.get("ballot") or {}
        group = b.get("group")
        if not group:
            return
        ballot = self._ballot()
        try:
            ballot.submit_proposal(group, task_id, text_out, source="daemon")
        except Exception as e:      # noqa: BLE001 —— 提案登记是审计旁路，失败不挡主链路
            print("[exec:%s] 投票提案登记失败 %s: %s"
                  % (self.reg.runner_id, task_id, e))
        finally:
            ballot.close()

    def _run_gather(self, task_id: str, payload: dict) -> str:
        """ballot_gather 汇合任务：确定性 rank() 出推荐，不自动确认。

        推荐≠生效——胜出仍由人类在控制台 confirm()（Ballot 纪律：
        默认不自动生效）。本任务 complete 后把推荐结果写进 result 供前端展示。
        """
        bg = payload["ballot_gather"]
        group = bg.get("group")
        if not group:
            self.sched.fail(task_id, error="ballot_gather 缺少 group")
            return "failed"
        ballot = self._ballot()
        try:
            ranked = ballot.rank(group)
        except Exception as e:      # noqa: BLE001
            self.sched.fail(task_id, error="投票排序失败：%s" % e)
            return "failed"
        finally:
            ballot.close()
        rec = ranked.get("recommended")
        self.sched.complete(task_id, result={
            "op": "ballot",
            "group": group,
            "recommended": rec,
            "confidence": ranked.get("confidence"),
            "needs_human": ranked.get("needs_human"),
            "reason": ranked.get("reason"),
            "candidates": [p["task_id"] for p in (ranked.get("ranked") or [])],
        })
        return "done"

    # ---------------- 高风险命令识别 ----------------
    def _is_high_risk_shell(self, cmd) -> bool:
        """shell cmd 是否命中破坏性/不可逆模式（见 _HIGH_RISK_SHELL_PATTERNS）。"""
        if not cmd:
            return False
        s = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        return any(p.search(s) for p in _HIGH_RISK_SHELL_PATTERNS)

    # ---------------- 主循环 ----------------
    def run_once(self, max_tasks: int = 1) -> list:
        """处理一批就绪任务（测试/单批用）。返回 [(task_id, status), ...]。

        注意：claim_ready 返回的是**完整任务 dict**（无就绪任务返回 None），
        不是 task_id；payload 在个别路径下可能是 JSON 字符串，统一解析成 dict。
        status 除 done/failed/needs_human 外还可能是 "requeued"——跨机锁被
        别的机器持有，任务已放回 registered 重排（计入批次上限，防打转）。
        """
        out = []
        # 应急处置与熔断：熔断开启时本执行器不认领任何新任务（任务留在 registered），
        # 实现"出事能熔断"。状态落盘，由控制台触发/解除，进程无关。
        try:
            from governance import circuit_breaker as _cb
            if _cb.is_open():
                return out
        except Exception:  # noqa: BLE001 - 熔断检查失败按未熔断处理，不拖垮执行
            pass
        for _ in range(max_tasks):
            task = self.sched.claim_ready(runner_id=self.reg.runner_id)
            if not task:
                break
            task_id = task["task_id"]
            payload = task.get("payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            token = self._claim_guard(task_id)
            if token is None:
                out.append((task_id, "requeued"))
                self._processed.append((task_id, "requeued"))
                continue
            self._current_claim = (task_id, token) if token else None
            try:
                status = self.process_task(task_id, payload)
            finally:
                self._current_claim = None
                self._release_guard(task_id, token)
            out.append((task_id, status))
            self._processed.append((task_id, status))
            # 任务终态 → 远程通道推送（飞书/企微）。旁路：推送失败不影响执行。
            self._notify_terminal(task_id, status, payload)
        return out

    def _notify_terminal(self, task_id: str, status: str, payload: dict):
        """把任务终态推送到已订阅的远程通道。**异常一律吞掉**（旁路纪律）。"""
        try:
            import notify as _notify
            title = str(payload.get("title") or payload.get("cmd") or "")[:120]
            _notify.push_task_event(status, task_id, title=title,
                                    detail="执行器：%s" % self.reg.runner_id)
        except Exception:  # noqa: BLE001 —— 推送绝不挡执行
            pass

    def run(self, duration: float = None):
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        print("[exec:%s] 启动 | bot=%s | 工作区=%s | 心跳 %.0fs"
              % (self.reg.runner_id, self.bot_id, self.workspace.dir, self.heartbeat_interval))
        deadline = (time.time() + duration) if duration else None
        try:
            while not self._stop.is_set():
                if deadline and time.time() >= deadline:
                    break
                try:
                    self.run_once(max_tasks=4)
                except Exception as e:      # 单批异常不拖垮常驻进程
                    print("[exec:%s] 批次异常: %s" % (self.reg.runner_id, e))
                self._stop.wait(self.poll_interval)
        finally:
            self._stop.set()
            hb.join(timeout=1)
            self.close()

    def _heartbeat_loop(self):
        self.reg.beat()
        self._cluster_beat()
        while not self._stop.wait(self.heartbeat_interval):
            try:
                self.reg.beat()
            except Exception as e:          # 心跳失败只记录，不退出
                print("[exec:%s] 心跳失败: %s" % (self.reg.runner_id, e))
            self._cluster_beat()

    def _cluster_beat(self):
        """跨机心跳 + 处理中任务锁的续期（长任务防锁过期被重抢）。"""
        if not (self.cluster and self.cluster.enabled):
            return
        try:
            self.cluster.beat(self.reg.runner_id,
                              meta={"bot_id": self.bot_id})
        except Exception as e:      # noqa: BLE001 —— 旁路纪律
            print("[exec:%s] 跨机心跳失败: %s" % (self.reg.runner_id, e))
        cur = self._current_claim
        if not cur:
            return
        task_id, token = cur
        name = "claim:" + task_id
        try:
            if self.cluster.renew(name, token):
                return
            # 续期失败：锁消失是正常竞态（刚放掉），易主才值得喊
            holder = self.cluster.holder(name)
            if holder and holder != token:
                print("[exec:%s] 任务 %s 的跨机锁已易主（过期被抢？），"
                      "本次仍跑完，幂等由编排层保证"
                      % (self.reg.runner_id, task_id))
        except Exception as e:      # noqa: BLE001 —— 旁路纪律
            print("[exec:%s] 跨机锁续期失败: %s" % (self.reg.runner_id, e))

    def stop(self):
        self._stop.set()

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.sched.close()
        finally:
            try:
                if self.cluster and self.cluster.enabled:
                    # 优雅下线：先摘心跳（别让别的机器以为我们还活着），再关连接
                    try:
                        self.cluster.heartbeat.drop(self.reg.runner_id)
                    except Exception:    # noqa: BLE001 —— 下线清理尽力而为
                        pass
                    self.cluster.close()
            finally:
                self.reg.close()
        print("[exec:%s] 已停止" % self.reg.runner_id)


def run_daemon(db_path, duration=None, bot_id=None, workspace_root=None):
    """模块级入口（Windows spawn 兼容：闭包无法 pickle，target 必须是模块级函数）。"""
    daemon = ExecutionDaemon(db_path=db_path, bot_id=bot_id, workspace_root=workspace_root)

    def _handler(signum, _frame):
        print("收到信号 %s，准备退出…" % signum)
        daemon.stop()

    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):
        pass
    daemon.run(duration=duration)
    return daemon


def main():
    ap = argparse.ArgumentParser(description="常驻执行 daemon（真执行，非模拟）")
    ap.add_argument("--db-path", default="data/task_registry.db")
    ap.add_argument("--bot-id", default=None, help="Bot 标识（默认 exec-<runner 后 8 位>）")
    ap.add_argument("--workspace-root", default=None,
                    help="工作区根目录（默认 ~/.sectrust/workspaces）")
    ap.add_argument("--heartbeat", type=float, default=30.0)
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--duration", type=float, default=None, help="仅测试：运行 N 秒后退出")
    args = ap.parse_args()
    daemon = ExecutionDaemon(
        db_path=args.db_path, bot_id=args.bot_id, workspace_root=args.workspace_root,
        heartbeat_interval=args.heartbeat, poll_interval=args.poll,
    )
    _install(daemon)
    daemon.run(duration=args.duration)


def _install(daemon):
    def _handler(signum, _frame):
        print("收到信号 %s，准备退出…" % signum)
        daemon.stop()
    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):
        pass


if __name__ == "__main__":
    main()
