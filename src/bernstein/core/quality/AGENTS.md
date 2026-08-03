# Quality gates and verification

The verification layer between a worker's diff and merge or human
review: a configurable gate pipeline plus the janitor's claim
verification.

## Key files

| File | Purpose |
|---|---|
| `gate_pipeline.py` | `VALID_GATE_NAMES` registry, `GateStatus`, pipeline dataclasses |
| `gate_runner.py` | Gate dispatch and execution (subprocess discipline, timeouts) |
| `quality_gates.py` | `QualityGatesConfig` plus the core gate implementations |
| `janitor.py` | Claim verification: did the agent do what its result claims |
| `review_pipeline/` | Fresh-context cross-model review gate |
| `formal_verification.py` | Z3/Lean4 checks over scalar task metadata |

## Invariants

- Gate names are a closed set: a step name must be in
  `VALID_GATE_NAMES` or come from a registered gate plugin; the runner
  rejects anything else (`gate_runner.py`).
- Defaults are deliberate: `lint`, `pii_scan`, `dlp_scan` on; `tests`,
  `type_check`, and the heavier gates off (`quality_gates.py`). Do not
  flip defaults as a side effect of another change.
- Blocking vs advisory semantics are per-gate; a new gate must declare
  which it is.
- No package-level `__getattr__` re-export magic in this package;
  import submodules by full path (`__init__.py` explains why).

## Testing

Single files only, e.g.
`uv run pytest tests/unit/test_quality_gates.py -x -q`; runner and
pipeline behaviour lives in the `test_gate_*.py` files.
