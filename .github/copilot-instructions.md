# Copilot Instructions

Bernstein is a multi-agent orchestration system for CLI coding agents (Python 3.12+). The orchestrator is deterministic Python — NOT an LLM.

## Architecture

```
src/bernstein/
  core/                    # 21 sub-packages (orchestration, agents, tasks, quality, ...)
    __init__.py            # MetaPathFinder redirects old imports to sub-packages
    defaults.py            # All configurable constants (150+)
    server.py              # FastAPI task server (HTTP :8052)
    orchestration/         # Run loop, ticks, drain, scheduling
    agents/                # Lifecycle, spawning, identity, signals
    tasks/                 # Lifecycle, store, claim, retry, models
    quality/               # Gates, testing, review, architecture
    security/              # Auth, RBAC, permissions, compliance
    observability/         # Metrics, tracing, logging, alerting
    protocols/             # MCP, A2A, ACP, gRPC, cluster
    routing/               # Model routing, policies, bandit
    cost/                  # Tracking, budgets, forecasting
    tokens/                # Monitoring, context, compaction
    config/                # Seed parsing, settings, feature gates
    git/                   # Operations, worktrees, merge
    persistence/           # Store backends, WAL, checkpoints
    planning/              # Plans, recipes, planner
    communication/         # Bulletin, notifications, signals
    knowledge/             # Knowledge base, RAG, memory
    plugins_core/          # Plugin management, skills
  adapters/                # CLI agent adapters (claude, codex, gemini, etc.)
  cli/                     # CLI commands (commands/, display/, utils/, plan/)
  tui/                     # Textual TUI widgets
```

## Key constraints

- The orchestrator/scheduler is deterministic Python. NEVER add LLM calls for coordination.
- Agents are short-lived: spawn per task, execute, exit. No long-running sessions.
- All runtime state lives in `.sdd/` as files (JSONL, YAML). No databases.
- Use frozen dataclasses/TypedDict for all data — never raw dicts.
- Type hints on ALL public functions. Pyright strict mode must pass.
- Async for IO-bound, sync for CPU-bound.
- New constants go in `core/defaults.py`, not inline.
- New modules go in the appropriate sub-package, not in `core/` root.

## Before committing

```bash
uv run ruff check src/ --fix
uv run ruff format src/
uv run pyright src/
uv run python scripts/run_tests.py -x
```

NEVER run `uv run pytest tests/` — it leaks 100GB+ RAM. Always use `scripts/run_tests.py`.

## Git rules

- Default branch is `main`. NEVER push to `master`.
- One commit per logical change. Conventional commits (`feat:`, `fix:`, `refactor:`).
- Don't modify files outside the task scope.

## Testing

- Tests in `tests/unit/test_<module>.py`.
- Use `pytest` with `unittest.mock` for external deps.
- Use `pytest.approx()` for float comparisons, not `==`.
- Use `tmp_path` fixture for file I/O tests.
- Test factories available in `tests/factories.py`.
