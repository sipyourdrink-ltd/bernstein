# Contributing to Bernstein

Thanks for your interest! Here's how to get started.

## Quick Start

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e ".[dev]"
```

## Testing

```bash
uv run python scripts/run_tests.py -x        # all tests (isolated per-file, stops on first failure)
uv run python scripts/run_tests.py -k router  # filter by keyword
uv run pytest tests/unit/test_foo.py -x -q    # single file (fast)
```

> **WARNING: Never run `uv run pytest tests/ -x -q`** — the full suite keeps references across 2000+ tests and can leak 100+ GB RAM. The isolated runner in `scripts/run_tests.py` caps each file at ~200 MB.

## Linting & type checking

```bash
uv run ruff check src/
uv run ruff format src/
uv run pyright src/
```

All three must pass before committing. No exceptions, no "fix later."

## Development Workflow

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make your changes
3. Run checks:
   ```bash
   uv run ruff check src/
   uv run pyright src/
   uv run python scripts/run_tests.py -x
   ```
4. Commit with a clear message
5. Open a PR against `main`

## Code Style

- Python 3.12+, type hints on every public function and method
- `from __future__ import annotations` at the top of every module
- Max line length: 120 (enforced by ruff)
- Ruff rules: E, F, W, I, UP, B, SIM, TCH, RUF
- No dict soup — use `@dataclass` or `TypedDict`, not raw `dict[str, Any]`
- Enums over string literals for any value with a fixed set of options
- Google-style docstrings on all public symbols
- Async only for IO-bound code; sync for CPU-bound/pure logic
- Thin orchestration facade, isolated core logic, separate adapters

See [AGENTS.md](AGENTS.md) for the full doctrine, including doctrine, change classification, conflict protocol, and zero-tolerance failures.

## CLI Structure

The CLI is split into two layers under `src/bernstein/cli/`:

**Top-level** (`cli/`): `main.py` (Click group), `run.py`, `run_cmd.py`, `live.py`, `dashboard.py`, `helpers.py`, `ui.py`, `status.py`

**Commands sub-package** (`cli/commands/`): 70+ command modules including:

| Module | Purpose |
|---|---|
| `run_cmd.py` | `bernstein run` / `-g` orchestration entry point |
| `stop_cmd.py` | `bernstein stop` graceful shutdown |
| `status_cmd.py` | `bernstein status` / `bernstein ps` |
| `evolve_cmd.py` | `bernstein evolve` subcommands |
| `agents_cmd.py` | `bernstein agents` catalog commands |
| `advanced_cmd.py` | Advanced / less common commands |
| `debug_cmd.py` | `bernstein debug-bundle` diagnostics |
| `cost.py` | Cost tracking display |
| `doctor_cmd.py` | Pre-flight health checks |
| `checkpoint_cmd.py` | Checkpoint save/restore |
| `task_cmd.py` | Direct task manipulation |
| `workspace_cmd.py` | Multi-repo workspace commands |
| `audit_cmd.py` | Audit log inspection |
| `ci_cmd.py` | CI integration commands |
| `policy_cmd.py` | Policy management |
| `triggers_cmd.py` | External trigger management |

When adding a new CLI command, create a new `*_cmd.py` module in `cli/commands/` and register it in `main.py`.

## Supported CLI Adapters

Bernstein ships with 18 adapters (17 specific + 1 generic). When writing a new adapter, check that it isn't already implemented:

| Adapter | File | Agent |
|---------|------|-------|
| `aider` | `adapters/aider.py` | [Aider](https://aider.chat) |
| `amp` | `adapters/amp.py` | [Amp](https://ampcode.com) |
| `claude` | `adapters/claude.py` | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) |
| `codex` | `adapters/codex.py` | [Codex CLI](https://github.com/openai/codex) |
| `cody` | `adapters/cody.py` | [Cody](https://sourcegraph.com/cody) |
| `continue` | `adapters/continue_dev.py` | [Continue.dev](https://continue.dev) |
| `cursor` | `adapters/cursor.py` | [Cursor](https://www.cursor.com) |
| `gemini` | `adapters/gemini.py` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) |
| `goose` | `adapters/goose.py` | [Goose](https://block.github.io/goose/) |
| `iac` | `adapters/iac.py` | Infrastructure-as-Code agent |
| `kilo` | `adapters/kilo.py` | [Kilo](https://kilo.dev) |
| `kiro` | `adapters/kiro.py` | [Kiro](https://kiro.dev) |
| `ollama` | `adapters/ollama.py` | [Ollama](https://ollama.ai) (local models) |
| `opencode` | `adapters/opencode.py` | [OpenCode](https://opencode.ai) |
| `qwen` | `adapters/qwen.py` | [Qwen](https://github.com/QwenLM/Qwen-Agent) |
| `roo_code` | `adapters/roo_code.py` | [Roo Code](https://github.com/RooVetGit/Roo-Code) |
| `tabby` | `adapters/tabby.py` | [Tabby](https://tabby.tabbyml.com) |
| `generic` | `adapters/generic.py` | Any CLI agent (catch-all) |

### Writing a Custom Adapter

Adapters implement the `CLIAdapter` ABC from `adapters/base.py`:

```python
class CLIAdapter(ABC):
    @abstractmethod
    def spawn(self, *, prompt, workdir, model_config, session_id, mcp_config=None) -> SpawnResult: ...
    @abstractmethod
    def is_alive(self, pid: int) -> bool: ...
    @abstractmethod
    def kill(self, pid: int) -> None: ...
    @abstractmethod
    def name(self) -> str: ...
    def detect_tier(self) -> ApiTierInfo | None: ...  # optional
```

Steps:
1. Create `src/bernstein/adapters/mycli.py` implementing all four abstract methods. See `adapters/claude.py` for a complete reference.
2. Register in `adapters/registry.py`: `_ADAPTERS["mycli"] = MyCLIAdapter`
3. Run checks: `uv run ruff check src/ && uv run pyright src/ && uv run python scripts/run_tests.py -x`
4. Open a PR — include a short note on how you tested it.

**Important:** All adapter `.spawn()` implementations must wrap the CLI command with `build_worker_cmd()` from `adapters/base.py`. This sets the process title and writes the PID metadata file that the orchestrator uses for `bernstein ps` and crash detection.

### Writing a Custom CI Parser

CI parsers implement the `CILogParser` protocol from `core/ci_log_parser.py`:

```python
class CILogParser(Protocol):
    name: str
    def parse(self, raw_log: str) -> list[CIFailure]: ...
```

Steps:
1. Create `src/bernstein/adapters/ci/<name>.py` from the template in `templates/ci-parsers/TEMPLATE.py`. See `adapters/ci/github_actions.py` for a working example.
2. Register: `from bernstein.core.ci_log_parser import register_parser; register_parser(MyCIParser())`
3. Run checks and open a PR.

## Writing a Custom Role

Role templates live in `templates/roles/<role-name>/` with three files:

- `system_prompt.md` — agent persona and standing instructions
- `task_prompt.md` — per-task instructions template
- `config.yaml` — default model and effort

Built-in roles: `manager`, `backend`, `frontend`, `qa`, `security`, `architect`, `devops`, `reviewer`, `docs`, `ml-engineer`, `prompt-engineer`, `retrieval`, `vp`, `analyst`, `resolver`, `visionary`, `ci-fixer`.

Copy an existing role and customize:

```bash
cp -r templates/roles/backend templates/roles/data-engineer
```

The new role is available immediately — no code changes required.

## Architecture Principles

- **Deterministic orchestrator** — no LLM calls for scheduling/coordination
- **Short-lived agents** — spawn per task batch, exit when done
- **File-based state** — everything in `.sdd/`, no databases
- **Pluggable adapters** — new CLI agents via `adapters/base.py` ABC
- **Branch is `main`** — never `master`
- **No monoliths** — don't create or extend god-files (>400 LOC soft, >600 LOC hard stop)

## Process management

Bernstein writes PID metadata to `.sdd/runtime/pids/`. Use those to find and stop processes. **Never** `pkill -f bernstein` or `pgrep bernstein` — it will kill the orchestrator indiscriminately.

```bash
# Correct: signal via file
echo "stop" > .sdd/runtime/signals/<role>-<session>/SHUTDOWN

# Correct: use bernstein CLI
bernstein stop

# WRONG: grep-kill
pkill -f bernstein   # kills everything including your own shell session
```

## Recognition

All contributors are listed in [CONTRIBUTORS.md](CONTRIBUTORS.md).
Outstanding contributions are featured in our
[monthly Community Spotlight](https://alexchernysh.com/blog)
blog posts, which are shared on Twitter/X, LinkedIn, and dev.to.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
