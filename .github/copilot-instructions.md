# Copilot Instructions

This is Bernstein — a multi-agent orchestrator for CLI coding agents (Claude Code, Codex, Gemini CLI, Qwen).

## Key constraints

- The orchestrator/scheduler must be deterministic Python. Never add LLM calls for coordination or scheduling decisions.
- Agents are short-lived: spawn per task batch, execute, exit. Do not create long-running agent processes.
- All runtime state lives in `.sdd/` as files (JSONL, YAML, Markdown). No databases.
- Use Pydantic models for all data structures.
- Type hints on all public functions. `pyright` strict mode must pass.

## Before committing

```bash
uv run ruff check src/
uv run pyright src/
uv run python scripts/run_tests.py -x
```

## Architecture

- `src/bernstein/core/server.py` — FastAPI task server (HTTP API on :8052)
- `src/bernstein/core/orchestrator.py` — deterministic scheduling loop
- `src/bernstein/core/spawner.py` — launches CLI agents
- `src/bernstein/core/janitor.py` — verifies task completion via signals
- `src/bernstein/adapters/` — pluggable CLI adapters (inherit from `CLIAdapter` ABC)
- `src/bernstein/evolution/` — self-evolution with safety gates
