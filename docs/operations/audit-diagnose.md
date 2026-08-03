# Single-run fault localisation: `bernstein audit diagnose` (issue #2928)

Every run's journal (`.sdd/runs/<run_id>/journal.jsonl`) already records
each tool call, tool result, provider-state mutation, and knob selection as
a Merkle-chained step. `bernstein audit verify` proves a chain is intact and
`bernstein replay debug <LEFT> <RIGHT>` localises where two runs diverge --
but neither can point at the first faulty step of a *single* run that
recorded cleanly and still went wrong. `audit diagnose` closes that gap: it
turns a failure signal into a pure predicate over the recorded steps, names
the minimal step index at which the predicate first holds, and seals the
finding into a signed receipt any holder of the journal can re-derive
offline.

## TL;DR

| Verb | What it does |
|---|---|
| `bernstein audit diagnose <RUN_ID> --signal <S> --sign-key KEY` | Name the first faulty step (`culprit_index` + `culprit_step_hash`) and write a signed diagnosis receipt under `.sdd/evidence/` |
| `bernstein audit diagnose verify <RECEIPT> [--public-key PEM]` | Re-derive the culprit offline and assert byte-identity with the receipt |

## Failure signals

`--signal` selects how "this run went wrong" becomes a predicate over the
recorded steps. Every adapter reads on-disk records only -- no network, no
live process -- so the resolved predicate is reproducible.

| Signal | Backing record | Culprit criterion |
|---|---|---|
| `gate[:RECEIPT_HASH]` | Sealed verdict receipts under `.sdd/eval/gate/` | First step whose payload carries the rejected suite / candidate result-set content hash. Bare `gate` resolves the most recent `significant_regression` receipt deterministically (highest `(timestamp, receipt_hash)`). |
| `artefact:<path>` | The signed lineage log (`.sdd/lineage/log.jsonl`), **gated first** | The lineage gate (byte-canonical parsing, detached signatures against `.sdd/agents` cards, parent anchoring, operator HMAC when `BERNSTEIN_LINEAGE_OP_SECRET` is set — the same checks `bernstein audit taint` requires) must pass before any predicate is shaped; then the culprit is the first step whose payload carries the content hash of an untrusted provenance record in the artefact's taint closure. The content-addressed parent chain (culprit record → artefact tip) is attached to the receipt as `lineage_path`, and the gate mode used is disclosed in the sealed params as `lineage_gate.operator_hmac_checked`. |
| `incident:<case-id>` | A synthesised incident eval case (`src/bernstein/eval/cases/incidents/`) | First step whose payload carries the case's recorded failure text, byte-exact. |
| `replay` | The journal itself | First step at which the Merkle chain fails to recompute (`chain_break`). An intact chain reports clean and writes no receipt. |

The culprit is attributed with a `reason_code` from the same machine-readable
vocabulary `bernstein replay` divergence reporting uses
(`src/bernstein/core/replay/diff.py`): `bad_input_content_hash`,
`first_failing_tool_result`, `provider_state_mutation` (when the culprit row
is a provider-side context mutation), or `chain_break`.

## The diagnosis receipt

The receipt is the deliverable, not the terminal output. It is
content-addressed (canonical JSON, `receipt_hash` over the body),
Ed25519-signed with the key you pass via `--sign-key` (the lineage
customer-signing key machinery), and carries an operator HMAC under the
audit-chain key plus the current audit-chain head (`chain_head_hmac`) as an
anchor. Key fields:

| Field | Meaning |
|---|---|
| `run_id`, `journal_head` | Run identity; the head is recomputed from the on-disk payloads |
| `journal_file_sha256` | Exact bytes of the diagnosed journal |
| `culprit_index`, `culprit_step_hash` | The named step and its exact `event_hash` |
| `reason_code`, `reason` | Attribution, machine- and human-readable |
| `predicate_id`, `predicate_hash`, `signal` | What "failed" meant, embedded so a verifier reproduces the predicate without the gate / lineage / incident stores |
| `lineage_path` | Content-addressed parent chain (artefact signal) |
| `chain_head_hmac`, `operator_hmac`, `signature` | Anchors: audit chain head, operator HMAC, Ed25519 |

No wall-clock value enters the receipt, so two independent invocations over
the same journal produce byte-identical receipts -- diff them if you doubt it.

## Worked example

```bash
# a nightly run went red at the eval gate; name the step that caused it
bernstein audit diagnose run-2026-08-02-nightly --signal gate \
  --sign-key /etc/bernstein/lineage-signing.pem

# months later, on another machine, re-check the claim offline
bernstein audit diagnose verify \
  .sdd/evidence/diagnosis-run-2026-08-02-nightly-<hash>.json \
  --public-key lineage-signing-pub.pem
```

Exit codes -- diagnose: `0` chain intact / nothing to report; `1` culprit
named and receipt written; `2` fail-closed refusal. verify: `0` the culprit
re-derives byte-for-byte; `1` verification failed; `2` usage error or
unreadable receipt.

## Fail-closed contract

The diagnosis is a projection of the signed record, never an inference
beside it. Each of these refuses with exit `2` and writes **no** receipt:

- the journal is missing or empty ("no signed per-step record for run X");
- the journal contains any non-blank line that does not parse as a JSON
  object (a torn or corrupted write) -- ordinary journal readers tolerate a
  torn tail, but the diagnostic reader refuses a filtered sequence so every
  reported index counts physical journal lines;
- the audit HMAC key is unavailable (`load`-only resolution -- diagnose
  never creates key material);
- `--sign-key` is omitted (no unsigned findings);
- the lineage gate fails for an `artefact:` signal (an unsigned, malformed,
  or reparented entry must never shape a sealed predicate);
- the chain does not recompute and a content signal was requested (use
  `--signal replay` to localise the break itself);
- the signal's fingerprint appears in no recorded step -- the command
  refuses to guess rather than fall back to a heuristic log scan.

`verify` fails on a journal mutated anywhere -- at or after the culprit step
included -- because the receipt pins both the recomputed head and the exact
journal bytes.

## Relationship to the replay surface

`bernstein replay` reconstructs and diffs (`--verify`, `--from-step`,
`debug`, two-run path diff); `audit diagnose` localises a single run's first
fault and signs the finding. Use `replay debug <RUN> --fork-from N` with the
receipt's `culprit_index` to reproduce the run from just before the culprit.

## Cross-references

- [HMAC-chained audit log: operator guide](../security/audit-log.md) --
  "Diagnosing a run" section.
- [Per-step session replay](replay.md) and
  [deterministic replay](deterministic-replay.md).
- [Standard verifiable audit receipts](../security/audit-receipt.md).
- Source: `src/bernstein/core/replay/diagnose.py`,
  `diagnose_signals.py`, `diagnosis_receipt.py`;
  CLI in `src/bernstein/cli/commands/audit_cmd.py`.
