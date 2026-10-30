"""岗位 ↔ AI 同事绑定（分工路由）。

把"角色（岗位）"真正接通到"具体 AI 同事（模型 profile）"——这是平台从
"一个 AI 干所有活"走向"按岗位分工"的关键一层。

设计要点（与既有纪律一致）：
- **默认不绑定**（colleague_id 空 = "平台默认同事" = 当前激活模型）：已有行为不破坏，
  渐进接入。用户在「设置」里把某个岗位绑到具体同事后才生效。
- **诚实回退**：create_task 只写"预期的同事"；真正执行时若绑定的同事没配密钥/
  不可用，daemon 的 _select_profile 会诚实回退到平台默认同事，并在结果里写明
  "绑定同事 X 未配置，已用 Y"——绝不静默假装分配成功。
- **绑定文件与 ROLES 解耦**：ROLES 是岗位定义（审计/权限视角），绑定是运行期
  配置（可改、可持久化），落盘 data/role_routing.json。
"""

import json
import os

# 绑定文件：与 model_profiles.json 同目录（data/）
_BINDING_FILE = "data/role_routing.json"

# 平台默认同事的展示名（未绑定 / 绑定同事不可用时的落点）
DEFAULT_LABEL = "平台默认同事"


def _roles():
    """惰性导入 ROLES，避免与 console_api 形成模块级循环依赖。"""
    from console_api import ROLES
    return ROLES


def _binding_path() -> str:
    db = os.environ.get("SECTRUST_DATA_DIR")
    if db:
        return os.path.join(db, "role_routing.json")
    return _BINDING_FILE


def load_bindings() -> dict:
    """返回 {role_id: colleague_id}，未配置的岗位回退为空串（=默认同事）。"""
    roles = {r["id"]: "" for r in _roles()}
    path = _binding_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            for rid, cid in (data.get("bindings") or {}).items():
                if rid in roles:
                    roles[rid] = str(cid or "")
        except (OSError, ValueError):
            # 损坏的绑定文件不阻断平台——回退到全默认，下次保存会覆盖
            pass
    return roles


def save_binding(role_id: str, colleague_id: str) -> bool:
    """保存单个岗位的同事绑定。role_id 必须合法；colleague_id 空串=解绑(默认)。"""
    valid = {r["id"] for r in _roles()}
    if role_id not in valid:
        raise ValueError("未知岗位：%s" % role_id)
    bindings = load_bindings()
    bindings[role_id] = str(colleague_id or "")
    path = _binding_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"bindings": bindings}, fh, ensure_ascii=False, indent=2)
    return True


def binding_for(role_id: str) -> str:
    """某岗位当前绑定的同事 profile id（空串=平台默认同事）。"""
    return load_bindings().get(role_id, "")


def colleague_label(profiles, colleague_id: str) -> str:
    """把 colleague_id 翻译成可读名；空串或查不到→默认同事标签。"""
    if not colleague_id:
        return DEFAULT_LABEL
    p = profiles.get(colleague_id) if profiles is not None else None
    if p is None:
        return DEFAULT_LABEL
    return p.name or p.id
