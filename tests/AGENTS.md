# Test suite

Layered pytest suite. `unit/` is the big one (2100+ files, no network);
`integration/` needs a running server; `contract/` holds the adapter
capability contracts; plus `property/`, `snapshot/`, `golden/`,
`perf/`, `chaos/`, `stress/`, `pentest/`, and `benchmarks/`.

## How to run

```
uv run python scripts/run_tests.py -x         # all tests, isolated per-file
uv run python scripts/run_tests.py -k router  # filter by keyword
uv run pytest tests/unit/test_foo.py -x -q    # single file (fast)
```

## Invariants

- NEVER run bare `pytest` over the whole suite: retained test objects mean
  2000+ files leak into 100+ GB of RAM. The isolated per-file runner caps
  memory per file (`scripts/run_tests.py` docstring; `CONTRIBUTING.md`).
- Markers are strict (`--strict-markers` in `pyproject.toml`);
  register a new marker there before using it.
- Async tests carry an explicit `@pytest.mark.asyncio` (asyncio mode
  is strict).
- Runtime type-checking via beartype is opt-in per environment
  (`BEARTYPE_USE_CLAW=enable`; CI's beartype job sets it, local defaults off).
- Docs-guard tests (`unit/test_naming_policy_docs.py`,
  `unit/test_nested_agents_context.py`) pin repo-level invariants;
  extend them when adding gated docs.
- Tests for `scripts/*.py` load the script via importlib; git-derived
  behaviour runs on synthetic repos (`unit/test_context_staleness.py`).

## Gotchas

- CI shards the suite with `scripts/run_tests.py --shard N/M`; a test
  that depends on sibling-file ordering is already broken.
- Live adapter conformance tests are opt-in via the `--live` flag
  registered in `conftest.py`.

<!-- Reviewed 2026-08-18 against this subtree; the notes above still hold. -->
