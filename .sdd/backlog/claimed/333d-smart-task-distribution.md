# 333d — Smart Task Distribution (No Greedy Claiming)

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** none

## Problem

Currently all agents claim all available tasks greedily — the first agent to finish grabs the next task regardless of whether it's the right agent for the job. This causes:
1. Backend agents doing docs work (wrong specialization)
2. All agents pile onto the same role's tasks while other roles starve
3. No load balancing — 5 agents on backend, 0 on QA

## Design

### Role-locked claiming
Each agent is spawned for a specific role. It can ONLY claim tasks matching its role. The orchestrator enforces this:

```python
# Current (broken): agent grabs any open task
# New: orchestrator pre-assigns tasks to agents by role
```

### Fair distribution algorithm
Each tick, the orchestrator:
1. Count open tasks per role: `{backend: 5, qa: 3, docs: 2}`
2. Count active agents per role: `{backend: 2, qa: 0, docs: 0}`
3. Spawn new agents for starving roles first (QA and docs get agents before backend gets a 3rd)
4. Cap per-role: no more than `ceil(max_agents * role_tasks / total_tasks)` agents per role

### Rebalancing
Every N ticks, check if distribution is skewed:
- If a role has 0 agents but >0 tasks → spawn immediately
- If a role has more agents than tasks → don't spawn more
- If all tasks for a role are done → agent exits, slot freed for other roles

## Files to modify

- `src/bernstein/core/orchestrator.py` (fair distribution in tick)
- `src/bernstein/core/tick_pipeline.py` (group_by_role priority ordering)

## Completion signal

- No role starves while another has excess agents
- Agents only work on tasks matching their role
