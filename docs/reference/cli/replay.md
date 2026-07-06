# Replay & Replay-Filter

`bernstein replay` re-displays the events from a past orchestration run so you can debug, diff, and reproduce. There are two replay surfaces:

1. **`bernstein replay`** *(source: `cli/commands/advanced_cmd.py:876`)* - the original replay, optionally with task-trace re-submission.
2. **`bernstein replay-filter`** (registered as `replay`'s filter wrapper) *(source: `cli/commands/replay_filter_cmd.py:164`)* - adds `--filter`, `--event-type`, `--agent`, `--search`.

Both read from the same underlying log on disk; the filter command is a strict superset of the basic command for read-only inspection. The basic command additionally supports **task-trace replay**, which re-creates a new task from a stored task trace and (optionally) compares the replay's `result_summary` against the original via a colour diff.

## What replay does (and doesn't do)

Replay is **deterministic re-display** of a past run's recorded events. Every event the orchestrator emitted - `run_started`, `agent_spawned`, `task_claimed`, `task_completed`, `task_verification_failed`, `agent_reaped`, `run_completed` - is replayed in order with its original timing offsets.

What replay **does not** do:

- It does not re-execute external HTTP calls. Any HTTP traffic the original agents performed (LLM API calls, GitHub API writes, webhook deliveries) is captured in the log but not re-issued.
- State mutations to remote services (a PR opened, a Slack message sent, a row inserted into your database) are **not** rolled back or repeated.
- It does not re-create branches or worktrees. The git state is whatever your repo currently is.

For full re-execution of the **same task** with a (potentially different) model, use the task-trace mode: `bernstein replay <task_id> --model opus`. This re-submits the original task description (plus any `--extra-context` you provide) as a new task on the running server and waits for it to finish, then renders a diff between the original and the new `result_summary`. (`cli/commands/advanced_cmd.py:841-873`.)

## Where replay state lives

```
.sdd/
  runs/
    <run_id>/
      journal.jsonl          # canonical Merkle-chained event journal (one JSON event per line)
      metadata.json          # session metadata (started_at, git_branch, git_sha, config_hash)
      divergence_report.json # written by `replay --verify` when a step diverges
  traces/
    <task_id>-<timestamp>.json  # per-task traces (used by task-trace replay)
```

- The canonical run-event journal is `journal.jsonl`, written by the always-on `EventJournal` (`core/replay/journal.py`). Each event chains as `H(prev_hash, event_type, payload_hash, monotonic_index)` and the head hash is the run identity.
- Recording is on by default; `BERNSTEIN_REPLAY_RETENTION=N` bounds how many past run journals survive on disk (oldest run directories are pruned) instead of an on/off gate.
- At run finalization the journal head is sealed into the run's lineage spine, so the replay identity and artifact provenance share one root.
- Session metadata is parsed by `read_session_replay_metadata()` from `core/runtime_state.py`.
- Task traces are loaded by `core.traces.TraceStore` (`cli/commands/replay_filter_cmd.py:108-117`).

## Verifying and rebuilding from the journal

- `bernstein replay <RUN_ID> --verify` recomputes the journal's Merkle chain and reports byte-identity, or the exact first divergent step index. On divergence it writes `divergence_report.json` (`step_index`, `expected_hash`, `actual_hash`) and exits non-zero. An injected non-deterministic tool result surfaces as a precise hash mismatch rather than a silent drift.
- `bernstein replay <RUN_ID> --from-step N` rebuilds a deterministic state projection by walking events `[0, N)`. Two independent invocations produce byte-identical output, so the reconstruction is reproducible.

The fingerprint shown after a replay is the Merkle head over the journal's event chain; identical decision streams produce identical heads, which is how you verify two runs really are the same.

---

## `bernstein replay`

**Synopsis:** `bernstein replay RUN_ID_OR_TASK_ID [flags]`

**Flags:** *(source: `cli/commands/advanced_cmd.py:876-906`)*

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID_OR_TASK_ID` | required | Run ID, the literal `latest`, the literal `list`, or a task ID. |
| `--sdd-dir PATH` | `.sdd` | Path to the `.sdd` state directory. |
| `--as-json` | off | Emit raw JSONL (one event per line) instead of the Rich table. |
| `--limit N` | none | Show only the first N events. |
| `--model NAME` | none | Override model for **task-trace replay** (e.g. `opus`, `sonnet`, `o3`). |
| `--extra-context TEXT` | none | Append extra hint text to the replayed task description. |

**Resolution rules:**

- `bernstein replay list` - print every recorded run with timing, branch, SHA, event count, log size.
- `bernstein replay latest` - replay the most recent run.
- `bernstein replay <run_id>` - replay a specific run by directory name.
- `bernstein replay <task_id>` (no run with that ID exists) - falls through to **task-trace replay**: re-submit the task and diff result summaries.

The Rich table columns are `TIME` (offset from `run_started`), `EVENT`, `AGENT`, `TASK`, `DETAIL`. Common detail keys: `model`, `role`, `cost_usd`, `fingerprint`, `tick`, `failed_signals`. Events are colour-coded by type (`run_started` / `task_completed` are green; `agent_reaped` and `task_verification_failed` are red).

```bash
# What ran most recently?
bernstein replay latest

# Specific run, machine-readable
bernstein replay 20260415-143022 --as-json | jq '.events[] | select(.event=="task_completed")'

# Re-execute task T-abc123 on Opus instead of whatever it ran on originally
bernstein replay T-abc123 --model opus --extra-context "Make sure tests pass on Python 3.11."
```

---

## `bernstein replay-filter`

A strict superset of `bernstein replay` for read-only inspection. Adds four orthogonal filters that compose with each other.

**Synopsis:** `bernstein replay-filter RUN_ID [flags]`

**Flags:** *(source: `cli/commands/replay_filter_cmd.py:164-186`)*

| Flag | Default | Meaning |
|---|---|---|
| `RUN_ID` | required | Same as `replay`: run ID, `latest`, `list`, or task ID. |
| `--sdd-dir PATH` | `.sdd` | Path to the `.sdd` state directory. |
| `--as-json` | off | Emit raw JSONL. |
| `--limit N` | none | Show only the first N **filtered** events. |
| `--filter "k=v,..."` | none | Comma-separated key=value filters; values are regex (case-insensitive). |
| `--event-type TYPE` | none | Show only events of a specific type. |
| `--agent ID` | none | Show only events from this agent (substring match). |
| `--search TEXT` | none | Full-text substring search across all event fields. |
| `--model NAME` | none | Override model for task-trace replay. |
| `--extra-context TEXT` | none | Append text to a replayed task description. |

The four filters compose with AND semantics: an event must match every active filter.

**Filter expression syntax (`--filter`):** comma-separated `key=value` pairs. Values are regular expressions, applied case-insensitively against the event field's stringified value. So `--filter "role=backend,status=done"` keeps events whose `role` field matches `backend` AND whose `status` field matches `done`.

```bash
# Just the agent_spawned events from the latest run
bernstein replay latest --event-type agent_spawned

# Anything mentioning "fail" in the most recent run, as JSON
bernstein replay latest --search fail --as-json | jq '.events[]'

# Backend tasks that completed
bernstein replay latest --filter "role=backend,status=done" --limit 10

# All events from a specific agent session, with full JSON output
bernstein replay 20260415-143022 --agent backend-abc --as-json
```

When `--as-json` is set, the filtered output schema is:

```json
{
  "run_id": "<id>",
  "events": [ /* filtered events, capped to --limit */ ],
  "total_matched": 42
}
```

---

## Common use cases

**Reproduce a flaky failure.** Run `bernstein replay-filter latest --event-type task_verification_failed` to see exactly which gate failed and on which agent. The detail column carries the failed signal names; cross-reference with `.sdd/traces/` for the agent's full transcript.

**Compare models on the same task.** Find the run where the task succeeded:

```bash
bernstein replay-filter latest --search "T-abc123" --event-type task_completed
```

then re-run with a different model:

```bash
bernstein replay T-abc123 --model sonnet --extra-context "Use Pydantic v2"
```

The CLI prints a diff of the two `result_summary` strings.

**Verify a fix.** After fixing a bug, run the failing task again with `bernstein replay <task_id>` and compare. If the fingerprint changes only in the expected places, you have evidence the fix held.

---

## Limits

- Replay does not re-issue HTTP calls. Mocking a remote dependency from the original log is not supported - agents in task-trace replay make **fresh** calls.
- Side effects to remote services (PRs, messages, webhooks, DB rows) from the original run are **not undone** by replay. There is no "rewind" mode.
- Run-event replay only re-renders what was recorded. If `recorder.py` (`cli/commands/replay_filter_cmd.py:232`) didn't capture an event class, it will not appear.
- Fingerprints depend on the exact recorder version. A replay log written by an older Bernstein may produce a different fingerprint when re-fingerprinted by a newer build.
- Task-trace replay submits a **new** task - it does not retroactively re-run the original task in place. The original task's record stays in the archive untouched.

For deeper integrity guarantees, see [`fingerprint`](../cli-reference.md#bernstein-fingerprint) (re-computes the run's SHA-256 and verifies it against a stored reference).
