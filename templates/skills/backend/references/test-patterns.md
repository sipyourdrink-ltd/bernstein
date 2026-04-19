# Test patterns (Bernstein)

- Test runner: `uv run python scripts/run_tests.py -x`.
- Single test file: `uv run pytest tests/unit/test_foo.py -x -q`.
- NEVER run `uv run pytest tests/ -x -q` — it leaks 100+ GB RAM across the full suite.

## Structure
- Unit tests live in `tests/unit/`.
- Integration tests live in `tests/integration/` and may touch the network /
  filesystem.
- Use `pytest-asyncio` for async tests; mark them with `@pytest.mark.asyncio`.
- Prefer fixtures over setup/teardown methods.

## Style
- Name tests after the scenario: `test_<thing>_<condition>_<expected>`.
- Cover happy path, edge cases, and error paths in that order.
- Mock external dependencies (HTTP, databases) — never internal logic.
- Use `respx` for httpx mocking, `pytest-benchmark` for perf-sensitive code.

## Regression tests
- When fixing a bug, the failing test comes first. Commit the test + fix
  together so the regression is captured in history.
