# Glossary

Bernstein-specific terms used throughout the codebase and documentation.

---

### Bulletin Board

An append-only communication channel where agents post findings, blockers, and status updates visible to all other agents in the same run. Implemented in `src/bernstein/core/communication/bulletin.py`.

### CAS Store

A content-addressed store that deduplicates artifact content by SHA-256 hash. When two agents emit identical output (e.g., the same generated file), only one copy is persisted on disk and both runs reference the same blob. Backs the workspace-sync optimization in `bridges/r2_sync.py` and the local artifact pool. Implemented in `src/bernstein/core/persistence/cas_store.py`.

### Cascade Router

The cost-aware, bandit-driven model escalator that picks the cheapest viable Claude tier for a given task and escalates on failure. Default chain is `sonnet -> opus` (the earlier `haiku` tier was retired). Escalation triggers on (1) explicit task failure, (2) janitor verification rejection, or (3) low-confidence regex scan over the agent's last 2000 chars (e.g., "I'm not sure", "TODO: escalate"). Per-(role, model) success rates persist to `.sdd/metrics/bandit_state.json`; chain reports to `.sdd/metrics/cascade_chains.jsonl`. Implemented in `src/bernstein/core/routing/cascade_router.py`.

### Cross-Model Verifier

An optional quality-pipeline component that re-runs a completed task on a second model and compares outputs to detect plausible-but-wrong code. Off by default; doubles cost for the verified subset. Used in conjunction with the janitor when correctness matters more than budget. Implemented in `src/bernstein/core/quality/cross_model_verifier.py`.

### Caching Adapter

A wrapper adapter that intercepts spawn calls to enable prompt prefix deduplication and response reuse. Delegates actual execution to the underlying adapter while tracking cache break events across agents. Implemented in `src/bernstein/adapters/caching_adapter.py`.

### Circuit Breaker

A state machine (CLOSED → OPEN → HALF_OPEN) that prevents infinite retry loops when an agent or provider repeatedly fails. After N consecutive failures, the breaker "opens" and blocks further attempts until a recovery probe succeeds. Implemented in `src/bernstein/core/observability/circuit_breaker.py`.

### Conformance Harness

A testing framework that replays golden transcripts against live adapters (with mocked subprocesses) to detect protocol drift and adapter regressions. Implemented in `src/bernstein/adapters/conformance.py`.

### Debug Bundle

A diagnostic archive containing logs, state files, configuration, and runtime metadata collected via `bernstein debug` for troubleshooting. Implemented in `src/bernstein/core/observability/debug_bundle.py`.

### Drain

Stop accepting new work and wait for active agents to finish their current tasks. Used during graceful shutdown or rolling upgrades. Implemented in `src/bernstein/core/orchestration/drain.py`.

### Fast Path

An optimization that skips full planning for simple, single-file tasks. Instead of decomposing into subtasks, the agent handles the work directly. Implemented in `src/bernstein/core/quality/fast_path.py`.

### Lethal Trifecta

The structural shape of a prompt-injection exfiltration: an execution path
that simultaneously accesses **private data**, ingests **untrusted input**,
and can **externally communicate**. Bernstein's capability matrix tags
every tool/MCP server/adapter with which of the three it carries and
refuses any spawn whose tool chain unions all three. Engine-layer check,
runs in the spawner before any agent process starts, bypass-immune in the
policy graph. See [docs/security/lethal-trifecta.md](../security/lethal-trifecta.md)
for the threat model and default capability table; implementation in
`src/bernstein/core/security/capability_matrix.py`.

### Janitor

The verification system that checks whether an agent's work is correct - runs lint, type-checks, tests, and other quality gates before accepting work. Distinct from the **Cross-Model Verifier** (which double-checks output by re-running on a second model) and the **Reviewer** (which performs LLM-based review). Implemented in `src/bernstein/core/quality/janitor.py`.

The janitor also has a maintenance role: it periodically reaps orphaned worktrees and stale agent state. The cleanup interval is governed by `janitor.worktree_cleanup_interval_s` and `janitor.max_orphan_age_s` in `bernstein.yaml`.

### Env Isolation

The process of filtering environment variables before spawning agents to prevent credential leakage. Only variables required for the agent's function are passed through. Implemented in `src/bernstein/adapters/env_isolation.py`.

### Nudge

A message sent to a stalled agent to prompt it to continue working. Part of the heartbeat and idle detection system. Implemented in `src/bernstein/core/orchestration/nudge_manager.py`.

### Peak-Hour Router

A cost-aware scheduling component that routes tasks to cheaper providers or defers non-urgent work during peak pricing hours. Implemented in `src/bernstein/core/cost/peak_hour_router.py`.

### Protocol Negotiation

Runtime handshake that determines which protocol version (MCP, A2A, ACP) a connected client or agent supports, ensuring compatibility is verified at connection time rather than at failure time. Implemented in `src/bernstein/core/protocols/protocol_negotiation.py`.

### Quality Gate

Automated checks (lint, type-check, tests, coverage) that must pass before work is accepted or merged. Gates run in sequence and any failure blocks the pipeline. Implemented in `src/bernstein/core/quality/quality_gates.py`.

### Reap

Killing or collecting agents that have exceeded their timeout or become unresponsive. Part of the agent lifecycle management. Implemented in `src/bernstein/core/agents/agent_lifecycle.py`.

### SDD

Software-Defined Development - the `.sdd/` directory where all runtime state lives: worktrees, sessions, task logs, and agent data. Initialized in `src/bernstein/core/orchestration/bootstrap.py`.

### Schema Registry

A versioned catalog of message schemas for MCP, A2A, and ACP protocols, enabling forward/backward compatibility checks and migration paths. Implemented in `src/bernstein/core/protocols/schema_registry.py`.

### Skills Injector

Writes role-specific Claude Code skills (`.claude/skills/*.md`) into the agent's worktree before spawn. This moves orchestration boilerplate into skills that survive context compaction, reducing prompt size by 30-40%. Implemented in `src/bernstein/adapters/skills_injector.py`.

### Spawn

Creating a short-lived agent process for a task batch. The spawner handles prompt construction, worktree setup, and process management. Implemented in `src/bernstein/core/agents/spawner.py`.

### Tick

The orchestrator's polling cycle (approximately 3 seconds). Each tick fetches pending tasks, spawns agents, checks heartbeats, and evaluates quality gates. Implemented in `src/bernstein/core/orchestration/orchestrator.py`.

### WAL

Write-Ahead Log -- the durable journal of state transitions used for crash recovery. Every task or agent state change is appended to `.sdd/wal/wal.jsonl` *before* being applied to in-memory state. On startup, `wal_replay.py` walks any incomplete entries and re-applies them so the orchestrator picks up exactly where it stopped. Combined with the **CAS Store** and Merkle integrity hashing, this gives Bernstein process-crash, host-reboot, and partial-merge survival without an external database. Implemented in `src/bernstein/core/persistence/wal.py` (writer) and `src/bernstein/core/persistence/wal_replay.py` (replay). The fsync policy is configurable via `wal.fsync` in `bernstein.yaml`.

### Warm Pool

A pre-spawn pool of agent processes kept idle so newly-claimed tasks see lower spawn latency. Instead of fork-and-init on every task, the orchestrator hands a queued process its prompt and the agent is already past CLI startup costs. Sized for typical concurrency; can be disabled if RAM is constrained (each pre-spawned process holds its baseline working set). Implemented in `src/bernstein/core/agents/warm_pool.py` and `src/bernstein/core/agents/spawner_warm_pool.py`.

### Worktree

An isolated git worktree per agent, located at `.sdd/worktrees/{session_id}`. Each agent works in its own branch without interfering with others. Implemented in `src/bernstein/core/git/worktree.py`.

---

### ACP Bridge

The ACP (Agent Client Protocol) adapter that lets ACP-aware editors (e.g. Zed) use Bernstein as their multi-agent backend over stdio or HTTP. Implemented in `src/bernstein/core/protocols/acp/`. See `bernstein acp serve --stdio | --http :PORT`.

### Autofix Daemon

A persistent background process that watches Bernstein-opened PRs for CI failures, pulls the failure logs, and dispatches a scoped repair agent. Caps at three attempts per PR and labels each attempt in the audit log. Implemented in `src/bernstein/core/autofix/`. CLI: `bernstein autofix {start|stop|status|attach}`.

### Audit Chain

The HMAC-SHA256-chained, append-only JSONL log under `.sdd/audit/`. Every orchestrator action is signed with a key concatenated with the prior entry's HMAC, so editing any line invalidates every following signature. Key lives outside `.sdd/` (XDG state default) so a writer of the log can't read or rotate the key. Implemented in `src/bernstein/core/security/audit.py`. Operator guide: [`docs/security/audit-log.md`](../security/audit-log.md). CLI: `bernstein audit {show|verify|seal|query|export}`.

### Credential Vault

OS-keychain-backed token store for provider credentials (GitHub, OpenAI, Anthropic, etc.). Agents receive scoped credentials at spawn time without touching `.env` files. Implemented in `src/bernstein/core/security/vault/`. CLI: `bernstein connect <provider>`, `bernstein creds {list|revoke|test}`.

### Fleet Dashboard

A unified view of all Bernstein orchestrator instances reachable on the current host or configured server. Useful for monitoring parallel sessions and CI fleets. Implemented in `src/bernstein/core/fleet/`. CLI: `bernstein fleet [--web HOST:PORT]`.

### MCP Catalog

A community-maintained index of installable MCP servers. Bernstein can browse, search, and install entries without leaving the terminal. Schema: `docs/reference/mcp-catalog-schema.json`. Implemented in `src/bernstein/core/protocols/mcp_catalog/`. CLI: `bernstein mcp catalog {browse|search|install}`.

### Review Pipeline DSL

A YAML format for expressing multi-phase quality-review flows (lint, type-check, security scan, etc.) that run sequentially before a task is accepted. Starter templates live in `templates/review/*.yaml`. Implemented in `src/bernstein/core/quality/review_pipeline/`. CLI: `bernstein review --pipeline review.yaml`.
