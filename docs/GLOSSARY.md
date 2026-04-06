# Glossary

Bernstein-specific terms used throughout the codebase and documentation.

---

### Bulletin Board

An append-only communication channel where agents post findings, blockers, and status updates visible to all other agents in the same run. Implemented in `src/bernstein/core/bulletin.py`.

### Circuit Breaker

A state machine (CLOSED → OPEN → HALF_OPEN) that prevents infinite retry loops when an agent or provider repeatedly fails. After N consecutive failures, the breaker "opens" and blocks further attempts until a recovery probe succeeds. Implemented in `src/bernstein/core/circuit_breaker.py`.

### Drain

Stop accepting new work and wait for active agents to finish their current tasks. Used during graceful shutdown or rolling upgrades. Implemented in `src/bernstein/core/drain.py`.

### Fast Path

An optimization that skips full planning for simple, single-file tasks. Instead of decomposing into subtasks, the agent handles the work directly. Implemented in `src/bernstein/core/fast_path.py`.

### Janitor

The verification system that checks whether an agent's work is correct — runs lint, type-checks, tests, and other quality gates before accepting work. Implemented in `src/bernstein/core/janitor.py`.

### Nudge

A message sent to a stalled agent to prompt it to continue working. Part of the heartbeat and idle detection system. Implemented in `src/bernstein/core/nudge_manager.py`.

### Quality Gate

Automated checks (lint, type-check, tests, coverage) that must pass before work is accepted or merged. Gates run in sequence and any failure blocks the pipeline. Implemented in `src/bernstein/core/quality_gates.py`.

### Reap

Killing or collecting agents that have exceeded their timeout or become unresponsive. Part of the agent lifecycle management. Implemented in `src/bernstein/core/agent_lifecycle.py`.

### SDD

Software-Defined Development — the `.sdd/` directory where all runtime state lives: worktrees, sessions, task logs, and agent data. Initialized in `src/bernstein/core/bootstrap.py`.

### Spawn

Creating a short-lived agent process for a task batch. The spawner handles prompt construction, worktree setup, and process management. Implemented in `src/bernstein/core/spawner.py`.

### Tick

The orchestrator's polling cycle (approximately 3 seconds). Each tick fetches pending tasks, spawns agents, checks heartbeats, and evaluates quality gates. Implemented in `src/bernstein/core/orchestrator.py`.

### Worktree

An isolated git worktree per agent, located at `.sdd/worktrees/{session_id}`. Each agent works in its own branch without interfering with others. Implemented in `src/bernstein/core/worktree.py`.
