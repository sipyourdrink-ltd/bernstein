# Unstoppable --evolve mode with competitive intelligence

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
The evolve mode (#200) just re-runs codebase analysis. It needs to be truly autonomous and unstoppable — continuously researching, planning, building, testing, and deploying improvements. It should run for hours/days unattended.

## The evolve loop
```
while running:
    1. RESEARCH — what do users need? what are competitors doing? what's trending?
    2. ANALYZE  — read own codebase, run tests, check metrics from last cycle
    3. PLAN     — create improvement tasks based on research + analysis
    4. EXECUTE  — spawn agents to implement tasks
    5. VERIFY   — run tests, check quality, rollback if broken
    6. COMMIT   — git commit successful changes
    7. SLEEP    — wait N minutes, then repeat
```

## Key behaviors
- **Never break the build** — run full test suite before committing. If tests fail, rollback and create a fix task for next cycle.
- **Git discipline** — each improvement is a separate commit with descriptive message
- **Cost awareness** — track API spend per cycle. Stop if budget exceeded.
- **Cycle logging** — each cycle writes to .sdd/metrics/evolve_cycles.jsonl with: timestamp, research_queries, tasks_created, tasks_completed, tasks_failed, tests_passed, tests_failed, commits_made, cost_usd
- **Diminishing returns detection** — if 3 consecutive cycles produce zero successful changes, increase sleep interval (backoff)
- **Priority rotation** — alternate between: new features, test coverage, code quality, documentation, performance

## CLI
```bash
bernstein --evolve                    # run forever
bernstein --evolve --max-cycles 10    # stop after 10 cycles
bernstein --evolve --budget 50.0      # stop at $50 spend
bernstein --evolve --interval 600     # 10 min between cycles
```

## Files
- src/bernstein/cli/main.py — --evolve, --max-cycles, --budget, --interval flags
- src/bernstein/core/orchestrator.py — evolve loop logic
- src/bernstein/core/researcher.py — web research (from #204)

## Acceptance criteria
- `bernstein --evolve` runs continuously
- Each cycle: research → plan → execute → verify → commit
- Budget cap stops the loop when exceeded
- Diminishing returns backoff works
- Cycle metrics are logged
- Tests pass after every commit (or rollback)
