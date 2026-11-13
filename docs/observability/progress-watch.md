# ProgressWatch - agent liveness via session-log growth

ProgressWatch is a small, caller-driven observability building block that can
help a caller decide whether a spawned CLI agent is "stuck or just thinking"
without parsing the agent's own stdout. It watches a structured session log on
disk: if the log file's mtime or size moves forward, the log is active. Growth
indicates activity, but does not establish that work was completed. If it has
not moved for the inactivity threshold, the watcher records a stall event.

ProgressWatch does not start itself or connect itself to the default dispatch
runtime. A caller must create it, register sessions, call `tick()` on its own
cadence, and decide what to do with returned events and verdicts. The default
runtime currently has no ProgressWatch caller: it does not register logs, tick
the watcher, forward stall events, or act on kill verdicts.

This is an activity signal, not a completion percentage. For run and task
completion estimates and how they differ from other observability signals, see
[Cumulative progress tracking](cumulative-progress.md).

The watcher does not spawn its own thread and does not kill processes
directly. `tick()` returns newly-detected `StallEvent` objects and also buffers
them for `drain_pending_events()`; these are data for the caller, not actions
or automatic lifecycle-hook emissions. `kill_if_stale(session_id)` returns a
`KillVerdict` (`none`, `sigterm`, or `sigkill`) but sends no signal. A caller
that owns the process and lifecycle hook bus would need to choose and perform
any signal or event forwarding itself.

## Public surface

| Symbol | Purpose |
| --- | --- |
| `ProgressWatch.register(session_id, log_path, adapter=...)` | Begin watching a log file for a session. |
| `ProgressWatch.unregister(session_id)` | Stop watching. Idempotent. |
| `ProgressWatch.tick()` | Sample every registered log. Returns newly-detected stalls. |
| `ProgressWatch.drain_pending_events()` | Read and clear the buffered stall events. |
| `ProgressWatch.kill_if_stale(session_id)` | Return the kill verdict for one session (`none`/`sigterm`/`sigkill`). |
| `CLIAdapter.supports_session_log_watch` | Class capability flag; `True` when the adapter can expose a structured log path (not a runtime connection). |
| `CLIAdapter.session_log_path_for(session_id)` | Return the absolute log path for the given Bernstein session, or `None`. |

## Defaults and timing

The watcher has constructor defaults, not working operator settings. The
`agents.progress_watch.*` names are not read from runtime configuration, and
there is no enabled switch connected to the default runtime. Callers can
provide per-instance `inactivity_seconds` and
`kill_after_inactivity_seconds` arguments; their defaults are `120` seconds
before a stall event and `300` seconds before a `sigkill` verdict. These are
also exposed as `DEFAULT_INACTIVITY_SECONDS` and
`DEFAULT_KILL_AFTER_INACTIVITY_SECONDS` constants.

`DEFAULT_POLL_INTERVAL_SECONDS` is `30` seconds, a suggested caller cadence;
it does not configure an active default dispatch loop. Event timing uses
`time.time()` (wall-clock Unix timestamps) by default, and callers can inject
a clock. The fake-clock tests inject a manually advanced clock to check idle
thresholds and event timestamps deterministically.

## Lifecycle event

`LifecycleEvent.AGENT_PROGRESS_STALLED` names the event a caller may choose to
forward. The watcher only returns and buffers `StallEvent` data; it does not
publish this lifecycle event, and the default runtime does not forward it.
A caller that integrates with the lifecycle hook bus can forward a payload
with this shape:

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

The watcher itself is adapter-agnostic. An adapter can set
`supports_session_log_watch = True` and override
`session_log_path_for(session_id)` to provide a path to a caller. The inherited
value is `False` and the inherited path method returns `None`; these adapter
capabilities do not connect ProgressWatch to the default dispatch runtime.

| Adapter | `supports_session_log_watch` | Log location |
| --- | --- | --- |
| `claude` | yes | `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` (latest match) |
| `codex` | no (planned) | `~/.codex/sessions/<id>.log` (per upstream CLI) |
| `cursor` | no (planned) | `~/.cursor/agents/<id>.jsonl` (per upstream CLI) |
| `aider` | no (planned) | `<workdir>/.aider.chat.history.md` |
| `gemini` | no (planned) | `~/.gemini/sessions/<id>.log` |
| `opencode` | no (planned) | `~/.opencode/sessions/<id>.jsonl` |

Adapters not in this table use the inherited default and do not provide a
session-log path. This table describes adapter capabilities only; it does not
imply that the default dispatch runtime registers or watches any of them.
New adapters can provide the override without changes to the watcher itself.

## Operational notes

* The watcher treats a missing log file as "no growth", not as an
  error. Adapters may register a log path before the CLI creates the
  file; the first observed mtime/size move counts as the first growth.
* Stall events are sticky: once a session has been flagged as stalled,
  the watcher does not re-emit until the log grows again, at which
  point the sticky state clears and the cycle starts over.
* Auto-restart of a killed agent is out of scope for this primitive
  (see the retry-with-continuation work).
