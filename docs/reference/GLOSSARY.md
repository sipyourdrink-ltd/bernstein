# Glossary

Bernstein-specific terms used throughout the codebase and documentation.

---

### Bulletin Board

An append-only communication channel where agents post findings, blockers, and status updates visible to all other agents in the same run. Implemented in `src/bernstein/core/bulletin.py`.

### Caching Adapter

A wrapper adapter that intercepts spawn calls to enable prompt prefix deduplication and response reuse. Delegates actual execution to the underlying adapter while tracking cache break events across agents. Implemented in `src/bernstein/adapters/caching_adapter.py`.

### Circuit Breaker

A state machine (CLOSED → OPEN → HALF_OPEN) that prevents infinite retry loops when an agent or provider repeatedly fails. After N consecutive failures, the breaker "opens" and blocks further attempts until a recovery probe succeeds. Implemented in `src/bernstein/core/circuit_breaker.py`.

### Conformance Harness

A testing framework that replays golden transcripts against live adapters (with mocked subprocesses) to detect protocol drift and adapter regressions. Implemented in `src/bernstein/adapters/conformance.py`.

### Debug Bundle

A diagnostic archive containing logs, state files, configuration, and runtime metadata collected via `bernstein debug` for troubleshooting. Implemented in `src/bernstein/core/observability/debug_bundle.py`.

### Drain

Stop accepting new work and wait for active agents to finish their current tasks. Used during graceful shutdown or rolling upgrades. Implemented in `src/bernstein/core/drain.py`.

### Fast Path

An optimization that skips full planning for simple, single-file tasks. Instead of decomposing into subtasks, the agent handles the work directly. Implemented in `src/bernstein/core/fast_path.py`.

### Janitor

The verification system that checks whether an agent's work is correct — runs lint, type-checks, tests, and other quality gates before accepting work. Implemented in `src/bernstein/core/janitor.py`.

### Env Isolation

The process of filtering environment variables before spawning agents to prevent credential leakage. Only variables required for the agent's function are passed through. Implemented in `src/bernstein/adapters/env_isolation.py`.

### Nudge

A message sent to a stalled agent to prompt it to continue working. Part of the heartbeat and idle detection system. Implemented in `src/bernstein/core/nudge_manager.py`.

### Peak-Hour Router

A cost-aware scheduling component that routes tasks to cheaper providers or defers non-urgent work during peak pricing hours. Implemented in `src/bernstein/core/cost/peak_hour_router.py`.

### Protocol Negotiation

Runtime handshake that determines which protocol version (MCP, A2A, ACP) a connected client or agent supports, ensuring compatibility is verified at connection time rather than at failure time. Implemented in `src/bernstein/core/protocols/protocol_negotiation.py`.

### Quality Gate

Automated checks (lint, type-check, tests, coverage) that must pass before work is accepted or merged. Gates run in sequence and any failure blocks the pipeline. Implemented in `src/bernstein/core/quality_gates.py`.

### Reap

Killing or collecting agents that have exceeded their timeout or become unresponsive. Part of the agent lifecycle management. Implemented in `src/bernstein/core/agent_lifecycle.py`.

### SDD

Software-Defined Development — the `.sdd/` directory where all runtime state lives: worktrees, sessions, task logs, and agent data. Initialized in `src/bernstein/core/bootstrap.py`.

### Schema Registry

A versioned catalog of message schemas for MCP, A2A, and ACP protocols, enabling forward/backward compatibility checks and migration paths. Implemented in `src/bernstein/core/protocols/schema_registry.py`.

### Skills Injector

Writes role-specific Claude Code skills (`.claude/skills/*.md`) into the agent's worktree before spawn. This moves orchestration boilerplate into skills that survive context compaction, reducing prompt size by 30-40%. Implemented in `src/bernstein/adapters/skills_injector.py`.

### Spawn

Creating a short-lived agent process for a task batch. The spawner handles prompt construction, worktree setup, and process management. Implemented in `src/bernstein/core/spawner.py`.

### Tick

The orchestrator's polling cycle (approximately 3 seconds). Each tick fetches pending tasks, spawns agents, checks heartbeats, and evaluates quality gates. Implemented in `src/bernstein/core/orchestrator.py`.

### Worktree

An isolated git worktree per agent, located at `.sdd/worktrees/{session_id}`. Each agent works in its own branch without interfering with others. Implemented in `src/bernstein/core/worktree.py`.

---

### ACP Bridge

The ACP (Agent Client Protocol) adapter that lets ACP-aware editors (e.g. Zed) use Bernstein as their multi-agent backend over stdio or HTTP. Implemented in `src/bernstein/core/protocols/acp/`. See `bernstein acp serve --stdio | --http :PORT`.

### Autofix Daemon

A persistent background process that watches Bernstein-opened PRs for CI failures, pulls the failure logs, and dispatches a scoped repair agent. Caps at three attempts per PR and labels each attempt in the audit log. Implemented in `src/bernstein/core/autofix/`. CLI: `bernstein autofix {start|stop|status|attach}`.

### Credential Vault

OS-keychain-backed token store for provider credentials (GitHub, OpenAI, Anthropic, etc.). Agents receive scoped credentials at spawn time without touching `.env` files. Implemented in `src/bernstein/core/security/vault/`. CLI: `bernstein connect <provider>`, `bernstein creds {list|revoke|test}`.

### Fleet Dashboard

A unified view of all Bernstein orchestrator instances reachable on the current host or configured server. Useful for monitoring parallel sessions and CI fleets. Implemented in `src/bernstein/core/fleet/`. CLI: `bernstein fleet [--web HOST:PORT]`.

### MCP Catalog

A community-maintained index of installable MCP servers. Bernstein can browse, search, and install entries without leaving the terminal. Schema: `docs/reference/mcp-catalog-schema.json`. Implemented in `src/bernstein/core/protocols/mcp_catalog/`. CLI: `bernstein mcp catalog {browse|search|install}`.

### Review Pipeline DSL

A YAML format for expressing multi-phase quality-review flows (lint, type-check, security scan, etc.) that run sequentially before a task is accepted. Starter templates live in `templates/review/*.yaml`. Implemented in `src/bernstein/core/quality/review_pipeline/`. CLI: `bernstein review --pipeline review.yaml`.
