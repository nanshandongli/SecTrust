---
name: writing-plans
description: Use when breaking approved work into executable steps for this repository - each step needs exact file paths, complete code, and a verification command whose output proves the step, not just a description of intent
---

# Writing Plans

## Overview

**Core principle:** A plan is a contract, not a summary. It must be executable
by someone with no project context and no taste, and every step must state how
to prove itself.

A step without a verification command is a wish. When the step is done there is
no way to tell success from self-deception, which is exactly the failure
`verification-before-completion` exists to block.

## Step Shape

Every step carries four fields. Missing any one makes the step unverifiable:

```markdown
### Step N: <what changes>

- **Files:** exact paths (create vs modify)
- **Change:** complete code or precise diff description — not "update the logic"
- **Verify:** `python -m unittest tests.test_x -v`
- **Pass criteria:** what the output must contain (e.g. "Ran 3 tests ... OK")
```

2–5 minutes of work per step. If a step needs more, split it.

## Quick Reference

| Plan says | Verdict |
|-----------|---------|
| "refactor the parser" | ✗ no path, no proof |
| "edit `scanner/engine.py` to skip dotfiles" | △ has path, no proof |
| "edit `scanner/engine.py`; verify `python tools/scan_bench.py` shows F1 ≥ 0.95" | ✓ |

## Ordering Rules

1. **Failing test before the fix.** Write the test, watch it fail, then change
   code. A test written after the fix proves nothing about the fix.
2. **Cheapest discriminator first.** Order steps so a wrong assumption fails
   early and cheaply.
3. **One concern per step.** Mixed refactor + feature steps cannot be bisected.
4. **Dependencies explicit.** If step 5 needs step 3's file, say so.

## Verification Ladder

Pick the strongest available rung; a weaker rung is not a substitute:

```
1. Targeted test      python -m unittest tests.test_depth -v
2. Full suite         python -m unittest discover -s tests
3. Benchmark          python tools/scan_bench.py
4. Audit              python tools/self_audit.py
5. Real subprocess    end-to-end over stdio / HTTP (never in-memory fakes)
```

Rung 5 matters here: several bugs in this repo (stable hashing across processes,
relative-path scanning) were invisible to in-process tests and only appeared in
real subprocess runs.

## Common Mistakes

- Steps that describe outcomes instead of edits.
- "Then verify it works" with no command.
- Batching all verification to the end — failures then have no attributable step.
- Tests written after the implementation, which cannot fail.
- Plan so coarse it cannot be reviewed before execution starts.
