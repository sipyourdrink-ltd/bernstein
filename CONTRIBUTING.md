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

## Writing a Custom Role

Role templates let you define new specialist agent types (e.g., `data-engineer`, `ml-ops`, `dba`). Each role lives in its own directory under `templates/roles/` and consists of three files.

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

By contributing, you agree that your contributions will be licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
