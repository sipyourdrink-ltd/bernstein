# Lineage v2 (two-layer storage)

Audience: compliance / SRE operators who need detached child bodies for
large lineage payloads and a join-pointer (`child_sha`) that survives
parent-timeline truncation.

## Overview

Lineage v2 stores lineage in two HMAC-chained layers:

```
.sdd/lineage/v2/
  parent.jsonl                 # task_id, child_run_id, parent_call_id,
                               # summary, child_sha, hmac
  children/<sha>.jsonl         # full ChildBody payloads, hmac-chained per file
```

`child_sha` is the sha256 of the canonical first body line and acts as
the content-addressed join pointer. Append order is: write+fsync the
child file, then append the parent ref under `flock(LOCK_EX)`.

Source:

- `src/bernstein/core/lineage/v2_store.py` - `LineageV2Store`
- the `lineage v2` group in `src/bernstein/cli/commands/lineage_cmd.py`

v1 remains the default. v2 is **opt-in**.

## Enabling v2

Either:

- `BERNSTEIN_LINEAGE_V2=1` in the environment, or
- `bernstein.yaml` with `lineage.version: 2`.

When v2 is active the writer dual-writes through the v2 store; v1
readers keep working until the operator migrates out.

## CLI

```text
bernstein lineage v2 show TASK_ID        [--root PATH] [--output-json]
bernstein lineage v2 verify              [--root PATH] [--output-json]
bernstein lineage v2 export TASK_ID      [--format jsonl|sigstore]
                                         [--root PATH] [--output FILE]
```

`--root` defaults to `.sdd/lineage/v2/`.

### show

Reconstructs and prints the full timeline for `TASK_ID`. Use
`--output-json` for scripting; the table form prints run id, parent
call id, summary, body count, and the first 24 chars of `child_sha`.

### verify

Validates the HMAC chains across **both** layers. Exits `1` on
failure; the failure list names the offending file plus the broken
record.

### export

Emits the timeline for `TASK_ID` as:

- `--format jsonl` (default) - dump of parent refs + bodies.
- `--format sigstore` - SLSA v0.3 in-toto Statement per child, suitable
  for sigstore attestations.

## Layout

| File | Contents |
|------|---------|
| `parent.jsonl` | One line per parent ref. Fields: `task_id`, `child_run_id`, `parent_call_id`, `summary`, `child_sha`, `hmac`. |
| `children/<sha>.jsonl` | Full `ChildBody` payloads for the run named by `child_sha`. Each file is hmac-chained independently. |

Properties enforced by the store (and proved by 22 Hypothesis tests):

- HMAC unforgeability across both layers
- Replay determinism (same store + same task -> identical timeline)
- Append commutativity per task across concurrent writers
- Verify-after-truncate detects every truncation
- Orphan detection: a parent ref pointing to a missing child fails verify

## Examples

Force v2 for a single run and verify on completion:

```bash
BERNSTEIN_LINEAGE_V2=1 bernstein run plan.yaml
bernstein lineage v2 verify
```

Export attestations for an audit pack:

```bash
bernstein lineage v2 export task-9f3a \
  --format sigstore --output audit/task-9f3a.intoto.json
```

## Troubleshooting

**`Lineage v2: FAIL` after a hard kill.** The parent ref is appended
*after* the child file is fsynced. A crash between those steps leaves a
child file without a parent ref. Run `verify` to see which sha is
orphaned; remove `children/<sha>.jsonl` to clear, or replay the run.

**`No v2 records for task`.** The writer is still on v1. Confirm
`BERNSTEIN_LINEAGE_V2=1` is set in the orchestrator's environment, not
just the CLI shell; `bernstein run` inherits the env from where it was
launched.
