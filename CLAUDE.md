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
- `src/bernstein/core/` — organized into 22 sub-packages:
  - `orchestration/` — orchestrator lifecycle, tick pipeline, manager, evolution, drain, shutdown, bootstrap
  - `agents/` — spawner, agent discovery, heartbeat, idle detection, reaping, recycling, warm pool
  - `tasks/` — task store, lifecycle, retry, completion, batch mode, dead letter queue, fair scheduler
  - `quality/` — quality gates, CI monitor, janitor, cross-model verifier
  - `server/` — task server, API endpoints, middleware
  - `cost/` — cost tracking, anomaly detection, budget enforcement
  - `tokens/` — token monitoring, growth detection, auto-intervention
  - `security/` — HMAC audit logs, policy engine, PII gating
  - `config/` — configuration loading, defaults, validation
  - `observability/` — Prometheus metrics, OTel exporter, Grafana dashboards
  - `protocols/` — MCP server mode, A2A protocol support, protocol negotiation
  - `git/` — worktree management, merge queue, branch operations
  - `persistence/` — WAL crash recovery, file-based state, checkpointing
  - `planning/` — plan loading, task decomposition, dependency resolution
  - `routing/` — model/effort selection, cascade router
  - `communication/` — bulletin board, cross-agent messaging
  - `knowledge/` — knowledge graph, codebase impact analysis
  - `plugins_core/` — pluggy-based plugin system
  - `routes/` — HTTP route handlers
  - `memory/` — persistent memory stores (SQLite + vector cache)
  - `trigger_sources/` — external trigger integrations
  - `grpc_gen/` — generated gRPC stubs
  - Back-compat: `from bernstein.core.<old> import X` works via a `sys.meta_path` finder in `core/__init__.py` (`_CoreRedirectFinder`, `_REDIRECT_MAP`). The finder covers legacy names like `orchestrator.py`, `spawner.py`, `task_lifecycle.py`, etc. Top-level `.py` files outside sub-packages: `defaults.py`, `credential_scoping.py`, `example_gallery.py`, `prompt_optimizer.py`, `streaming_merge.py`. WARNING: new aliases MUST be added to `_REDIRECT_MAP` in `src/bernstein/core/__init__.py`; creating physical shim files will shadow the finder.
  - `defaults.py` — 150+ configurable constants
  - `credential_scoping.py` — per-agent credential scoping
  - `example_gallery.py` — example task/plan gallery
  - `prompt_optimizer.py` — prompt optimization helpers
  - `streaming_merge.py` — streaming merge utility
- `src/bernstein/adapters/` — 17 CLI agent adapters (aider, amp, claude, cloudflare, cody, codex, continue_dev, cursor, gemini, goose, iac, kilo, kiro, ollama, opencode, qwen, generic)
- `src/bernstein/cli/` — CLI entry points, decomposed into `commands/` sub-package (run_cmd, stop_cmd, status_cmd, agents_cmd, evolve_cmd, advanced_cmd, debug_cmd, etc.)
- `templates/roles/` — role system prompts (manager, vp, backend, frontend, qa, security, devops, architect, docs, reviewer, ml-engineer, prompt-engineer, retrieval, visionary, analyst, resolver, ci-fixer)
- `templates/prompts/` — prompt templates for planning and review
- `templates/plan.yaml` — project plan template
- `.sdd/` — file-based state (backlog, runtime, metrics, config)

## Plan Files (YAML)
- Describe multi-step projects with `stages` and `steps`
- Stages can have `depends_on: [stage_name]`
- Steps can have `goal`, `role`, `priority`, `scope`, `complexity`
- Execute with: `bernstein run plans/my-project.yaml`
- Skips LLM planning, deterministic task injection

## Task server API (http://127.0.0.1:8052)
- POST /tasks — create task
- GET /tasks?status=open — list tasks by status
- POST /tasks/{id}/complete — mark task done
- POST /tasks/{id}/fail — mark task failed
- POST /tasks/{id}/progress — report progress (files_changed, tests_passing, errors)
- POST /bulletin — post cross-agent finding/blocker
- GET /bulletin?since={ts} — read recent bulletins
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
