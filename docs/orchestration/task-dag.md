# Task DAG with explicit parallel flag

The task DAG layer lets a planner hand the orchestrator an authored list
of tasks whose **parallel-safety is declarative**, not inferred from
file overlap. Each task carries:

| Marker | Meaning |
| --- | --- |
| `[T<id>]` | Stable task identifier (required). |
| `[P]` | Safe to run concurrently with other parallel-safe siblings. |
| `[US<n>]` | User-story slice used as a rollback grouping. |
| `-> T###, T###` | Inline dependency arrow at end of line. |

## File formats

### Markdown checkbox list

```
# MVP plan

- [ ] [T001] [P] [US1] Add YAML loader
- [ ] [T002] [P] [US1] Add markdown loader
- [ ] [T003]    [US1] Wire orchestrator -> T001, T002
- [ ] [T004] [P] [US2] Render parallel batches
```

### YAML

```yaml
tasks:
  - id: T001
    description: Add YAML loader
    parallel_safe: true
    story_id: US1
  - id: T002
    description: Wire orchestrator
    depends_on: [T001]
    story_id: US1
```

## Walking the DAG

`bernstein.core.orchestration.task_dag.topological_iter_with_parallel`
yields one `frozenset[TaskNode]` per batch:

- All-`[P]` ready tasks merge into a single concurrent batch.
- Any serial task in the ready set runs alone; remaining `[P]` siblings
  form a pure parallel batch on the next iteration.
- Cycles raise `TaskDagCycleError` with the unresolved task ids.

## CLI

Render a DAG plan for review:

```
bernstein plan dag --file specs/mvp-tasks.md
bernstein tasks dag --file specs/mvp-tasks.md  # alias
```

Output marks each batch as `SERIAL` or `PARALLEL` and lists user-story
rollback groups at the end.

## Scheduler consumption

`bernstein.core.orchestration.adaptive_parallelism.tasks_safe_to_run_in_parallel`
prefers the declarative `parallel_safe` flag when both tasks carry it
and falls back to the legacy file-overlap heuristic only for legacy
tasks that lack the attribute. Tasks generated through the new planner
path always set the flag, so the heuristic only runs for older entries.

## Rollback grouping

Tasks sharing a `story_id` form a rollback unit: the orchestrator
surfaces "story `<id>` complete" as a single milestone and supports a
story-scoped revert. Tasks without `story_id` remain independent.
