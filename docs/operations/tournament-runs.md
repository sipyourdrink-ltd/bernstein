# Tournament runs

Run several independent attempts at one hard task in parallel and pick the
winner with scripted evaluators - no model call in the decision path - then
inspect or offline-verify exactly why one attempt won.

## Why

A single attempt at a hard task often lands mediocre, and the usual fix is an
operator re-running the task by hand and eyeballing diffs. A tournament fans
out `attempts` independent siblings of one task and selects the winner by
scripted checks (test pass rate, lint status, coverage, an arbitrary
command's exit status), each carrying a weight and a direction. The result an
operator inspects is not just "attempt 3 won" - it is a signed
`TournamentReceipt` naming every attempt's content hash, every evaluator's
output, every blended score, the winner, and the lineage edges (one
`chosen`, the rest `sibling`).

## Declaring a tournament

A tournament is declared with a `TournamentSpec`: a number of `attempts` and
an ordered, non-empty set of evaluators.

```python
from bernstein.core.tournament.spec import EvaluatorSpec, TournamentSpec

spec = TournamentSpec(
    attempts=3,
    evaluators=(
        EvaluatorSpec(name="tests", weight=2.0, higher_is_better=True),
        EvaluatorSpec(name="lint", weight=1.0, higher_is_better=True),
    ),
)
```

`TournamentSpec.spec_hash()` is a `sha256:` content hash of the canonical
spec, so the same declared tournament always hashes to the same value and can
be pinned into the receipt for offline replay. `attempts` must be `>= 1`,
evaluator names must be unique, total weight must be positive, and the only
supported `tie_break` is `attempt_hash` (stable ascending order).

## Running one

`TournamentRunner` (`src/bernstein/core/tournament/runner.py`) is a
programmatic primitive, not a `bernstein run` flag: it takes three callbacks
- a `spawner` that fans out `n` sibling attempts and returns their ids, an
`evaluator` that blocks until attempts finish and returns their outcomes, and
an optional `reclaimer` that tears down losing attempts - so it is
agent-agnostic and testable without a live scheduler. There is currently no
CLI subcommand that drives a fan-out directly; an integrator wires
`TournamentRunner` into their own dispatch path.

`TournamentRunner.run()`:

1. Resolves the per-ticket budget ceiling with the same primitive operators
   already configure (`resolve_ticket_cap_usd`) and raises
   `TournamentBudgetExceeded` before fan-out starts if
   `attempts * per_attempt_cost_usd` would exceed it.
2. Fans out the siblings via the `spawner` callback (optionally through a
   cache-window warm-up, see below).
3. Collects evaluator outputs via the `evaluator` callback.
4. Emits the signed, spine-anchored selection receipt.
5. Runs the optional `reclaimer` against every non-winning attempt.

Evaluator outputs are collected separately from scoring: an evaluator
produces an `EvaluatorOutput(name, value, detail)` per attempt, where `value`
is a normalized number (the scorer clamps to `[0, 1]`). The one evaluator
helper the module ships is `command_status_output()`, which runs a scripted
command with no shell and maps exit code `0` to `1.0`, any other exit or a
subprocess error to `0.0`. Any other evaluator (test pass rate, coverage
delta, mutation score) is supplied by the integrator's own `evaluator`
callback in the same `EvaluatorOutput` shape.

## Selection

Selection is a pure function of the outputs and the spec - no model, clock,
or randomness:

- Each evaluator contributes `weight * signal` to an attempt's blended score,
  where `signal` is the clamped output value (or `1 - value` when
  `higher_is_better=False`); a missing output scores as the worst case (`0`).
  The sum is normalized by total weight.
- Attempts rank by descending score, ties broken by ascending attempt hash.
- The top-ranked attempt wins.

Given the same outcomes and spec, `select_winner()` always returns the same
winner regardless of input order, and `selection_projection_bytes()` (the
bytes hashed into the receipt) is byte-identical across replays.

## Inspecting and verifying a receipt

```
bernstein tournament show <task>
bernstein tournament verify <task>
```

`bernstein tournament show <task>` renders the receipt: winner, attempt
count, evaluator names, tie-break rule, spine anchor, and a per-attempt table
(rank, attempt hash, score, `chosen`/`sibling` edge). `-w/--workdir` sets the
project root (default `.`). Exit `0` when a receipt exists, `1` when there is
none.

`bernstein tournament verify <task>` recomputes the selection offline:
replays the deterministic scorer over the recorded evaluator outputs, checks
that exactly one edge is `chosen` over the recorded attempts, verifies the
Ed25519 signature over the canonical binding, and re-anchors the receipt
against the tournament lineage spine. Exit codes: `0` verified, `1` no
receipt, `2` mismatch (a tampered score, a hand-picked winner, or a tampered
receipt/spine). `bernstein audit verify` runs the same integrity check across
every tournament receipt in a project, alongside every other chained
receipt.

## Cache-window fan-out (optional)

When a `CacheWindowFanout` config is supplied and the resolved adapter
supports a cache window, the runner issues one warm-up call to prime the
shared prompt prefix before the siblings spawn, instead of racing all
siblings to write the cache independently. See [Cost-aware
scheduling](cost-aware-scheduling.md) for how the warm-up and cache-hit
counts are recorded and used elsewhere in cost-aware dispatch. This is
off/unset by default.

## Limitations

- No CLI or seed-file syntax declares `attempts` / evaluators on a task and
  triggers a fan-out automatically; `TournamentRunner` is invoked
  programmatically by an integrator supplying spawn/evaluate/reclaim
  callbacks.
- Only one built-in evaluator (`command_status_output`, an exit-status check)
  ships in the module; every other evaluator kind is caller-supplied.

## Source

- `src/bernstein/core/tournament/spec.py` - `TournamentSpec`,
  `EvaluatorSpec`.
- `src/bernstein/core/tournament/evaluators.py` - `EvaluatorOutput`,
  `AttemptOutcome`, `command_status_output`.
- `src/bernstein/core/tournament/scorer.py` - deterministic scoring and
  winner selection.
- `src/bernstein/core/tournament/runner.py` - `TournamentRunner`, budget
  gating.
- `src/bernstein/core/tournament/receipt.py` - `TournamentReceipt`, emission
  and verification.
- `src/bernstein/cli/commands/tournament_cmd.py` - `bernstein tournament
  show` / `verify`.
