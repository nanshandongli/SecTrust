"""
governance —— 安全合规治理层（对齐《智能体应用安全基本要求》六项强制能力）

本包在不破坏既有架构（security.oauth / console_api.SessionStore / approval /
audit_suite / assets）的前提下，补齐"专业选型硬性要求"里尚未覆盖的能力：

  · rbac.py           身份认证与权限（把现有 JWT 会话的 role 落到端点的强校验）
  · circuit_breaker.py 应急处置与熔断（全局紧急熔断，daemon 认领前检查）
  · asset_registry.py  资产盘点与管理（AI 同事 / 工具 / 模型 / 数据源 的清册）
  · content_guard.py   多模态内容防护 + 大小模型组合（规则引擎做前端脱敏 /
                       后端合规校验，大模型只做中段语义）
  · interop.py         互联互通（GB/Z 185—2026 精神的智能体身份/能力/发现描述符）
  · compliance.py      全链路审计追溯（能力映射）+ 合规自评报告（可验证优先）

设计纪律（与项目一致）：零第三方依赖（仅标准库 + 既有模块）；失败诚实降级，
不伪造"已合规"；状态进程无关（熔断/资产清册落盘，控制台与 daemon 共享）。
"""

from . import rbac
from . import circuit_breaker
from . import asset_registry
from . import content_guard
from . import interop
from . import compliance
from . import kb_control
from . import etsi

__all__ = [
    "rbac",
    "circuit_breaker",
    "asset_registry",
    "content_guard",
    "interop",
    "compliance",
    "kb_control",
    "etsi",
]

__version__ = "0.1.0"
