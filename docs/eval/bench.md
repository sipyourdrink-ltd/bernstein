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
4. **What a verdict cost is part of the record**: tokens, USD and wall-clock
   per task ride in the bundle, `bench compare` reports their deltas, and
   `--budget` stops a run that would overspend and records each refusal as a
   receipt the verifier checks (#5464).

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

# The same, refusing to spend more than $0.50 (CI)
bernstein bench run golden-v1 --out my-bundle.json --budget 0.50
```

This executes every task in `golden-v1` via the real adapter, collects
per-task run receipts (journal head + spine head), scores them with the
`harness.py` multiplicative scorer, records each task's tokens, USD cost
and duration as the adapter reports them, and writes a signed
`SubmissionBundle` to `my-bundle.json`.

With `--budget <usd>`, the runner checks the cumulative spend before each
task and, once it reaches the limit, stops running tasks: every remaining
task gets a **refusal receipt** (`status: "refused"`, `refusal_reason:
"budget_exceeded: …"`) with `passed: false` and `score: 0.0`, so the bundle
says which tasks did not run and why. The command then prints
`Budget exceeded: limit $…, spent $…; K/N tasks refused …` and **exits 2**,
because a run the budget cut short is not a completed run and a CI log
reader must not mistake its score for one. The check runs *before* each
task, so the first task always runs and the task that crosses the limit
completes: spend can overshoot by at most one task's cost, which cannot be
known before that task runs. `--budget` does not combine with
`--reliability` — the reliability runner enforces no budget, and the
command refuses the pair rather than run K attempts uncapped.

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

### 4. Compare two bundles

```bash
bernstein bench compare a.json b.json
```

`bench compare` ranks two bundles by score, but only when they were
produced by the **same harness settings**.  Every bundle carries a
`harness_fingerprint` (see [Bundle format](#bundle-format)) — a SHA-256
over the canonical JSON of the `scheduler_config` mapping that shaped
the run.  When the fingerprints differ, `compare` prints which settings
keys differ and **refuses to rank**, because a score gap across
differing harness settings is a harness change, not a model change:

```text
Harness fingerprints differ: 9f1c48d0aa21… vs 5b07d39c88fe…
Differing harness settings: prompt_template
Refusing to rank: a score gap across differing harness settings is a
harness change, not a model change.  Pass --allow-harness-drift to rank
anyway.
```

Pass `--allow-harness-drift` to rank anyway (the differing keys are
still printed).  The stored fingerprint is recomputed from the raw
`scheduler_config` beside it before it is trusted; a bundle whose stored
fingerprint does not match its own settings fails with an integrity
error even with the flag.

When either bundle carries resource metrics, the ranking is followed by
the deltas of B relative to A — cost in USD (absolute and percent),
tokens, and wall-clock duration:

```text
Cost     : $0.0500 -> $0.0300 (-0.0200, -40.0%)
Tokens   : 100 -> 80 (-20)
Duration : 1.00s -> 0.80s (-0.20s)
```

The percentage is `n/a` when A cost nothing — a $0 to $0.05 jump is not a
0.0% change. When either bundle carries budget refusals a further line,
`Refused  : 0 -> 2 tasks never ran (budget)`, follows, and the markdown and
JSON reports carry `refused_a` / `refused_b`: a budget-cut run is cheaper
than a complete one only because tasks never ran, and the report says so
rather than letting a truncation read as a saving.

`--format markdown` renders the full report — summary table plus a
per-task breakdown — and `--format json` emits it as a document (the
`CompareResult` shape in `compare.py`); with either, stdout carries only
the report and the harness verdict goes to stderr. The harness check
gates every format: a cost delta across differing harness settings is
as meaningless as a score delta.

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

## Abstention, and the three rates

A run that declines a task it cannot verify used to score exactly like one that
submitted a confidently wrong patch: both were a `failed`, both sat in the
denominator of `resolve_rate = resolved / attempted`, and neither in the
numerator. That rewards guessing, because a guess can only raise the resolve
rate and an abstention can only lower it.

An instance may now end `abstained`, carrying an `abstention_reason`. It is not
an attempt at the task, it is a declared refusal to answer one, so it is
excluded from `attempted` — and because an abstention scores above a wrong
answer, claiming one costs a stated reason.

Three rates, because no one of them answers the operator's question alone:

| Rate | Definition | What it tells you |
|---|---|---|
| **Resolve rate** | `resolved / attempted`, where `attempted` excludes skipped **and** abstained | Of the answers the run gave, how many were right |
| **Abstain rate** | `abstained / taken_on`, where `taken_on` excludes only skipped | How often the run said it could not tell |
| **Confident-error rate** | `wrong / (wrong + resolved)` | Of the answers it gave, how many were wrong |

Read them together. A high resolve rate beside a high abstain rate is a run
that answers rarely and well; the same resolve rate beside a zero abstain rate
and a high confident-error rate is a run that answers everything and is often
wrong. The resolve rate on its own cannot separate those two, which is why
raising it by guessing used to be free.

`errors` are excluded from both halves of the confident-error rate: a harness
crash is not the run being confidently wrong, and counting it as one would move
the number for something the run did not do.

**Existing bundles are unaffected.** A bundle written before abstentions
existed has `abstained: 0`, so `attempted` is `total - skipped` for it exactly
as it always was and its published resolve rate does not move.

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
  "harness_fingerprint": "<sha256 of canonical scheduler_config JSON>",
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
      "harness_output": {"...": "..."},
      "tokens": 1250,
      "cost_usd": 0.0045,
      "duration_seconds": 1.82
    }
  ],
  "total_tokens": 12500,
  "total_cost_usd": 0.045,
  "total_duration_seconds": 18.25,
  "signature": "<Ed25519 JWS>",
  "signer_fingerprint": "..."
}
```

`tokens` and `cost_usd` are what the adapter reported for the task (`0`
when it reported nothing); `duration_seconds` is the adapter's figure, or
the runner's own wall-clock measurement of the task when the adapter
reported none. They are bound into
`bundle_hash` through the task record, so a bundle cannot be re-labelled
cheaper after signing — but they are written only when at least one of
them is set, so a bundle emitted before the fields existed carries none,
hashes exactly as it did, and still loads. The three `total_*` fields are
sums, recomputable from the task records, and, like `overall_score`, are
not part of the hash.

A task the budget refused carries a refusal receipt instead of a run
receipt:

```json
{
  "task_id": "refactor_rename_symbol",
  "receipt": {
    "journal_head": "",
    "spine_head": "",
    "run_id": "refusal-refactor_rename_symbol",
    "status": "refused",
    "refusal_reason": "budget_exceeded: limit $0.0010 exceeded (spent $0.0010)"
  },
  "passed": false,
  "score": 0.0,
  "harness_output": {"refusal": "budget_exceeded"}
}
```

`bench verify` does not replay a refusal — there is nothing to replay —
it checks that the bundle claims nothing for the task: `passed` false and
`score` zero, else the task is reported as `FABRICATED_SCORE`.

The `receipt` is the replay substrate.  The `score` only means something
because the receipt exists to replay it.  Removing or corrupting the receipt
makes the entire bundle fail verification.

`harness_fingerprint` is a SHA-256 over the canonical JSON (sorted keys,
no whitespace) of the full `scheduler_config` mapping.  It is a pure
projection of the settings carried beside it — it does not participate
in `bundle_hash` — and is recomputed by `bench compare` before it is
trusted.  The whole mapping is hashed rather than a named-key allowlist,
so a setting nobody thought to list still changes the fingerprint instead
of silently drifting two runs onto one identity; wall-clock time, paths,
and run identity are excluded because they are not harness settings.
Bundles emitted before the field existed load unchanged (the fingerprint
is derived on load), and comparing against one reads as drift requiring
`--allow-harness-drift`.

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

# Build and run the golden suite (hermetic mock adapter); budget_usd=None runs everything
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

## Suite Rotation, Private Holdout, and Saturation Tracking

A fixed public benchmark suite naturally saturates as agents and models overfit to specific task distributions. To maintain evaluation integrity:

1. **Versioned Manifests & Holdout Binding**:
   - `BenchSuite` binds both public tasks and an optional `holdout_hash` into its content-addressed `suite_hash`.
   - The private holdout tasks remain unpublished and local-only; only their cryptographic hash (`holdout_hash`) is published on manifests and `SubmissionBundle` artefacts so external third parties can verify that a holdout evaluation ran against the pinned task set without exposing the secret test definitions.
2. **Private Holdout Isolation**:
   - `HoldoutBenchRunner` executes holdout tasks locally and enforces by construction that results and specifications are never written or leaked to public paths (raising `HoldoutIsolationError`).
3. **Saturation & Rotation Detection**:
   - When the public-set pass rate exceeds **90%** (`>= 0.90`) across **three (3) consecutive baseline submissions**, the suite is declared *saturated*.
   - `check_suite_saturation()` / `Leaderboard.check_rotation_due()` flags that rotation is due, triggering promotion of private holdout tasks into the public set and seeding a fresh private holdout distribution.

---

## Task Admission Gate: Contamination Check

A benchmark task whose reference solution exists verbatim or with high n-gram overlap in public code hosting measures retrieval rather than problem-solving capability.

During task admission:
- `check_solution_contamination(solution, public_corpus, n=5, threshold=0.8)` computes normalized token n-grams and evaluates overlap against public corpora.
- An admission verdict (`ContaminationVerdict`) is recorded.
- If verbatim match or n-gram overlap exceeds the threshold (`>= 80%`), admission is rejected.

---

## File map

```
src/bernstein/eval/bench/
├── __init__.py          # public API re-exports
├── suite.py             # BenchSuite, BenchTask (content-addressed, holdout binding)
├── bundle.py            # SubmissionBundle, TaskResult (carries holdout_hash; tokens, cost, duration)
├── compare.py           # compare_bundles, CompareResult, TaskComparison (#5464)
├── contamination.py     # Contamination check & admission gate (n-gram fingerprinting)
├── rotation.py          # Suite saturation & rotation detection
├── runner.py            # BenchRunner (budget gate), HoldoutBenchRunner (isolated execution)
├── verifier.py          # BenchVerifier, VerificationStatus
├── leaderboard.py       # Leaderboard, LeaderboardEntry, Markdown render & rotation alert
├── reliability.py       # pass^k reliability floor (see reliability.md)
├── tool_surface_suite.py# tool-surface risk evaluation suite (tool-surface-v1)
└── golden_suite.py      # starter golden-v1 task suite

tests/unit/eval/bench/
├── test_bench.py                   # TDD suite — core acceptance criteria
├── test_bench_cost_budget.py       # cost accounting, compare deltas, budget gate and refusal receipts (#5464)
├── test_rotation_contamination.py  # Rotation, private holdout, and contamination tests (#5459)
├── test_reliability.py             # pass^k reliability floor tests
└── test_tool_surface_risk_suite.py # tool surface risk suite tests

docs/eval/
├── bench.md                  # this document
├── reliability.md            # pass^k reliability floor
└── trajectory-receipts.md   # offline-verifiable benchmark score receipts (#2925)
```

---

## Tool-Surface Risk Suite (`tool-surface-v1`)

The `tool-surface-v1` benchmark suite evaluates MCP tool servers against lethal capability combinations, particularly the **Risky Triple** (untrusted input ingestion, sensitive data reach, and external egress).

Controls covered: `CTRL-TOOL-INVENTORY`, `ASI02`, `AST04`.

### Risk Classification Matrix

| Risk Class | Description | Approval Gate | Unconfigured Approver |
|---|---|---|---|
| `CRITICAL` | Risky Triple present (sensitive reach + untrusted input + egress), or wildcard permissions without auth | **Forced** | Deny by default |
| `HIGH` | Wildcard permissions with strong auth, or sensitive reach with egress or untrusted input | **Forced** | Deny by default |
| `MEDIUM` | Sensitive reach alone, egress alone, or untrusted input alone | None | Allowed |
| `LOW` | Read-only public tool surface (anonymous / weak auth) | None | Allowed |
| `MINIMAL` | Read-only local tool surface (authenticated) | None | Allowed |

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
