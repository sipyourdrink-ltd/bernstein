# Contributing to Bernstein

Thanks for your interest! Here's how to get started.

## Community

Join the [Bernstein Discord](https://discord.gg/bernstein) to:
- Get help in `#help`
- Share what you built in `#show-and-tell`
- Discuss adapter development in `#adapters`
- Post benchmark results in `#benchmarks`
- Talk to contributors in `#dev`

## Quick Start

```bash
git clone https://github.com/chernistry/bernstein && cd bernstein
uv venv && uv pip install -e ".[dev]"
uv run python scripts/run_tests.py -x
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
   uv run python scripts/run_tests.py -x
   ```
4. Commit with a clear message
5. Open a PR against `main`

## Testing Your Changes

After making changes, verify them end-to-end before opening a PR.

1. **Start the system** with a test goal:
   ```bash
   uv run bernstein -g "Add a hello-world utility function" --headless
   ```
   The task server starts automatically on port 8052.

2. **Submit a test task** via curl:
   ```bash
   curl -s -X POST http://127.0.0.1:8052/tasks \
     -H "Content-Type: application/json" \
     -d '{"title": "smoke test", "description": "Print hello world", "role": "backend"}'
   ```

3. **Check logs** to confirm agents spawned and completed work:
   ```bash
   ls .sdd/runtime/logs/
   tail -f .sdd/runtime/logs/<session-id>.log
   ```

4. **Stop the system** when done:
   ```bash
   uv run bernstein stop
   ```

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

## CLI Structure

The CLI is decomposed into separate modules under `src/bernstein/cli/`:

| Module | Purpose |
|---|---|
| `main.py` | Click group and top-level flags |
| `run_cmd.py` | `bernstein run` / `-g` orchestration entry point |
| `stop_cmd.py` | `bernstein stop` graceful shutdown |
| `status_cmd.py` | `bernstein status` / `bernstein ps` |
| `evolve_cmd.py` | `bernstein evolve` subcommands |
| `agents_cmd.py` | `bernstein agents` catalog commands |
| `advanced_cmd.py` | Advanced / less common commands |
| `auth_cmd.py` | Authentication commands |
| `checkpoint_cmd.py` | Checkpoint save/restore |
| `delegate_cmd.py` | Task delegation commands |
| `eval_benchmark_cmd.py` | Benchmark evaluation harness |
| `task_cmd.py` | Direct task manipulation |
| `triggers_cmd.py` | Trigger management |
| `workspace_cmd.py` | Multi-repo workspace commands |
| `wrap_up_cmd.py` | Session wrap-up commands |
| `helpers.py` | Shared utilities (PID files, port checks, output formatting) |
| `errors.py` | CLI error types and handlers |
| `cost.py` | Cost tracking display |
| `dashboard.py` | Web dashboard launcher |
| `live.py` | TUI dashboard attachment |
| `ui.py` | Shared UI components |

When adding a new CLI command, create a new `*_cmd.py` module and register it in `main.py`.

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

3. **Run checks**: `uv run ruff check src/ && uv run pyright src/ && uv run python scripts/run_tests.py -x`.

4. Open a PR — include a short note on how you tested it (e.g., ran a real task with `bernstein run --adapter mycli`).

## Writing a Custom CI Parser

CI parsers let Bernstein understand failures from different CI systems and route fix tasks to the right agent. Implement the `CILogParser` protocol from `src/bernstein/core/ci_log_parser.py`.

### Interface

```python
class CILogParser(Protocol):
    name: str                                    # e.g. "gitlab", "circleci"
    def parse(self, raw_log: str) -> list[CIFailure]: ...
```

`CIFailure` fields: `kind` (a `CIFailureKind` enum), `job` (step/stage name), `message` (human-readable summary), `raw` (original log excerpt).

### Steps

1. **Create** `src/bernstein/adapters/ci/<name>.py` from the template in `templates/ci-parsers/TEMPLATE.py`.
   See `src/bernstein/adapters/ci/github_actions.py` for a complete working example.

2. **Register** your parser so the CI fix pipeline can use it:
   ```python
   from bernstein.core.ci_log_parser import register_parser
   from bernstein.adapters.ci.myci import MyCIParser
   register_parser(MyCIParser())
   ```

3. **Run checks**: `uv run ruff check src/ && uv run pyright src/ && uv run python scripts/run_tests.py -x`.

4. Open a PR — mention which CI provider you tested against.

## Writing a Custom Role

Role templates let you define new specialist agent types (e.g., `data-engineer`, `ml-ops`, `dba`). Each role lives in its own directory under `templates/roles/` and consists of three files.

### Built-in roles

| Role | Purpose |
|---|---|
| `analyst` | Evaluates proposals for feasibility, ROI, and risk — produces APPROVE/REVISE/REJECT verdicts |
| `architect` | High-level system design and technical decision-making |
| `backend` | Server-side code, APIs, data models |
| `devops` | CI/CD, infrastructure, deployment |
| `docs` | Documentation, guides, contributor-facing writing |
| `frontend` | UI components, client-side code |
| `manager` | Task planning and coordination (used by the orchestrator) |
| `ml-engineer` | Machine learning pipelines and model integration |
| `prompt-engineer` | Prompt design and LLM interaction patterns |
| `qa` | Testing and quality assurance |
| `resolver` | Resolves git merge conflicts between concurrent agent branches |
| `retrieval` | RAG pipelines, embedding, and search |
| `reviewer` | Code review and feedback |
| `security` | Security audits and vulnerability assessment |
| `visionary` | Generates bold product proposals from a user perspective |
| `vp` | Executive-level strategy and prioritization |

Assign any role to a task with `"role": "<role-name>"` in the task payload.

### Directory structure

```
templates/roles/<role-name>/
├── system_prompt.md   # Agent persona and standing instructions
├── task_prompt.md     # Per-task instructions template
└── config.yaml        # Model and effort defaults
```

### system_prompt.md

This file defines the agent's identity, specialization, and work style. It is rendered once per agent session by the spawner and supports the following template variables:

| Variable | Value |
|---|---|
| `{{GOAL}}` | Title of the first task in the batch |
| `{{TASK_DESCRIPTION}}` | Formatted block listing all tasks in the batch |
| `{{PROJECT_STATE}}` | Contents of `.sdd/project.md` (empty string if absent) |
| `{{AVAILABLE_ROLES}}` | Comma-separated list of all role directories |
| `{{INSTRUCTIONS}}` | Completion curl commands for all tasks |
| `{{SPECIALISTS}}` | Agency specialist agent list (non-empty for `manager` role only) |

Conditional blocks are supported:

```
{{#IF PROJECT_STATE}}
## Project context
{{PROJECT_STATE}}
{{/IF}}

{{#IF_NOT PROJECT_STATE}}
No project context available.
{{/IF_NOT}}
```

Unknown placeholders are left as-is; nested conditionals are not supported.

### task_prompt.md

This file contains per-task instructions. It uses a separate set of variables that are substituted per task:

| Variable | Value |
|---|---|
| `{{TASK_TITLE}}` | Task title |
| `{{TASK_DESCRIPTION}}` | Task description text |
| `{{TASK_ID}}` | Task ID (used in the completion curl command) |
| `{{FILES}}` | Newline-separated list of owned files (empty if none) |
| `{{CONTEXT}}` | Additional task context (empty if none) |

Use `{{#IF FILES}}` and `{{#IF CONTEXT}}` to make sections optional:

```markdown
{{#IF FILES}}
## Files to work with
{{FILES}}
{{/IF}}

{{#IF CONTEXT}}
## Context
{{CONTEXT}}
{{/IF}}
```

### config.yaml

Controls the default model and effort for this role:

```yaml
default_model: sonnet      # "opus" or "sonnet"
default_effort: high       # "max", "high", "normal", or "low"
max_tasks_per_session: 3   # integer; how many tasks this agent handles per spawn
```

The spawner reads `config.yaml` first; if present, it overrides the heuristic routing logic. `max_tasks_per_session` is read by the orchestrator to cap batch size per session.

### Minimal working example

Copy an existing role and customize it:

```bash
cp -r templates/roles/backend templates/roles/data-engineer
```

Then edit the three files:

**system_prompt.md** — change the persona:
```markdown
# You are a Data Engineer

You design and implement data pipelines, ETL jobs, and warehouse schemas.

## Your specialization
- Python (dbt, Airflow, Spark, Pandas)
- SQL (BigQuery, Snowflake, Postgres)
- Data modeling and schema design

## Current task
{{TASK_DESCRIPTION}}
```

**task_prompt.md** — keep the structure, adjust instructions:
```markdown
# Task: {{TASK_TITLE}}

## Description
{{TASK_DESCRIPTION}}

{{#IF FILES}}
## Files to work with
{{FILES}}
{{/IF}}

## Instructions
1. Read all listed files before writing any code
2. Prefer incremental models and idempotent transforms
3. Run pipeline tests before marking complete

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <what was implemented>"}'
```
```

**config.yaml**:
```yaml
default_model: sonnet
default_effort: high
max_tasks_per_session: 2
```

The new role is available immediately — no code changes required. Assign tasks to it with `"role": "data-engineer"` and Bernstein will use your template.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
