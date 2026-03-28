# 518 — Scenario benchmarks: 20 real orchestration tasks for eval

**Role:** qa
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium
**Depends on:** [515]

## Problem
Existing benchmarks (`benchmark.py`) check structural signals (file_exists, import_succeeds) but never spawn real agents. They test the harness, not the orchestrator. Per Anthropic's eval guidance: "Start with 20-50 simple tasks drawn from real failures."

The eval harness (#515) needs actual scenarios to evaluate. This ticket creates them.

## Design

### 20 scenarios across 4 tiers

Each scenario = a small deterministic task with a known correct outcome:

**Smoke (5 tasks, <$0.10 each, <60s):**
1. Add docstring to a specific function
2. Fix a deliberately broken import
3. Add a missing `__init__.py`
4. Rename a variable across one file
5. Add a type annotation to a function signature

**Standard (7 tasks, <$0.50 each, <180s):**
6. Add a new CLI command with click decorator
7. Write 3 unit tests for an existing function
8. Extract a method from a long function
9. Add error handling to a try/except block
10. Fix a failing test by reading the error and correcting code
11. Add a config option to YAML parsing
12. Implement a simple dataclass from a spec

**Stretch (5 tasks, <$2.00 each, <600s):**
13. Add a new REST endpoint with request validation
14. Refactor a module to split into 2 files, preserving imports
15. Add integration test that starts server and queries it
16. Implement a feature requiring changes to 3+ files
17. Fix a multi-file bug (error in file A caused by logic in file B)

**Adversarial (3 tasks, <$1.00 each, <300s):**
18. Ambiguous spec — agent must ask clarifying questions or make reasonable assumption
19. Task with intentionally wrong completion signals — agent should flag the issue
20. Task that requires reading git history to understand context

### Scenario format (YAML)
Extends existing benchmark format:
```yaml
id: add-docstring-to-spawner
tier: smoke
setup:
  # Reset to known state
  command: "git checkout HEAD -- src/bernstein/core/spawner.py"
task:
  title: "Add Google-style docstring to Spawner.spawn_agent"
  role: backend
  effort: low
  model: sonnet
expected_signals:
  - type: file_contains
    path: src/bernstein/core/spawner.py
    value: "Args:"
  - type: test_passes
    value: "uv run pytest tests/unit/test_spawner.py -x -q"
  - type: command_succeeds
    value: "uv run ruff check src/bernstein/core/spawner.py"
limits:
  max_cost_usd: 0.10
  max_duration_seconds: 60
  max_retries: 0
```

### Scenario repo
Each scenario runs against Bernstein's own codebase (self-referential eval). Setup step resets modified files to a known state. This avoids maintaining separate test repos.

### Stochastic handling
Agent behavior is non-deterministic. Run each scenario 3 times, report:
- Pass rate (0/3, 1/3, 2/3, 3/3)
- Mean cost and duration
- Failure classification (from #517 taxonomy)

Scenario is "passing" only if 2/3+ runs succeed.

## Files
- .sdd/eval/scenarios/ (new directory) — 20 YAML scenario files
- src/bernstein/eval/scenario_runner.py (new) — scenario execution engine
- tests/unit/test_scenario_runner.py (new)

## Completion signals
- path_exists: .sdd/eval/scenarios/01-add-docstring.yaml
- file_contains: src/bernstein/eval/scenario_runner.py :: ScenarioRunner
- test_passes: uv run pytest tests/unit/test_scenario_runner.py -x -q
