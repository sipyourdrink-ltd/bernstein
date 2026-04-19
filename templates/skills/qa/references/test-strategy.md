# Test strategy (Bernstein)

## Layers

- **Unit** (`tests/unit/`) — one module under test, fast, mocked boundaries.
- **Integration** (`tests/integration/`) — multiple modules, may touch disk
  or the task server.
- **Contract** (`tests/protocol/`) — verifies API schema stability.
- **Pentest** (`tests/pentest/`) — adversarial, verifies no prompt injection
  or credential leakage.

## Running
- `uv run python scripts/run_tests.py -x` — isolated per file, prevents
  memory leaks.
- `uv run pytest tests/unit/test_foo.py -x -q` — single file.
- NEVER `uv run pytest tests/ -x -q` — leaks 100+ GB RAM on the full suite.

## Coverage
- Prefer meaningful assertions over high coverage percentages.
- A well-named failing test is better than a green test with vague asserts.

## Async / flaky
- `pytest.mark.asyncio` for coroutines.
- `pytest-timeout` to pin runtimes on CI.
- Flaky tests get fixed, not retried. Quarantine to `tests/quarantine/` if
  you need to ship first, then file a follow-up.
