---
name: registry-first
description: Use when starting any multi-step, long-running, or cross-process work in this repository - register the task in the SQLite registry before acting, claim it with CAS, beat heartbeats, and gate completion on evidence
---

# Registry First

## Overview

**Core principle:** Work that outlives a prompt belongs in the registry, not in
the conversation.

A chat transcript is not durable: it compacts, it is lost on restart, and other
processes cannot see it. This repo ships a cross-process task registry
(`task_registry.py`, SQLite WAL) precisely so that state survives sessions and
runners. If a task matters to anyone but the current turn, it goes in first.

## When It Applies

| Situation | Register? |
|-----------|-----------|
| Single question, one answer | No — just answer |
| Multi-step change across files | Yes |
| Anything running > ~30s | Yes (heartbeat) |
| Work another process/runner may pick up | Yes (CAS claim) |
| Work whose completion needs proof | Yes (evidence gate) |

## The Loop

```python
from task_registry import TaskRegistry
reg = TaskRegistry()
tid = reg.register_task(None, {"title": "..."})     # None → 自动生成 id
reg.claim_task(tid)                                  # CAS 认领，防重复执行
reg.set_progress(tid, progress=0.3, log="...")
reg.beat()                                           # 心跳，证明还活着
```

Only then complete — and completion is gated:

```python
from agentskills.evidence import EvidenceGate, Evidence
gate = EvidenceGate(db_path="data/evidence.db")
gate.gate_complete(reg, tid, "全部测试通过", ev)      # 证据不足会被驳回
```

## Rules

1. **Register before the first edit**, not after the first failure.
2. **Claim with CAS.** Two runners claiming the same task is the classic
   duplicate-execution bug; `claim_task` is atomic for exactly this reason.
3. **Beat while running.** A silent runner is indistinguishable from a dead one;
   `list_runners(stale_after=90)` prunes by heartbeat.
4. **Never mark completed by hand.** Route through the evidence gate, or the
   state is a claim, not a fact.

## Common Mistakes

- Registering retroactively once blocked — progress is then invisible while it
  matters most.
- Setting status directly with `update_task_status` to skip the gate.
- Long work with no heartbeat, then wondering why the task looks orphaned.
- Treating the registry as a log: it is state. Logs go in `set_progress(log=...)`.
