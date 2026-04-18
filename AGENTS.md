# AGENTS.md

## Mission

Bernstein is a **multi-agent orchestration platform** for CLI coding agents.
It is the Kubernetes of AI software engineering — spawn agents, assign tasks,
verify output, merge results, learn from failures, repeat. The goal is to build
the most reliable, observable, and effective orchestrator in the ecosystem.

Bernstein orchestrates SHORT-LIVED agents (1–3 tasks each, then exit). State
lives in FILES (`.sdd/`), not in agent memory. Agents are spawned fresh per
task — no "sleep" problem. The orchestrator itself is DETERMINISTIC CODE, not
an LLM. It works with ANY CLI agent (Claude Code, Codex, Gemini CLI, etc.).

**Stack:** Python 3.12+, Starlette/FastAPI task server, Textual TUI, git
worktree isolation, YAML-based task specs, JSONL metrics/traces.

## Doctrine

Optimize for: reliability, agent effectiveness, observability, safe iteration,
and user trust. Do not optimize for: clever abstractions nobody asked for,
premature generalization, architecture theater, or broad refactors without
measured gain.

### Engineering principles

1. **Smallest safe delta.** Isolate one change per commit. Preserve rollback
   clarity. If a change touches 5+ files, consider splitting.

2. **No monoliths.** Do not create or extend god-files. Split by capability.
   Triggers: >400 LOC (soft), >600 LOC (hard stop unless justified), mixed
   concerns, multiple reasons to change. The orchestrator is already split
   across `orchestrator.py`, `tick_pipeline.py`, `task_lifecycle.py`, and
   `agent_lifecycle.py` — follow this pattern.

3. **Structure.** Thin orchestration facade, isolated core logic, separate
   adapters, explicit schemas, prompt building separate from retrieval.

4. **OOP where useful, pure funcs where better.** Small classes for stateful
   collaborators; pure functions for deterministic transforms, parsing, scoring.
   Prefer composition over inheritance. Use Protocols/ABCs only at real seams.

5. **Strict typing.** No dict soup, loose `Any`, or silent `Optional` misuse
   in core paths. Type all public APIs. Use `TypedDict`/`@dataclass` for
   internal records, Pydantic only for FastAPI request/response boundaries.
   Pyright strict is mandatory for all touched code.

6. **Async for IO, sync for CPU.** No blocking sync IO in async paths. No
   fire-and-forget without owned lifecycle. Use explicit timeouts. Do not
   break SSE, streaming, or telemetry.

7. **Observability.** Preserve or improve logging, metrics, traces, token
   accounting, and agent signal files. No hidden globals, silent fallbacks,
   or unauditable magic.

8. **Performance.** Avoid repeated parsing, N+1 HTTP calls, needless
   serialization, duplicate work. Cache only when invalidation is safe.

9. **Testing.** Add the smallest deterministic tests proving the change works.
   No fake-green tests. Always mock the CLI adapter and HTTP calls. Use
   `tmp_path` for filesystem.

10. **YAGNI.** Don't build for hypothetical future requirements. Three similar
    lines is better than a premature abstraction.

### Change classification

| Class | What | Example |
|-------|------|---------|
| **A** | Tiny low-risk patch (1-2 files, <50 lines) | Fix typo, add log line, extract constant |
| **B** | Narrow feature or fix (2-5 files, <200 lines) | New quality gate, adapter fix, endpoint |
| **C** | Bounded refactor + logic (5-10 files) | Split module, new subsystem, drain system |
| **D** | Major feature branch | New TUI screen, protocol support, provider |
| **E** | Investigate / needs discussion | Conflict, unclear requirement, risky change |

For C and D changes: write a plan before coding. For A and B: just do it.

### Zero-tolerance failures

- Ignoring Pyright strict on touched code
- Leaving Ruff/pytest failures for "later"
- Blocking sync IO in async paths
- Breaking SSE, telemetry, or agent signal protocol
- Creating new god-files (>600 LOC)
- Untyped new core logic (`Any` soup)
- `pkill -f bernstein` or `pgrep bernstein` (use PID files)
- Running `pytest tests/` without the isolated runner
- Committing `.sdd/runtime/` contents
- Pushing to `master` (branch is `main`)

### Conflict protocol

If you discover conflicting behavior between code, docs, tests, or specs:

```
[CONFLICT DETECTED]
File(s): ...
Conflict: ...
Why it matters: ...
Smallest safe resolution: ...
```

Do not paper over conflicts. Report and resolve explicitly.

## Setup

```bash
uv venv && uv pip install -e ".[dev]"
```

## Testing

```bash
uv run python scripts/run_tests.py -x        # all tests (isolated per-file, stops on first failure)
uv run python scripts/run_tests.py -k router  # filter by keyword
uv run pytest tests/unit/test_foo.py -x -q    # single file (fast)
```

**NEVER run `uv run pytest tests/ -x -q`** — the full suite keeps references
across 2000+ tests and can leak 100+ GB RAM. The isolated runner in
`scripts/run_tests.py` caps each file at ~200 MB.

## Linting & type checking

```bash
uv run ruff check src/
uv run ruff format src/
uv run pyright src/
```

All three must pass before committing. No exceptions, no "fix later."

## Code style

- Python 3.12+, type hints on every public function and method
- Max line length: 120 (enforced by ruff)
- `from __future__ import annotations` at the top of every module
- Ruff rules: E, F, W, I, UP, B, SIM, TCH, RUF
- **No dict soup** — use `@dataclass` or `TypedDict`, not raw `dict[str, Any]`
- Enums over string literals for any value that has a fixed set of options
- Google-style docstrings on all public symbols
- Async only for IO-bound code; sync for CPU-bound/pure logic
- Concise inline comments only for non-obvious logic or dangerous edges

---

## Module map
<!-- AUTO-GENERATED: run `uv run python scripts/gen_agents_md.py --update` to refresh -->

### `src/bernstein/core/` — orchestration engine

| File                                                | Purpose |
|-----------------------------------------------------|---------|
| `models.py`                                         | Core data models for tasks, agents, and cells |
| `server.py`                                         | FastAPI task server — central coordination point for all agents |
| `orchestrator.py`                                   | Orchestrator loop: watch tasks, spawn agents, verify completion, repeat |
| `tick_pipeline.py`                                  | Tick pipeline helpers: task fetching, batching, and server interaction |
| `task_lifecycle.py`                                 | Task lifecycle: claim, spawn, complete, retry, decompose |
| `agent_lifecycle.py`                                | Agent lifecycle: tracking, heartbeat, crash detection, reaping |
| `spawner.py`                                        | Spawn short-lived CLI agents for task batches |
| `router.py`                                         | Route tasks to appropriate model and effort level with tier awareness |
| `janitor.py`                                        | Verify task completion via concrete signals |
| `context.py`                                        | Gather project context for the manager's planning prompt |
| `a2a.py`                                            | A2A (Agent-to-Agent) protocol support |
| `agency_loader.py`                                  | Load Agency agent personas as additional Bernstein role templates |
| `agent_discovery.py`                                | Auto-discover installed CLI coding agents, check login status, and register capabilities |
| `agent_signals.py`                                  | Agent signal file protocol: WAKEUP, SHUTDOWN, and HEARTBEAT |
| `api_usage.py`                                      | API usage tracking and metrics collection |
| `approval.py`                                       | Approval gates: configurable review step between janitor verification and merge |
| `batch_router.py`                                   | Batch API routing for non-urgent tasks |
| `bootstrap.py`                                      | Bootstrap orchestration: coordinate startup, task planning, and agent spawning |
| `bulletin.py`                                       | Append-only bulletin board for cross-agent communication |
| `ci_fix.py`                                         | CI self-healing: detect failing CI jobs and create fix tasks |
| `ci_log_parser.py`                                  | Generic CI log parser with adapter pattern |
| `cluster.py`                                        | Cluster coordination: node registration, heartbeats, topology management |
| `complexity_advisor.py`                             | Complexity Advisor: single-agent vs multi-agent mode selection |
| `cost.py`                                           | Intelligent cost optimization engine |
| `cost_history.py`                                   | Cost history persistence and alert logic |
| `cost_tracker.py`                                   | Per-run cost budget tracker |
| `cross_model_verifier.py`                           | Cross-model verification: route completed task diffs to a different model for review |
| `evolution.py`                                      | Backward-compatibility shim — delegates to bernstein.evolution package |
| `fast_path.py`                                      | Fast-path execution for trivial tasks that don't need an LLM agent |
| `file_discovery.py`                                 | File discovery and project context gathering |
| `file_locks.py`                                     | File-level locking for concurrent agent safety |
| `git_basic.py`                                      | Basic git operations: run, status, staging, committing |
| `git_context.py`                                    | Git read operations for building agent context |
| `git_ops.py`                                        | Centralized git write operations for Bernstein |
| `git_pr.py`                                         | Pull request and branching operations |
| `github.py`                                         | GitHub API integration for evolve coordination |
| `graph.py`                                          | Task dependency graph with critical-path and parallelism analysis |
| `guardrails.py`                                     | Output guardrails: secret detection, scope enforcement, dangerous operations |
| `heartbeat.py`                                      | Agent heartbeat and stall detection |
| `hijacker.py`                                       | Automatic tier hijacking — detects and routes to free tier opportunities |
| `home.py`                                           | Global ~/.bernstein home directory management |
| `knowledge_base.py`                                 | Knowledge base, file indexing, and task context enrichment |
| `lessons.py`                                        | Agent lesson propagation system |
| `llm.py`                                            | Async native LLM client for Bernstein manager and external models |
| `manager.py`                                        | Manager Intelligence — LLM-powered task decomposition and review |
| `manager_models.py`                                 | Manager result types and data models |
| `manager_parsing.py`                                | Manager LLM response parsing |
| `manager_prompts.py`                                | Manager prompt templates and rendering |
| `mcp_manager.py`                                    | MCP server lifecycle manager |
| `mcp_registry.py`                                   | MCP server auto-discovery and per-task configuration |
| `merge_queue.py`                                    | FIFO merge queue for serialized branch merging with conflict routing |
| `metric_collector.py`                               | Metrics collection and recording |
| `metric_export.py`                                  | Metrics export and reporting functionality |
| `metrics.py`                                        | Performance metrics collection and storage (facade) |
| `multi_cell.py`                                     | Multi-cell orchestrator: coordinates multiple cells, each with its own manager + workers |
| `notifications.py`                                  | Webhook notification system for Bernstein run events |
| `policy.py`                                         | Policy engine for tier optimization and provider routing |
| `pr_size_governor.py`                               | PR Size Governor — auto-split large agent PRs into reviewable chunks |
| `preflight.py`                                      | Pre-flight checks: validate CLI, API key, port availability before bootstrap |
| `prometheus.py`                                     | Prometheus metrics for Bernstein |
| `prompt_caching.py`                                 | Prompt caching orchestration for token savings via prefix detection |
| `quality_gates.py`                                  | Automated quality gates: lint, type-check, and test gates after task completion |
| `quarantine.py`                                     | Cross-run task quarantine — track repeatedly-failing tasks across Bernstein runs |
| `rag.py`                                            | Lightweight codebase RAG using SQLite FTS5 (BM25 ranking) |
| `rate_limit_tracker.py`                             | Rate-limit-aware scheduling: per-provider throttle tracking and 429 detection |
| `researcher.py`                                     | Web research module for evolve mode |
| `retrospective.py`                                  | Run retrospective report generation |
| `rule_enforcer.py`                                  | Organizational rule enforcement: load .bernstein/rules.yaml, check violations |
| `seed.py`                                           | Seed file parser for bernstein.yaml |
| `server_launch.py`                                  | Server and spawner lifecycle: startup, health checks, task injection, cleanup |
| `session.py`                                        | Session state persistence for fast resume after bernstein stop/restart |
| `signals.py`                                        | Pivot signal system for strategic re-evaluation of tickets |
| `store.py` / `store_redis.py` / `store_postgres.py` | Abstract TaskStore base class for pluggable storage backends |
| `store_factory.py`                                  | Storage backend factory for the Bernstein task server |
| `sync.py`                                           | Sync .sdd/backlog/*.yaml files with the task server |
| `task_store.py`                                     | Thread-safe in-memory task store with JSONL persistence |
| `token_monitor.py`                                  | Token growth monitor with auto-intervention |
| `traces.py`                                         | Agent execution trace storage, parsing, and replay utilities |
| `upgrade_executor.py`                               | Autonomous upgrade executor with transaction-like safety and rollback |
| `worker.py`                                         | bernstein-worker: visible process wrapper for spawned CLI agents |
| `workspace.py`                                      | Multi-repo workspace orchestration |
| `worktree.py`                                       | WorktreeManager — git worktree lifecycle for agent session isolation |

**Modules added after initial map** (in alphabetical order):

| File | Purpose |
|------|---------|
| `auth.py` | SSO / SAML / OIDC authentication for the Bernstein task server |
| `auth_middleware.py` | Authentication middleware for the Bernstein task server |
| `cascade_router.py` | Cost-aware model cascading router |
| `circuit_breaker.py` | Real-time circuit breaker for purpose enforcement |
| `context_degradation_detector.py` | Monitor agent quality over time; restart when degraded |
| `graduation.py` | Pilot-to-production graduation framework |
| `plan_approval.py` | Plan mode: pre-execution cost estimation and human approval |
| `planner.py` | Task planning: LLM-powered goal decomposition and replan |
| `repo_index.py` | Repository intelligence index — lightweight code graph for agent context |
| `reviewer.py` | Task review: LLM-powered completion review and queue correction |
| `semantic_cache.py` | Semantic caching layer for LLM requests |
| `semantic_graph.py` | Semantic code graph — symbol-level dependency graph for context routing |
| `benchmark_gate.py` | Benchmark regression gate — block merge when performance degrades |
| `cost_anomaly.py` | Cost anomaly detection with Z-score signaling |
| `log_redact.py` | PII redaction filter for Python logging |
| `loop_detector.py` | Agent loop and file-lock deadlock detection |
| `spawn_prompt.py` | Prompt rendering utilities for agent spawning |
| `task_completion.py` | Task completion, retry, and post-completion processing |
| `trigger_manager.py` | Event-driven trigger manager — evaluates incoming events against user-defined rules |
| `trigger_sources/` | Trigger source adapters: `github.py`, `slack.py`, `file_watch.py`, `webhook.py` |

### `src/bernstein/core/routes/` — FastAPI router modules

| File | Purpose |
|------|---------|
| `agents.py` | Agent inspection routes — logs, kill signals, and SSE output streams |
| `auth.py` | Authentication routes for SSO / SAML / OIDC flows (OIDC, SAML, device flow, session) |
| `costs.py` | Cost budget routes |
| `dashboard.py` | Dashboard routes — file lock inspection |
| `graduation.py` | Graduation framework routes — stage inspection, event recording, and promotion |
| `plans.py` | Plan approval routes — list, view, approve, and reject execution plans |
| `quality.py` | Quality metrics routes — success rate, token usage, p50/p90/p99 completion times |
| `slack.py` | Slack webhook routes — slash command and Events API endpoints |
| `status.py` | Status, health, metrics, dashboard, and SSE event routes |
| `tasks.py` | Task CRUD routes, agent heartbeats, bulletin board, A2A, cluster, session streaming |
| `webhooks.py` | Inbound webhook routes for external event ingestion |

### `src/bernstein/adapters/` — CLI agent adapters

| File                 | Purpose |
|----------------------|---------|
| `aider.py`           | Aider CLI adapter |
| `amp.py`             | Amp CLI adapter |
| `base.py`            | Base adapter for CLI coding agents |
| `caching_adapter.py` | Caching wrapper for CLI adapters to enable prompt prefix deduplication |
| `claude.py`          | Claude Code CLI adapter |
| `codex.py`           | OpenAI Codex CLI adapter |
| `env_isolation.py`   | Environment variable isolation for spawned agents |
| `gemini.py`          | Google Gemini CLI adapter |
| `generic.py`         | Generic CLI adapter for arbitrary coding agent CLIs |
| `manager.py`         | Manager adapter — spawns the internal Python ManagerAgent as a CLI participant |
| `qwen.py`            | Qwen CLI adapter for OpenAI compatible models |
| `registry.py`        | Adapter registry — look up CLI adapters by name |
| `roo_code.py`        | Roo Code CLI adapter |
| `cody.py`            | Sourcegraph Cody CLI adapter |
| `continue_dev.py`    | Continue.dev CLI adapter |
| `cursor.py`          | Cursor CLI adapter |
| `goose.py`           | Goose CLI adapter |
| `kilo.py`            | Kilo Code CLI adapter |
| `kiro.py`            | Kiro CLI adapter |
| `ollama.py`          | Ollama local model CLI adapter |
| `opencode.py`        | OpenCode CLI adapter |
| `tabby.py`           | Tabby CLI adapter |
| `claude_agents.py`   | Claude Agents SDK adapter |
| `iac.py`             | Infrastructure-as-Code adapter |
| `mock.py`            | Mock adapter for testing |
| `skills_injector.py` | Skills injection middleware for adapters |
| `conformance.py`     | Adapter conformance test suite |
| `ci/`                | CI system adapters for log parsing and failure extraction (github_actions.py) |

### `src/bernstein/agents/` — agent catalog & discovery

| File                 | Purpose |
|----------------------|---------|
| `agency_provider.py` | AgencyProvider — loads CatalogAgent instances from msitarzewski/agency-agents format |
| `catalog.py`         | Agent catalog registry — loads agent definitions from external sources |
| `discovery.py`       | Agent directory auto-discovery for Bernstein |
| `registry.py`        | Dynamic agent registry with YAML-based definitions and hot-reload support |

### `src/bernstein/cli/` — Click CLI

| File                    | Purpose |
|-------------------------|---------|
| `advanced_cmd.py`       | Advanced tools and utilities for Bernstein CLI |
| `agents_cmd.py`         | Agent catalog management commands: sync, list, validate, showcase, match, discover |
| `cost.py`               | Bernstein cost — spend visibility across all recorded metrics |
| `dashboard.py`          | Bernstein TUI -- retro-futuristic agent orchestration dashboard |
| `errors.py`             | Structured error reporting for Bernstein CLI |
| `eval_benchmark_cmd.py` | Evaluation and benchmarking commands for Bernstein CLI |
| `evolve_cmd.py`         | Evolution commands: evolve run/review/approve/status/export |
| `helpers.py`            | Shared constants, helpers, and utilities for Bernstein CLI modules |
| `live.py`               | Live view helpers for ``bernstein live --classic`` |
| `main.py`               | CLI entry point for Bernstein -- multi-agent orchestration |
| `run.py`                | Enhanced run output for ``bernstein run`` |
| `run_cmd.py`            | Run commands: init, conduct, downbeat (legacy start), and the main CLI group |
| `status.py`             | Formatted status output for ``bernstein status`` |
| `status_cmd.py`         | Status and diagnostic commands: status, ps, doctor |
| `stop_cmd.py`           | Stop commands: soft/hard stop, shutdown signals, session save, ticket recovery |
| `task_cmd.py`           | Task lifecycle commands for Bernstein CLI |
| `ui.py`                 | Shared Rich UI components for Bernstein CLI |
| `workspace_cmd.py`      | Workspace and configuration commands for Bernstein CLI |

### `src/bernstein/evolution/` — self-evolution engine

| File                 | Purpose |
|----------------------|---------|
| `aggregator.py`      | Metrics aggregation with EWMA, CUSUM, BOCPD, and Goodhart defenses |
| `applicator.py`      | Change applicator — execute upgrades via file modification |
| `benchmark.py`       | Tiered benchmark runner for evolution validation |
| `circuit.py`         | CircuitBreaker — halt evolution when safety conditions are violated |
| `creative.py`        | Creative evolution pipeline — visionary → analyst → production gate |
| `cycle_runner.py`    | Evolution cycle execution engine |
| `detector.py`        | Opportunity detection from aggregated metrics |
| `gate.py`            | ApprovalGate and EvalGate — risk-stratified routing for evolution proposals |
| `governance.py`      | Adaptive governance for the evolution system |
| `invariants.py`      | InvariantsGuard — hash-lock safety-critical files |
| `loop.py`            | Autoresearch evolution loop — continuous self-improvement via experiment cycles |
| `proposal_scorer.py` | Proposal risk scoring and routing classification |
| `proposals.py`       | Upgrade proposal generation |
| `report.py`          | Evolution observability — history table and static report generation |
| `risk.py`            | Strategic Risk Score (SRS) computation for evolution proposals |
| `sandbox.py`         | SandboxValidator — isolated testing of evolution proposals |
| `types.py`           | Shared types for the evolution system |

### `src/bernstein/eval/` — evaluation harness

| File                 | Purpose |
|----------------------|---------|
| `baseline.py`        | Baseline tracking for eval-gated evolution |
| `golden.py`          | Golden benchmark suite — curated tasks for eval |
| `harness.py`         | Eval harness — multiplicative scoring, LLM judge, failure taxonomy |
| `judge.py`           | LLM judge — evaluate code quality of agent-produced changes |
| `metrics.py`         | Custom eval metrics — each metric is a dataclass with a compute method |
| `scenario_runner.py` | Scenario runner — execute YAML-defined eval scenarios against the live codebase |
| `taxonomy.py`        | Failure taxonomy — classify every eval failure into a closed set |
| `telemetry.py`       | Telemetry contract — strict schema for agent output metadata |

### `src/bernstein/plugins/` — plugin system (pluggy)

| File           | Purpose |
|----------------|---------|
| `hookspecs.py` | Hook specifications — defines extension points for Bernstein plugins |
| `manager.py`   | Plugin manager — discovers, loads, and invokes Bernstein plugins |

### `src/bernstein/tui/` — Textual TUI

| File         | Purpose |
|--------------|---------|
| `app.py`     | Main Textual application for the Bernstein TUI session manager |
| `widgets.py` | Custom Textual widgets for the Bernstein TUI |

### `src/bernstein/github_app/` — GitHub App integration

| File           | Purpose |
|----------------|---------|
| `app.py`       | GitHub App authentication: JWT creation and installation token exchange |
| `ci_router.py` | CI failure routing: blame attribution and enriched fix-task generation |
| `mapper.py`    | Event-to-task conversion: maps GitHub webhook events to Bernstein task payloads |
| `webhooks.py`  | Webhook parsing and HMAC-SHA256 signature verification |

### `src/bernstein/mcp/` — MCP server

| File        | Purpose |
|-------------|---------|
| `server.py` | Bernstein MCP server |

### `src/bernstein/benchmark/` — SWE-bench

| File           | Purpose |
|----------------|---------|
| `swe_bench.py` | SWE-Bench evaluation harness for Bernstein |

### Key non-package directories

| Path                     | Purpose |
|--------------------------|---------|
| `templates/roles/`         | Jinja2 role prompts (manager, backend, qa, security, devops, etc.) |
| `templates/prompts/`       | Prompt templates (judge.md, etc.) — bundled into wheel |
| `.sdd/`                    | All runtime state (never commit `.sdd/runtime/`) |
| `.sdd/backlog/open/`       | YAML task specs waiting to be picked up |
| `.sdd/backlog/claimed/`    | Tasks currently being worked |
| `.sdd/backlog/done/`       | Completed tasks (automated sync moves files here) |
| `.sdd/backlog/closed/`     | Completed tasks (manual sprint scripts move files here) |
| `.sdd/runtime/`            | PIDs, logs, session state, signal files |
| `.sdd/metrics/`            | JSONL metric records |
| `.sdd/traces/`             | JSONL agent traces |
| `.sdd/agents/catalog.json` | Registered agent catalog |
| `tests/unit/`              | Fast unit tests (no network) |
| `tests/integration/`       | Integration tests (require running server) |
| `scripts/run_tests.py`     | Per-file isolated test runner |

---

## Naming conventions

### Files
- `snake_case.py` for all Python modules
- Test files: `test_<module_name>.py` mirrors source structure
- Backlog task files: `p{priority}_c{complexity}_{date}_{type}_{slug}.yaml`
- Role templates: `<role-name>.md` or `<role-name>/` directory

### Classes
- PascalCase: `TaskGraph`, `AgentSpawner`, `TierAwareRouter`
- Enums: PascalCase name, SCREAMING_SNAKE members: `TaskStatus.IN_PROGRESS`
- Dataclasses preferred over Pydantic models in core; Pydantic only for FastAPI request/response

### Functions & methods
- `snake_case`, verbs: `spawn_for_tasks()`, `verify_task()`, `build_worker_cmd()`
- Private helpers: leading underscore `_read_cached()`, `_render_prompt()`
- Async functions: prefix with nothing special, but always `async def` and awaited correctly
- Module-level helpers that accept the orchestrator as explicit arg (not `self`): free functions in `task_lifecycle.py` / `agent_lifecycle.py`

### Variables & constants
- `snake_case` for variables
- `SCREAMING_SNAKE` for module-level constants: `MAX_JUDGE_RETRIES`, `JUDGE_MODEL`
- Private module-level caches: `_FILE_CACHE`, `_DIR_CACHE`

### Task IDs
- Short hex string: `16e2d84f94aa` (12 hex chars from `uuid.uuid4().hex[:12]`)

### Agent session IDs
- Full UUID4: `str(uuid.uuid4())`

### Roles
- Lowercase hyphenated: `backend`, `qa`, `security`, `devops`, `docs`, `frontend`, `architect`, `manager`

---

## Test patterns

### File structure
```python
"""Tests for <module> — <what is mocked>."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

# --- Fixtures ---

@pytest.fixture()
def my_thing(tmp_path):
    ...

# --- TestClassName ---

class TestMyThing:
    def test_happy_path(self, ...) -> None:
        ...

    def test_failure_case(self, ...) -> None:
        ...
```

### Async tests
```python
import pytest

@pytest.mark.asyncio
async def test_something(client: AsyncClient) -> None:
    resp = await client.post("/tasks", json={...})
    assert resp.status_code == 200
```

Use `httpx.ASGITransport` + `AsyncClient` against the FastAPI app directly — no real network:
```python
from httpx import ASGITransport, AsyncClient
from bernstein.core.server import create_app

@pytest.fixture()
async def client(tmp_path):
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

### Shared fixtures (from `tests/conftest.py`)
- `make_task()` — factory for `Task` with defaults; override only what matters
- `mock_adapter_factory(pid=42)` — returns a `MagicMock(spec=CLIAdapter)` with `.spawn()` returning `SpawnResult`
- `sdd_dir(tmp_path)` — temp `.sdd/` with standard subdirectories
- `_memory_guard` (autouse) — forces GC after every test; aborts if RSS > 2 GB

### Mocking rules
- **Always mock the CLI adapter** in spawner/orchestrator tests — never shell out for real
- **Always mock httpx calls** in orchestrator tests — use `unittest.mock.patch` or inject fake responses
- **Real filesystem via `tmp_path`** — never mock `Path` or file I/O when `tmp_path` works
- **No database** — state is files; use `tmp_path` for `.sdd/`

### Class-based tests
Group related cases: `class TestSpawnForTasks:`, `class TestProviderType:`. Each method is one scenario.

---

## Known gotchas

### Memory: never run the full test suite in one process
`pytest tests/` will leak memory across 2000+ test files and can hit 100 GB. Always use:
```bash
uv run python scripts/run_tests.py -x
```
The script runs each `test_*.py` file in a fresh subprocess.

### Process management: PID files, never pgrep/pkill
Bernstein writes PID metadata JSON files to `.sdd/runtime/pids/`. Use those to find and stop processes. Never `pkill -f bernstein` or `pgrep bernstein` — it will kill the orchestrator indiscriminately.

```bash
# Correct: signal via file
echo "stop" > .sdd/runtime/signals/<role>-<session>/SHUTDOWN

# Correct: use bernstein CLI
bernstein stop

# WRONG: grep-kill
pkill -f bernstein   # kills everything including your own shell session if bernstein is in the path
```

### `evolution.py` is a shim
`src/bernstein/core/evolution.py` is a backward-compat re-export shim. The real implementation lives in `src/bernstein/evolution/`. Don't add code to the shim — extend the package.

### Orchestrator split across three files
`orchestrator.py` is the public façade. The actual logic is split:
- `tick_pipeline.py` — data containers and task fetching
- `task_lifecycle.py` — claim/spawn/complete/retry
- `agent_lifecycle.py` — heartbeat/crash/reap

If you're editing orchestration behavior, read all three before touching any one.

### Manager split across three sub-modules
`manager.py` is the public façade for the LLM-powered Manager. The logic is split:
- `manager_models.py` — `ReviewResult`, `QueueCorrection`, `QueueReviewResult` dataclasses
- `manager_parsing.py` — JSON response parsing from LLM calls
- `manager_prompts.py` — prompt template loading and rendering

`manager.py` imports from all three and exposes `ManagerAgent`. Don't add models/parsing/prompts to `manager.py` itself — extend the relevant sub-module.

### `from __future__ import annotations` is mandatory
All modules use this for forward references and PEP 604 union syntax. Without it, type annotations that reference yet-to-be-defined classes fail at import.

### File-based state survives restarts; runtime state does not
`.sdd/backlog/` and `.sdd/metrics/` persist across restarts and are git-friendly. `.sdd/runtime/` contains ephemeral PIDs, logs, and signal files — never commit it. The server flushes tasks to `.sdd/runtime/tasks.jsonl` but that's only a recovery checkpoint.

### Task IDs are 12-char hex, not UUID4
```python
task_id = uuid.uuid4().hex[:12]  # "16e2d84f94aa"
```
Don't use full UUIDs for task IDs — the server, backlog filenames, and trace files all expect the short form.

### `Task` uses optimistic locking (`version` field)
Every `POST /tasks/{id}/complete` or `/fail` increments `task.version`. If two agents try to complete the same task, the second call gets a 409. Build your agent completion code to handle 409 gracefully.

### Adapters must use `build_worker_cmd()` for process visibility
All adapter `.spawn()` implementations must wrap the CLI command with `build_worker_cmd()` from `adapters/base.py`. This sets the process title and writes the PID metadata file that the orchestrator uses for `bernstein ps` and crash detection.

### `pytest-asyncio` mode
The project uses `pytest-asyncio`. Async tests need `@pytest.mark.asyncio`. Async fixtures need `@pytest_asyncio.fixture()` (not plain `@pytest.fixture()`).

### Ruff `TCH` rules require `TYPE_CHECKING` guards
Any import used only for type annotations must be under `if TYPE_CHECKING:`. Ruff will flag imports that can be moved there. This is enforced in CI.

### Role templates are Jinja2, not plain strings
Files in `templates/roles/` are Jinja2 templates. The `TemplateRenderer` in `templates/renderer.py` resolves them. When adding a new role, create `templates/roles/<role>.md` and register it in the role catalog.

### `.sdd/backlog/claimed/` is the source of truth during execution
When an agent starts, the task file moves from `open/` → `claimed/`. On success the automated sync system moves it to `done/`. If you find tasks stuck in `claimed/`, the agent likely crashed — run janitor cleanup or use `bernstein gc`. Note: manual sprint scripts may move completed tickets to `closed/` instead — both directories are checked by cleanup commands.

### Rule enforcement runs after quality gates — `.bernstein/rules.yaml` is optional
`rule_enforcer.py` reads `.bernstein/rules.yaml` from the working directory (not `.sdd/`). If the file is absent, enforcement is silently skipped — no error. `error`-severity violations hard-block merge; `warning` violations are soft-flags only. Violations are appended to `.sdd/metrics/rule_violations.jsonl`.

### Agent lessons are tag-matched and decay over time
`lessons.py` stores lessons in `.sdd/memory/lessons.jsonl`. Retrieval is by tag overlap with the current task — not vector search. Confidence decays exponentially over time. The same lesson filed twice from different agents raises its confidence rather than creating a duplicate.

### Prompt caching keys are SHA-256 hashes of the system prefix
`prompt_caching.py` deduplicates system prompts by hashing the role prompt + shared context. If you change a role template or context, the cache key changes automatically. Cache hits are logged to `.sdd/caching/`. The `CachingAdapter` wrapper in `adapters/caching_adapter.py` applies this transparently to any adapter.

### `ComplexityAdvisor` gates single vs. multi-agent mode
`core/complexity_advisor.py` inspects task `owned_files` and cross-file dependency scores to choose `ComplexityMode.SINGLE` or `ComplexityMode.MULTI`. Tasks routed to `SINGLE` skip spawning sub-agents. This fires before the spawner — if you see tasks not fanning out, check the advisor output first.

### Default branch is `main`
Never push to or create a branch named `master`. PRs target `main`. The git config enforces this via CI.

### `planner.py` / `plan_approval.py` — plan mode is opt-in
When `plan_mode` is enabled in orchestrator config, the planner decomposes goals into `PLANNED`-status tasks and holds them for human approval via `POST /plans/{id}/approve`. Tasks stay frozen until approved — agents will not pick them up. Approval routes are in `routes/plans.py`.

### `trigger_manager.py` reads `.bernstein/triggers.yaml`
Event-driven triggers are configured in `.bernstein/triggers.yaml` (not `.sdd/`). The `TriggerManager` evaluates incoming `TriggerEvent` objects against configured rules and creates tasks when rules match. Trigger sources (`trigger_sources/`) normalize raw events (GitHub webhooks, Slack events, file-system changes, generic HTTP webhooks) into `TriggerEvent` before evaluation.

### `repo_index.py` caches its graph for 30 minutes
`get_or_build_graph()` persists the code graph to `.sdd/index/codebase.db`. The cache expires after 30 minutes by default. If you need a fresh graph after a large refactor, delete the cache file or call `build_repo_graph()` directly. The graph is used by `semantic_graph.py` for symbol-level context routing.

### `cascade_router.py` vs `router.py`
`router.py` is tier-aware model selection (which model, which tier). `cascade_router.py` is cost-aware cascading (try cheap model first, escalate on failure/low confidence). They are separate concerns — don't conflate them. `cascade_router.py` wraps `router.py` output.

### `circuit_breaker.py` halts misbehaving agents
The circuit breaker monitors agent output for purpose violations. When it fires, it sends a `SHUTDOWN` signal to the offending agent and marks the task `failed`. Check `.sdd/runtime/signals/<role>-<session>/SHUTDOWN` if an agent exits unexpectedly.

### `graduation.py` is the pilot-to-production gate
`graduation.py` stages work through configurable promotion stages (e.g. pilot → staging → production). Stage transitions fire events recorded via `POST /graduation/events`. The graduation routes are at `routes/graduation.py`.

### `reviewer.py` is separate from `janitor.py`
`janitor.py` verifies task completion via concrete signals (file exists, tests pass). `reviewer.py` uses an LLM to review the quality of what was produced and can push corrections back into the queue. Both run post-task, in that order.

### `loop_detector.py` runs inside the orchestrator tick
`check_loops_and_deadlocks()` in `agent_lifecycle.py` polls file modification times each tick. When the same agent edits the same file more than `LOOP_EDIT_THRESHOLD` times within `LOOP_WINDOW_SECONDS`, the agent is killed. Deadlock detection builds a wait-for graph from `FileLockManager` and breaks cycles by releasing the oldest lock holder.

### `log_redact.py` is installed globally at bootstrap
`install_pii_filter()` is called in `bootstrap.py` and attaches to the root logger. All log handlers (file, console, structured) receive sanitised messages — emails, phone numbers, SSNs, and credit card numbers are replaced with `[REDACTED]`.

### `cost_anomaly.py` signals are acted on in `task_completion.py`
After task completion, cost data is checked against historical Z-scores. `AnomalySignal.LOG` just logs, `AnomalySignal.PAUSE_SPAWNING` stops new agent spawning, and `AnomalySignal.KILL_AGENT` terminates the expensive agent.

---

## Strategic context

Bernstein is an **open-source project** aiming to become the standard
orchestrator for AI coding agents. Key competitive advantages to protect:

1. **Agent-agnostic** — works with any CLI agent, not locked to one vendor
2. **Deterministic orchestrator** — scheduling is code, not LLM (predictable, auditable)
3. **File-based state** — `.sdd/` is git-friendly, inspectable, recoverable
4. **Self-evolving** — Bernstein develops itself via `bernstein evolve`
5. **Enterprise-ready** — approval gates, audit trails, cost tracking, compliance

When making decisions, ask: does this make Bernstein more reliable for users
who trust it with their codebase? Does this make agents more effective at
completing tasks? Does this make the system more observable when things go wrong?

### Architecture invariants (do not violate)

- The orchestrator is deterministic code. No LLM in the scheduling loop.
- Agents are short-lived. No persistent agent processes.
- State lives in `.sdd/` files. No hidden in-memory-only state.
- Every agent runs in a git worktree. Main branch is never dirty.
- Task completion is verified by concrete signals, not trust.
- Git branch is `main`. Never `master`.

### What makes a good contribution

- Fixes a real failure mode observed in production
- Improves agent success rate (fewer retries, better prompts)
- Improves observability (better logs, metrics, traces)
- Reduces cost (smarter model selection, caching, batching)
- Reduces time-to-completion (parallelism, fast path, scheduling)
- Has tests proving it works
- Is small enough to review in 5 minutes

### What does NOT make a good contribution

- Refactoring that doesn't fix a bug or enable a feature
- Adding abstractions for one caller
- Config options nobody asked for
- "Improving" code style in files you didn't otherwise touch
- Architecture changes without a design doc

## Commit & PR instructions

- Branch from `main`
- Title: imperative mood ("Add X", "Fix Y", "Refactor Z")
- Run `uv run ruff check src/ && uv run pyright src/ && uv run python scripts/run_tests.py -x` before committing
- One logical change per PR/commit
- Mark task complete on the task server when done:
  ```bash
  curl -s -X POST http://127.0.0.1:8052/tasks/<id>/complete \
    -H "Content-Type: application/json" \
    -d '{"result_summary": "Done: <description>"}'
  ```

## What to work on

Check `.sdd/backlog/open/` for YAML task specs. Each file has a role, priority,
and description. Take tasks matching your role. Use `bernstein status` to see
what's running. Prioritize by priority field (1=critical, 2=normal, 3=nice-to-have). Note: ticket filenames use a 0-based prefix (p0/p1/p2/p3/p4) but the task server normalises priority to 1–3 on ingestion.

When picking tasks: prefer tasks where you can make measurable progress in
15-30 minutes. If a task seems too large, decompose it into subtasks. If a
task is blocked by another task, skip it and take the next one.
