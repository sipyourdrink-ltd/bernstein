# Contributing to Bernstein

Thanks for your interest! Here's how to get started.

## Quick Start

```bash
git clone https://github.com/sipyourdrink-ltd/bernstein && cd bernstein
uv venv && uv pip install -e ".[dev]"
```

## Testing

```bash
uv run python scripts/run_tests.py -x        # all tests (isolated per-file, stops on first failure)
uv run python scripts/run_tests.py -k router  # filter by keyword
uv run pytest tests/unit/test_foo.py -x -q    # single file (fast)
```

> **WARNING: Never run `uv run pytest tests/ -x -q`** - the full suite keeps references across 2000+ tests and can leak 100+ GB RAM. The isolated runner in `scripts/run_tests.py` caps each file at ~200 MB.

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
   uv run lint-imports          # architecture contracts (adapter/core boundary)
   uv run python scripts/run_tests.py -x
   ```
4. Commit with a clear message
5. Open a PR against `main`

All non-trivial changes land via PR with at least one approving review. Security-touching changes need two approvals or operator-only push. Full process and reviewer expectations: [docs/CODE_REVIEW.md](docs/CODE_REVIEW.md).

### Docs alongside code

Every PR that adds or changes a feature MUST update docs in the same PR:

- User-visible behaviour: update the relevant `README.md` section.
- Operator workflows: update `docs/operations/<area>.md`.
- Public API surface: regenerate `docs/api/` schemas.
- Architecture or new module: update `docs/sdd/` and run `uv run bernstein agents-md sync` so AGENTS.md, CLAUDE.md, `.goosehints`, `CONVENTIONS.md`, and `.cursor/rules/*.mdc` stay aligned.
- New test layer: also update `docs/contributing/testing.md`.

PRs without the matching docs change will be sent back. Docs and code ship together.

### Pre-push hook

Install the versioned pre-push hook to catch lint and architecture-contract
violations before they reach CI:

```bash
ln -sf ../../scripts/git-hooks/pre-push .git/hooks/pre-push
chmod +x scripts/git-hooks/pre-push
```

The hook is **blocking** on ruff and `lint-imports`, **advisory** on pyright.
The most common architecture-contract violation it catches is an adapter
importing a scheduler-internal module, e.g.
`from bernstein.core.tasks.models import ModelConfig` (wrong) instead of
`from bernstein.core.models import ModelConfig` (canonical). See
`.importlinter` for the full contract list.

### Auto-heal on CI failure

When CI fails on `main`, the `bernstein-ci-fix` workflow
(`.github/workflows/bernstein-ci-fix.yml`) runs Bernstein in headless mode
against the failing commit, opens an `auto-heal/<sha>` branch with the
proposed fix, and creates an `auto-heal: fix CI on <sha>` PR for review.
If Bernstein can't produce a clean diff in 3 iterations within \$5, the
workflow falls back to opening a `ci-fix` issue. Auto-heal is gated by
the `BERNSTEIN_CI_FIX_ENABLED` repo variable, refuses to recurse on
`auto-heal:` PRs, and only fires for canonical-repo pushes (never forks).

## Code Style

- Python 3.12+, type hints on every public function and method
- `from __future__ import annotations` at the top of every module
- Max line length: 120 (enforced by ruff)
- Ruff rules: E, F, W, I, UP, B, SIM, TCH, RUF
- No dict soup - use `@dataclass` or `TypedDict`, not raw `dict[str, Any]`
- Enums over string literals for any value with a fixed set of options
- Google-style docstrings on all public symbols
- Async only for IO-bound code; sync for CPU-bound/pure logic
- Thin orchestration facade, isolated core logic, separate adapters

### Broad-except policy (route handlers)

`src/bernstein/core/routes/**.py` is policed by a CI lint
(`scripts/check_routes_broad_except.py`) that fails the build when a
bare `except Exception:` clause appears without a justification marker on
the line itself or in one of the three preceding comment lines.

Two markers are recognised:

- `intentional-broad-except`: legitimate best-effort path (telemetry,
  optional analytics, lineage append, etc.). The body should route any
  sensitive message through `bernstein.core.sanitize.sanitize_log`.
- `bot-ack: <short-tag>`: previously reviewed broad clause; the tag
  identifies the rationale (e.g. `bot-ack: pre-existing-1723`,
  `bot-ack: legacy-shim`).

If the clause is not actually best-effort, narrow it to the realistic
exceptions the helper can raise (typically `OSError`, `json.JSONDecodeError`,
`KeyError`, `ValueError`, or a domain-specific class). Never convert a
broad `except Exception:` into `except BaseException:` -- `KeyboardInterrupt`
and `SystemExit` must propagate.

Run locally before committing:

```bash
uv run python scripts/check_routes_broad_except.py
```

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

Bernstein ships with 40+ CLI agent adapters, plus a generic catch-all. `src/bernstein/adapters/registry.py` is the source of truth for the exact set - check it before writing a new adapter. A subset is shown here for orientation:

| Adapter | File | Agent |
|---------|------|-------|
| `aider` | `adapters/aider.py` | [Aider](https://aider.chat) |
| `amp` | `adapters/amp.py` | [Amp](https://ampcode.com) |
| `claude` | `adapters/claude.py` | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) |
| `cloudflare` | `adapters/cloudflare_agents.py` | [Cloudflare Agents](https://developers.cloudflare.com/agents/) |
| `codex` | `adapters/codex.py` | [Codex CLI](https://github.com/openai/codex) |
| `cody` | `adapters/cody.py` | [Cody](https://sourcegraph.com/cody) |
| `continue` | `adapters/continue_dev.py` | [Continue](https://continue.dev) |
| `cursor` | `adapters/cursor.py` | [Cursor](https://www.cursor.com) |
| `devin_terminal` | `adapters/devin_terminal.py` | [Devin Terminal](https://devin.ai) (Cognition) |
| `gemini` | `adapters/gemini.py` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) |
| `goose` | `adapters/goose.py` | [Goose](https://block.github.io/goose/) |
| `iac` | `adapters/iac.py` | Infrastructure-as-Code agent |
| `junie` | `adapters/junie.py` | [JetBrains Junie](https://junie.jetbrains.com) |
| `kilo` | `adapters/kilo.py` | [Kilo](https://kilo.dev) |
| `kiro` | `adapters/kiro.py` | [Kiro](https://kiro.dev) |
| `ollama` | `adapters/ollama.py` | [Ollama](https://ollama.ai) (local models) |
| `openai_agents` | `adapters/openai_agents.py` | [OpenAI Agents SDK v2](https://openai.github.io/openai-agents-python/) |
| `opencode` | `adapters/opencode.py` | [OpenCode](https://opencode.ai) |
| `q_dev` | `adapters/q_dev.py` | [AWS Q Developer CLI](https://docs.aws.amazon.com/amazonq/) |
| `qwen` | `adapters/qwen.py` | [Qwen Code](https://github.com/QwenLM/qwen-code) |
| `generic` | `adapters/generic.py` | Any CLI agent (catch-all) |

Full list: `src/bernstein/adapters/registry.py`. Adapter guide: `docs/adapters/ADAPTER_GUIDE.md`.

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
4. Open a PR - include a short note on how you tested it.

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

- `system_prompt.md` - agent persona and standing instructions
- `task_prompt.md` - per-task instructions template
- `config.yaml` - default model and effort

Built-in roles: `adversary`, `analyst`, `architect`, `backend`, `ci-fixer`, `devops`, `docs`, `frontend`, `manager`, `ml-engineer`, `prompt-engineer`, `qa`, `resolver`, `retrieval`, `reviewer`, `security`, `visionary`, `vp`.

Copy an existing role and customize:

```bash
cp -r templates/roles/backend templates/roles/data-engineer
```

The new role is available immediately - no code changes required.

## Architecture Principles

- **Deterministic orchestrator** - no LLM calls for scheduling/coordination
- **Short-lived agents** - spawn per task batch, exit when done
- **File-based state** - everything in `.sdd/`, no databases
- **Pluggable adapters** - new CLI agents via `adapters/base.py` ABC
- **Branch is `main`** - never `master`
- **No monoliths** - don't create or extend god-files (>400 LOC soft, >600 LOC hard stop)

## Process management

Bernstein writes PID metadata to `.sdd/runtime/pids/`. Use those to find and stop processes. **Never** `pkill -f bernstein` or `pgrep bernstein` - it will kill the orchestrator indiscriminately.

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
