# 飞书接入指南（notify.py + assistant_channels.py）

> 定位：把协作审计台的**真实审计事件**接到飞书群——不是「配了个 webhook 就叫接通」，
> 而是**审批请求、审批决定、任务完成/失败、源码自扫高危、成本快照**这些事件
> 在**发生的那一刻**就真的出现在群里。

---

## 1. 之前缺什么（真实缺口，不是想象）

2026-10-28 排查发现：`AssistantSettings.send()` **只被「测试」按钮调用**。

```
grep 全部调用方 → 唯一命中：console_web.py 的 /api/assistant/channels/test
```

也就是说：飞书通道**配置上是通的**（webhook 早已存好、`enabled: true`、
实测推送返回 `code: 0 / success`），但**产品侧根本没接线**——
审批决定、高危发现、任务完成**都不会自动到飞书**。

飞书是「连上了但没接线」。本次补的就是这根线。

---

## 2. 出站：事件 → 飞书群

### 2.1 架构

```
审批/任务/自扫/成本
      │  （事件发生）
      ▼
ApprovalCenter.on_event ──┐
execution/daemon 终态 ────┤
runner 终态 ──────────────┼──▶ notify.NotifyEngine.notify()
console_web._run_action ──┘         │
                                     ├─ 查订阅表（事件类型 × 通道）
                                     ├─ 去重窗口（5 分钟）
                                     ├─ 对每个已启用且已订阅的通道
                                     │     └─▶ AssistantSettings.send()
                                     └─ 写推送日志 data/notify_log.jsonl
```

### 2.2 事件类型

| 事件 ID | 标签 | 触发时机 |
|---|---|---|
| `approval.pending` | 审批请求 | 有人发起高风险动作等待人工审批 |
| `approval.decided` | 审批决定 | 审批被批准/拒绝/过期 |
| `security.high_risk` | 高危安全发现 | 一键自扫命中高危/严重级别 |
| `task.completed` | 任务完成 | 任务进入 done/completed 终态 |
| `task.failed` | 任务失败 | 任务进入 failed/needs_human 终态 |
| `cost.anomaly` | 成本异常 | 成本快照生成 |

事件目录是**单一事实来源**（`notify.EVENT_TYPES`），前端不硬编码，
由 `GET /api/assistant/notify` 下发。

### 2.3 订阅

默认**所有已启用通道订阅全部事件**——用户明确要求「让产品链接上飞书」，
默认全开才能真的收到消息；不想要某一类可以在「助理 → 事件订阅」面板单独关掉
（**显式可关 > 静默不推**）。

订阅表落 `data/notify_subscriptions.json`（已 gitignore）。

### 2.4 三条纪律（改动不可破坏）

1. **推送失败绝不阻塞主流程**。审批已经落库了，推送失败只能记账，
   不能把审批接口打成 500。这与执行层「死信记账失败不挡执行」同族。
2. **不假装成功**。平台返回 `errcode/code != 0` 一律如实记失败，
   企微返回 `93000 invalid webhook url` 也如实写进日志（不吞）。
3. **去重键要跨进程稳定**。用 `sha1` 而不是内置 `hash()`——
   内置 `hash()` 每进程随机盐，跨进程不一致，去重就失效了。

### 2.5 端点

| 端点 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/api/assistant/notify` | GET | 只读 | 事件目录 + 订阅 + 最近推送记录 + 入站配置 |
| `/api/assistant/notify/subscriptions` | POST | 需登录 | 整表覆盖订阅 |
| `/api/assistant/notify/test` | POST | 需登录 | 推一条事件自检（区别于「测试」按钮的固定文案） |

---

## 3. 入站：飞书群里回复即控制（诚实边界）

### 3.1 边界必须说清

**飞书群机器人 webhook（`open-apis/bot/v2/hook/`）是「只出不进」的。**

推过去的卡片拿不到回调，机器人也**收不到群消息**。
所以「在飞书群里回复 `/approve ap-xxx` 就完成审批」**不能靠群机器人实现**。

要真正双向，必须：

> **飞书企业自建应用 + 事件订阅**（接收 `im.message.receive_v1`）
> → 把群消息 POST 到本产品的 `/api/assistant/inbound`

本产品提供的是**后半段**：一个带令牌的入站端点 + 白名单指令解析 +
**走原有审批中心**（带审计轨迹）执行决定。

### 3.2 入站端点

```
POST /api/assistant/inbound
Header: X-Notify-Token: <入站令牌>
Body:   {"text": "/approve ap-xxxx", "user": "alice"}
```

也兼容飞书事件订阅的原始结构（`event.message.content.text`）。

**令牌从哪来**：登录后在「助理 → 飞书入站」面板可见；
令牌落 `data/notify_inbound_token`（已 gitignore）。
**未登录时 `GET /api/assistant/notify` 不返回令牌**——
这个端点只读且不鉴权，把入站令牌明文挂上去等于任何人都能远程批你的审批。

### 3.3 指令白名单

| 指令 | 作用 |
|---|---|
| `/approve <审批ID>` | 批准指定审批 |
| `/reject <审批ID>` | 拒绝指定审批 |
| `/pending` | 列出当前待审批 |
| `/help` | 显示用法 |

**认不出来就报错，不猜、不执行。**（`parse_command()` 返回 `None` → HTTP 400）

执行走的是 `ApprovalCenter.decide()` —— 与控制台里点按钮**同一条路径**，
因此审计轨迹（`approval_decided`）照常产生，不是绕过审计的捷径。

---

## 4. 怎么接（3 步）

### 4.1 出站（推送事件到群）

1. 控制台 →「助理」→ 找到「飞书群机器人」→ 点「连接」
2. 粘贴 Webhook（群设置 → 群机器人 → 自定义机器人）
3. 打开「启用」→ 保存
4. 点「发送一条事件自检」——**群里出现消息即链路通**

### 4.2 入站（群里回复即控制，可选）

1. 飞书开放平台 → 创建**企业自建应用**
2. 开通机器人能力 + 订阅事件 `im.message.receive_v1`
3. 把群消息事件转发到 `https://<你的主机>/api/assistant/inbound`，
   并带上 `X-Notify-Token`
4. 在群里发 `/pending` 验证

### 4.3 命令行直连（排障）

```bash
python - <<'EOF'
import notify as nt
from assistant_channels import AssistantSettings
s = AssistantSettings(data_dir="data")
eng = nt.NotifyEngine(s)
print(eng.subscriptions())     # 看订阅
print(eng.enabled_channels())  # 看已启用通道
print(eng.recent(10))          # 看推送日志
EOF
```

---

## 5. 诚实边界

- 群机器人**只出不进**；入站必须自建应用 + 事件订阅（见第 3 节）。
- 入站端点只认 4 条白名单指令，**不做自然语言理解**——
  不猜、不执行「看起来像批准」的话。
- 推送**不保证送达**：网络、平台限流、群机器人被移除都会导致失败。
  失败会记进 `data/notify_log.jsonl`，但**没有重试队列**
  （重试策略属调度层决策，不在此暗做）。
- 企微/钉钉走同一套事件引擎，签名密钥只在本地用于签名，
  **绝不出现在推送体里**。
- 推送日志（`data/notify_log.jsonl`）含事件标题与摘要，
  **不含** webhook 明文与签名密钥。
