# 329 — Decompose Monoliths Round 2 (800-line rule)

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** large
**Depends on:** none

## Problem

After round 1 decomposition (#330a/b/c), new monoliths grew back and some weren't fully split. 14 files exceed 800 lines. The 800-line rule: any file >800 lines is doing too much and should be split by domain responsibility.

## Current monoliths (>800 lines)

| File | Lines | Action |
|------|-------|--------|
| `cli/main.py` | 2985 | Split remaining inline commands into modules |
| `core/orchestrator.py` | 2217 | Extract evolve integration, session mgmt |
| `core/server.py` | 1710 | Already has routes/, extract TaskStore to own module |
| `core/context.py` | 1326 | Split: context building vs knowledge base vs file discovery |
| `core/manager.py` | 1284 | Split: planning vs review vs decomposition |
| `evolution/loop.py` | 1261 | Split: cycle runner vs proposal scoring vs apply logic |
| `core/task_lifecycle.py` | 1254 | Split: claim/spawn vs completion vs retry |
| `core/bootstrap.py` | 1139 | Split: preflight checks vs server start vs orchestrator start |
| `evolution/aggregator.py` | 1134 | Split: data collection vs analysis vs report generation |
| `core/git_ops.py` | 934 | Split: basic git ops vs PR creation vs merge logic |
| `core/metrics.py` | 912 | Split: collection vs aggregation vs export |
| `core/agent_lifecycle.py` | 888 | Split: heartbeat vs crash detection vs orphan handling |
| `core/spawner.py` | 849 | Split: spawn logic vs MCP config vs worktree management |
| `core/router.py` | 832 | Split: routing rules vs provider health vs auto-routing |

## Design Principles

### Single Responsibility
Each module should do ONE thing. If you can't describe it in one sentence without "and", split it.

### Facade Pattern
Keep the original module as a thin facade that imports and re-exports from sub-modules. This preserves backward compatibility:
```python
# orchestrator.py (facade — stays as entry point)
from bernstein.core.tick_pipeline import group_by_role, fetch_all_tasks  # re-export
from bernstein.core.task_lifecycle import claim_and_spawn_batches  # re-export

class Orchestrator:
    def tick(self): ...  # delegates to sub-modules
```

### Max 600 lines per module
Target: no module exceeds 600 lines after decomposition. 800 is the hard limit.

### Naming Convention
Sub-modules named by domain:
- `context.py` → `context_builder.py`, `knowledge_base.py`, `file_discovery.py`
- `manager.py` → `planner.py`, `reviewer.py`, `decomposer.py`
- `bootstrap.py` → `preflight.py`, `server_launch.py`, `bootstrap.py` (thin orchestrator)

### Test Compatibility
All existing test imports must continue working via re-exports from facade modules. No test file should need changes for an internal restructure.

## Execution Plan

Split into 5 parallel sub-tasks (can be done by different agents):

### 329a: cli/main.py (2985→<600)
Move remaining inline commands to dedicated modules:
- `cli/task_cmd.py` — cancel, add-task, list-tasks, approve, reject, pending
- `cli/workspace_cmd.py` — workspace, config, plan
- `cli/advanced_cmd.py` — trace, replay, mcp, github, benchmark, eval, quarantine, completions
- `cli/main.py` — click group + imports only

### 329b: core modules >1000 lines
- `context.py` 1326 → `context_builder.py` + `knowledge_base.py`
- `manager.py` 1284 → `planner.py` + `reviewer.py`
- `task_lifecycle.py` 1254 → `task_claiming.py` + `task_completion.py`
- `bootstrap.py` 1139 → `preflight.py` + `bootstrap.py`

### 329c: evolution modules >1000 lines
- `evolution/loop.py` 1261 → `cycle_runner.py` + `proposal_scorer.py`
- `evolution/aggregator.py` 1134 → `data_collector.py` + `report_generator.py`

### 329d: core modules 800-1000 lines
- `git_ops.py` 934 → `git_basic.py` + `git_pr.py`
- `metrics.py` 912 → `metric_collector.py` + `metric_export.py`
- `agent_lifecycle.py` 888 → `heartbeat.py` + `crash_handler.py`
- `spawner.py` 849 → `spawn_core.py` + `spawn_config.py`
- `router.py` 832 → `routing_rules.py` + `provider_health.py`

### 329e: server + orchestrator (already partially done)
- `server.py` 1710 → extract TaskStore to `task_store.py`
- `orchestrator.py` 2217 → extract evolve integration to `evolve_orchestrator.py`

## Completion signal

- No Python file in src/bernstein/ exceeds 800 lines
- All existing tests pass without modification
- All imports from facade modules still work
- `uv run python scripts/run_tests.py -x` green
