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
4. **CI surface & Code Scanning**: SARIF v2.1.0 generation, PR check run scorecards, and delta comparison against signed baselines.

The primary artefact is not a leaderboard row — it is a **submission bundle** whose
score is recomputable from the replayable run receipts it embeds.

---

## Architecture

```
bernstein bench run <suite> [--ci] [--sarif-out <path>] [--baseline <path>]
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
                 ┌───────────────────────┼────────────────────────┐
                 ▼                       ▼                        ▼
       bernstein bench verify      SARIF Diagnostics       GitHub Check Run
                 │                 (SARIF v2.1.0 report)   (Scorecard Table)
        MATCH  ──┤
      DIVERGED ──┘
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
| Baseline integrity | Delta comparison requires a verified signed baseline; missing or unverifiable baseline yields a neutral conclusion |
| Regression gating | `--ci` fails on regressions exceeding `--regression-threshold`; compatible with branch protection |
| Leaderboard is honest | Only `bench verify`-passing bundles are projected into the table |

---

## Walkthrough

### 1. Run the suite

```bash
# Run the canonical golden-v1 suite and emit a submission bundle
bernstein bench run golden-v1 --out my-bundle.json

# Run in CI mode with SARIF output and baseline comparison
bernstein bench run golden-v1 --ci --sarif-out results.sarif --baseline main-bundle.json
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
  ✓ bash_command_pipeline                    MATCH
```

---

## CI Scorecard & Check Run

In continuous integration environments, `bernstein bench run --ci` compares the
current run against the baseline bundle from the default branch.

```bash
bernstein bench run golden-v1 \
  --ci \
  --sarif-out sarif.json \
  --baseline baseline-bundle.json \
  --repo owner/repo \
  --head-sha $GITHUB_SHA
```

### Scorecard Table Format

| Suite | Pass Rate | Score | Baseline Pass Rate | Delta | Bundle Hash | Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `golden-v1` | 100.0% | 1.00 | 100.0% | +0.0% | `3f9a2c1d4e5f` | ✓ PASS |

- **Success**: Verified baseline exists and pass rate meets/exceeds baseline within tolerance.
- **Failure**: Regression exceeds `--regression-threshold`.
- **Neutral**: Missing or unverified baseline bundle.

---

## Suite format

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

The `receipt` is the replay substrate. The `score` only means something
because the receipt exists to replay it. Removing or corrupting the receipt
makes the entire bundle fail verification.

---

## Python API

```python
from bernstein.eval.bench import (
    BenchRunner,
    BenchVerifier,
    MockReplayAdapter,
    build_golden_suite_v1,
    bundle_to_sarif,
    evaluate_ci_scorecard,
    Leaderboard,
    LeaderboardEntry,
)

# Build and run the golden suite (hermetic mock adapter)
suite = build_golden_suite_v1()
adapter = MockReplayAdapter()
runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={})
bundle = runner.run()

# Generate SARIF report
sarif_dict = bundle_to_sarif(bundle, suite)

# Evaluate CI scorecard against baseline
scorecard = evaluate_ci_scorecard(
    bundle=bundle,
    suite=suite,
    baseline_bundle=None,
    regression_threshold=0.0,
)
print(scorecard.to_markdown())

# Verify offline
verifier = BenchVerifier(suite=suite, adapter=adapter)
result = verifier.verify(bundle)
print(result.report())
# overall: MATCH

# Project to leaderboard
lb = Leaderboard(suite_hash=suite.suite_hash, suite_version=suite.version)
lb.add_entry(
    LeaderboardEntry(
        bundle_hash=bundle.bundle_hash(),
        suite_hash=bundle.suite_hash,
        suite_version=bundle.suite_version,
        overall_score=bundle.overall_score,
        pass_rate=bundle.pass_rate,
        num_tasks=len(bundle.task_results),
        submitted_at=bundle.submitted_at,
        bundle_path="bundles/my-bundle.json",
    )
)
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
├── sarif.py             # bundle_to_sarif (SARIF v2.1.0 diagnostics)
├── ci.py                # BenchScorecard, evaluate_ci_scorecard, post_bench_check_run
├── leaderboard.py       # Leaderboard, LeaderboardEntry, Markdown render
├── reliability.py       # pass^k reliability floor (see reliability.md)
└── golden_suite.py      # starter golden-v1 task suite

tests/unit/eval/bench/
├── test_bench.py        # TDD suite — all acceptance criteria
├── test_bench_ci.py     # CI surface, SARIF & scorecard tests (#5458)
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

## Acceptance criteria (from issue #2932 & #5458)

- [x] `bernstein bench run <suite>` produces a signed submission bundle; two runs of the same suite on the same inputs produce byte-identical per-task receipts (empirical determinism).
- [x] `bernstein bench verify <bundle>` recomputes every task's score by replaying the embedded receipts offline, with no access to the submitter's machine, and reports MATCH or the exact task whose replay diverged.
- [x] A bundle with a fabricated score (verdict flipped without a matching replayable run) is rejected at the diverging task; removing or corrupting a task's receipt makes the whole bundle fail verification.
- [x] The suite is content-addressed: two runners on the same suite hash provably ran the same task set; a changed task changes the suite hash.
- [x] SARIF v2.1.0 output generated per failed task for code scanning integrations.
- [x] Check run scorecard table comparing pass rate delta against signed verified baseline.
- [x] Baseline verification: neutral conclusion on unverified or missing baseline, failure on regression.
- [x] The leaderboard projection lists only `bench verify`-passing bundles, each row linking its bundle hash.
- [x] Docs shipped in the same PR.
