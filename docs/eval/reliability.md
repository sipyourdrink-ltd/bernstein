# pass^k reliability floor

> Best-of-N reporting shows the ceiling: did the task pass at least once?
> The operator deciding whether to run unattended needs the floor: does it pass every time?

---

## Why a floor, and why Bernstein can report one

A task that succeeds once in eight attempts and a task that succeeds every
time both read as "passed" under single-run or best-of-N scoring. They are
worlds apart operationally.

Repeating a task `k` times only measures reliability when "same conditions"
can actually be held fixed. Bernstein's scheduler is deterministic — no LLM
in the coordination loop, and a run leaves a replayable journal — so
coordination (task graph, tool routing, controlled seeds) can be held
byte-identical across attempts. The only thing allowed to vary is the
genuinely stochastic element: model sampling. Under that setup an all-of-k
metric measures model reliability, not coordination luck.

`bernstein bench run --reliability k` runs every suite task `k` times under
fixed coordination and reports:

| Metric | Definition | Role |
|---|---|---|
| `pass@1` | fraction of tasks where **at least one** of the `k` attempts passed | the ceiling — what best-of-N reporting shows |
| `pass^k` | fraction of tasks where **all** `k` attempts passed | the floor — the headline number |

`pass^k <= pass@1` always holds; a gap between the two is the signature of
flaky tasks.

### Estimator notes (what the numbers do and do not claim)

- With per-attempt success probability `p` and `n` recorded attempts of
  which `c` passed, the unbiased estimator of the all-of-k probability
  `p^k` is `C(c, k) / C(n, k)` — the probability that `k` attempts drawn
  without replacement all passed. (The familiar any-of-k counterpart is
  `1 - C(n-c, k) / C(n, k)`.)
- The runner records exactly `n = k` attempts per task, where the all-of-k
  estimator degenerates to the indicator "all `k` attempts passed". That
  indicator is what each task contributes to the aggregate, and it is
  unbiased for `p^k`.
- The floor is a **point estimate, not a confidence bound**: with small
  `k`, a flaky task can still show a clean floor by luck. Raising `k`
  tightens the floor (`p^k` falls monotonically in `k` for `p < 1`) at
  linear cost in runs. "Floor" here means *floor relative to best-of-N
  reporting*, which the metric is by construction, not a statistical
  lower bound on `p`.

---

## The reliability receipt

The primary artefact is not the number — it is a signed **reliability
receipt** binding the floor to the exact fixed-coordination runs that
produced it:

```json
{
  "receipt_hash": "<sha256 of everything except the signature fields>",
  "suite_hash": "...",
  "suite_version": "golden-v1",
  "k": 5,
  "scheduler_config": {"scheduler": "default"},
  "emitted_at": 1753000000.0,
  "pass_at_1": 1.0,
  "pass_caret_k": 0.8,
  "coordination_ok": true,
  "task_results": [
    {
      "task_id": "file_io_read_write",
      "task_hash": "...",
      "coordination_hash": "<sha256 of the shared coordination projection>",
      "coordination_identical": true,
      "attempts": [
        {
          "task_id": "file_io_read_write",
          "task_hash": "...",
          "receipt": {"journal_head": "...", "spine_head": "...", "run_id": "...", "events": ["..."]},
          "receipt_hash": "<sha256 of the receipt bytes at emit time>",
          "passed": true,
          "score": 1.0,
          "harness_output": {}
        }
      ]
    }
  ],
  "signature": "...",
  "signer_fingerprint": "..."
}
```

Every attempt embeds its own replayable run receipt with an emit-time
receipt hash — the same receipt structure a submission bundle carries,
repeated `k` times. The sealed `pass_at_1` / `pass_caret_k` values are
claims; the verifier re-derives both.

### Coordination projection

Fixed coordination is checked on a **coordination projection** of each run
receipt: per-attempt run identity (`run_id`) and the content-hash heads
that commit to model output (`journal_head`, `spine_head`) are dropped,
timing fields are dropped from every event, and model-output events
(`model.*` kinds) keep every field **except** the declared stochastic
payload fields (`sample` in the bench event vocabulary). Undeclared
fields inside a model event — routing, tool selection, scheduler state —
default to coordination (fail-closed), so divergence there fails
admission instead of being silently erased. Two fixed-coordination
attempts must have byte-identical projections; only the declared
model-output payloads may differ. Attempts that diverge anywhere else
make the floor inadmissible — it would be measuring scheduler noise, not
model sampling.

Receipts are schema-validated before they are hashed into a coordination
identity: every event must be an object with a non-empty string `kind`
and an integer `seq`. The runner raises on a malformed adapter receipt at
emit time; the verifier and `reliability-check` report a malformed
receipt instead of proceeding.

---

## Walkthrough

### 1. Run with a reliability budget

```bash
bernstein bench run golden-v1 --reliability 5 --out reliability.json
```

```
pass^5 floor : 80.0%  (all 5 attempts must pass)
pass@1       : 100.0%  (any attempt passed — the ceiling)
coordination : held fixed
```

`bernstein eval --reliability k` (the spelling issue #2933 asked for) is a
thin alias for the same command: it accepts `--suite`, `--out`,
`--scheduler`, and `--stub-signer`, delegates into the identical run path,
and emits the identical signed receipt. Verification is unchanged — use
the two `bench` verbs below; the eval surface adds no reliability logic of
its own.

### 2. Verify the receipt offline

```bash
bernstein bench reliability-verify reliability.json
```

The verifier needs no access to the emitting machine. It rejects:

| Attack | Caught by | Status |
|---|---|---|
| Inflated (or deflated) `pass^k` claim | aggregates recomputed from replayed attempt verdicts | `FABRICATED_FLOOR` |
| Flipped per-attempt verdict | replaying that attempt's receipt | `FABRICATED_SCORE` |
| Byte-flip inside an embedded run receipt | emit-time receipt hash recompute | `HASH_MISMATCH` |
| Stripped attempt (fewer than `k` receipts) | attempt count check | `MISSING_RECEIPT` |
| Stripped failing task (cherry-picked suite) | full suite coverage check | `MISSING_RECEIPT` |
| Coordination divergence across attempts | coordination projection recompute | `COORDINATION_DIVERGED` |
| Malformed event schema (non-string `kind`, non-integer `seq`) | receipt schema validation before hashing | `MALFORMED_RECEIPT` |
| Missing or invalid signature | signature check | `UNSIGNED` |

A cherry-picked or fabricated floor fails because the replays do not
reproduce the claimed outcomes; stripping the replay substrate makes the
floor unverifiable rather than silently higher. The number has no meaning
without the receipts.

### 3. Prove the coordination was held fixed

```bash
bernstein bench reliability-check reliability.json
```

Re-runs one attempt and asserts the fresh run's coordination projection is
byte-identical to the recorded one (with a fully deterministic adapter the
entire receipt is byte-identical; under model sampling only the
model-output payloads differ). On divergence the first divergent field is
named. This is the control that makes a low floor attributable to model
sampling rather than hidden coordination non-determinism — without it,
`pass^k` is noise.

Attempt alignment: `ReplayAdapter.run_task` carries no attempt index, so
the check replays attempts `0..N` in order on a freshly positioned adapter
and compares position `N` against the recorded attempt `N` — well-defined
for stateful adapters too.

---

## Python API

```python
from bernstein.eval.bench import (
    MockReplayAdapter,
    ReliabilityRunner,
    ReliabilityVerifier,
    StubReliabilitySigner,
    build_golden_suite_v1,
    reliability_check,
)

suite = build_golden_suite_v1()
adapter = MockReplayAdapter()

runner = ReliabilityRunner(suite=suite, adapter=adapter, scheduler_config={}, k=5)
receipt = StubReliabilitySigner().sign(runner.run())
print(f"pass^5 floor: {receipt.pass_caret_k:.2%}  pass@1: {receipt.pass_at_1:.2%}")

verifier = ReliabilityVerifier(suite=suite, adapter=adapter)
print(verifier.verify(receipt).report())     # overall: MATCH

print(reliability_check(receipt, suite, adapter).report())  # reliability-check: PASS
```

`StochasticMockReplayAdapter` (seed-parameterised) models model-sampling
variance under fixed coordination for tests: attempt receipts differ only
in their `model.output` payload, and the verdict is a pure function of the
sampled value embedded in the receipt, so offline verification still
replays every attempt.

---

## Scope and boundaries

- The bench CLI drives the hermetic `MockReplayAdapter` on all paths
  today; wiring the real scenario-runner/journal adapter behind the
  `ReplayAdapter` protocol is a separate step, the same boundary the
  submission-bundle surface documents. Until then, real-agent
  fixed-coordination repetition is an operator-side exercise of the same
  Python API.
- Signatures are verified cryptographically, fail-closed. Production
  receipts carry a detached Ed25519 JWS over the receipt hash, keyed by
  the install identity and fingerprinted with the same keyid the install
  publishes at `/.well-known/agent.json/keys`. The verifier resolves the
  fingerprint against its trusted-key map (`--signer-key <pem>` on the
  CLI, plus the local install key when present); an unresolvable
  fingerprint or a failed verification is `UNSIGNED`, never `MATCH`.
  Stub-signed receipts (fingerprint marked `-stub`) are test-grade and
  verified via the deterministic stub construction.
- The receipt seals verdicts and replay substrate, not wall-clock timing:
  timing fields are deliberately outside the coordination projection.

---

## File map

```
src/bernstein/eval/bench/
├── reliability.py       # ReliabilityRunner, ReliabilityReceipt,
│                        # ReliabilityVerifier, reliability_check
└── runner.py            # + StochasticMockReplayAdapter

src/bernstein/cli/commands/
└── eval_benchmark_cmd.py  # `bernstein eval --reliability` alias

tests/unit/eval/bench/
└── test_reliability.py  # TDD suite — all acceptance criteria

tests/unit/cli/
└── test_eval_reliability_alias.py  # eval alias == bench path

docs/eval/
└── reliability.md       # this document
```

See [`bench.md`](bench.md) for the submission-bundle surface this builds on.
