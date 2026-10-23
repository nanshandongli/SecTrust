"""
轻量 MCP Server（零依赖，纯标准库实现）

把本产品的三块能力暴露成**标准 MCP 工具**，Claude Code / Cursor / 任何支持 stdio MCP 的
客户端都能直接接入，生态杠杆最大：

  ┌─────────────── MCP 工具（stdio, JSON-RPC 2.0） ───────────────┐
  │  list_templates      列出内置合规扫描模板                      │
  │  request_scan        发起扫描 -> 进入审批中心(pending)          │
  │  approve / deny      审批：批准 / 拒绝                         │
  │  execute_scan        执行已批准的扫描（沙箱隔离 + 审批放行）    │
  │  run_scan            一步式（可选 auto_approve）               │
  │  list_tasks          任务登记处只读查询                        │
  │  get_task            单条任务详情                              │
  │  list_runners        runner 存活状态（stale 判定）             │
  │  list_approvals      审批中心只读查询                          │
  │  memory_set/get/search/list/top/forget  轻量长期记忆（P2）     │
  │  memory_semantic_search  语义检索（向量层，路线 3）            │
  │  validate_secret         密钥活体验证（路线 2，降误报）        │
  │  push_protection         pre-push 钩子 / CI 阻断（路线 2）     │
  │  queue_publish/consume   分布式队列（路线 1，跨机）            │
  │  api_health              远端 HTTP API 健康探测（路线 1）      │
  └──────────────────────────┬───────────────────────────────────┘
                             │ 复用同一 SQLite（同机共享）
                             ▼
         task_registry.db（审批中心/沙箱/登记处） + memory.db（长期记忆）

为什么零依赖：
- MCP 的 stdio 形态本质是「换行分隔的 JSON-RPC 2.0」。我们实现最精简但完整的部分
  （initialize / tools/list / tools/call / ping / notifications），对客户端完全兼容；
- 不强制 `pip install mcp`，开箱即跑；如需 SSE/HTTP 形态（如 Open WebUI）再叠加官方 SDK。

安全姿态（与产品一致）：
- request_scan 默认只创建 pending 审批，**不执行**；必须由 approve 显式放行（Human-in-the-loop）；
- 所有扫描都走 sandbox 隔离 + 审批双闸门；run_scan(auto_approve=True) 仅用于可信自动化；
- 本 Server 自身只读/只写 task_registry.db，不暴露任何额外端口。

接入示例（.mcp.json，Claude Code / Cursor 直接读取）：
  {
    "mcpServers": {
      "task-registry": {
        "command": "python",
        "args": ["mcp_server.py"],
        "env": { "DB_PATH": "data/task_registry.db" }
      }
    }
  }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Optional

# ---- 纯逻辑层（无 MCP 依赖），供测试直接调用 ----
from approval import ApprovalCenter
from memory import DEFAULT_DB_PATH as DEFAULT_MEMORY_DB_PATH, MemoryStore
from task_registry import DEFAULT_DB_PATH, TaskRegistry
from templates import (
    execute_scan,
    get_template,
    list_templates,
    request_scan,
    run_scan,
)

# ---------------------------------------------------------------------------
# 协议版本（MCP 2026-07-28 对齐）
#
# 2026-07-28 是自立项以来最大的一次修订，核心变化：
#   · 无状态核心：移除 initialize/initialized 握手与 Mcp-Session-Id，
#     客户端能力改由每个请求 _meta 里的 io.modelcontextprotocol/* 携带；
#     新增 server/discover 做能力协商；版本不匹配返回 UnsupportedProtocolVersionError。
#   · Roots / Sampling / Logging 进入 12 个月弃用倒计时；HTTP+SSE 传输正式弃用。
#   · 服务器成为 OAuth 2.1 资源服务器（见 security/oauth.py 的 RFC 9728/8707/9207）。
#
# 兼容策略：同时声明"支持的最新版"与"仍可服务的历史版"，让 2024 老客户端与
# 2026 新客户端都能连上——这是自建服务器落地的现实约束，不能一刀切。
# ---------------------------------------------------------------------------
LATEST_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18",
                               "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION      # 对外声明用最新版
SERVER_NAME = "task-registry-mcp"
SERVER_VERSION = "0.4.0"

# 2026-07-28 起被弃用的特性（保留可服务性，但不再推荐新客户端依赖）
DEPRECATED_FEATURES = {
    "roots": "改用工具参数或资源 URI 传递文件/目录",
    "sampling": "改为直接调用 LLM provider 的 API",
    "logging": "改为写 stderr 或 OpenTelemetry",
    "http+sse": "改用 Streamable HTTP",
}

# 客户端能力/版本在 _meta 中的键（2026-07-28：握手移除后改走 _meta）
_META_PREFIX = "io.modelcontextprotocol/"


def _client_protocol_version(req: dict) -> Optional[str]:
    """从请求中提取客户端声明的协议版本：优先 _meta（新规），回退 params（旧握手）。"""
    meta = (req.get("params") or {}).get("_meta") or req.get("_meta") or {}
    v = meta.get(_META_PREFIX + "protocolVersion")
    if v:
        return v
    return (req.get("params") or {}).get("protocolVersion")


def _unsupported_version_error(rid, client_version: Optional[str]) -> dict:
    return {
        "jsonrpc": "2.0", "id": rid,
        "error": {
            "code": -32602,
            "message": "UnsupportedProtocolVersionError",
            "data": {
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "latest": LATEST_PROTOCOL_VERSION,
                "client": client_version,
            },
        },
    }


# ---- 工具定义：name / 中文描述 / JSON Schema 输入 ----
_TOOLS = [
    {
        "name": "list_templates",
        "description": "列出内置的合规扫描模板（密钥泄露、硬编码凭证、依赖漏洞、CI/CD 配置、代码卫生基线）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_scan",
        "description": "发起一次合规扫描请求：任务登记 + 进入审批中心(pending, 默认拒绝)。返回 task_id/approval_id。"
                       "注意：默认不执行，需再调用 approve 放行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string", "description": "模板 id，如 secret_leak / hardcoded_credential / dependency_vuln / cicd_config / generic_sast"},
                "repo_dir": {"type": "string", "description": "被扫描的本地仓库/目录绝对路径"},
                "channel": {"type": "string", "default": "mcp"},
                "user_id": {"type": "string", "default": "mcp"},
                "repo_label": {"type": "string", "description": "可选，展示用的仓库标签（如 git url）"},
            },
            "required": ["template_id", "repo_dir"],
        },
    },
    {
        "name": "approve",
        "description": "批准一个待审批的扫描请求（Human-in-the-loop 放行）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "by": {"type": "string", "default": "mcp"},
            },
            "required": ["approval_id"],
        },
    },
    {
        "name": "deny",
        "description": "拒绝一个待审批的扫描请求（拦截，不执行）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "by": {"type": "string", "default": "mcp"},
            },
            "required": ["approval_id"],
        },
    },
    {
        "name": "execute_scan",
        "description": "执行一个已批准的扫描：沙箱隔离运行模板脚本，结果写回登记处。未批准则拒绝执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "approval_id": {"type": "string"},
                "repo_dir": {"type": "string", "description": "被扫描目录（需与 request_scan 时一致）"},
                "sandbox_backend": {"type": "string", "default": "auto", "description": "auto / docker / local / local-enforced"},
                "template_id": {"type": "string", "description": "可选，模板 id（缺省从任务 payload 读取）"},
            },
            "required": ["task_id", "approval_id", "repo_dir"],
        },
    },
    {
        "name": "run_scan",
        "description": "一步式扫描：发起并（可选自动批准）执行，返回完整报告。auto_approve=false 时只返回 pending。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "repo_dir": {"type": "string"},
                "auto_approve": {"type": "boolean", "default": False},
                "sandbox_backend": {"type": "string", "default": "auto", "description": "auto / docker / local / local-enforced"},
                "engine": {"type": "string", "default": "regex",
                           "description": "regex=内置正则（默认，零依赖）；semgrep=深度引擎（未安装自动回退 regex）"},
                "channel": {"type": "string", "default": "mcp"},
                "user_id": {"type": "string", "default": "mcp"},
                "repo_label": {"type": "string"},
            },
            "required": ["template_id", "repo_dir"],
        },
    },
    {
        "name": "list_tasks",
        "description": "任务登记处只读查询：可选按 runner_id / status 过滤，限制返回条数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "runner_id": {"type": "string"},
                "status": {"type": "string", "description": "registered/running/done/failed"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "get_task",
        "description": "查询单条任务详情（含 payload 元数据）。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "list_runners",
        "description": "查询 runner 存活状态；超过 stale_after 秒无心跳标记为 stale。",
        "inputSchema": {
            "type": "object",
            "properties": {"stale_after": {"type": "integer", "default": 90}},
        },
    },
    {
        "name": "list_approvals",
        "description": "审批中心只读查询：可选按 status(pending/approved/rejected/expired) 过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    # ---- 轻量长期记忆（P2）：跨会话记住用户偏好 / 指令模板 / repo 路径 ----
    {
        "name": "memory_set",
        "description": "写入/覆盖一条长期记忆。category 建议用 preference(用户偏好)/command(指令模板)/repo(仓库路径)；"
                       "value 会尝试按 JSON 解析，失败则存原文。同文件跨会话持久化。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "分类：preference / command / repo 或自定义"},
                "key": {"type": "string", "description": "分类内唯一键，如 code_review_model"},
                "value": {"type": "string", "description": "记忆内容（JSON 或纯文本）"},
                "note": {"type": "string", "description": "可选，人类可读说明"},
            },
            "required": ["category", "key", "value"],
        },
    },
    {
        "name": "memory_get",
        "description": "读取一条长期记忆；不存在返回 null。读取会累加访问计数（支撑「常用」排名）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["category", "key"],
        },
    },
    {
        "name": "memory_search",
        "description": "在 key/note/value 文本里做子串检索（跨会话模糊回忆用户偏好等）。可按 category 过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_list",
        "description": "列出某分类下的全部记忆（按更新时间倒序）。需传入 category。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "如 preference / command / repo"},
                "limit": {"type": "integer", "default": 1000},
            },
            "required": ["category"],
        },
    },
    {
        "name": "memory_top",
        "description": "访问频次最高的 N 条记忆（常用指令模板 / 常用 repo 路径）。不传 category 取全局 Top N。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "n": {"type": "integer", "default": 5},
            },
        },
    },
    {
        "name": "memory_forget",
        "description": "删除一条长期记忆；返回是否真正删除到了。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["category", "key"],
        },
    },
    # ---- 产品化新增（路线 1/2/3）----
    {
        "name": "memory_semantic_search",
        "description": "语义检索长期记忆（向量层）：按自然语言查询召回最相关记忆，如「代码审查用哪个模型」。离线默认 hashed n-gram 余弦，可选 OpenAI embedding。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
                "category": {"type": "string", "description": "可选：preference/command/repo"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "validate_secret",
        "description": "密钥活体验证：判断一个疑似密钥是否仍然有效（离线默认返回 unknown，配置 GITHUB_TOKEN 后可真实校验）。用于降低扫描误报。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "secret": {"type": "string"},
                "secret_type": {"type": "string", "default": "",
                                "description": "可选：github/openai/aws 等；留空自动推断"},
            },
            "required": ["secret"],
        },
    },
    {
        "name": "push_protection",
        "description": "生成推送保护制品：为 git 仓库写入 pre-push 钩子，或返回 GitHub Actions CI 片段，阻断带高危发现的提交。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_dir": {"type": "string", "description": "本地 git 仓库路径（写 pre-push 钩子）"},
                "mode": {"type": "string", "default": "hook",
                         "description": "hook=写 pre-push 钩子；ci=返回 GitHub Actions yaml 文本"},
                "severity_threshold": {"type": "string", "default": "high"},
            },
        },
    },
    {
        "name": "queue_publish",
        "description": "分布式队列发布消息（路线 1：放弃同机前提）。后端由 QUEUE_BACKEND 决定：local=SQLite 持久化队列（默认），redis=Redis。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "message": {"type": "object"},
            },
            "required": ["topic", "message"],
        },
    },
    {
        "name": "queue_consume",
        "description": "从分布式队列消费一批消息（默认取后即确认）。用于跨进程/跨机的 runner 拉取任务。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "batch": {"type": "integer", "default": 10},
                "ack": {"type": "boolean", "default": True},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "api_health",
        "description": "查询远端 HTTP API 服务健康状态（路线 1：分布式形态）。需配置 API_BASE（可选 API_TOKEN）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "description": "可选，缺省用环境变量 API_BASE"},
            },
        },
    },
    {
        "name": "scan_deep",
        "description": ("深度凭证扫描（78 条结构化检测器 + 香农熵兜底未知形态）："
                        "返回按厂商/严重级统计与 CI 闸门建议。支持基线（只报新增）、"
                        "diff-aware 增量、SARIF 2.1.0 输出。"
                        "所有凭证默认脱敏，报告不会二次泄露；写入文件需显式给路径。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "待扫描的目录或文件绝对路径"},
                "providers": {"type": "array", "items": {"type": "string"},
                              "description": "只扫指定厂商，如 aws / github / aliyun（可选）"},
                "categories": {"type": "array", "items": {"type": "string"},
                               "description": "按标签筛选，如 cloud / vcs / pii / ai（可选）"},
                "severities": {"type": "array", "items": {"type": "string"},
                               "description": "按严重级筛选：critical/high/medium/low/info"},
                "high_entropy": {"type": "boolean", "default": True,
                                 "description": "是否启用高熵兜底（捕获未知形态凭证）"},
                "only_changed": {"type": "boolean", "default": False,
                                 "description": "只扫 git 变更文件（PR 场景）"},
                "since": {"type": "string", "default": "HEAD~1"},
                "baseline_path": {"type": "string",
                                  "description": "基线文件路径；提供后 only 新增计入闸门"},
                "write_baseline": {"type": "boolean", "default": False},
                "sarif_out": {"type": "string",
                              "description": "可选：把结果写成 SARIF 2.1.0 文件"},
                "fail_on": {"type": "string", "default": "high",
                            "description": "闸门阻断的最低严重级"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "memory_hybrid_search",
        "description": ("混合检索长期记忆：向量 + BM25 + 实体三路召回后 RRF 融合，"
                        "并按时间衰减与访问频次重排。可按记忆类型与作用域过滤。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
                "kinds": {"type": "array", "items": {"type": "string"},
                          "description": "semantic / episodic / procedural / working"},
                "user_id": {"type": "string", "description": "记忆作用域：用户"},
                "agent_id": {"type": "string", "description": "记忆作用域：智能体"},
                "run_id": {"type": "string", "description": "记忆作用域：会话/运行"},
                "org_id": {"type": "string", "description": "记忆作用域：组织"},
                "category": {"type": "string"},
                "explain": {"type": "boolean", "default": False,
                            "description": "返回各路信号分数，便于排查为何命中"},
                "apply_decay": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "skill_list",
        "description": ("列出可用的 Agent Skills（渐进式披露第 1 段：只返回 name + description，"
                        "约 100 tokens/个）。借鉴 obra/superpowers 的 SKILL.md 体系；"
                        "需要正文时用 skill_load 激活，不要一次性全读。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "可选：按问题/场景搜索，如 'claiming done' '调试'"},
                "include_broken": {"type": "boolean", "default": False,
                                   "description": "是否包含未通过规范校验的技能包"},
            },
        },
    },
    {
        "name": "skill_load",
        "description": ("激活并读取某个 Skill 的完整正文（渐进式披露第 2 段）。"
                        "只在任务确实匹配时调用——正文比 description 大两个数量级。"),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "技能名，如 verification-before-completion"}},
            "required": ["name"],
        },
    },
    {
        "name": "evidence_submit",
        "description": ("提交'完成声明'的验证证据并机器核验（借鉴 superpowers 的 "
                        "verification-before-completion：Evidence before claims）。"
                        "核验项：命令是否为真命令、退出码 0、输出非空、证据新鲜、"
                        "可选 expect/forbid 正则、声明措辞不含 should/probably/应该/大概。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "实际跑过的验证命令"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string", "description": "命令输出（建议给尾部 2000 字符即可）"},
                "stderr": {"type": "string", "description": "错误输出；unittest/pytest 结果常在 stderr"},
                "claim": {"type": "string", "description": "你想做的完成声明"},
                "task_id": {"type": "string"},
                "expect": {"type": "string", "description": "可选：输出必须命中的正则"},
                "forbid": {"type": "string", "description": "可选：输出不得命中的正则"},
                "ran_at": {"type": "number", "description": "可选：命令运行的 unix 时间戳，缺省=现在"},
                "ttl": {"type": "number", "default": 300, "description": "证据新鲜度窗口（秒）"},
            },
            "required": ["command", "exit_code", "claim"],
        },
    },
    {
        "name": "evidence_run",
        "description": ("由系统亲自跑一遍验证命令并核验（真跑模式）——Agent 无法伪造的最强证据。"
                        "受命令白名单约束：只允许测试/构建/只读类前缀（python -m unittest、pytest、"
                        "go test、npm test、git diff --stat 等）。需 EVIDENCE_ALLOW_RUN=1 开启。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "claim": {"type": "string"},
                "task_id": {"type": "string"},
                "cwd": {"type": "string"},
                "expect": {"type": "string"},
                "forbid": {"type": "string"},
                "timeout": {"type": "number", "default": 120},
            },
            "required": ["command", "claim"],
        },
    },
    {
        "name": "task_complete_verified",
        "description": ("带证据完成任务：证据通过核验才会把任务置为 completed，"
                        "否则状态不变并返回拒因。这是把 superpowers 的软约束"
                        "（prompt 里写着 mandatory，但没人拦）升级为硬门禁。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "claim": {"type": "string"},
                "command": {"type": "string"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "expect": {"type": "string"},
                "forbid": {"type": "string"},
            },
            "required": ["task_id", "claim", "command", "exit_code"],
        },
    },
    {
        "name": "dag_add",
        "description": ("DAG 建图：登记一个带依赖的子任务（depends_on 必须先已存在，"
                        "先添上游再添下游）。拒绝：重复 task_id / 未知依赖 / 自依赖 / 成环。"
                        "task_id 缺省自动生成。配合 planner.Plan.build() 可一键落整张图。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "缺省自动生成 dag-<hex>"},
                "payload": {"type": "object",
                            "description": "任务负载：name/instruction/skill/model/quality/plan 等"},
                "depends_on": {"type": "array", "items": {"type": "string"},
                               "description": "上游 task_id 列表（必须已登记）"},
            },
        },
    },
    {
        "name": "dag_claim_ready",
        "description": ("认领一个就绪任务（registered 且依赖全部 completed）——"
                        "外部 AI 作为 worker 接入多 AI 流水线的入口。"
                        "并发安全：写锁内选+认领，两个 worker 不可能领到同一个。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "runner_id": {"type": "string", "description": "worker 标识"},
                "task_id": {"type": "string",
                            "description": "可选：只认领指定任务（也须已就绪）"},
            },
            "required": ["runner_id"],
        },
    },
    {
        "name": "dag_complete",
        "description": ("完成一个 running 中的任务（幂等：重复调用返回 False 不报错）。"
                        "前置条件：所有依赖已 completed——完成事件本身就是图一致性检查点。"
                        "完成后其下游自动就绪。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "result": {"type": "object", "description": "可选：结果写入 payload.result"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "dag_status",
        "description": ("查看 DAG 全貌：波次分层、就绪集、各任务状态与等待原因、统计。"
                        "缺省返回整图；带 task_id 时附该任务的等待详情。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "可选：只看某个任务的等待详情"},
            },
        },
    },
    {
        "name": "ballot_submit",
        "description": ("投票/辩论模式：向投票组提交一个方案（worker 产出后调用）。"
                        "提交即按该组登记的质量判据打分；同一 task_id 重复提交以最后一次为准。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "投票组名（payload.ballot.group）"},
                "task_id": {"type": "string", "description": "提交方案的任务 id（分支任务）"},
                "output": {"type": "string", "description": "方案全文"},
                "source": {"type": "string", "description": "可选：提交方标识（agent/runner）"},
            },
            "required": ["group", "task_id", "output"],
        },
    },
    {
        "name": "ballot_status",
        "description": ("投票/辩论模式：查看投票组（各方案得分/违规/推荐）。"
                        "rank=true 时执行择优排序——只产出推荐，不自动生效；"
                        "平分/无判据/未交齐一律标记需人工。生效需人工确认（见 ballot_confirm）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "投票组名；缺省列出全部"},
                "rank": {"type": "boolean",
                         "description": "true 时执行择优排序（有副作用：更新推荐与状态）"},
            },
        },
    },
    {
        "name": "ballot_confirm",
        "description": ("投票/辩论模式：人工确认某个方案生效。这是推荐之外唯一的生效路径——"
                        "系统不自动拍板。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "投票组名"},
                "task_id": {"type": "string", "description": "要生效的方案 task_id"},
                "by": {"type": "string", "description": "确认人标识，缺省 human"},
                "reason": {"type": "string", "description": "可选：确认理由（进审计）"},
            },
            "required": ["group", "task_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# 工具注解（Tool Annotations）—— MCP 的"风险词汇"
#
# 注解让客户端在**调用前**就能判断风险，而不必执行后才知道后果。
# 对本项目尤其重要：execute_scan / run_scan 会真跑命令、memory_forget 会删数据、
# approve 会放行被闸门拦住的操作——这些都应该被宿主 UI 显式提示。
#
# 四个注解的语义（按规范）：
#   readOnlyHint     —— 不修改任何状态
#   destructiveHint  —— 可能造成不可逆变更（删除/覆写）
#   idempotentHint   —— 重复调用结果一致
#   openWorldHint    —— 会访问本组织/本机之外的世界（联网、外部 API）
# ---------------------------------------------------------------------------
_TOOL_ANNOTATIONS: "dict[str, dict]" = {
    # 只读
    "list_templates":        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "list_tasks":            {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "get_task":              {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "list_runners":          {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "list_approvals":        {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "memory_get":            {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "memory_list":           {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "memory_top":            {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "memory_search":         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "memory_semantic_search": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    "validate_secret":       {"readOnlyHint": True, "idempotentHint": True,
                              "openWorldHint": True,  # 可能联网做活体验证
                              "title": "密钥活体验证（可能联网）"},
    "api_health":            {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    # 写：非破坏性
    "request_scan":          {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": False,
                              "title": "发起扫描请求（默认进入审批，不执行）"},
    "approve":               {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "放行审批（安全敏感操作）"},
    "deny":                  {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "拒绝审批"},
    "memory_set":            {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False},
    "queue_publish":         {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": False},
    "queue_consume":         {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": False},
    "push_protection":       {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "生成 git 钩子/CI 配置（写入需人工确认）"},
    # 执行：会真跑命令
    "execute_scan":          {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": True,
                              "title": "执行已审批的扫描（会在沙箱内运行命令）"},
    "run_scan":              {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": True,
                              "title": "未经审批直接扫描（危险，需显式确认）"},
    # 破坏性
    "memory_forget":         {"readOnlyHint": False, "destructiveHint": True,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "删除记忆（不可逆）"},
    "scan_deep":             {"readOnlyHint": True, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "深度凭证扫描（只读分析，输出全脱敏）"},
    "memory_hybrid_search":  {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "混合检索记忆（会刷新访问计数）"},
    "skill_list":            {"readOnlyHint": True, "idempotentHint": True,
                              "openWorldHint": False,
                              "title": "列出可用技能（只给 name+description）"},
    "skill_load":            {"readOnlyHint": True, "idempotentHint": True,
                              "openWorldHint": False,
                              "title": "激活技能正文（渐进式披露第 2 段）"},
    "evidence_submit":       {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": True, "openWorldHint": False,
                              "title": "提交并核验完成证据（写入审计表）"},
    "evidence_run":          {"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": False,
                              "title": "系统亲自跑验证命令（受白名单约束）"},
    "task_complete_verified": {"readOnlyHint": False, "destructiveHint": False,
                               "idempotentHint": False, "openWorldHint": False,
                               "title": "带证据完成任务（证据不足则拦截）"},
    # ---- 编排层 DAG（多 AI 流水线；见 orchestration.py）----
    "dag_add":              {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": False, "openWorldHint": False,
                             "title": "DAG 建图：登记带依赖的子任务"},
    "dag_claim_ready":      {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": False, "openWorldHint": False,
                             "title": "认领就绪任务（并发安全；外部 AI 当 worker）"},
    "dag_complete":         {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False,
                             "title": "完成任务（依赖须全完成；幂等）"},
    "dag_status":           {"readOnlyHint": True, "idempotentHint": True,
                             "openWorldHint": False,
                             "title": "查看 DAG 波次/就绪/统计（只读）"},
    "ballot_submit":        {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": False, "openWorldHint": False,
                             "title": "向投票组提交一个方案（可覆盖重投）"},
    "ballot_status":        {"readOnlyHint": True, "idempotentHint": True,
                             "openWorldHint": False,
                             "title": "查看投票组；rank=true 时才排序（有副作用）"},
    "ballot_confirm":       {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False,
                             "title": "人工确认方案生效（唯一生效路径）"},
}


def _annotated_tools() -> list:
    """把注解注入工具清单（不改动 _TOOLS 本体，保持统计口径稳定）。"""
    out = []
    for t in _TOOLS:
        item = dict(t)
        ann = _TOOL_ANNOTATIONS.get(t["name"])
        if ann:
            item["annotations"] = ann
        out.append(item)
    return out


def server_capabilities() -> dict:
    """2026-07-28 的能力声明：供 server/discover 与旧 initialize 共用。

    刻意**不声明** roots / sampling / logging —— 它们已进入 12 个月弃用窗口，
    新服务器不应再采用（本实现从未依赖它们，因此是零成本对齐）。
    """
    return {
        "tools": {"listChanged": False},
        "resources": {},
        "prompts": {},
        # 本服务器是 OAuth 2.1 资源服务器（远程传输下生效；stdio 无鉴权面）
        "authorization": {
            "resourceServer": True,
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "rfc": ["9728", "8707", "9207"],
        },
        "experimental": {"tasks": False},   # Tasks 已移出核心，成为扩展
    }


def _db_path() -> str:
    return os.getenv("DB_PATH") or DEFAULT_DB_PATH


def _memory_db_path() -> str:
    return os.getenv("MEMORY_DB_PATH") or DEFAULT_MEMORY_DB_PATH


def _evidence_db_path() -> str:
    """证据审计库独立成文件：证据是"不可篡改的事实"，应与业务库分开备份与保留。"""
    return os.getenv("EVIDENCE_DB_PATH") or os.path.join(
        os.path.dirname(DEFAULT_DB_PATH) or "data", "evidence.db")


def _ballot_db_path() -> str:
    """投票库独立成文件：方案全文与推荐记录是审计材料，与任务表分开留存。"""
    return os.getenv("BALLOT_DB_PATH") or os.path.join(
        os.path.dirname(DEFAULT_DB_PATH) or "data", "ballot.db")


def _build_queue():
    """按环境变量构建队列：QUEUE_BACKEND=redis/local/auto，本地库路径用 MQ_DB_PATH。"""
    from transport.queue import build_queue
    return build_queue(
        backend=os.getenv("QUEUE_BACKEND", "auto"),
        db_path=os.getenv("MQ_DB_PATH") or None,
        redis_url=os.getenv("REDIS_URL") or None,
    )


# ---- 工具分发：每个 handler 返回「可 JSON 序列化的结果文本」 ----
def _dispatch(name: str, args: dict) -> str:
    args = args or {}
    if name == "list_templates":
        return json.dumps(list_templates(), ensure_ascii=False, indent=2)

    if name == "request_scan":
        # 仅发起审批（pending），不执行；批准由 approve 工具显式放行
        req = request_scan(
            template_id=args["template_id"],
            repo_dir=args["repo_dir"],
            db_path=_db_path(),
            channel=args.get("channel", "mcp"),
            user_id=args.get("user_id", "mcp"),
            repo_label=args.get("repo_label"),
        )
        return json.dumps(req, ensure_ascii=False, indent=2)

    if name == "approve":
        ac = ApprovalCenter(db_path=_db_path())
        ok = ac.decide(args["approval_id"], "approve", by=args.get("by", "mcp"))
        st = ac.status(args["approval_id"])
        ac.close()
        return json.dumps({"approval_id": args["approval_id"], "ok": ok, "status": st}, ensure_ascii=False)

    if name == "deny":
        ac = ApprovalCenter(db_path=_db_path())
        ok = ac.decide(args["approval_id"], "reject", by=args.get("by", "mcp"))
        st = ac.status(args["approval_id"])
        ac.close()
        return json.dumps({"approval_id": args["approval_id"], "ok": ok, "status": st}, ensure_ascii=False)

    if name == "execute_scan":
        # auto_approve 参数在此无关，强制走已批准路径；未批准由 execute_scan 内部拦截
        res = execute_scan(
            task_id=args["task_id"],
            approval_id=args["approval_id"],
            repo_dir=args["repo_dir"],
            db_path=_db_path(),
            sandbox_backend=args.get("sandbox_backend", "auto"),
            template_id=args.get("template_id"),
        )
        return json.dumps(res, ensure_ascii=False, indent=2)

    if name == "run_scan":
        res = run_scan(
            template_id=args["template_id"],
            repo_dir=args["repo_dir"],
            db_path=_db_path(),
            sandbox_backend=args.get("sandbox_backend", "auto"),
            auto_approve=bool(args.get("auto_approve", False)),
            channel=args.get("channel", "mcp"),
            user_id=args.get("user_id", "mcp"),
            repo_label=args.get("repo_label"),
            engine=args.get("engine", "regex"),
        )
        return json.dumps(res, ensure_ascii=False, indent=2)

    if name == "list_tasks":
        reg = TaskRegistry(db_path=_db_path())
        rows = reg.list_tasks(
            runner_id=args.get("runner_id"),
            status=args.get("status"),
            limit=int(args.get("limit", 100)),
        )
        reg.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "get_task":
        reg = TaskRegistry(db_path=_db_path())
        rows = reg.list_tasks(limit=100000)
        reg.close()
        for t in rows:
            if t["task_id"] == args["task_id"]:
                return json.dumps(t, ensure_ascii=False, indent=2)
        return json.dumps({"error": "not_found", "task_id": args["task_id"]}, ensure_ascii=False)

    if name == "list_runners":
        reg = TaskRegistry(db_path=_db_path())
        rows = reg.list_runners(stale_after=int(args.get("stale_after", 90)))
        reg.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "list_approvals":
        ac = ApprovalCenter(db_path=_db_path())
        rows = ac.list_approvals(status=args.get("status"), limit=int(args.get("limit", 100)))
        ac.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)

    # ---- 轻量长期记忆（P2）----
    if name == "memory_set":
        store = MemoryStore(db_path=_memory_db_path())
        raw = args["value"]
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        store.remember(args["category"], args["key"], value, note=args.get("note"))
        item = store.recall(args["category"], args["key"], touch=False)
        store.close()
        return json.dumps(
            {"category": args["category"], "key": args["key"], "value": item, "stored": True},
            ensure_ascii=False,
        )

    if name == "memory_get":
        store = MemoryStore(db_path=_memory_db_path())
        val = store.recall(args["category"], args["key"], default=None, touch=True)
        store.close()
        return json.dumps(
            {"category": args["category"], "key": args["key"], "value": val},
            ensure_ascii=False,
        )

    if name == "memory_search":
        store = MemoryStore(db_path=_memory_db_path())
        rows = store.search(
            args["query"], category=args.get("category"), limit=int(args.get("limit", 50))
        )
        store.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "memory_list":
        store = MemoryStore(db_path=_memory_db_path())
        rows = store.list_category(args["category"], limit=int(args.get("limit", 1000)))
        store.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "memory_top":
        store = MemoryStore(db_path=_memory_db_path())
        rows = store.top_used(category=args.get("category"), n=int(args.get("n", 5)))
        store.close()
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "memory_forget":
        store = MemoryStore(db_path=_memory_db_path())
        removed = store.forget(args["category"], args["key"])
        store.close()
        return json.dumps(
            {"category": args["category"], "key": args["key"], "removed": removed},
            ensure_ascii=False,
        )

    # ---- 产品化新增（路线 1/2/3）----
    if name == "memory_semantic_search":
        from memory_semantic import SemanticMemory
        # embedding="auto"：按 EMBEDDING_PROVIDER 环境变量选向量来源。
        # 不设该变量时就是默认哈希索引（行为与从前一致）。
        sm = SemanticMemory(db_path=_memory_db_path(), embedding="auto")
        hits = sm.search_semantic(
            args["query"], k=int(args.get("k", 5)), category=args.get("category")
        )
        sm.close()
        rows = [
            {"category": c, "key": k, "score": round(s, 4), "value": v}
            for c, k, s, v in hits
        ]
        return json.dumps(rows, ensure_ascii=False, indent=2)

    if name == "validate_secret":
        from scanner.validators import validate_secret
        res = validate_secret(args["secret"], args.get("secret_type", ""))
        # 只回传判定，绝不把密钥原文写进日志/结果
        return json.dumps(
            {"secret_type": args.get("secret_type", ""), **res}, ensure_ascii=False
        )

    if name == "push_protection":
        mode = args.get("mode", "hook")
        if mode == "ci":
            from scanner.push_protection import github_actions_yaml
            return json.dumps(
                {"mode": "ci",
                 "yaml": github_actions_yaml(
                     severity_threshold=args.get("severity_threshold", "high"),
                     use_semgrep=True)},
                ensure_ascii=False, indent=2,
            )
        if not args.get("repo_dir"):
            raise ValueError("mode=hook 需要提供 repo_dir")
        from scanner.push_protection import generate_pre_push_hook
        path = generate_pre_push_hook(
            args["repo_dir"], severity_threshold=args.get("severity_threshold", "high")
        )
        return json.dumps({"mode": "hook", "hook_path": path, "written": True},
                          ensure_ascii=False)

    if name == "queue_publish":
        q = _build_queue()
        try:
            mid = q.publish(args["topic"], args["message"] or {})
            return json.dumps({"topic": args["topic"], "message_id": mid},
                              ensure_ascii=False)
        finally:
            q.close()

    if name == "queue_consume":
        q = _build_queue()
        try:
            msgs = q.consume(args["topic"], batch=int(args.get("batch", 10)))
            if args.get("ack", True):
                for m in msgs:
                    q.ack(m.get("id") or m.get("message_id"))
            return json.dumps(msgs, ensure_ascii=False, indent=2)
        finally:
            q.close()

    if name == "api_health":
        from api.client import ApiClient
        base = args.get("base_url") or os.getenv("API_BASE")
        if not base:
            return json.dumps({"error": "missing_api_base",
                               "hint": "传 base_url 或设置环境变量 API_BASE"},
                              ensure_ascii=False)
        c = ApiClient(base, token=os.getenv("API_TOKEN"))
        return json.dumps(c.health(), ensure_ascii=False)

    if name == "scan_deep":
        from scanner.engine import scan_path
        res = scan_path(
            args["target"],
            providers=args.get("providers"),
            categories=args.get("categories"),
            severities=args.get("severities"),
            high_entropy=bool(args.get("high_entropy", True)),
            only_changed=bool(args.get("only_changed", False)),
            since=args.get("since", "HEAD~1"),
            baseline_path=args.get("baseline_path"),
            write_baseline=bool(args.get("write_baseline", False)),
            fail_on=args.get("fail_on", "high"),
        )
        # 默认只回摘要 + 脱敏后的发现，避免把整份 findings 灌进上下文
        out = {
            "mode": res["mode"],
            "scanned_files": res["scanned_files"],
            "rules": res["rules"],
            "summary": res["summary"],
            "gate": res["gate"],
            "elapsed_ms": res["elapsed_ms"],
            "findings": res["findings"],
        }
        if res.get("baseline_path"):
            out["new_summary"] = res.get("new_summary")
            out["resolved_since_baseline"] = len(res.get("resolved_since_baseline") or [])
        sarif_out = args.get("sarif_out")
        if sarif_out:
            from scanner.sarif import write_sarif
            from scanner.findings import Finding
            fs = [Finding(**{k: v for k, v in f.items() if k != "secret"})
                  for f in res["findings"]]
            out["sarif_path"] = write_sarif(fs, sarif_out, root=args["target"])
        return json.dumps(out, ensure_ascii=False, indent=2)

    if name == "memory_hybrid_search":
        from memory_hybrid import HybridMemory
        scope = {k: args[k] for k in ("user_id", "agent_id", "run_id", "org_id")
                 if args.get(k)}
        hm = HybridMemory(db_path=_memory_db_path(), embedding="auto")
        try:
            hits = hm.search(args["query"], k=int(args.get("k", 5)),
                             scope=scope or None, kinds=args.get("kinds"),
                             category=args.get("category"),
                             apply_decay=bool(args.get("apply_decay", True)),
                             explain=bool(args.get("explain", False)))
        finally:
            hm.close()
        return json.dumps(hits, ensure_ascii=False, indent=2, default=str)

    # ---- Agent Skills（SKILL.md 渐进式加载，借鉴 obra/superpowers）----
    if name == "skill_list":
        from agentskills.loader import SkillLibrary
        lib = SkillLibrary()
        q = args.get("query")
        items = lib.search(q, k=8) if q else lib.list(
            include_broken=bool(args.get("include_broken", False)))
        return json.dumps({"skills": items, "stats": lib.stats()},
                          ensure_ascii=False, indent=2)

    if name == "skill_load":
        from agentskills.loader import SkillLibrary
        lib = SkillLibrary()
        try:
            spec = lib.activate(args["name"])
        except KeyError as e:
            return json.dumps({"error": str(e),
                               "available": [s["name"] for s in lib.list()]},
                              ensure_ascii=False)
        return json.dumps({"name": spec.name, "description": spec.description,
                           "body": spec.body, "files": spec.files,
                           "tokens_est": spec.tokens_est,
                           "attribution": "derived from obra/superpowers (MIT)"},
                          ensure_ascii=False)

    # ---- 证据门禁（verification-before-completion 的硬落地）----
    if name == "evidence_submit" or name == "task_complete_verified":
        from agentskills.evidence import EvidenceGate, Evidence
        gate = EvidenceGate(db_path=_evidence_db_path(),
                            allow_run=os.getenv("EVIDENCE_ALLOW_RUN") == "1")
        ev = Evidence(
            command=args.get("command", ""),
            exit_code=int(args.get("exit_code", -1)),
            stdout=args.get("stdout", ""), stderr=args.get("stderr", ""),
            claim=args.get("claim", ""), task_id=args.get("task_id", ""),
            expect=args.get("expect", ""), forbid=args.get("forbid", ""),
            ran_at=float(args["ran_at"]) if args.get("ran_at") else time.time(),
            ttl=float(args.get("ttl", 300)),
        )
        if name == "evidence_submit":
            v = gate.verify(ev)
            eid = gate.record(ev, v)
            return json.dumps({"evidence_id": eid, "verdict": v.to_dict(),
                               "evidence": ev.redacted()},
                              ensure_ascii=False, indent=2)

        # task_complete_verified：核验不过则任务状态不变
        # 注意：TaskRegistry 已在模块顶部导入；此处**不可**再局部 import——
        # 那会让整个 _dispatch 里的同名引用退化为未绑定局部变量
        # （实测踩到：所有 list_tasks/get_task 全部 UnboundLocalError）。
        reg = TaskRegistry(db_path=_db_path())
        try:
            return json.dumps(
                gate.gate_complete(reg, args["task_id"], args.get("claim", ""), ev),
                ensure_ascii=False, indent=2)
        finally:
            reg.close()

    if name == "evidence_run":
        from agentskills.evidence import EvidenceGate
        if os.getenv("EVIDENCE_ALLOW_RUN") != "1":
            return json.dumps(
                {"error": "evidence_run_disabled",
                 "hint": "真跑会执行 shell 命令。设置 EVIDENCE_ALLOW_RUN=1 开启，"
                         "且命令必须命中测试/构建/只读白名单",
                 "allowlist_prefixes": list(
                     __import__("agentskills.evidence", fromlist=["RUN_ALLOWLIST"]
                                ).RUN_ALLOWLIST)},
                ensure_ascii=False, indent=2)
        gate = EvidenceGate(db_path=_evidence_db_path(), allow_run=True,
                            run_timeout=float(args.get("timeout", 120)))
        try:
            return json.dumps(
                gate.run_and_gate(args["command"], args.get("claim", ""),
                                  task_id=args.get("task_id", ""),
                                  cwd=args.get("cwd", ""),
                                  expect=args.get("expect", ""),
                                  forbid=args.get("forbid", "")),
                ensure_ascii=False, indent=2)
        except PermissionError as e:
            return json.dumps({"error": "command_not_allowed", "reason": str(e)},
                              ensure_ascii=False, indent=2)

    # ---- 编排层 DAG（多 AI 流水线；实现见 orchestration.py）----
    if name in ("dag_add", "dag_claim_ready", "dag_complete", "dag_status"):
        from orchestration import DagError, DagScheduler
        s = DagScheduler(db_path=_db_path())
        try:
            if name == "dag_add":
                tid = args.get("task_id") or f"dag-{uuid.uuid4().hex[:12]}"
                try:
                    created = s.add_task(tid, payload=args.get("payload"),
                                         depends_on=args.get("depends_on") or [])
                except DagError as e:
                    return json.dumps({"error": "dag_rejected", "reason": str(e)},
                                      ensure_ascii=False)
                return json.dumps({"ok": True, "task_id": created,
                                   "depends_on": args.get("depends_on") or []},
                                  ensure_ascii=False, indent=2)

            if name == "dag_claim_ready":
                t = s.claim_ready(task_id=args.get("task_id"),
                                  runner_id=args.get("runner_id", ""))
                if t is None:
                    return json.dumps({"ok": True, "claimed": None,
                                       "hint": "当前没有就绪任务（可能都在等待上游完成）"},
                                      ensure_ascii=False)
                return json.dumps({"ok": True, "claimed": t},
                                  ensure_ascii=False, indent=2, default=str)

            if name == "dag_complete":
                ok = s.complete(args["task_id"], result=args.get("result"))
                return json.dumps({"ok": True, "completed": ok,
                                   "task_id": args["task_id"],
                                   "note": None if ok else
                                   "幂等保护：该任务此前已完成或未在 running"},
                                  ensure_ascii=False)

            # dag_status
            out = {"stats": s.stats(), "waves": s.waves(),
                   "ready": [t["task_id"] for t in s.ready_tasks()]}
            if args.get("task_id"):
                out["waiting_on"] = s.waiting_on(args["task_id"])
                out["task"] = s.registry.get_task(args["task_id"])
            else:
                out["graph"] = s.graph()
            return json.dumps(out, ensure_ascii=False, indent=2, default=str)
        finally:
            s.close()

    if name in ("ballot_submit", "ballot_status", "ballot_confirm"):
        from ballot import Ballot, BallotError
        b = Ballot(db_path=_ballot_db_path())
        try:
            if name == "ballot_submit":
                try:
                    r = b.submit_proposal(args["group"], args["task_id"],
                                          args["output"],
                                          source=args.get("source", ""))
                except BallotError as e:
                    return json.dumps({"error": "ballot_rejected",
                                       "reason": str(e)},
                                      ensure_ascii=False)
                return json.dumps({"ok": True, **r}, ensure_ascii=False, indent=2)

            if name == "ballot_status":
                if args.get("rank") and args.get("group"):
                    try:
                        return json.dumps(b.rank(args["group"]),
                                          ensure_ascii=False, indent=2, default=str)
                    except BallotError as e:
                        return json.dumps({"error": "ballot_rejected",
                                           "reason": str(e)},
                                          ensure_ascii=False)
                if args.get("group"):
                    d = b.detail(args["group"])
                    if d is None:
                        return json.dumps({"error": "ballot_rejected",
                                           "reason": f"投票组不存在: {args['group']}"},
                                          ensure_ascii=False)
                    return json.dumps(d, ensure_ascii=False, indent=2, default=str)
                return json.dumps({"ballots": b.ballots()},
                                  ensure_ascii=False, indent=2, default=str)

            # ballot_confirm
            try:
                d = b.confirm(args["group"], args["task_id"],
                              by=args.get("by", "human"),
                              reason=args.get("reason", ""))
            except BallotError as e:
                return json.dumps({"error": "ballot_rejected", "reason": str(e)},
                                  ensure_ascii=False)
            return json.dumps({"ok": True, **d}, ensure_ascii=False,
                              indent=2, default=str)
        finally:
            b.close()

    raise ValueError(f"未知工具: {name}")


class McpServer:
    """极简 MCP（stdio / JSON-RPC 2.0）服务器。仅实现工具调用所需的最小协议面。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path

    # ---- 协议处理 ----
    def handle(self, req: dict) -> Optional[dict]:
        method = req.get("method")
        rid = req.get("id")  # None 表示通知（notification），无需回复
        params = req.get("params") or {}

        # ---- 协议版本协商（2026-07-28）：客户端能力改走 _meta，握手已移除 ----
        client_v = _client_protocol_version(req)
        if client_v and client_v not in SUPPORTED_PROTOCOL_VERSIONS:
            return _unsupported_version_error(rid, client_v)

        # ---- server/discover（2026-07-28 新增，取代 initialize 做能力协商）----
        if method == "server/discover":
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": (client_v
                                        if client_v in SUPPORTED_PROTOCOL_VERSIONS
                                        else LATEST_PROTOCOL_VERSION),
                    "supportedProtocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "capabilities": server_capabilities(),
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                                   "description": "跨进程任务登记处 + 深度合规扫描 + 长期记忆"},
                    "deprecatedFeatures": DEPRECATED_FEATURES,
                },
            }

        if method == "initialize":
            # 兼容 2024/2025 老客户端：握手已在新版移除，但仍需应答否则连不上。
            # 协商规则：客户端声明的版本若受支持则回它，否则回最新版。
            negotiated = client_v if client_v in SUPPORTED_PROTOCOL_VERSIONS \
                else PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "protocolVersion": negotiated,
                    "capabilities": server_capabilities(),
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                                   "description": "跨进程任务登记处 + 深度合规扫描 + 长期记忆"},
                    "_meta": {
                        _META_PREFIX + "protocolVersion": negotiated,
                        # 提示新客户端改用 discover / _meta，握手只是兼容层
                        _META_PREFIX + "deprecatedHandshake": True,
                    },
                },
            }
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _annotated_tools()}}
        if method == "tools/call":
            name = params.get("name")
            try:
                text = _dispatch(name, params.get("arguments"))
                return {
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}], "isError": False},
                }
            except Exception as e:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True},
                }
        # 未知方法：有 id 则回错误，无 id（通知）则静默
        if rid is not None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        return None

    # ---- stdio 传输（换行分隔 JSON）----
    def serve_stdio(self, instream=None, outstream=None):
        instream = instream or sys.stdin
        outstream = outstream or sys.stdout
        for line in instream:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except Exception:
                continue
            resp = self.handle(req)
            if resp is not None:
                outstream.write(json.dumps(resp, ensure_ascii=False) + "\n")
                outstream.flush()


def main():
    ap = argparse.ArgumentParser(description="task-registry 轻量 MCP Server（stdio, 零依赖）")
    ap.add_argument("--db-path", default=None, help="SQLite 路径（默认取 $DB_PATH 或 data/task_registry.db）")
    ap.add_argument("--log", default=None, help="可选：把协议日志写到该文件（默认仅 stderr 诊断）")
    args = ap.parse_args()

    if args.db_path:
        os.environ["DB_PATH"] = args.db_path

    # 协议错误/诊断只走 stderr，绝不污染 stdout（stdout 是 MCP 传输通道）
    print(f"[mcp] 启动 {SERVER_NAME} v{SERVER_VERSION}，DB={_db_path()}", file=sys.stderr)

    # 强制 stdout/stderr 为 UTF-8：本 Server 通过 stdio 回传含中文的 JSON，
    # 若继承 Windows 控制台代码页会导致编码失败；stderr 也一并设置以便诊断。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    server = McpServer(db_path=args.db_path)
    try:
        server.serve_stdio()
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
