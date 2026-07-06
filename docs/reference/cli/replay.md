# Replay

`bernstein replay` re-displays the events from a past orchestration run so you can debug, diff, and reproduce. It has a single command surface with several subcommands:

- **`bernstein replay <RUN_ID>`** - the original replay, optionally with task-trace re-submission.
- **`bernstein replay diff RUN_A RUN_B`** - localise the first divergence between two recorded runs.
- **`bernstein replay export <AGENT_ID> -o RECEIPT`** - write a portable per-step receipt.
- **`bernstein replay publish <AGENT_ID> -o RECEIPT`** - write a redacted receipt for publishing.
- **`bernstein replay verify <RECEIPT>`** - offline verifier for an exported receipt.
- **`bernstein replay diff-journal A B`** - per-step divergence finder across two journals.

All subcommands read from the same underlying journal on disk. The base command additionally supports **task-trace replay**, which re-creates a new task from a stored task trace and (optionally) compares the replay's `result_summary` against the original via a colour diff.

## What replay does (and doesn't do)

Replay is **deterministic re-display** of a past run's recorded events. Every event the orchestrator emitted - `run_started`, `agent_spawned`, `task_claimed`, `task_completed`, `task_verification_failed`, `agent_reaped`, `run_completed` - is replayed in order with its original timing offsets.

What replay **does not** do:

- It does not re-execute external HTTP calls. Any HTTP traffic the original agents performed (LLM API calls, GitHub API writes, webhook deliveries) is captured in the log but not re-issued.
- State mutations to remote services (a PR opened, a Slack message sent, a row inserted into your database) are **not** rolled back or repeated.
- It does not re-create branches or worktrees. The git state is whatever your repo currently is.

For full re-execution of the **same task** with a (potentially different) model, use the task-trace mode: `bernstein replay <task_id> --model opus`. This re-submits the original task description (plus any `--extra-context` you provide) as a new task on the running server and waits for it to finish, then renders a diff between the original and the new `result_summary`. (`cli/commands/advanced_cmd.py`.)

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
- Task traces are loaded by `TraceStore` (`core/observability/traces.py`).

## Verifying and rebuilding from the journal

- `bernstein replay <RUN_ID> --verify` recomputes the journal's Merkle chain and reports byte-identity, or the exact first divergent step index. On divergence it writes `divergence_report.json` (`step_index`, `expected_hash`, `actual_hash`) and exits non-zero. An injected non-deterministic tool result surfaces as a precise hash mismatch rather than a silent drift.
- `bernstein replay <RUN_ID> --from-step N` rebuilds a deterministic state projection by walking events `[0, N)`. Two independent invocations produce byte-identical output, so the reconstruction is reproducible.

The fingerprint shown after a replay is the Merkle head over the journal's event chain; identical decision streams produce identical heads, which is how you verify two runs really are the same.

---

## `bernstein replay`

**Synopsis:** `bernstein replay RUN_ID_OR_TASK_ID [flags]`

**Flags:** *(source: `cli/commands/advanced_cmd.py`)*

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

## Subcommands

Beyond the base run/task replay, `bernstein replay` exposes subcommands for diffing runs and for exporting and verifying portable receipts.

### `bernstein replay diff RUN_A RUN_B`

Localises the first divergence between two recorded runs. Walks both event chains in lockstep and reports the first step whose hash differs, so a non-deterministic drift between two runs surfaces as a precise step index rather than a wall of diff.

```bash
bernstein replay diff 20260415-143022 20260415-150118
```

### `bernstein replay diff-journal A B`

Per-step divergence finder across two journals. Like `diff` but operates directly on two journal paths, reporting the first step index where the chains diverge.

### `bernstein replay export <AGENT_ID> -o RECEIPT`

Writes a portable per-step receipt for an agent's journal to the path given by `-o`. The receipt carries the step chain and its head hash so it can be verified offline by another party.

```bash
bernstein replay export backend-abc -o receipt.json
```

### `bernstein replay publish <AGENT_ID> -o RECEIPT`

Same as `export`, but produces a redacted receipt suitable for publishing. Sensitive fields are stripped while the head hash still anchors the visible steps.

```bash
bernstein replay publish backend-abc -o receipt.public.json
```

### `bernstein replay verify <RECEIPT>`

Offline verifier for an exported or published receipt. Recomputes the receipt's chain and reports byte-identity or the first divergent step. Exits non-zero on mismatch.

```bash
bernstein replay verify receipt.json
```

---

## Common use cases

**Reproduce a flaky failure.** Run `bernstein replay latest` and read the `EVENT` column for `task_verification_failed` rows to see exactly which gate failed and on which agent. The detail column carries the failed signal names; cross-reference with `.sdd/traces/` for the agent's full transcript. For a machine-readable pass, use `bernstein replay latest --as-json | jq '.events[] | select(.event=="task_verification_failed")'`.

**Compare models on the same task.** Find the run where the task succeeded:

```bash
bernstein replay latest --as-json | jq '.events[] | select(.event=="task_completed")'
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
- Run-event replay only re-renders what was recorded. If the `EventJournal` (`core/replay/journal.py`) did not capture an event class, it will not appear.
- Fingerprints depend on the exact recorder version. A replay log written by an older Bernstein may produce a different fingerprint when re-fingerprinted by a newer build.
- Task-trace replay submits a **new** task - it does not retroactively re-run the original task in place. The original task's record stays in the archive untouched.

For deeper integrity guarantees, see [`fingerprint`](../cli-reference.md#bernstein-fingerprint) (re-computes the run's SHA-256 and verifies it against a stored reference).
