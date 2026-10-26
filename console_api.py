"""
console_api.py —— 协作审计台「工作台」后端 API 层

定位：
    console_web.py 负责 HTTP 路由与前端 SPA；本模块负责**业务逻辑**，
    让 Web 层保持薄（只做参数解析与序列化）。工作台新增的能力集中在这里：
      · 登录/会话（本地免密 + 角色选择，签发本地会话令牌）
      · 新建任务（真正写入 TaskRegistry + 记录轨迹）
      · 审计名单（可调度的"审计员"花名册）
      · 助理（技能型助理 / 内置专家）
      · 专家技能连接器（SkillHub 搜索安装 + 本地连接器状态）
      · 定时任务（复用 scheduler.Scheduler）
      · 资料库（复用 assets.AssetStore + 产物目录）

设计原则（对齐 docs/honest_boundary_paradigm.md）：
  - **写操作全部显式**：只有"新建任务"和"定时任务增删"是写；其余一律只读。
  - **不伪造**：SkillHub 不可达就说不可达；助理清单来自真实已装技能，不编造。
  - **不越权**：本地免密登录是**单机演示级**身份，不声称任何真实鉴权强度，
    并在 login 响应里显式声明这一边界。
  - **零第三方依赖**：只用标准库 + 本仓库既有模块。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
SESSION_TTL = 8 * 3600  # 本地会话 8 小时

# WorkBuddy 式角色（可作为审计员/助理的"岗位"）
ROLES = [
    {"id": "designer", "name": "素材设计", "desc": "生成营销素材、配图与文案变体",
     "skills": ["image-gen", "copywriting"], "model": "gpt-4o"},
    {"id": "writer", "name": "内容撰写", "desc": "长文、报告与结案材料撰写",
     "skills": ["writing-plans"], "model": "gpt-4o"},
    {"id": "auditor", "name": "安全审计", "desc": "密钥扫描、依赖与合规检查",
     "skills": ["verification-before-completion"], "model": "claude-3-5-sonnet-latest"},
    {"id": "analyst", "name": "数据分析", "desc": "成本归因、轨迹统计与异常发现",
     "skills": ["systematic-debugging"], "model": "gpt-4o-mini"},
    {"id": "publisher", "name": "发布执行", "desc": "对外发布类高危动作（需审批）",
     "skills": [], "model": "gpt-4o-mini"},
]


# --------------------------------------------------------------------------
# 1) 登录与会话
# --------------------------------------------------------------------------
class SessionStore:
    """本地会话：令牌 → {user, role, created, last_seen}。

    诚实边界：这是**单机演示级**身份识别，不做密码校验、不防伪造令牌。
    它的作用是让界面有"登录"形态与角色上下文，不是安全边界。
    """

    def __init__(self, ttl: int = SESSION_TTL):
        self.ttl = ttl
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def login(self, user: str, role: str = "auditor") -> Dict[str, Any]:
        user = (user or "").strip() or "operator"
        if not re.match(r"^[A-Za-z0-9_\u4e00-\u9fa5.\-]{1,32}$", user):
            raise ValueError("用户名只允许字母/数字/中文/._-，长度 1~32")
        if not any(r["id"] == role for r in ROLES):
            raise ValueError("未知角色：%s" % role)
        token = "sess-%d-%s" % (int(time.time()), os.urandom(6).hex())
        self._sessions[token] = {
            "user": user, "role": role,
            "created": time.time(), "last_seen": time.time(),
        }
        return {"token": token, "user": user, "role": role,
                "expires_in": self.ttl,
                "notice": "本地演示级身份：无密码校验、令牌不防伪造，"
                          "不构成安全边界；仅用于界面角色上下文。"}

    def verify(self, token: str) -> Optional[Dict[str, Any]]:
        s = self._sessions.get(token or "")
        if not s:
            return None
        if time.time() - s["last_seen"] > self.ttl:
            self._sessions.pop(token, None)
            return None
        s["last_seen"] = time.time()
        return dict(s)

    def logout(self, token: str) -> bool:
        return self._sessions.pop(token or "", None) is not None

    def list_users(self) -> List[Dict[str, Any]]:
        """在岗会话（审计名单的一部分：谁在线、什么角色）。"""
        out = []
        for tok, s in self._sessions.items():
            if time.time() - s["last_seen"] > self.ttl:
                continue
            out.append({"token_suffix": tok[-6:], "user": s["user"],
                        "role": s["role"],
                        "idle_seconds": int(time.time() - s["last_seen"])})
        return out


# --------------------------------------------------------------------------
# 2) 审计名单（花名册）
# --------------------------------------------------------------------------
def audit_roster(roles: List[Dict[str, Any]],
                 sessions: List[Dict[str, Any]],
                 skills: List[Dict[str, Any]],
                 models: Optional[List[str]] = None) -> Dict[str, Any]:
    """可被调度的"审计员"名单：岗位 × 在岗状态 × 可用技能。"""
    installed = {s.get("slug") or s.get("name") for s in skills}
    by_role: Dict[str, List[Dict[str, Any]]] = {}
    for s in sessions:
        by_role.setdefault(s["role"], []).append(s)
    entries = []
    for r in roles:
        on_duty = by_role.get(r["id"], [])
        entries.append({
            "id": r["id"], "name": r["name"], "desc": r["desc"],
            "model": r["model"],
            "skills_declared": r["skills"],
            "skills_installed": [sk for sk in r["skills"] if sk in installed],
            "on_duty": len(on_duty),
            "on_duty_users": [u["user"] for u in on_duty],
            "status": "on_duty" if on_duty else "standby",
        })
    return {
        "entries": entries,
        "roles": roles,
        "models": models or [],
        "counts": {
            "roles": len(entries),
            "on_duty": sum(1 for e in entries if e["on_duty"]),
            "standby": sum(1 for e in entries if not e["on_duty"]),
        },
        "note": "岗位来自内置角色表；在岗状态由本地会话推导（单机演示级）。",
    }


# --------------------------------------------------------------------------
# 3) 助理（技能型助理 + 内置专家）
# --------------------------------------------------------------------------
BUILTIN_EXPERTS = [
    {"id": "exp-sectrust", "name": "安全白皮书撰稿人", "kind": "内置专家",
     "desc": "把沙箱/最小权限/密钥扫描事实组织成对外白皮书，坚持诚实边界。",
     "skills": ["verification-before-completion", "writing-plans"]},
    {"id": "exp-auditor", "name": "审计轨迹分析师", "kind": "内置专家",
     "desc": "分析 Trace/成本/审批三路证据，定位异常与阻塞项。",
     "skills": ["systematic-debugging"]},
    {"id": "exp-planner", "name": "任务拆解规划师", "kind": "内置专家",
     "desc": "把自然语言目标拆成带依赖的 DAG 工序，缺参数标待补不编造。",
     "skills": ["writing-plans"]},
]


def assistants(skills: List[Dict[str, Any]],
              library_skills: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """助理清单：来自真实已装技能 + 内置专家，不编造。"""
    lib = library_skills or []
    out = []
    for e in BUILTIN_EXPERTS:
        out.append({
            "id": e["id"], "name": e["name"], "kind": e["kind"], "desc": e["desc"],
            "skills": e["skills"],
            "installed": all(any((s.get("name") == sk or s.get("slug") == sk)
                                 for s in skills + lib) for sk in e["skills"])
            if e["skills"] else True,
        })
    for s in skills:
        out.append({
            "id": "skill-%s" % (s.get("slug") or s.get("name")),
            "name": s.get("name") or s.get("slug"),
            "kind": "SkillHub 技能" if s.get("source") == "skillhub" else "本地技能",
            "desc": s.get("description") or "",
            "skills": [s.get("slug") or s.get("name")],
            "installed": True,
        })
    return {"assistants": out, "counts": {"total": len(out),
                                         "experts": len(BUILTIN_EXPERTS),
                                         "skills": len(skills)},
            "note": "助理由「内置专家」与「真实已装技能」构成；未安装的专家技能会标未安装。"}


# --------------------------------------------------------------------------
# 4) 专家技能连接器（SkillHub + 本地连接器）
# --------------------------------------------------------------------------
def _skillhub_search(keyword: str, limit: int = 8,
                     timeout: float = 8.0) -> Dict[str, Any]:
    """调 SkillHub 公开 API 搜索技能。不可达时诚实报错，不返回假列表。"""
    url = ("https://api.skillhub.cn/api/skills?keyword=%s&pageSize=%d"
           % (urllib.parse.quote(keyword or ""), limit))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SecTrustConsole/0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": "SkillHub 不可达：%s" % e, "results": []}
    items = data.get("data") or data.get("items") or data.get("list") or []
    if isinstance(items, dict):
        items = items.get("list") or items.get("items") or []
    out = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        out.append({
            "slug": it.get("slug") or it.get("name") or "",
            "name": it.get("name") or it.get("title") or it.get("slug") or "",
            "desc": (it.get("description") or it.get("summary") or "")[:200],
            "author": it.get("author") or it.get("owner") or "",
            "downloads": it.get("downloads") or it.get("stars") or 0,
        })
    return {"ok": True, "results": out}


def _skillhub_install(slug: str, target_dir: str,
                      timeout: float = 90.0) -> Dict[str, Any]:
    """用官方 skillhub CLI 安装到 WorkBuddy 用户级技能目录。

    CLI 不可用或安装失败都如实返回错误码，**不谎报成功**。
    """
    cli = shutil.which("skillhub")
    if not cli:
        return {"ok": False, "error": "未找到 skillhub CLI（先安装官方 CLI 才能安装技能）"}
    try:
        proc = subprocess.run(  # noqa: S603
            [cli, "install", slug, "--dir", target_dir],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "安装超时（%ss）" % timeout}
    except OSError as e:
        return {"ok": False, "error": "调用 CLI 失败：%s" % e}
    if proc.returncode != 0:
        return {"ok": False,
                "error": (proc.stderr or proc.stdout or "退出码 %d" % proc.returncode)[:400]}
    return {"ok": True, "output": (proc.stdout or "")[:600],
            "note": "已安装到 %s，重启后可在助理/技能面板看到。" % target_dir}


def connector_status(skills_dir: str) -> Dict[str, Any]:
    """连接器总览：SkillHub 连通性 + 本地连接器 + 已装技能数。"""
    cli = shutil.which("skillhub")
    skillhub_ok = False
    skillhub_note = "未安装 skillhub CLI"
    if cli:
        try:
            proc = subprocess.run([cli, "--version"], capture_output=True,  # noqa: S603
                                  text=True, timeout=8)
            skillhub_ok = proc.returncode == 0
            skillhub_note = (proc.stdout or proc.stderr or "").strip()[:120] or "已安装"
        except (OSError, subprocess.TimeoutExpired):
            skillhub_note = "CLI 存在但无法执行"
    local_connectors = []
    try:
        import connectors  # noqa: F401
        local_connectors = [{"name": "github", "desc": "GitHub 技能源同步"},
                            {"name": "imap", "desc": "IMAP 邮件技能源"}]
    except Exception:  # noqa: BLE001
        pass
    return {
        "skillhub": {"available": skillhub_ok, "note": skillhub_note,
                     "cli": cli or None},
        "local_connectors": local_connectors,
        "skills_dir": skills_dir,
        "note": "SkillHub 为社区技能市场（公开只读 API + 官方 CLI 安装）；"
                "本地连接器用于技能源同步。",
    }


# --------------------------------------------------------------------------
# 5) 定时任务（复用 scheduler.Scheduler）
# --------------------------------------------------------------------------
def job_payload_template() -> Dict[str, Any]:
    """可用的定时任务模板（预置几类常用巡检）。"""
    return {
        "presets": [
            {"id": "daily_self_scan", "name": "每日密钥自扫",
             "cron": "0 3 * * *", "desc": "每天 03:00 扫描真实业务源码并出具泄露报告"},
            {"id": "hourly_cost", "name": "每小时成本巡检",
             "cron": "0 * * * *", "desc": "每小时汇总成本并标记离群点"},
            {"id": "weekly_closure", "name": "每周结案报告",
             "cron": "0 9 * * 1", "desc": "每周一 09:00 生成上周任务结案报告"},
        ],
        "cron_help": "标准 5 段 cron：分 时 日 月 周（周 0=周日）。"
                     "也可改用间隔模式（每 N 秒）。",
    }


def add_job(scheduler: Any, job_id: str, name: str, cron: Optional[str],
            interval: Optional[float], payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        job = scheduler.add_job(job_id, cron=cron, interval=interval,
                                 payload=payload or {}, name=name or job_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "job": job}


# --------------------------------------------------------------------------
# 6) 资料库（素材 + 产物）
# --------------------------------------------------------------------------
def library_index(project_root: str) -> Dict[str, Any]:
    """资料库索引：产物目录 + 素材库统计。"""
    entries: List[Dict[str, Any]] = []

    # 6.1 产物目录（结案报告、压测报告、演示页）
    for sub, kind, label in (("reports", "报告", "演示与证据页"),
                              ("data", "产物", "结案报告与压测产物"),
                              ("docs", "文档", "设计与范式文档")):
        d = os.path.join(project_root, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d))[:60]:
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp):
                continue
            if not fn.lower().endswith((".md", ".json", ".html", ".pdf", ".sarif",
                                        ".csv", ".txt")):
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            entries.append({
                "name": fn, "kind": kind, "group": label,
                "path": os.path.join(sub, fn).replace("\\", "/"),
                "size": st.st_size,
                "mtime": st.st_mtime,
                "ext": os.path.splitext(fn)[1].lstrip(".").lower(),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)

    assets_stats: Dict[str, Any] = {}
    versions: List[Dict[str, Any]] = []
    try:
        from assets import AssetStore
        store = AssetStore()
        try:
            assets_stats = store.stats()
            items = store.list(limit=40)
            for it in items:
                versions.append({
                    "asset_id": it.get("asset_id"), "name": it.get("name"),
                    "kind": it.get("kind"), "size": it.get("size"),
                    "task_id": it.get("task_id"), "created_at": it.get("created_at"),
                })
        finally:
            store.close()
    except Exception as e:  # noqa: BLE001
        assets_stats = {"error": str(e)}

    return {
        "entries": entries,
        "assets": assets_stats,
        "asset_items": versions,
        "counts": {"documents": len(entries), "assets": len(versions)},
        "note": "资料库汇总本地产物与素材库（内容寻址 + 版本链），全部只读索引。",
    }


# --------------------------------------------------------------------------
# 7) 新建任务（写操作）
# --------------------------------------------------------------------------
def create_task(registry: Any, tracer: Any, title: str, role: str,
                depends_on: Optional[List[str]] = None,
                template: str = "", priority: str = "normal",
                note: str = "", operator: str = "operator") -> Dict[str, Any]:
    """真正登记一个新任务，并记录一条轨迹事件（可审计）。

    写操作纪律：只写 tasks 表 + trace 事件；不动其他表。
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "任务标题不能为空"}
    if len(title) > 120:
        return {"ok": False, "error": "任务标题过长（上限 120 字）"}
    if not any(r["id"] == role for r in ROLES):
        return {"ok": False, "error": "未知角色：%s" % role}
    deps = [d for d in (depends_on or []) if isinstance(d, str) and d.strip()]
    # 依赖必须存在且不能自依赖
    if deps:
        known = {t.get("task_id") for t in registry.list_tasks(limit=10000)}
        missing = [d for d in deps if d not in known]
        if missing:
            return {"ok": False, "error": "依赖任务不存在：%s" % ", ".join(missing)}

    task_id = "task-%s" % os.urandom(5).hex()
    payload = {
        "title": title,
        "role": role,
        "template": template or "",
        "priority": priority,
        "note": note,
        "created_by": operator,
        "created_at": time.time(),
    }
    try:
        registry.register_task(task_id, payload, depends_on=deps)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "登记失败：%s" % e}

    # 记录轨迹（可审计：谁在什么时候建了什么）
    trace_id = None
    if tracer is not None:
        try:
            trace_id = tracer.start_trace(title="新建任务：%s" % title, kind="task_create",
                                         started_by=operator)
            tracer.record(trace_id, "task_created", task_id=task_id, actor=operator,
                          summary="登记任务 %s（角色 %s）" % (task_id, role),
                          detail={"title": title, "role": role, "depends_on": deps,
                                  "template": template, "priority": priority})
            tracer.finish(trace_id, status="completed")
        except Exception:  # noqa: BLE001
            trace_id = None
    return {"ok": True, "task_id": task_id, "trace_id": trace_id,
            "payload": payload, "depends_on": deps}
