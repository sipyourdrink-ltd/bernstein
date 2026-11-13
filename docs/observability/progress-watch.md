# ProgressWatch - agent liveness via session-log growth

ProgressWatch is a small observability primitive that decides whether a
spawned CLI agent is "stuck or just thinking" without parsing the
agent's own stdout. It watches a structured session log on disk: if the
log file's mtime or size moves forward, the session log is active. Growth
indicates activity, but does not establish that work was completed. If it
has not moved for `agents.progress_watch.inactivity_seconds`, the
watcher records a stall and asks the dispatch loop to escalate.

This is an activity signal, not a completion percentage. For run and task
completion estimates and how they differ from other observability signals, see
[Cumulative progress tracking](cumulative-progress.md).

The watcher does not spawn its own thread and does not kill processes
directly. The dispatch loop tick (every 30s by default) calls
`ProgressWatch.tick()` and inspects the returned `StallEvent` list.
When a session has been idle past
`agents.progress_watch.kill_after_inactivity_seconds`,
`kill_if_stale(session_id)` returns a `sigkill` verdict that the loop
uses to decide what signal to send.

## Public surface

| Symbol | Purpose |
| --- | --- |
| `ProgressWatch.register(session_id, log_path, adapter=...)` | Begin watching a log file for a session. |
| `ProgressWatch.unregister(session_id)` | Stop watching. Idempotent. |
| `ProgressWatch.tick()` | Sample every registered log. Returns newly-detected stalls. |
| `ProgressWatch.drain_pending_events()` | Read and clear the buffered stall events. |
| `ProgressWatch.kill_if_stale(session_id)` | Return the kill verdict for one session (`none`/`sigterm`/`sigkill`). |
| `CLIAdapter.supports_session_log_watch` | Class attribute; `True` when the adapter exposes a structured log path. |
| `CLIAdapter.session_log_path_for(session_id)` | Return the absolute log path for the given Bernstein session, or `None`. |

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `agents.progress_watch.enabled` | `true` | Master switch for the watcher. |
| `agents.progress_watch.inactivity_seconds` | `120` | Idle gap before a stall is emitted. |
| `agents.progress_watch.kill_after_inactivity_seconds` | `300` | Idle gap before the watcher escalates to SIGKILL. |

These defaults are also exposed as constants on
`bernstein.core.observability.progress_watch` for callers that want to
build a watcher without parsing config:

* `DEFAULT_INACTIVITY_SECONDS`
* `DEFAULT_KILL_AFTER_INACTIVITY_SECONDS`
* `DEFAULT_POLL_INTERVAL_SECONDS`

## Lifecycle event

When a stall is detected, the dispatch loop forwards the
`StallEvent` onto the lifecycle hook bus:

```text
event:   agent.progress_stalled
data:
  adapter:             "<adapter name>"
  log_path:            "<absolute path>"
  last_log_growth_ts:  <unix ts>
  detected_ts:         <unix ts>
```

The event name is also available as
`LifecycleEvent.AGENT_PROGRESS_STALLED`.

## Per-adapter log paths

The watcher itself is adapter-agnostic. Adapters opt in by setting
`supports_session_log_watch = True` and overriding
`session_log_path_for(session_id)`. The default returns `None`, which
makes the dispatch loop skip the adapter and fall back to plain
process-exit detection.

| Adapter | `supports_session_log_watch` | Log location |
| --- | --- | --- |
| `claude` | yes | `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` (latest match) |
| `codex` | no (planned) | `~/.codex/sessions/<id>.log` (per upstream CLI) |
| `cursor` | no (planned) | `~/.cursor/agents/<id>.jsonl` (per upstream CLI) |
| `aider` | no (planned) | `<workdir>/.aider.chat.history.md` |
| `gemini` | no (planned) | `~/.gemini/sessions/<id>.log` |
| `opencode` | no (planned) | `~/.opencode/sessions/<id>.jsonl` |

Adapters not in this table use the inherited default and rely on
process-exit detection. New adapters can opt in at any time by adding
the override; no changes to the watcher itself are required.

## Operational notes

* The watcher treats a missing log file as "no growth", not as an
  error. Adapters may register a log path before the CLI creates the
  file; the first observed mtime/size move counts as the first growth.
* Stall events are sticky: once a session has been flagged as stalled,
  the watcher does not re-emit until the log grows again, at which
  point the sticky state clears and the cycle starts over.
* Auto-restart of a killed agent is out of scope for this primitive
  (see the retry-with-continuation work).
