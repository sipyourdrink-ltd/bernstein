# Test suite

Layered pytest suite. `unit/` is the big one (2800+ files, external network blocked by default), more and more of it in subdirectories that mirror `src/bernstein/`; `integration/` includes in-process fixtures and service-backed tests; `contract/` holds the adapter capability contracts; `conformance/auditor/` scores what an exported bundle alone can answer; `protocol/` is the MCP/A2A/ACP version matrix; plus `property/`, `snapshot/`, `golden/`, `perf/`, `chaos/`, `stress/`, `pentest/`, and `benchmarks/`. Shared fakes (vault, ssh) live in `support/`.

Focused regressions in `unit/routing/`, `unit/security/`, and `unit/tasks/` cover task-scoped ladder changes, PII placeholder boundaries, and concurrent or rolled-back task claims.

## How to run

```
uv run python scripts/run_tests.py -x         # all unit tests, isolated per-file
uv run python scripts/run_tests.py -k router  # filter by keyword
uv run python scripts/run_tests.py tests/unit/test_foo.py[::test_name]  # one file/test
uv run python scripts/run_tests.py --affected origin/main  # only tests the change reaches
```

## Invariants

- NEVER run bare `pytest` over the whole suite: retained test objects leak into 100+ GB of RAM. The isolated per-file runner caps it (`scripts/run_tests.py`; `CONTRIBUTING.md`).
- `conftest.py` attempts a 2 GB `RLIMIT_AS` cap where supported; lanes that need more can raise it via `BERNSTEIN_MEM_GUARD_GB`. Each file has a 300 s default budget (`BERNSTEIN_TEST_FILE_TIMEOUT_SECONDS`).
- Markers are strict (`--strict-markers` in `pyproject.toml`); register a new marker there before using it.
- Async tests carry an explicit `@pytest.mark.asyncio` (asyncio mode is strict).
- Runtime type-checking via beartype is opt-in per environment (`BEARTYPE_USE_CLAW=enable`; CI's beartype job sets it, local defaults off).
- Docs-guard tests (`unit/test_naming_policy_docs.py`, `unit/test_nested_agents_context.py`) pin repo-level invariants; extend them when adding gated docs. The second caps each context file, this one included, at 40 lines.
- `unit/test_token_orphans.py` fails on caller-less `core/tokens/` modules; `KNOWN_ORPHANS_TOKENS` only shrinks.
- `unit/test_orphan_security_modules.py` is the same ratchet for `core/security/`; each `KNOWN_ORPHAN_REASONS` entry needs a machine-checkable `#NNNN` or `remove-by:YYYY-MM-DD` (#5505).
- Tests for `scripts/*.py` load the script via importlib; git-derived behaviour runs on synthetic repos (`unit/test_context_staleness.py`).
- A file with no executed tests (all skipped, or nothing collected) is reported as `no-tests`, totalled apart from `passed`; it stays non-fatal.

## Gotchas

- CI shards the suite with `scripts/run_tests.py --shard N/M`, balanced by `fixtures/ci/test-shard-durations.json`; a test that depends on sibling-file ordering is already broken.
- A change under `src/`, `scripts/`, `tests/` or `.github/workflows/` with an empty affected set fails closed, except changes confined to `contract/`, `property/` or `snapshot/` (covered by full jobs), or deleted test files.
- Live adapter conformance tests are opt-in via the `--live` flag registered in `conftest.py`.

<!-- Reviewed 2026-10-24 against main 6085c58aeb87, including the merged authority and claim regressions. -->
