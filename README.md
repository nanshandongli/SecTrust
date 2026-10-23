<div align="center">

# SecTrust

**An auditable, always-on AI colleague platform — with humans still in charge.**

Cross-process task registry → DAG orchestration → HITL approvals → compliance scanning → long-term memory,
exposed as a **zero-dependency MCP server** (36 tools, pure Python stdlib).

[English](README.md) | [简体中文](README.zh-CN.md)

**Status: experimental (v0.8.0-alpha)** — 482 tests passing, real multi-process demos, no production traffic yet.

</div>

---

## Why SecTrust

Most multi-agent frameworks give you *coordination* and hope for the best. SecTrust gives you
**an operating base where AI colleagues hold real jobs — and every consequential step has a human checkpoint**:

- **A minimal, reliable contract.** AI coworkers on the same machine exchange task metadata through one
  local SQLite file (WAL mode) — zero ops, transactional, crash-safe. Need to go cross-machine?
  HTTP API and an optional Redis-backed queue are already there.
- **Humans approve by default.** An ApprovalCenter gates dangerous actions (`pending → approved/rejected/expired`,
  **expires to *deny***, never to *allow*), with WeCom / Feishu push out of the box.
- **Costs are routed, not burned.** A built-in cost router grades every request *simple / medium / complex* —
  a one-liner runs as a single task, not a 7-node DAG. Estimated LLM calls are attached to every task for attribution.
- **Three collaboration modes.** Pipeline (planner + DAG), iterative refinement (quality gate sends work back
  with feedback until it passes), and **voting** (fan the same task out to N workers, score the proposals with
  the same quality rules, recommend a winner — *a human confirms; ranking alone never takes effect*).

## Quickstart

Zero dependencies (pure Python stdlib). Python ≥ 3.10, no `pip install` needed:

```bash
git clone https://github.com/nanshandongli/SecTrust.git
cd SecTrust

python demo.py                                        # ① core contract self-test (real cross-process concurrency)
python -m unittest discover -s tests -p "test_*.py"   # ② full suite: 482 tests
python task_center_web.py                             # ③ open http://127.0.0.1:8090 — live task dashboard
```

**Wire it into any MCP host** (Claude Desktop, WorkBuddy, …):

```json
{
  "mcpServers": {
    "sectrust": {
      "command": "python",
      "args": ["/absolute/path/to/SecTrust/mcp_server.py"]
    }
  }
}
```

36 tools go live immediately: task register/claim, DAG orchestration (`dag_*`), quality rework loops,
proposal voting (`ballot_*`), compliance scanning, long-term memory, approval queries.

## What's inside

| Layer | What it does |
|---|---|
| `task_registry.py` | The core contract: `TaskRegistry` — tasks/runners tables, CAS claiming, self-healing connections |
| `orchestration.py` | DAG scheduler: dependencies, readiness waves, failure cascades, parallel-safe claiming |
| `planner.py` | Decomposer: one sentence → a subtask DAG with skills & quality rules; **cost routing (simple/medium/complex)**; slot extraction never fabricates |
| `agentskills/quality.py` | Quality loop: verify output → send back with feedback → escalate to human after 3 reworks |
| `ballot.py` | Voting: N proposals scored by the same quality rules; `rank()` recommends, `confirm()` takes effect |
| `arbiter.py` | Conflict arbitration: registers competing claims, **defaults to human judgment** |
| `approval.py` | HITL ApprovalCenter: pending/approved/rejected/**expired-to-deny** |
| `sandbox/` | Enforced local sandbox: rlimits, process-tree kill, output truncation |
| `scanner/` | Compliance scanner: 78 detection rules, entropy engine, fingerprints, SARIF, gitleaks benchmark (F1 = 1.0) |
| `memory_semantic.py` / `memory_hybrid.py` | Memory: BM25 + entities + optional real embeddings (Ollama/OpenAI), vector-space guards against silent dimensional corruption |
| `channels/` | WeCom / Feishu adapters with real AES-256-CBC callback verification |
| `games/` | Two-player Chinese chess — a live proof that the base supports real-time multi-agent play |
| `mcp_server.py` | The MCP server: 36 tools over stdio, JSON-RPC 2.0, protocol 2026-07-28 |

## Honest status

What's proven: single-machine multi-process orchestration end-to-end; 482 tests; benchmark-aligned scanner
(2 TP / 0 FP on gitleaks vectors); stress-tested write throughput (~685 TPS at 8 processes, CAS claiming with
zero duplicates); MCP tools verified from a clean install.

What's **not** proven yet: cross-machine deployments (implemented, untested under real network latency);
production traffic; PyPI/MCP Registry listing (materials ready); real WeCom/Feishu credentials.
The Chinese docs ([README.zh-CN.md](README.zh-CN.md), [PRODUCT_SUMMARY.md](PRODUCT_SUMMARY.md)) keep a
running "honesty ledger" of exactly this kind.

## Docs

- [DESIGN.md](DESIGN.md) — runtime extension architecture (channels / skills / models), trust boundaries, key management
- [README.zh-CN.md](README.zh-CN.md) — full Chinese documentation (deployment, WAL ops, workflows)
- [PRODUCT_SUMMARY.md](PRODUCT_SUMMARY.md) — feature inventory, market position, roadmap (P0/P1/P2)
- [SECURITY.md](SECURITY.md) — security narrative; [SELF_AUDIT_REPORT.md](SELF_AUDIT_REPORT.md) — reproducible self-audit
- [RELEASING.md](RELEASING.md) — PyPI / MCP Registry publishing steps
- [CHANGELOG.md](CHANGELOG.md) — version history

## License

[AGPL-3.0-only](LICENSE). In short: use it, study it, modify it, run your AI colleagues with it —
if you offer SecTrust itself as a hosted service, the network-use clause requires you to share your
modified source. Commercial dual-licensing may be considered; open an issue to discuss.
