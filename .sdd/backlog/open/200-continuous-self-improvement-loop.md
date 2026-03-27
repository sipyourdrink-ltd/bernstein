# Continuous self-improvement loop (--evolve mode)

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
When all backlog tickets are done, Bernstein stops. It should have a `--evolve` mode where it continuously:
1. Runs the codebase analysis (read code, run tests, check coverage, review architecture)
2. Identifies improvement opportunities (gaps, bugs, missing tests, performance issues)
3. Creates new tasks in the task server
4. Executes them
5. Repeats

## Implementation
- Add `--evolve` flag to CLI: `bernstein --evolve` or `bernstein -e`
- When orchestrator detects all tasks done + evolve mode is on:
  1. Spawn a "manager" agent that analyzes the codebase
  2. Manager creates new improvement tasks via POST /tasks
  3. Orchestrator picks them up and spawns worker agents
  4. Cycle repeats every N minutes (configurable, default 10)
- Add a `max_cycles` config option (default: unlimited)
- Add a `max_cost_usd` safety cap
- Log each evolution cycle to `.sdd/metrics/evolution_cycles.jsonl`

## Files
- src/bernstein/cli/main.py — add --evolve flag
- src/bernstein/core/orchestrator.py — detect idle state, trigger re-planning
- src/bernstein/core/bootstrap.py — pass evolve flag through

## Acceptance criteria
- `bernstein --evolve` continuously finds and executes improvements
- Stops when max_cycles or max_cost reached
- Each cycle is logged
- Tests cover the idle detection and re-planning trigger
