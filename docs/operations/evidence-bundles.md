# Verification evidence bundles

A completed task's proof-of-done artefacts - test output, coverage, lint,
an optional screenshot or recording - are content-addressed, sealed into a
signed bundle, and anchored in a dedicated lineage spine, so the bundle a
reviewer inspects *is* the receipt rather than a log line describing one.

```
bernstein evidence show <task>
bernstein evidence verify <task>
```

## Why

"Done" is normally a status plus scattered logs: the test-runner output, the
coverage report, and any screenshot die with the worktree once the task is
reaped. This module makes the artefact a reviewer consumes at review time be
the proof - every producer output is stored content-addressed, bound into a
signed record, and anchored in the evidence lineage spine, so a tampered
evidence file is detected exactly like a tampered chain entry.

## Declaring evidence producers

A task opts in by declaring `evidence_producers` in its spec - a list of
producers, each with a `name`, a `kind`, a `command` (argv, no shell), and a
`required` flag:

| Kind | Notes |
|---|---|
| `test`, `coverage`, `lint`, `generic` | Plain producers; output is captured stdout+stderr. |
| `screenshot`, `recording` | Media producers; additionally sealed with a signed C2PA content credential. |

`required: true` (the default) means a non-zero exit blocks the gate
verdict; `required: false` (advisory) never blocks - a failure only attaches
a failure record. A task that declares no producers is a zero-touch no-op:
no gate runs, no directory is created.

## Sealing (automatic, fail-open)

The orchestrator seals a bundle for every task that reaches the terminal
completed state, right before its worktree is reclaimed
(`seal_evidence_on_completion` in `core/evidence/completion_gate.py`). Each
declared producer's command runs (default timeout 600s), its output is
stored content-addressed and capped at 1 MiB per blob, and the items are
bound into a canonical, Ed25519-signed record anchored in the evidence
lineage spine (`core/lineage/spine.py`). The bundle is mirrored into the
HMAC audit chain as an evidence-bundle event.

Sealing is deliberately **fail-open**: it must never block, delay, or fail a
task completion. Any error raised while running or sealing producers is
caught, logged, and swallowed, so completion proceeds unchanged. The gate
verdict (`gate_passed`) is recorded in the bundle for the reviewer but is not
retroactively enforced against the task the orchestrator already accepted.

The gate verdict itself is `gate_passed = all(required producers passed)`;
advisory producer failures never affect it.

## Inspecting and verifying a bundle

```
bernstein evidence show <task>
bernstein evidence verify <task>
```

`bernstein evidence show <task>` renders the sealed bundle: gate verdict,
bundle hash, spine anchor, and a per-producer table (kind, required or
advisory, pass/fail, exit code, stored size, content hash). `-w/--workdir`
sets the project root (default `.`). Exit `0` when a bundle exists, `1`
when there is none.

`bernstein evidence verify <task>` recomputes the bundle offline from the
sealed record and the stored blobs alone:

1. Checks the Ed25519 signature over the canonical binding.
2. Verifies the evidence lineage spine and the bundle's spine anchor.
3. Re-hashes every stored evidence blob against the sealed manifest -
   naming any item whose content diverges.
4. For media items, re-verifies the signed C2PA content credential against
   the stored media bytes.

Exit codes: `0` verified, `1` no bundle, `2` mismatch (a tampered evidence
file, bundle, or spine). `bernstein audit verify` runs the same integrity
check across every evidence bundle in a project, alongside every other
chained receipt.

## Where evidence lives

```
.sdd/evidence/bundles/<task>.json           # sealed bundle (one per task)
.sdd/evidence/blobs/<hex[:2]>/<hex>          # content-addressed producer output
.sdd/lineage/evidence/spine.jsonl            # evidence lineage spine (Merkle + HMAC)
```

Blob storage is idempotent (an identical blob is written once) and garbage
collected: `EvidenceStore.gc()` removes any blob not referenced by a live
bundle.

## Relationship to the in-process verification gate

`bernstein evidence show` / `verify` is the offline-authoritative surface
for a bundle; the in-process verification-gate hooks (which enforce a
task's write-path allowlist from `owned_files` and its required
`evidence_producers` at spawn time) are a separate, earlier-stage
enforcement layer. See [Hook gate](hook-gate.md).

## Limitations

- Producer output is capped at 1 MiB per blob; output beyond the cap is
  truncated before it is hashed and stored (the truncation is recorded on
  the item, not silently dropped).
- Sealing is fail-open by design: a producer or sealing failure never fails
  the task completion it was meant to attach evidence to - it only means no
  bundle (or a partial one) exists for a reviewer to inspect.

## Source

- `src/bernstein/core/evidence/bundle.py` - `EvidenceProducer`,
  `EvidenceBundle`, `EvidenceStore`, `build_evidence_bundle`,
  `run_evidence_gate`, `verify_evidence_bundle`.
- `src/bernstein/core/evidence/completion_gate.py` -
  `seal_evidence_on_completion`, the fail-open orchestrator wiring.
- `src/bernstein/cli/commands/evidence_cmd.py` - `bernstein evidence show`
  / `verify`.
