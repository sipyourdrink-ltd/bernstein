# Decision log

Audience: operators reconstructing *why* the router / profile / gate
picked what it picked, without re-reading four modules of routing code.

## Overview

Every routing / criterion-profile / gate-fire decision in Bernstein
writes one append-only JSONL record to `.sdd/runtime/decisions.jsonl`.

Source:

- `src/bernstein/core/observability/decision_log.py`
- `src/bernstein/cli/commands/decisions_cmd.py`

The log is append-only, concurrent-safe (in-process lock + append-mode
fd; cross-process safety from the kernel for the small line sizes
written here), and malformed-line tolerant on read.

## Record schema (`schema_version: 1`)

| Field | Use |
|-------|-----|
| `ts` | Float epoch seconds |
| `decision_id` | `dec-<uuid>` |
| `kind` | `model_route`, `mode_profile`, `criterion_profile`, `gate_fire` |
| `chosen` | Winner id |
| `alternatives` | `[{"id", "score", "reason"}]` (truncated at `MAX_ALTERNATIVES`) |
| `confidence` | `0.0 - 1.0` |
| `rationale` | Human-readable summary |
| `parent_decision_id` | Parent decision id (for chained routing) |
| `policy_path` | Ordered list of policies that fired |
| `winner_score` | Numeric score for the chosen alternative |
| `inputs` | Free-form `{"task_id": ..., ...}` |

## CLI

```text
bernstein decisions tail [-n N] [--path PATH]
bernstein decisions search [--kind KIND] [--since DURATION] [--path PATH]
```

- `--kind` filters to one decision kind (e.g. `model_route`).
- `--since` accepts `30s`, `15m`, `2h`, `1d`; it keeps records at or
  after the cutoff computed from the current time. When `--kind` and
  `--since` are both supplied, both filters apply, so results must match
  the kind and fall within the time window.
- `--path` overrides the default `.sdd/runtime/decisions.jsonl`.

`tail` defaults to the most recent 20 records of every kind. A positive
`-n N` limits the output to the last N records; a nonpositive value does
not limit it and therefore shows all readable records. Replay and both
commands preserve the log's write order; tail selects the end of that
ordered sequence without reversing it. Search filters likewise keep that
order.

Both commands render a Rich table with `ts`, `kind`, `chosen`,
`confidence`, and a trimmed rationale. Use redirection / shell tooling
on the raw JSONL when you need scriptable output.

## Examples

Show the last 50 decisions of any kind (not necessarily routing):

```bash
bernstein decisions tail -n 50
```

To see only routing decisions, use the kind filter:

```bash
bernstein decisions search --kind model_route
```

Look at every gate that fired in the last hour:

```bash
bernstein decisions search --kind gate_fire --since 1h
```

For example, if the log in write order contains `model_route(A)`,
`gate_fire(B)`, then `mode_profile(C)`, `tail -n 2` shows
`gate_fire(B)` and `mode_profile(C)`, while
`search --kind model_route` shows only `model_route(A)`. Tail is a
recency slice across all kinds; search is a kind filter, not a tail.

Pipe to `jq` for richer analysis:

```bash
jq -c 'select(.kind == "model_route")' .sdd/runtime/decisions.jsonl \
  | jq -s 'group_by(.chosen) | map({model: .[0].chosen, n: length})'
```

## Disabling

Set `BERNSTEIN_DECISION_LOG=0` in the orchestrator environment. The
single `record_decision` entry point becomes a no-op; routing paths
remain unchanged. The CLI subcommands keep working against any existing
log file.

## Bounded payloads

`alternatives` is truncated to `MAX_ALTERNATIVES` entries before
serialisation; a routing bug that explores 10,000 candidates cannot
fill the disk through this channel.

## Troubleshooting

**`bernstein decisions tail` is empty.** Either the log was disabled
via `BERNSTEIN_DECISION_LOG=0`, or no decisions have been recorded yet in
this workdir. Run a small task first, then re-tail.

**Concurrent writers from a multi-process run.** Cross-process append
safety relies on the kernel's atomicity guarantee for sub-`PIPE_BUF`
writes. If you are sharing a log across hosts (NFS), wrap the runs
with separate log paths and merge offline.

**Malformed line warnings.** A partial line (machine killed mid-write)
will be skipped by `iter_records` with a debug log. Trim the trailing
broken line by hand if it bothers your downstream tooling.
