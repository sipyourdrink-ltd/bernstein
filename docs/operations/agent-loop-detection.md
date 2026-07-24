# Agent edit-loop detection

An agent is "looping" when it edits the same file more than a few times in a
short window - typically a fix-verify-fail cycle where the agent keeps
re-editing without making progress. Bernstein tracks per-agent, per-file
edit counts each tick and kills any agent that crosses the threshold, so the
task can be retried or escalated instead of burning tokens indefinitely.

Not to be confused with [Agent crash loops](agent_crash_loop.md), which
covers a session repeatedly failing to *spawn* (a respawn budget with
backoff and a parked state) - a different failure mode in a different
module.

Source: `src/bernstein/core/observability/loop_detector.py`
(`LoopDetector.detect_loops`), applied by
`src/bernstein/core/agents/agent_lifecycle.py`
(`check_loops_and_deadlocks` → `_recover_loops`), called each tick from
`core/orchestration/orchestrator.py`.

## How it works

1. Each tick, `_poll_file_mtimes` checks the modification time of every file
   currently locked by an active agent. When a file's mtime has advanced
   since the last poll, the edit is recorded against that agent
   (`detector.record_edit(agent_id, file_path, mtime)`).
2. `detect_loops()` prunes edit records older than the detection window and
   counts edits per `(agent_id, file_path)` pair.
3. Any pair whose count exceeds the threshold is reported as a
   `LoopDetection` (agent id, file path, edit count, window).
4. `_recover_loops` kills the offending agent, propagates the abort to any
   child agents, clears its lock-wait state, and releases its file locks.

```
Loop detected: agent <id> edited '<path>' <N> times in <window>s - killing agent
```

## Thresholds

| Constant | Default | Meaning |
|---|---|---|
| `LOOP_EDIT_THRESHOLD` | 3 | more than 3 edits (i.e. 4+) to the same file within the window trips detection |
| `LOOP_WINDOW_SECONDS` | 300.0 | sliding window (5 minutes) edits are counted over |

Both are module-level constants in `loop_detector.py`; they are not exposed
through `bernstein.yaml`. Callers can pass different values directly to
`detect_loops(threshold=..., window_seconds=...)`, but the orchestrator's
built-in call uses the defaults.

## What is not covered here

The same module (`LoopDetector`) also implements **deadlock detection** -
building a wait-for graph from active file locks and pending lock waits, and
resolving cycles by releasing the oldest lock holder's lock. That is a
separate row in the feature matrix and is out of scope for this page.

## Limitations

- Detection is mtime-based polling, not an edit-intent signal - a legitimate
  agent that touches the same file many times in quick succession (e.g.
  applying several small, correct fixes) can trip the same threshold as a
  genuine fix-verify-fail cycle.
- No per-project override surface today; changing the threshold or window
  requires calling `detect_loops()` with explicit arguments from custom
  orchestration code, not a config file.
