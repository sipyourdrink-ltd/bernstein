# Quality gates and verification

The verification layer between a worker's diff and merge or human review: a configurable gate pipeline plus the janitor's claim verification.

## Key files

| File | Purpose |
|---|---|
| `gate_pipeline.py` | `VALID_GATE_NAMES` registry, `GateStatus`, `GateResult`, and `VerificationScope` (what a gate exercised and what it could not, #5397) |
| `merge_receipt.py` | Signed merge-admission receipt binds head/base SHAs, gate results, ruleset and coverage sets; canonical JSON bytes shared with `gate_pipeline.py` |
| `gate_runner.py` | Gate dispatch and execution (subprocess discipline, timeouts) |
| `quality_gates.py` | `QualityGatesConfig` plus the core gate implementations |
| `janitor.py` | Claim verification: did the agent do what its result claims |
| `absence_coverage.py` | Classifies a completion built on an absence claim (`glob_exists`/`file_contains` "not found", or a journal read) as `unverified` unless a coverage record backs it (#3650/#3769/#3770/#3771) |
| `verifier_ladder.py` | Multi-tier verifier ladder with signed, re-derivable per-tier receipts (#2927) |
| `review_pipeline/` | Fresh-context cross-model review gate, the ruleset a verdict is produced under (`review_pipeline/ruleset.py`), and the bounded review -> fix -> re-check contour with one chained receipt per pass (`review_pipeline/contour.py`, #4481) |
| `behavior_probe.py` | The one gate that executes the diff: boundary inputs derived from the changed callables' signatures, no model and no randomness, crash-level claim, replayable probe receipt (#3377) |
| `collusion.py`, `collusion_gate.py` | Cross-task check at merge admission (#5398): three invariants over pairs of tasks whose effect footprints couple (`../lineage/dependency.py`). The result is a `GateResult`, and its record is bound into the merge receipt (`merge_receipt.py`, schema v3) |
| `formal_verification.py` | Z3/Lean4 checks over scalar task metadata |

## Invariants

- Gate names are a closed set: a step name must be in `VALID_GATE_NAMES` or come from a registered gate plugin; anything else is rejected (`gate_runner.py`). A name goes in the set only when `GateRunner` can dispatch it (#6156); `cross_task_collusion` is left out on purpose, since only merge admission and the eval harness call it.
- Defaults are deliberate: `lint`, `pii_scan`, `dlp_scan`, `run_config` on; `tests`, `type_check`, heavier gates off (`quality_gates.py`). Never flip one as a side effect; `run_config` is a safety invariant (`../config/run_overlay.py`).
- Blocking vs advisory semantics are per-gate; a new gate declares which. No package-level `__getattr__` re-export magic here (`__init__.py` explains why).
- A required gate that ends `inconclusive` blocks exactly as `fail` does; the verdict differs, the outcome does not (#4181). The collusion gate is `inconclusive` whenever a candidate task has no recorded footprint.
- `behavior_probe` claims crashes, not semantics: undocumented exception, return type contradicting the annotation, or no return inside the budget. Widening it to semantic assertions would make its verdict a matter of opinion.
- Absence-claim coverage fails closed: a coverage record that cannot be read back classifies as no-coverage, never as a fabricated pass (`absence_coverage.py`).
- The review contour fails closed too: a spent budget, a missing fix runner, or a fix pass that landed no commit ends in `needs-operator` and a non-zero exit code, never in an approval (`review_pipeline/contour.py`); an empty ruleset leaves the reviewer prompt byte-identical (`review_pipeline/ruleset.py`).

## Testing

Single files only, e.g. `uv run pytest tests/unit/test_quality_gates.py -x -q`; runner and pipeline behaviour lives in the `test_gate_*.py` files, and the collusion gate in `tests/unit/quality/test_cross_task_collusion_gate.py`.

<!-- Reviewed 2026-10-24 against main b80e0aa71ee7; the notes above still hold. -->
