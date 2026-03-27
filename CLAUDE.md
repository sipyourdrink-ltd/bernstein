# Bernstein — Development Instructions

You are working on Bernstein, a multi-agent orchestration system for CLI coding agents.

## Project philosophy
- Bernstein orchestrates SHORT-LIVED agents (1-3 tasks each, then exit)
- State lives in FILES (.sdd/), not in agent memory
- Agents are spawned fresh per task — no "sleep" problem
- Model and effort are chosen per-task based on complexity
- The system should work with ANY CLI agent (Claude Code, Codex, Gemini CLI, etc.)
- The orchestrator is DETERMINISTIC CODE, not an LLM — no LLM-based scheduling

## Architecture
- `src/bernstein/` — Python package (3.12+, hatchling build)
- `src/bernstein/core/` — task server, spawner, orchestrator, janitor, evolution, routes/, agent_discovery, quality_gates, token_monitor
- `src/bernstein/adapters/` — CLI agent adapters (claude, codex, gemini, qwen, aider, amp, roo_code, generic)
- `src/bernstein/cli/` — CLI entry points (run_cmd, stop_cmd, status_cmd, agents_cmd, evolve_cmd, advanced_cmd, workspace_cmd, etc.)
- `templates/roles/` — role system prompts (manager, backend, qa, security, etc.)
- `templates/prompts/` — prompt templates for planning and review
- `.sdd/` — file-based state (backlog, runtime, metrics, config)

## Task server API (http://127.0.0.1:8052)
- POST /tasks — create task
- GET /tasks?status=open — list tasks by status
- POST /tasks/{id}/complete — mark task done
- POST /tasks/{id}/fail — mark task failed
- GET /status — dashboard summary

## Self-evolving
This project develops itself. Run `bernstein run` in this directory to spawn
agents that read the codebase, plan improvements, and execute them.

## Git rules
- Default branch is `main`. NEVER push to or create a branch called `master`.
- When pushing, always use `main`: `git push origin main`
- PRs target `main` as base branch

## Coding standards
- Python 3.12+, strict typing (Pyright strict)
- Ruff for linting, pytest for tests
- Google-style docstrings
- No dict soup — use dataclasses/TypedDict
- Async where IO-bound, sync where CPU-bound
- Run tests: `uv run python scripts/run_tests.py -x` (isolated per-file, prevents memory leaks)
- Run single file: `uv run pytest tests/unit/test_foo.py -x -q`
- NEVER run `uv run pytest tests/ -x -q` — leaks 100+ GB RAM across 2000+ tests
