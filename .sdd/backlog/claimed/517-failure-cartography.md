# 517 — Failure cartography: classify, track, and learn from every failure

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** medium
**Depends on:** [515]

## Problem
Failures are logged as "agent died" or "janitor failed" with no classification. We can't answer: "What TYPE of failure happens most? Is it getting worse? Which tasks are unstable?" Without a failure taxonomy, evolution can't target the root causes.

Pattern stolen from rag_challenge/eval/failure_cartography.py.

## Implementation

### 1. Failure taxonomy (closed set)
Every task failure gets classified into exactly one category:
- **ORIENTATION_MISS**: agent spent >50% turns on exploration (from log analysis)
- **SCOPE_CREEP**: agent modified files outside owned_files
- **TEST_REGRESSION**: agent's changes broke existing tests
- **INCOMPLETE**: agent ran out of turns before finishing
- **TIMEOUT**: wall-clock limit exceeded
- **CONFLICT**: merge conflict with concurrent agent
- **CONTEXT_MISS**: agent explicitly asked for info not in prompt
- **HALLUCINATION**: agent generated non-compiling or referencing nonexistent code
- **SIGNAL_LOSS**: task had no completion signals (systemic issue from #retry bug)
- **INFRA**: server/spawner/network failure (not agent's fault)

### 2. Auto-classification
In janitor / orchestrator, classify failures by parsing agent logs:
- Count file reads vs writes → orientation ratio
- Check `git diff` for files outside owned_files
- Parse test output for regressions
- Check turn count vs max_turns

### 3. Drift tracking
For tasks that appear in multiple eval runs, track outcome stability:
```json
{"task": "add-auth-endpoint", "runs": [
  {"run": 1, "outcome": "PASS", "category": null},
  {"run": 2, "outcome": "FAIL", "category": "CONTEXT_MISS"},
  {"run": 3, "outcome": "PASS", "category": null}
]}
```
Unstable tasks (flip-flopping) indicate: non-determinism, context sensitivity, or task ambiguity.

### 4. CLI
```bash
bernstein eval failures              # show failure taxonomy breakdown
bernstein eval failures --drift      # show unstable tasks
bernstein eval failures --category CONTEXT_MISS  # filter by type
```

### 5. Evolution integration
Feed failure distribution to evolution detector:
- If ORIENTATION_MISS > 30%: prioritize context injection improvements
- If TIMEOUT > 20%: increase turn limits or split tasks
- If CONFLICT > 10%: improve file ownership or add git integration

## Files
- src/bernstein/eval/taxonomy.py (new or extend from #515)
- src/bernstein/eval/cartography.py (new) — drift tracking, classification
- src/bernstein/core/janitor.py — emit failure classification
- tests/unit/test_failure_cartography.py (new)

## Completion signals
- file_contains: src/bernstein/eval/cartography.py :: FailureCartography
- test_passes: uv run pytest tests/unit/test_failure_cartography.py -x -q
