---
name: systematic-debugging
description: Use when a bug, test failure, or unexpected behavior needs a root cause rather than a patch, especially after two failed fix attempts, when the symptom is intermittent, or when a fix works but the reason is unknown
---

# Systematic Debugging

## Overview

**Core principle:** Process over guessing. Find the root cause *before* changing
code; a fix that works without knowing why is a fix you cannot trust.

Random fixes mutate the system until the symptom disappears while the cause
stays. Cost: the same bug returns, and the second regression is harder to
attribute. This skill is derived from `obra/superpowers` (MIT), adapted to this
repo's audit tooling.

## The Four Phases

```
1. OBSERVE       Reproduce and capture the exact symptom + trigger conditions
2. HYPOTHESIZE   Enumerate candidate causes, ranked by what the evidence rules out
3. INSTRUMENT    Add the minimum probe that discriminates between candidates
4. FIX + PROVE   Fix the cause, then prove it: revert the fix, see test fail, restore
```

Do not skip from phase 1 to phase 4. The urge to do so is the failure mode.

## Quick Reference

| Symptom | First move | Anti-pattern |
|---------|-----------|--------------|
| Test fails | Read the failure output in full; find the first failing assertion | Re-running hoping it flakes away |
| Works locally, fails in CI | Diff environment (versions, cwd, env vars, locale) | "CI is flaky" |
| Intermittent | Identify the varying input: time, ordering, concurrency, cache | Adding `sleep()` |
| Regression after refactor | Bisect the commit range | Reverting everything |
| Silent wrong result | Instrument the boundary where the value should change | Guessing at the sink |

## Techniques

**Root-cause tracing.** Start at the symptom and walk *backwards* through the
call chain, asserting what each step should have produced. The first step whose
actual value diverges from expectation is where the cause lives — not where the
error surfaced.

**Defense in depth.** After fixing, ask: why did no earlier layer catch this?
Add the check at the earliest layer that *could* have caught it, not at the layer
where you happened to fix it.

**Condition-based waiting.** Never `sleep(N)` to wait for an asynchronous state.
Poll for the predicate with a deadline; a sleep hides races instead of resolving
them and makes the suite slow for everyone.

## Prove It (the red-green proof)

```
1. Fix the cause
2. Run the test → PASS
3. Revert the fix → run the test → MUST FAIL
4. Restore the fix → run the test → PASS
```

If step 3 passes, your test never covered the bug. Say so instead of claiming it.

## Use This Repo's Audit Tools

```bash
python tools/self_audit.py --only security   # 穿透式安全自查
python tools/scan_bench.py                   # 扫描召回/精确率基准
python -m unittest discover -s tests         # 全量回归（136 例）
```

Against a cross-process bug, check `task_registry` state and WAL first — many
"mysterious" failures here are connection/visibility issues, not logic errors.

## Common Mistakes

- Fixing before reproducing: you cannot verify a fix for a bug you cannot trigger.
- Changing several variables at once — then nothing is attributable.
- Accepting "it passes now" without the step-3 revert proof.
- Instrumenting permanently: probes are scaffolding, delete them after.
- Declaring victory at the symptom layer when the cause is one layer up.
