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

## Writing a Custom Adapter

Adapters let Bernstein spawn any CLI coding agent. Implement the `CLIAdapter` ABC from `src/bernstein/adapters/base.py`.

### Interface

```python
class CLIAdapter(ABC):
    def spawn(self, *, prompt: str, workdir: Path, model_config: ModelConfig,
              session_id: str, mcp_config: dict | None = None) -> SpawnResult: ...
    def is_alive(self, pid: int) -> bool: ...
    def kill(self, pid: int) -> None: ...
    def name(self) -> str: ...
    def detect_tier(self) -> ApiTierInfo | None: ...  # optional, returns None by default
```

`SpawnResult` fields: `pid: int`, `log_path: Path`, `proc: object | None` (the `Popen` handle, used for poll-based alive checks).

`ModelConfig` fields your `spawn()` will care about: `model` (e.g. `"opus"`, `"gpt-4.1"`), `effort` (`"max"`, `"high"`, `"normal"`), `max_tokens`.

### Steps

1. **Create** `src/bernstein/adapters/mycli.py` and implement all four abstract methods.
   See `src/bernstein/adapters/claude.py` for a complete reference — it shows how to build the CLI command, redirect stdout/stderr to `log_path`, and return a `SpawnResult`.

2. **Register** in `src/bernstein/adapters/registry.py`:
   ```python
   from bernstein.adapters.mycli import MyCLIAdapter
   _ADAPTERS["mycli"] = MyCLIAdapter
   ```
   Or register at runtime: `from bernstein.adapters.registry import register_adapter; register_adapter("mycli", MyCLIAdapter)`.

3. **Run checks**: `uv run ruff check src/ && uv run pyright src/ && uv run pytest`.

4. Open a PR — include a short note on how you tested it (e.g., ran a real task with `bernstein run --adapter mycli`).

## License

By contributing, you agree that your contributions will be licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
