"""
execution 包 —— 执行层（2026-10-25 对照 Grok Bot 差距分析后开建）。

定位（见差距分析）：治理层（任务登记/DAG/审批/仲裁/记忆）已经存在且是我们的
差异化；执行层补的是"AI 同事真正干活的地方"——每个 Bot 一个独立工作区、
一个常驻 daemon 认领执行、一个可编程浏览器。

纪律（与项目其他模块一致）：
- 命令执行一律走 sandbox.SandboxManager，**不自己起 subprocess**——
  越权预检、超时强杀、containment 标注都是现成的，不能绕过；
- 缺密钥/缺能力时**明确报错或降级到 Mock**，不静默造假；
- 任何"发送/发布/购买/删除"语义的动作，必须经过 ApprovalCenter 人工批准
  （daemon 里强制执行，见 execution/daemon.py）。
"""

from .workspace import BotWorkspace                                    # noqa: F401
from .daemon import ExecutionDaemon, run_daemon                        # noqa: F401
from .browser import BrowserSkill, find_edge                           # noqa: F401
