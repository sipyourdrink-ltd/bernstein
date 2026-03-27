# Contributing to Bernstein

Thanks for your interest! Here's how to get started.

## Quick Start

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## Ways to Contribute

- **Bug reports** — open an issue with steps to reproduce
- **Feature ideas** — open a discussion or issue
- **Code** — fork, branch, PR (see below)
- **Docs** — typo fixes, examples, guides
- **Adapters** — add support for new CLI agents (Cursor, Aider, etc.)

## Development Workflow

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make your changes
3. Run checks:
   ```bash
   uv run ruff check src/
   uv run pyright src/
   uv run pytest
   ```
4. Commit with a clear message
5. Open a PR against `master`

## Code Style

- Python 3.12+, type hints everywhere
- `ruff` for linting, `pyright` strict mode for types
- Max line length: 120
- Tests go in `tests/unit/` or `tests/integration/`

## Architecture Principles

- **Deterministic orchestrator** — no LLM calls for scheduling/coordination
- **Short-lived agents** — spawn per task batch, exit when done
- **File-based state** — everything in `.sdd/`, no databases
- **Pluggable adapters** — new CLI agents via `adapters/base.py` ABC

## License

By contributing, you agree that your contributions will be licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
