# Gate adjudication records

Every maker-checker or judge-panel phase-gate decision writes a signed
**adjudication record** anchored to the run's lineage spine, so the gate's
verdict is not just a pass/fail line in a log but a recomputable proof of
what the panel actually saw.

```
bernstein gate verify <run_id> --inputs inputs.json [--workdir DIR]
```

## Why

A gate decision historically left no attestable record of what the checker
or panel actually saw. `bernstein gate verify` recomputes the inputs hash
from the claimed inputs and confirms the recorded panel saw exactly those
inputs, then confirms the record is still anchored in a spine that itself
verifies.

## What a record binds

Each adjudication is one signed record:

| Field | Meaning |
|---|---|
| `run_id` | The run whose journal the record anchors to. |
| `inputs_hash` | `sha256:` hash of the canonical inputs the panel saw. |
| `rubric_hash` | `sha256:` hash of the canonical rubric applied. |
| `panel_config` | Panel mode plus each judge's `model` / `temperature` / `prompt_hash`. |
| `per_judge_verdict` | One entry per judge, in panel declaration order. |
| `final_verdict` | The aggregated terminal verdict (`pass` / `fail`). |
| `timestamp` | Integer timestamp, stable across replays of a fixture. |
| `journal_entry_hash` | The lineage-spine entry hash over the record's canonical bytes. |

Records persist under `.sdd/lineage/<run_id>/adjudication_records/*.json`,
colocated with the run's spine directory.

## Panel independence

Panel construction rejects any panel whose judges share `model` +
`temperature` + `prompt_hash` (an identical `identity_hash`), so a panel
cannot silently agree on the same error. Two coordination modes are
supported:

- **`maker_checker`** — two roles; the checker (last judge) vetoes. Any
  judge failing fails the gate.
- **`panel`** — N independent judges aggregated by strict majority. Ties or
  a majority fail close the gate rather than pass it.

## How verify reconstructs the decision

`bernstein gate verify <run_id> --inputs inputs.json` recomputes, from the
persisted records and the presented inputs alone:

1. `inputs_hash` over the presented `--inputs` file matches a record's
   recorded `inputs_hash` — a tampered claim is detected.
2. The panel configuration is still independent (no two judges share an
   identity).
3. The record's lineage spine itself verifies.
4. The recorded `journal_entry_hash` matches the spine anchor recomputed
   over the record's canonical bytes.

Exit codes: `0` verified, `1` no record for the run (or bad `--inputs`),
`2` mismatch (a tampered claim, record, or spine).

## Cost attribution

A cheap-maker / capable-checker cost mode attributes maker and checker spend
to separate ledger labels (`adjudication.maker` / `adjudication.checker`)
so per-role gate cost is visible rather than lumped into one bucket.

## Source

`src/bernstein/core/quality/adjudication.py` (record model, aggregation,
verify), `src/bernstein/cli/commands/gate_cmd.py` (`bernstein gate verify`).
