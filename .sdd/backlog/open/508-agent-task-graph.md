# 508 — Agent-task dependency graph with graph theory optimizations

**Role:** architect
**Priority:** 2
**Scope:** medium
**Complexity:** high

## Problem
Tasks are assigned independently with no graph-level reasoning. The orchestrator groups by role but doesn't model task dependencies, agent capability overlap, or optimal parallelization. Graph theory can optimize: which tasks to parallelize, which to serialize, where bottlenecks form, and which agents are overloaded.

## Implementation

### 1. Task dependency graph
Build a DAG from task `depends_on` fields + inferred file-overlap edges:
- Nodes: tasks
- Edges: explicit deps (from ticket `depends_on`) + implicit deps (shared `owned_files`)
- Critical path analysis: longest chain determines minimum completion time
- Parallel width: max independent tasks at any point = optimal agent count

### 2. Agent capability graph
- Nodes: agent roles/catalog entries
- Edges: role overlap (backend + architect share system design skills)
- Use for: fallback routing (if no QA agent available, architect can review)

### 3. Orchestrator integration
- Before spawning, compute critical path and parallel width
- Adjust `max_agents` dynamically: don't spawn 6 agents if only 2 tasks are parallelizable
- Detect bottlenecks: if all remaining tasks depend on one in-progress task, alert user
- Visualize graph in dashboard (optional): show task dependencies as tree/DAG

### 4. Data structure
Use adjacency list in `.sdd/runtime/task_graph.json`:
```json
{
  "nodes": [{"id": "t1", "role": "backend", "status": "done"}, ...],
  "edges": [{"from": "t1", "to": "t2", "type": "depends_on"}, ...],
  "critical_path": ["t1", "t3", "t5"],
  "parallel_width": 3
}
```

## Files
- src/bernstein/core/graph.py (new) — TaskGraph, critical path, parallel width
- src/bernstein/core/orchestrator.py — integrate graph for spawn decisions
- tests/unit/test_graph.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_graph.py -x -q
- file_contains: src/bernstein/core/graph.py :: TaskGraph
