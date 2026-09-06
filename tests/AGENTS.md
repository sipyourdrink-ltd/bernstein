# Test suite

Layered pytest suite. `unit/` is the big one (2400+ files, no network);
`integration/` needs a running server; `contract/` holds the adapter
capability contracts; plus `property/`, `snapshot/`, `golden/`,
`perf/`, `chaos/`, `stress/`, `pentest/`, and `benchmarks/`.

## How to run

```
uv run python scripts/run_tests.py -x         # all tests, isolated per-file
uv run python scripts/run_tests.py -k router  # filter by keyword
uv run python scripts/run_tests.py tests/unit/test_foo.py[::test_name]  # one file/test
```

## Invariants

- NEVER run bare `pytest` over the whole suite: retained test objects leak into 100+ GB of RAM.
  The isolated per-file runner caps it (`scripts/run_tests.py`; `CONTRIBUTING.md`).
- Markers are strict (`--strict-markers` in `pyproject.toml`);
  register a new marker there before using it.
- Async tests carry an explicit `@pytest.mark.asyncio` (asyncio mode
  is strict).
- Runtime type-checking via beartype is opt-in per environment
  (`BEARTYPE_USE_CLAW=enable`; CI's beartype job sets it, local defaults off).
- Docs-guard tests (`unit/test_naming_policy_docs.py`, `unit/test_nested_agents_context.py`)
  pin repo-level invariants; extend them when adding gated docs.
- Exact-set orphan guards use `unit/_orphan_scan.py` to keep allowlists shrinking and compare
  pull-request merge results with the head tree, reporting base drift as a stale baseline.
- Tests for `scripts/*.py` load the script via importlib; git-derived
  behaviour runs on synthetic repos (`unit/test_context_staleness.py`).

## Gotchas

- CI shards the suite with `scripts/run_tests.py --shard N/M`; a test
  that depends on sibling-file ordering is already broken.
- Live adapter conformance tests are opt-in via the `--live` flag
  registered in `conftest.py`.

<!-- Reviewed 2026-08-27 against this subtree; the notes above still hold. -->
