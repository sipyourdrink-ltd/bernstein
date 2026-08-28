# Deadlock detection

Detect cycles in the agent file-lock wait-for graph and break them by
releasing the lock held longest, so two agents blocked on each other's
files don't stall forever.

## Why

Bernstein agents claim exclusive file locks (`FileLockManager`) before
writing to a path. If agent A holds `src/foo.py` and needs `src/bar.py`,
while agent B holds `src/bar.py` and needs `src/foo.py`, neither can
proceed - a classic two-agent deadlock. Deadlock detection builds a
wait-for graph each tick, finds cycles, and releases the lock held by the
oldest agent in the cycle so the other agent can make progress.

## How it works

`LoopDetector` (`src/bernstein/core/observability/loop_detector.py`) tracks
two independent things in the same class: file-edit loops (a separate
feature - an agent editing the same file too many times in a window - not
covered on this page) and file-lock waits. For deadlocks specifically:

- `record_lock_wait(waiting_agent_id, wanted_files, held_by, lock_timestamps=None)`
  records that an agent is blocked on files held by other agents, and
  remembers when each blocking lock was acquired.
- `detect_deadlocks(lock_mgr)` builds a directed wait-for graph
  (`waiting_agent → holder_agent`) from the recorded waits plus the live
  state of `FileLockManager.all_locks()`, then runs an iterative DFS
  (`_find_cycles`) to find every simple cycle.
- For each cycle found, `_oldest_lock_holder()` picks the agent holding the
  lock with the smallest `locked_at` timestamp as the **victim** - the
  rationale being that releasing the longest-held lock unblocks the agent
  that has been waiting relatively more of the deadlock's lifetime.
- `clear_wait(agent_id)` removes an agent's wait-for entries; callers are
  expected to call it when an agent acquires its locks, exits, or is
  killed, so stale entries don't produce phantom deadlock reports.

`check_loops_and_deadlocks(orch)` (`src/bernstein/core/agents/agent_recycling.py`,
also duplicated in `agent_lifecycle.py`) runs both halves every orchestrator
tick. For deadlocks it calls `detector.detect_deadlocks(lock_mgr)`, and for
every `DeadlockDetection` returned, releases the victim's lock
(`lock_mgr.release(victim_agent_id)`) and clears its wait-for state. This is
wired into the main tick loop in `orchestrator.py` (step "2d. Detect loops
and deadlocks", right before idle-agent recycling).

## The Wait-For Graph

`detect_deadlocks()` builds its cycle-finding graph using waits registered by `record_lock_wait()`. In the orchestrator, `Orchestrator._check_file_overlap` sees every file lock conflict when considering task batches. It defers batches when files are already locked, and records this wait in the `LoopDetector`. The wait is cleared when the batch is successfully claimed and spawned by an agent (in `task_lifecycle.py`), and when recovery logic releases an agent’s locks (for example, deadlock victim selection in `check_loops_and_deadlocks()`). This end-to-end wiring ensures that the wait-for graph stays current.

## Source

- `src/bernstein/core/observability/loop_detector.py` - `LoopDetector`,
  `DeadlockDetection`, cycle search, and victim selection.
- `src/bernstein/core/agents/agent_recycling.py` and
  `src/bernstein/core/agents/agent_lifecycle.py` - `check_loops_and_deadlocks()`,
  the tick-loop entry point (the orchestrator imports the copy in
  `agent_recycling.py`).
- `src/bernstein/core/orchestration/orchestrator.py` - tick-loop wiring and
  `_check_file_overlap` (`check_conflicts()` call site).
- `src/bernstein/core/persistence/file_locks.py` - `FileLockManager`,
  `FileLock`, `all_locks()`, `check_conflicts()`.
