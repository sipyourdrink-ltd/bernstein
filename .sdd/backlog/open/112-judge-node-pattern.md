# Implement Judge Node pattern (inspired by Hive)

**Role:** architect
**Priority:** 2 (normal)
**Scope:** large
**Complexity:** high

## Background
Hive (aden-hive/hive, 9.8k stars) uses a "Judge" pattern: a separate LLM evaluates
whether agents actually completed their work, returning ACCEPT or RETRY with confidence
scores. This is more powerful than Bernstein's current janitor which only checks
deterministic signals (path_exists, test_passes, file_contains).

## What to borrow from Hive
1. **LLM Judge for ambiguous tasks**: When completion signals alone can't verify quality
   (e.g. "refactor this for clarity"), spawn a cheap LLM (Sonnet) to review the diff
   and score it on criteria: correctness, completeness, code quality.
2. **ACCEPT/RETRY verdict**: If judge says RETRY, create a fix task with the judge's
   feedback as the description. Max 2 retries before failing.
3. **Confidence scores**: Judge returns 0.0-1.0 confidence. Below 0.7 = human review.
4. **Criteria alignment**: Judge criteria derived from task description + completion
   signals, not hardcoded.

## What NOT to borrow
- Hive's graph-based execution (we're batch-based by design, ADR-001)
- Hive's long-running Queen Bee (our manager is short-lived)
- Hive's web UI (we're CLI-first)
- Hive's event bus (we use HTTP task server)

## Implementation
- Extend janitor.py with a new signal type: `llm_judge`
- When `llm_judge` signal is present on a task:
  1. Read the agent's diff (git diff of files in owned_files)
  2. Construct a judge prompt: task description + diff + criteria
  3. Call Claude Sonnet (cheap) via Claude Code CLI: `claude -p "judge prompt" --model sonnet`
  4. Parse response for verdict (ACCEPT/RETRY) and confidence (0.0-1.0)
  5. If ACCEPT: mark task complete
  6. If RETRY (max 2): create fix task with judge feedback
  7. If confidence < 0.7: flag for human review

## Files
- src/bernstein/core/janitor.py (extend with llm_judge)
- tests/unit/test_janitor.py (new tests)

## Completion signals
- test_passes: uv run pytest tests/unit/test_janitor.py -x -q
- file_contains: src/bernstein/core/janitor.py :: llm_judge
