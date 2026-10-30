"""
rbac.py —— 身份认证与权限（把 JWT 会话的 role 落到端点的强校验）

既有底座：security.oauth 负责"令牌真伪"（HS256 验签 + aud 绑定 + 过期 + 吊销）；
console_api.SessionStore 签发带 role scope 的 JWT。本模块负责"令牌真伪"之上的
第二层——**这个角色能不能做这个动作**，即国标六项能力之"身份认证与权限"。

设计：
  · 权限以 "域.动作" 命名（task.create / files.write / governance.emergency …）；
  · 角色 → 权限集合为显式白名单（默认拒绝，未列出的角色只给只读壳）；
  · 守卫是纯函数，不依赖 HTTP/会话对象，便于单测与跨层（控制台 / API）复用；
  · 新增 admin 超管角色，握有关急熔断等高危权限；现有业务角色保持可用，不破现有会话。
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, Optional

# --------------------------------------------------------------------------
# 权限目录（所有可授权动作）
# --------------------------------------------------------------------------
PERMISSIONS: FrozenSet[str] = frozenset({
    # 任务
    "task.create",        # 新建 / 提交任务
    "task.read",          # 查看任务与结果
    "task.reset",         # 复位任务（高危：重跑）
    # 落盘
    "files.write",        # 写文件到工作区（高危）
    # 审批链
    "approval.read",      # 查看待审批
    "approval.act",       # 批准 / 驳回（改变任务走向）
    # 治理（熔断 / 资产 / 合规 / 互联）
    "governance.emergency",  # 触发 / 解除全局熔断（最高危）
    "governance.read",       # 查看治理状态
    "asset.read",         # 查看资产清册
    "asset.write",        # 登记 / 注销资产
    "compliance.read",    # 查看合规自评
    "compliance.export",  # 导出合规证据包
    "interop.read",       # 查看智能体互联描述符
    # 阶段 A 新增（知识库分级 / 出域审批 / 联邦互联写操作）
    "kb.label",           # 给 KB 条目打分级标签
    "egress.request",     # 申请数据出域
    "interop.write",      # 登记远程平台 / 联邦发现 / 任务委派（高危）
    "routing.write",      # 改岗位↔AI同事绑定（全局配置，限 admin）
})

# --------------------------------------------------------------------------
# 角色 → 权限白名单
#   复用 console_api.ROLES 的角色 id（designer/writer/auditor/analyst/publisher），
#   并补充 admin 超管。新增权限时只在此处增删，守卫逻辑不变。
# --------------------------------------------------------------------------
ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    # 安全审计岗：可提交、复位、审批、导出合规证据（默认登录角色）
    "auditor": frozenset({
        "task.create", "task.read", "task.reset",
        "approval.read", "approval.act",
        "governance.read", "asset.read",
        "compliance.read", "compliance.export", "interop.read",
        # 审计岗可打 KB 分级标签、发起出域申请；出域决策走 approval.act，
        # 其中 confidential 级需 governance.emergency（在 EgressApproval.decide 内二次把关）
        "kb.label", "egress.request",
    }),
    # 素材设计 / 内容撰写 / 数据分析：普通业务岗，可提交与查看
    "designer": frozenset({"task.create", "task.read",
                           "approval.read", "governance.read",
                           "asset.read", "compliance.read", "interop.read"}),
    "writer": frozenset({"task.create", "task.read",
                         "approval.read", "governance.read",
                         "asset.read", "compliance.read", "interop.read"}),
    "analyst": frozenset({"task.create", "task.read",
                          "approval.read", "governance.read",
                          "asset.read", "compliance.read", "interop.read"}),
    # 发布执行岗：含高危落盘，但不可复位/审批/熔断
    "publisher": frozenset({"task.create", "task.read", "files.write",
                            "approval.read", "governance.read",
                            "asset.read", "compliance.read", "interop.read"}),
    # 超管：全权（含紧急熔断与资产登记）
    "admin": PERMISSIONS,
}

# 未知角色给最小只读壳（默认拒绝，绝不放大权限）
_DEFAULT_PERMS: FrozenSet[str] = frozenset({
    "task.read", "approval.read", "governance.read",
    "asset.read", "compliance.read", "interop.read",
})


def role_permissions(role_id: Optional[str]) -> FrozenSet[str]:
    """返回角色拥有的权限集合（未知角色给最小只读壳）。"""
    if role_id and role_id in ROLE_PERMISSIONS:
        return ROLE_PERMISSIONS[role_id]
    return _DEFAULT_PERMS


def has_permission(role_id: Optional[str], perm: str) -> bool:
    """角色是否拥有某权限。"""
    if perm not in PERMISSIONS:
        # 未知权限一律拒绝（防御：避免拼写错误放大权限）
        return False
    return perm in role_permissions(role_id)


def require_permission(role_id: Optional[str], perm: str
                       ) -> tuple[bool, Optional[str]]:
    """守卫：返回 (ok, error)。error 为拒绝原因（含建议角色）。

    纯函数，不触碰会话/HTTP，便于在控制台与 API 两层复用。
    """
    if perm not in PERMISSIONS:
        return False, "未知权限：%s（请联系平台管理员）" % perm
    if has_permission(role_id, perm):
        return True, None
    # 谁有权？取最小授权角色集合，给调用方可读提示
    granted = sorted({r for r, ps in ROLE_PERMISSIONS.items() if perm in ps})
    hint = "、".join(granted) if granted else "（无角色拥有该权限）"
    return (False,
            "权限不足：需要 %s；当前角色「%s」无此权限。可由 %s 执行。"
            % (perm, role_id or "未知", hint))


def session_permissions(session: Optional[dict]) -> FrozenSet[str]:
    """从控制台会话 dict（含 role 字段）取权限集合。"""
    if not session:
        return frozenset()
    return role_permissions(session.get("role"))


def describe() -> dict:
    """供前端/合规报告展示的权限矩阵。"""
    return {
        "permissions": sorted(PERMISSIONS),
        "roles": {r: sorted(ps) for r, ps in ROLE_PERMISSIONS.items()},
        "default_role_perms": sorted(_DEFAULT_PERMS),
        "note": "白名单授权；未知角色仅获只读壳。熔断等高危权限仅 admin 持有。",
    }
