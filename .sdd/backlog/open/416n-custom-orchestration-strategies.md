# 416n — Custom Orchestration Strategies

**Role:** backend
**Priority:** 5 (low)
**Scope:** large
**Depends on:** #608

## Problem

The orchestration strategy (how tasks are decomposed, assigned, and merged) is hardcoded. Users with different workflows cannot customize the orchestration behavior without forking the code. A plugin system would enable community-driven growth and experimentation with novel orchestration patterns.

## Design

Build a plugin system for custom orchestration strategies. Define a strategy interface with hooks: `decompose(task) -> subtasks`, `assign(subtask, agents) -> agent`, `merge(results) -> output`, and `review(output) -> approved/rejected`. Ship three built-in strategies: "supervisor" (default — one planner, N workers), "round-robin" (tasks distributed evenly), and "specialist" (tasks matched to agent expertise). Users can create custom strategies as Python modules and register them in `.sdd/config.toml`. Strategies are loaded dynamically at runtime. Provide a strategy template with documentation. Support strategy composition: a custom strategy can delegate to built-in strategies for specific hooks. The plugin system should also support custom merge strategies and review pipelines as sub-components.

## Files to modify

- `src/bernstein/core/strategy.py` (new — strategy interface)
- `src/bernstein/strategies/supervisor.py` (new)
- `src/bernstein/strategies/round_robin.py` (new)
- `src/bernstein/strategies/specialist.py` (new)
- `src/bernstein/core/orchestrator.py`
- `docs/custom-strategies.md` (new)
- `tests/unit/test_strategies.py` (new)

## Completion signal

- Three built-in strategies available and selectable via config
- Custom Python strategy loads from user-specified module
- Strategy interface documented with examples
