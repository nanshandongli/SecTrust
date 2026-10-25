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
import json
import os
import signal
import threading
import time

from orchestration import REGISTERED, DagScheduler
from sandbox import SandboxManager
from task_registry import TaskRegistry
from transport.distributed import ClusterCoordinator

from .workspace import BotWorkspace, default_root

# payload 里声明这些 action 的，执行前必须走人工审批（HITL 红线）
_APPROVAL_ACTIONS = {"send", "publish", "buy", "delete", "pay", "post"}

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
    ):
        self.reg = TaskRegistry(db_path=db_path)
        self.sched = DagScheduler(db_path=db_path, registry=self.reg)
        self.bot_id = bot_id or ("exec-" + self.reg.runner_id[-8:])
        self.workspace = BotWorkspace(self.bot_id, root=workspace_root,
                                      sandbox=sandbox or SandboxManager.auto())
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.approval_wait = approval_wait if approval_wait is not None else _APPROVAL_WAIT
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
            # 激活 = 记录元数据+正文；模型驱动执行归 AgentRunner，不在此越界
            self.workspace.write_state({"last_skill": name, "last_skill_at": time.time()})
            return True, {"op": "skill", "name": name, "description": spec.description,
                          "body_bytes": spec.bytes}, None
        return False, {}, "未知 op: %r（支持 shell/skill/noop）" % op

    # ---------------- 单任务处理 ----------------
    def process_task(self, task_id: str, payload: dict) -> str:
        """处理一个已认领的任务，返回最终状态。"""
        action = str(payload.get("approval_action", "")).strip()
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
        ok, result, error = self._execute(task_id, payload)
        if ok:
            self.sched.complete(task_id, result=result)
            return "done"
        self.sched.fail(task_id, error=error or "执行失败")
        self._note_cluster_failure(task_id, error or "执行失败")
        return "failed"

    # ---------------- 主循环 ----------------
    def run_once(self, max_tasks: int = 1) -> list:
        """处理一批就绪任务（测试/单批用）。返回 [(task_id, status), ...]。

        注意：claim_ready 返回的是**完整任务 dict**（无就绪任务返回 None），
        不是 task_id；payload 在个别路径下可能是 JSON 字符串，统一解析成 dict。
        status 除 done/failed/needs_human 外还可能是 "requeued"——跨机锁被
        别的机器持有，任务已放回 registered 重排（计入批次上限，防打转）。
        """
        out = []
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
        return out

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
