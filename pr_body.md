## What

Wires the Orchestrator's file-lock conflict detection (`_check_file_overlap`) to the `LoopDetector` to populate the wait-for graph, enabling actual deadlock cycle-breaking.

## Why

Fixes #4673. Previously, `Orchestrator._check_file_overlap` detected lock conflicts but never passed them to `record_lock_wait()`. As a result, the deadlock detection graph remained empty, and deadlocks involving agent file-lock cycles were never broken, despite the detection loop running on every tick.

## How

- In `Orchestrator._check_file_overlap`, on deferment due to a lock conflict, called `LoopDetector.record_lock_wait()` passing `waiting_agent_id`, `wanted_files`, `held_by`, and `lock_timestamps`.
- Updated `task_lifecycle.py` to call `LoopDetector.clear_wait()` when a task batch is successfully claimed (indicating the agent is no longer waiting).
- Updated `docs/architecture/deadlock-detection.md` to reflect that the cycle-breaker is now wired end-to-end.
- Added unit tests in `test_orchestrator_tick_methods.py` and integration tests in `test_deadlock_detection.py` to prove that deadlocks are identified and resolved by releasing the oldest lock.

## Checklist

- [x] `uv run ruff check src/` passes
- [x] `uv run pyright src/` passes
- [x] `uv run python scripts/run_tests.py -x` passes
- [x] New code has type hints

### Documentation duty (every PR that touches a feature)

- [x] User-visible README section updated (or N/A if internal-only)
- [x] `docs/operations/<area>.md` updated (or N/A)
- [x] `docs/api/` schema regenerated if a public surface changed (or N/A)
- [x] `uv run bernstein agents-md sync` run so AGENTS.md, CLAUDE.md, `.goosehints`, `CONVENTIONS.md`, and `.cursor/rules/*.mdc` reflect any new module (or N/A)
- [x] Tests cover the documented behaviour
