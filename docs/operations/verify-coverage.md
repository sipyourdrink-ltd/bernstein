# `bernstein verify coverage`

`bernstein verify coverage <head-sha>` is the receipt-backed structural-coverage
report for a merge admission. It loads the `MergeAdmissionReceipt` for the
given commit and grades each of the four receipt fields that anchor the
admission decision, reporting which were satisfied, which were intentionally
skipped, and which remain unverified.

This is the **receipt-backed** coverage signal. It is distinct from the
self-reported coarse nudge signal the verification tracker writes to
`.sdd/metrics/verification_nudges.jsonl`; the tracker only knows what the
agent's log summary claimed, while this command reads the sealed
`MergeAdmissionReceipt` itself. See
[verification tracking](verification-tracking.md) for the relationship
between the two signals.

## Usage

```bash
bernstein verify coverage <head-sha> [--workdir DIR] [--json]
```

- `<head-sha>` (positional, required): the commit SHA the admission receipt
  was written for. The receipt path is resolved relative to the
  `--workdir` (default `.`).
- `--workdir` / `-w`: project root containing `.sdd/`. Defaults to `.`.
- `--json`: emit the report as JSON alongside the exit code (handy for
  hooks and CI gates). The shape is below.

## What gets graded

Four receipt fields anchor the admission decision:

| Receipt field         | Meaning                                                                                          |
|-----------------------|--------------------------------------------------------------------------------------------------|
| `gate_results_hash`   | Hash of the `(blast_radius, review_verdict, required_contexts)` tuple the gate pipeline produced |
| `ruleset_hash`        | Hash of the ruleset the decision ran under                                                       |
| `required_context_ids`| Required GitHub status contexts the merge satisfied                                               |
| `review_receipt_id`   | Spine entry hash of the review receipt this merge was covered by, when one exists                |

Each field is graded by **presence** on the receipt:

- `verified` — the field is populated.
- `skipped` — the field is intentionally absent (for example, an
  `authority: operator_review` admission does not need `ruleset_hash`).
- `unverified` — the field is required for this admission shape but
  absent; treat this as a fail-closed signal that the receipt is
  incomplete.

The report also surfaces two structural preconditions: `journal_head` and
`signature`. A missing `journal_head` is graded `unverified` (the receipt
does not anchor to the run journal), and a missing `signature` is graded
`unverified` (the receipt is unsigned and cannot be trusted at any tier).

## What the verifier does NOT do

The command grades **presence only**. It does not recompute
`gate_results_hash` from the diff or scope on disk: `MergeAdmissionReceipt`
is a sealed record (R19 — re-declaring a sealed field re-hashes every
record that carries it) and `gate_results_hash` cannot be reproduced
without `blast_radius`, which is intentionally not a receipt field. If
you need to recompute the gate outputs, run the gate pipeline against the
merge's diff, not this command.

## Exit codes

| Code | Meaning                                                                                                |
|-----:|--------------------------------------------------------------------------------------------------------|
| `0`  | Coverage is structurally consistent (every required field populated; intentionally-skipped fields are fine). |
| `1`  | No readable receipt for `head-sha`, or the input was otherwise unusable.                               |
| `2`  | Malformed / unknown admission shape: `head-sha` is present but none of the four coverage fields are populated. |
| `3`  | One or more required coverage fields are absent on a well-formed receipt (the admission was not actually covered). |

Treat `3` as a fail-closed signal: an admission with a missing required
field is not safe to merge. `2` means the receipt itself is malformed and
needs an operator's eye before any merge proceeds.

## JSON shape (`--json`)

```json
{
  "head_sha": "abc123...",
  "receipt": {
    "decision": "admit",
    "authority": "autonomous",
    "journal_head": "sha256:...",
    "signature": "..."
  },
  "coverage": {
    "gate_results": {"status": "verified"},
    "ruleset": {"status": "skipped", "reason": "..."},
    "context_ids": {"status": "verified"},
    "review_receipt": {"status": "unverified", "reason": "..."},
    "journal_head": {"status": "verified"},
    "signature": {"status": "verified"}
  },
  "ok": false,
  "exit_code": 3,
  "reason": "required coverage fields absent: review_receipt"
}
```

The `coverage` map is keyed by the canonical field names; each entry
carries `status` (`verified` / `skipped` / `unverified`) and an optional
`reason` string naming what is wrong.

## See also

- [Verification tracking](verification-tracking.md) — the self-reported
  coarse nudge signal and the difference between the two.
- [`bernstein verify receipt`](../reference/cli/verify.md) — verifies the
  receipt's signature and recomputes its journal head offline.
- [`bernstein gate verify`](../reference/FEATURE_MATRIX.md) — recomputes a
  gate panel's inputs hash against the ruleset.
