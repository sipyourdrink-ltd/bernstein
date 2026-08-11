# `bernstein-bench`: runnable, reproducibility-gated evaluation harness

> Every number on the leaderboard carries its own proof.
> A score that ships without a replayable receipt is indistinguishable from a hand-typed number.

---

## Overview

`bernstein-bench` is the public evaluation surface for the Bernstein orchestrator.
Unlike the internal harness (which runs operator-scored tasks on the operator's own
machine), `bernstein-bench` is designed so that:

1. **Any third party can run the same task set** on their own machine.
2. **The posted score is recomputable** by anyone from the embedded run receipts.
3. **A coordinator that puts a model in the scheduling loop cannot pass** the
   byte-identical reproducibility gate by construction.

The primary artefact is not a leaderboard row — it is a **submission bundle** whose
score is recomputable from the replayable run receipts it embeds.

---

## Architecture

```
bernstein bench run <suite>
        │
        ▼
 ┌─────────────┐     per-task     ┌──────────────────┐
 │  BenchSuite │ ───receipts────► │  SubmissionBundle│
 │ (content-   │                  │ {suite_hash,     │
 │  addressed) │                  │  per_task_       │
 └─────────────┘                  │  receipts,       │
                                  │  scores,         │
                                  │  scheduler_cfg}  │
                                  └──────┬───────────┘
                                         │
                                         ▼
                              bernstein bench verify
                                         │
                              MATCH  ────┤
                            DIVERGED ────┘
                                         │
                                         ▼
                                    Leaderboard
                              (only verified bundles)
```

### Key invariants

| Property | How it is enforced |
|---|---|
| Same task set | `suite_hash` = SHA-256 of ordered task hashes; two runners on the same hash ran the same tasks |
| Score = replay | `bench verify` replays every receipt offline and re-derives the verdict; mismatch → rejected |
| No fabrication | Flipping a verdict without a matching receipt fails verification at the diverging task |
| No missing receipts | An empty/absent receipt fails the entire bundle |
| Leaderboard is honest | Only `bench verify`-passing bundles are projected into the table |

---

## Walkthrough

### 1. Run the suite

```bash
# Run the canonical golden-v1 suite and emit a submission bundle
bernstein bench run golden-v1 --out my-bundle.json
```

This executes every task in `golden-v1` via the real adapter, collects
per-task run receipts (journal head + spine head), scores them with the
`harness.py` multiplicative scorer, and writes a signed
`SubmissionBundle` to `my-bundle.json`.

Two runs of the same suite on the same inputs produce **byte-identical
per-task receipts** — this is the empirical determinism property.

### 2. Verify the bundle

```bash
bernstein bench verify my-bundle.json
```

The verifier:

1. Confirms `bundle.suite_hash` matches the suite you loaded.
2. For each task result:
   - Checks the stored `receipt_hash` matches `sha256(receipt bytes)`.
   - Re-runs harness scoring against the receipt (no access to the
     submitter's machine).
   - Compares the replayed verdict to the stored verdict.
3. Reports **MATCH** or names the exact task whose replay diverged.

Example output:

```
bundle_hash : 3f9a2c1d…
suite_hash  : a7e4b82f…
overall     : MATCH

  ✓ file_io_read_write                       MATCH
  ✓ refactor_rename_symbol                   MATCH
  ✓ test_generation_happy_path               MATCH
  ✓ lint_fix_unused_import                   MATCH
  ✓ doc_update_docstring                     MATCH
```

### 3. Submit to the leaderboard

```bash
bernstein bench submit my-bundle.json
```

The submission gate runs `bench verify` first.  A bundle whose replayed
runs do not reproduce the claimed per-task outcomes is **rejected** before
any leaderboard entry is written.

The leaderboard (`docs/eval/leaderboard.md`) lists only verified bundles,
each row linking its bundle hash so anyone can re-verify.

---

## Reliability floor (`--reliability k`)

A submission bundle reports a single fixed-coordination run per task. To
report a **floor** instead of a ceiling — does every task pass *all* of
`k` attempts under byte-identical coordination, not just one? — run:

```bash
bernstein bench run golden-v1 --reliability 5 --out reliability.json
bernstein bench reliability-verify reliability.json
bernstein bench reliability-check reliability.json
```

This emits a signed reliability receipt reporting `pass@1` (any attempt
passed) and `pass^k` (all `k` attempts passed, the headline floor), with
all `k` per-attempt run receipts embedded so the floor is recomputable
offline. `bernstein eval --reliability k` is a thin alias for the same
run path — identical receipt, verified with the same two verbs above.
Full details: [reliability.md](reliability.md).

---

## Suite format

Suites are content-addressed JSON files:

```json
{
  "version": "golden-v1",
  "suite_hash": "<sha256 of ordered task hashes>",
  "tasks": [
    {
      "id": "file_io_read_write",
      "description": "...",
      "steps": ["..."],
      "assertions": [{"kind": "file_exists", "path": "..."}],
      "category": "file_io",
      "task_hash": "<sha256 of this task's canonical bytes>"
    }
  ]
}
```

`suite_hash` changes whenever any task is added, removed, modified, or reordered.
Two runners on the same `suite_hash` provably ran the same task set.

---

## Bundle format

```json
{
  "bundle_hash": "<sha256 of everything except signature>",
  "suite_hash": "...",
  "suite_version": "golden-v1",
  "submitted_at": 1753000000.0,
  "scheduler_config": {"...": "..."},
  "overall_score": 0.95,
  "pass_rate": 1.0,
  "task_results": [
    {
      "task_id": "file_io_read_write",
      "task_hash": "...",
      "receipt": {
        "journal_head": "<sha256>",
        "spine_head":   "<sha256>",
        "run_id": "...",
        "events": [...]
      },
      "receipt_hash": "<sha256 of receipt bytes>",
      "passed": true,
      "score": 1.0,
      "harness_output": {"...": "..."}
    }
  ],
  "signature": "<Ed25519 JWS>",
  "signer_fingerprint": "..."
}
```

The `receipt` is the replay substrate.  The `score` only means something
because the receipt exists to replay it.  Removing or corrupting the receipt
makes the entire bundle fail verification.

---

## Python API

```python
from bernstein.eval.bench import (
    BenchRunner,
    BenchVerifier,
    MockReplayAdapter,
    build_golden_suite_v1,
    Leaderboard,
    LeaderboardEntry,
)

# Build and run the golden suite (hermetic mock adapter)
suite = build_golden_suite_v1()
adapter = MockReplayAdapter()
runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={})
bundle = runner.run()

# Verify offline
verifier = BenchVerifier(suite=suite, adapter=adapter)
result = verifier.verify(bundle)
print(result.report())
# overall: MATCH

# Project to leaderboard
lb = Leaderboard(suite_hash=suite.suite_hash, suite_version=suite.version)
lb.add_entry(LeaderboardEntry(
    bundle_hash=bundle.bundle_hash(),
    suite_hash=bundle.suite_hash,
    suite_version=bundle.suite_version,
    overall_score=bundle.overall_score,
    pass_rate=bundle.pass_rate,
    num_tasks=len(bundle.task_results),
    submitted_at=bundle.submitted_at,
    bundle_path="bundles/my-bundle.json",
))
print(lb.to_markdown())
```

---

## Running the tests

```bash
# From the repo root:
pytest tests/unit/eval/bench/ -v
```

All tests use `MockReplayAdapter` — no network, no real adapters, no API keys.

---

## File map

```
src/bernstein/eval/bench/
├── __init__.py          # public API re-exports
├── suite.py             # BenchSuite, BenchTask (content-addressed)
├── bundle.py            # SubmissionBundle, TaskResult
├── runner.py            # BenchRunner, MockReplayAdapter, StochasticMockReplayAdapter
├── verifier.py          # BenchVerifier, VerificationStatus
├── leaderboard.py       # Leaderboard, LeaderboardEntry, Markdown render
├── reliability.py       # pass^k reliability floor (see reliability.md)
└── golden_suite.py      # starter golden-v1 task suite

tests/unit/eval/bench/
├── test_bench.py        # TDD suite — all acceptance criteria
└── test_reliability.py  # pass^k reliability floor tests

docs/eval/
├── bench.md                  # this document
├── reliability.md            # pass^k reliability floor
└── trajectory-receipts.md   # offline-verifiable benchmark score receipts (#2925)
```

---

## Trajectory receipts

Every number produced by `bernstein benchmark` ships as a **trajectory receipt**
— a content-addressed, spine-anchored envelope that lets any third party
re-derive the score offline without re-running the suite.

```bash
# Seal a run into a receipt
bernstein benchmark receipt emit <run_id>

# Verify offline — re-derives the score from embedded per-task components
bernstein benchmark receipt verify sha256:<receipt_hash>
```

`bernstein audit verify` sweeps trajectory receipts alongside every other
integrity pillar. Absence of receipts is a silent no-op; a present-and-tampered
receipt hard-fails the sweep.

See [`docs/eval/trajectory-receipts.md`](trajectory-receipts.md) for the full
CLI reference, the offline third-party (COSE/in-toto) verification path, and
the strip-the-substrate failure contract.

---

## Acceptance criteria (from issue #2932)

- [x] `bernstein bench run <suite>` produces a signed submission bundle; two runs of the same suite on the same inputs produce byte-identical per-task receipts (empirical determinism).
- [x] `bernstein bench verify <bundle>` recomputes every task's score by replaying the embedded receipts offline, with no access to the submitter's machine, and reports MATCH or the exact task whose replay diverged.
- [x] A bundle with a fabricated score (verdict flipped without a matching replayable run) is rejected at the diverging task; removing or corrupting a task's receipt makes the whole bundle fail verification.
- [x] The suite is content-addressed: two runners on the same suite hash provably ran the same task set; a changed task changes the suite hash.
- [x] The leaderboard projection lists only `bench verify`-passing bundles, each row linking its bundle hash.
- [x] Docs shipped in the same PR.
