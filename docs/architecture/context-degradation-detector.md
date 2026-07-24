# Context degradation detector

Track cross-model review verdicts per agent session; when a session
accumulates consecutive rejections, checkpoint it, shut it down cleanly, and
hand the replacement agent a summary of what was already tried instead of
letting it grind on with a degraded context window.

## Why

A long-running agent session can drift: context fills with dead ends, retry
attempts, and partial fixes, and code quality drops even though the agent
keeps working. The context degradation detector uses [cross-model
verification](quality-pipeline.md#cross-model-verifier)
results as a proxy for quality - repeated `request_changes` verdicts from
the reviewer model signal a session that should be restarted with a clean
context rather than pushed further.

## How it works

`ContextDegradationDetector` (`src/bernstein/core/tokens/context_degradation_detector.py`)
keeps a chronological history of cross-model verdicts per session:

- `record_verdict(session_id, task_id, verdict)` appends a `CrossModelVerdict`
  to the session's history. If the trailing run of `request_changes`
  verdicts reaches `consecutive_reject_threshold`, the session is added to
  the internal degraded set.
- `should_restart(session_id, tokens_used)` returns `True` once a session is
  flagged, or once `tokens_used` crosses `max_tokens_before_restart` (if
  that ceiling is set above `0`).
- `degraded_sessions()` returns the current set of flagged session IDs.
- `build_recovery_context(session)` renders a markdown block summarising the
  review history (which tasks were approved/rejected and by which reviewer)
  plus a fixed list of guidance bullets (run tests and linter before
  completing, address all reviewer issues, write a failing test first when
  unsure, keep diffs focused) for the replacement agent's prompt.
- `checkpoint(session)` builds a `ContextDegradationCheckpoint` (session id,
  task ids, verdict count, consecutive-reject count, token usage, recovery
  context) and persists it as JSON under `.sdd/runtime/context_checkpoints/<session_id>.json`.
- `clear(session_id)` drops tracking state for a session once it has been
  handled.

`evict_degraded_sessions(orch)` (`src/bernstein/core/tasks/task_lifecycle.py`)
runs each orchestrator tick, right after deadlock/loop detection. For every
session the detector has flagged: it checkpoints the session, stashes the
recovery-context markdown on `orch._context_recovery` keyed by every task ID
the session owned, writes a `SHUTDOWN` signal via the signal manager so the
agent exits at its next heartbeat, and clears the detector's tracking state.
The recovery context is picked up when the replacement agent's prompt is
built for those task IDs.

`record_verdict()` itself is called from `_run_cross_model_check()`
(`task_lifecycle.py`) immediately after every cross-model review completes,
so the detector only ever sees data when cross-model verification is
enabled and actually runs.

## Configuration

`ContextDegradationConfig` (frozen dataclass):

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `False` | Master on/off switch. Disabled by default. |
| `consecutive_reject_threshold` | `2` | Consecutive `request_changes` verdicts that flag a session. |
| `min_tasks_before_detection` | `1` | Minimum recorded verdicts before the threshold check runs, so one noisy review can't trigger a restart by itself. |
| `max_tokens_before_restart` | `0` | Restart once cumulative session tokens reach this value, independent of verdict history. `0` disables the token ceiling. |
| `checkpoint_dir` | `.sdd/runtime/context_checkpoints` | Where checkpoint JSON files are written, relative to the project workdir. |

Set it by constructing `OrchestratorConfig(context_degradation=ContextDegradationConfig(...))`
in Python. There is no `bernstein.yaml` key or CLI flag for this setting
today - it is a programmatic `OrchestratorConfig` field, so enabling it
requires embedding or scripting the orchestrator rather than a config file
edit.

## Limitation

The detector is inert unless [cross-model verification](quality-pipeline.md#cross-model-verifier)
is also enabled - `record_verdict()` is only ever called from the
cross-model review path, so a run with cross-model verification off never
feeds this detector any data, regardless of the `context_degradation.enabled`
setting. The `max_tokens_before_restart` ceiling is the only trigger that
does not depend on cross-model verdicts, but it still only takes effect for
a session that already has at least one recorded verdict (`should_restart`
checks `session_id in self._history`).

## Source

- `src/bernstein/core/tokens/context_degradation_detector.py` -
  `ContextDegradationConfig`, `ContextDegradationDetector`,
  `ContextDegradationCheckpoint`.
- `src/bernstein/core/tasks/task_lifecycle.py` - `_run_cross_model_check()`
  (feeds verdicts in) and `evict_degraded_sessions()` (tick-loop eviction).
- `src/bernstein/core/orchestration/orchestrator.py` - constructs the
  detector from `OrchestratorConfig.context_degradation` and calls
  `evict_degraded_sessions()` every tick.
- `src/bernstein/core/tasks/models.py` - `OrchestratorConfig.context_degradation`
  field definition.
