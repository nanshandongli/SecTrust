---
name: verification-before-completion
description: Use when about to claim work is complete, fixed, or passing, before committing, creating PRs, or marking a registry task completed - requires submitting fresh verification evidence through the evidence gate; evidence before assertions always
---

# Verification Before Completion

## Overview

**Core principle:** Evidence before claims, always.
**Violating the letter of this rule is violating the spirit of this rule.**

```
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

This skill is derived from `obra/superpowers` (MIT). The upstream version is a
*prompt* — it asks the model to self-police, and the model can ignore it. This
version is wired to a **gate**: `agentskills/evidence.py` refuses to move a task
to `completed` unless the evidence passes machine-checkable rules. The prompt is
advisory; the gate is not.

## The Gate Function

```
BEFORE claiming any status or expressing satisfaction:

1. IDENTIFY: What command proves this claim?
2. RUN: Execute the FULL command (fresh, complete)
3. READ: Full output, check exit code, count failures
4. VERIFY: Does output confirm the claim?
   - If NO: State actual status with evidence
   - If YES: State claim WITH evidence
5. ONLY THEN: Make the claim

Skip any step = lying, not verifying
```

Here step 2–4 are enforced by `EvidenceGate.verify()`, not by willpower.

## Quick Reference

| Claim | Requires | Not sufficient |
|-------|----------|----------------|
| Tests pass | Test command output: 0 failures | Previous run, "should pass" |
| Linter clean | Linter output: 0 errors | Partial check, extrapolation |
| Build succeeds | Build command: exit 0 | Linter passing, logs look good |
| Bug fixed | Test original symptom: passes | Code changed, assumed fixed |
| Regression test works | Red-green cycle verified | Test passes once |
| Agent completed | VCS diff shows changes | Agent reports "success" |
| Requirements met | Line-by-line checklist | Tests passing |

## Gate Checks (what actually blocks you)

| Check | Rule |
|-------|------|
| command is a command | ≥4 chars, no CJK, real executable token — a sentence is not a command |
| exit code | must be 0 |
| stdout | must be non-empty (silent success is not evidence) |
| freshness | age ≤ ttl (default 300s) — last run ≠ this run |
| expect regex | if given, stdout must match it |
| forbid regex | if given, stdout must NOT match it |
| wording | claim must not contain `should/probably/seems/应该/大概/似乎` |

## Rationalization Prevention

| Excuse | Reality |
|--------|---------|
| "Should work now" | RUN the verification |
| "I'm confident" | Confidence ≠ evidence |
| "Just this once" | No exceptions |
| "Linter passed" | Linter ≠ compiler |
| "Agent said success" | Verify independently |
| "I'm tired" | Exhaustion ≠ excuse |
| "Partial check is enough" | Partial proves nothing |
| "Different words so rule doesn't apply" | Spirit over letter |

## Key Patterns

**Tests:**
```
✅ [Run test command] [See: 34/34 pass] "All tests pass"
❌ "Should pass now" / "Looks correct"
```

**Regression tests (TDD Red-Green):**
```
✅ Write → Run (pass) → Revert fix → Run (MUST FAIL) → Restore → Run (pass)
❌ "I've written a regression test" (without red-green verification)
```

**With the registry:**
```python
from agentskills.evidence import EvidenceGate, Evidence
gate = EvidenceGate(db_path="data/evidence.db")
ev = Evidence(command="python -m unittest discover -s tests", exit_code=0,
              stdout="Ran 136 tests ... OK", claim="全部测试通过",
              task_id=task_id)
gate.gate_complete(registry, task_id, "全部测试通过", ev)   # 驳回或放行
```

## Common Mistakes

- Submitting the *claim text* as the `command` field — the gate rejects it.
- Reusing a stale run: evidence older than `ttl` is expired by design.
- Trusting a sub-agent's "success" report; check the diff yourself.
- Running a command unrelated to the claim (guard with `expect`).
- Declaring "Done!" before step 2 — the wording scan fires on this.

## When To Apply

**ALWAYS before:** any success/completion claim, any expression of satisfaction,
committing, PR creation, task completion, moving to next task, delegating to agents.

Applies to exact phrases, paraphrases, synonyms, and implications of success.
