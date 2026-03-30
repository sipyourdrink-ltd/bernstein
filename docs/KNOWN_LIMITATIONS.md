# Known Limitations

Bernstein is in active development. This page documents known constraints, their practical impact, and our plans to address them. We believe honest documentation of limitations is more valuable than pretending they don't exist.

---

## Agent Communication Latency

**What:** Claude Code agents receive commands in sub-second via stdin pipe IPC. Other agents (Codex, Gemini, Qwen, Aider) still use file-based signal polling with up to 60-second delay.

**Impact:** Non-Claude agents may not respond to broadcast commands or stop signals for up to a minute.

**Workaround:** Press **Q** in the TUI dashboard — this performs a graceful shutdown that tries stdin pipe first, then file-based signals, then SIGTERM after 5 seconds. Alternatively, run `bernstein stop` from another terminal. For immediate termination, use `bernstein stop --force`.

**Note:** Pressing Q no longer silently exits. It stops all agents before quitting. Pressing S (double-tap) performs an immediate hard stop.

**Plan:** Extend stdin pipe IPC to Codex (via `--pipe` flag) and other adapters that support stdin communication.

---

## Single-Machine Orchestration

**What:** Bernstein currently runs on a single machine. All agents, the task server, and the orchestrator share one host.

**Impact:** Parallelism is bounded by local CPU, memory, and API rate limits. For large-scale orchestration (50+ tasks), a single machine may bottleneck.

**Workaround:** Increase `--max-agents` cautiously (default: 6). Monitor system resources. Most workflows complete well within single-machine capacity.

**Plan:** Distributed cluster mode is designed (see `docs/superpowers/plans/2026-03-28-distributed-cluster-mode.md`). Multi-node orchestration with worker heartbeats and task stealing is planned for a future release.

---

## Rate Limit Detection is Reactive

**What:** Bernstein detects rate limits by scanning agent logs for 429 patterns *after* the agent fails. It cannot predict rate limits before they occur.

**Impact:** The first task on a rate-limited provider will fail before the system cascades to alternatives. One task's worth of time and tokens is lost.

**Workaround:** Set conservative `--budget` limits. The cascade fallback system (D00) automatically reassigns subsequent tasks to other available agents.

**Plan:** Proactive rate limit querying is difficult because CLI agents don't expose remaining quota. We're investigating adapter-level quota detection via provider APIs where available.

---

## Context Window Boundaries

**What:** Each agent spawns fresh with no memory of previous tasks. Context is limited to the system prompt, task description, and relevant file content.

**Impact:** Agents cannot reference decisions made in earlier tasks. Related tasks may produce inconsistent approaches if not explicitly coordinated via task descriptions.

**Workaround:** Write detailed task descriptions that include relevant context. Use the `depends_on` field to order tasks so later agents see earlier commits.

**Plan:** Context compression engine and cross-session knowledge propagation are planned (see roadmap items in `.sdd/backlog/open/`).

---

## No Real-Time Progress Streaming from Agents

**What:** Agent progress is observed via log files, not real-time streaming. The TUI dashboard reads logs on a polling interval.

**Impact:** There may be a brief delay between an agent completing work and the dashboard reflecting it.

**Workaround:** Use `bernstein logs -f <task-id>` for near-real-time log tailing.

**Plan:** Agent heartbeat frequency improvements and event-driven TUI updates are in the roadmap.

---

## Verification Depends on Test Quality

**What:** The janitor verifies agent output by running tests and linting. If the project has no tests or poor test coverage, verification is weaker.

**Impact:** On projects with low test coverage, the janitor may mark tasks as "done" even when the implementation has subtle bugs.

**Workaround:** Ensure your project has reasonable test coverage before running Bernstein on complex tasks. Consider adding a `completion_signals` section to your `bernstein.yaml` with specific verification commands.

**Plan:** Mutation testing validation (roadmap P0 item 6) will verify that tests actually catch bugs, not just achieve coverage numbers.

---

## File-Based State Storage

**What:** All state lives in `.sdd/` directory as flat files (JSON, JSONL, YAML). There is no database.

**Impact:** Concurrent access from multiple processes is coordinated via file locks but is less robust than a proper database. Very large backlogs (1000+ tasks) may experience slower reads.

**Workaround:** This is a deliberate design choice (see `docs/the-bernstein-way.md`, tenet: "Files over databases"). For most projects, file-based state is simpler, more portable, and requires no infrastructure.

**Plan:** For Bernstein Cloud (hosted SaaS), a database backend will be added. Self-hosted Bernstein will continue to use file-based storage as the default, with an optional database adapter.

---

## Provider-Specific Adapter Quirks

**What:** Each CLI agent (Claude, Codex, Cursor, Gemini, Qwen, Aider) has different capabilities, flags, output formats, and error behaviors.

**Impact:** Some features work better with certain agents. For example, Claude Code supports stream-json output; Codex has native sandbox mode; Gemini has the largest context window but different error formatting.

**Workaround:** Check `bernstein doctor` for adapter-specific diagnostics. Consult `docs/adapters.html` for per-adapter capabilities.

**Plan:** Adapter normalization is ongoing. Each release improves parity across adapters. The `CLIAdapter` interface (`src/bernstein/adapters/base.py`) defines the contract that all adapters must meet.

---

## Budget Estimation is Approximate

**What:** Cost estimates before execution are based on token count projections, which can vary significantly from actual usage.

**Impact:** The `bernstein cost estimate` command and execution plan cost projections may be 2-3x off from actual costs, especially for complex tasks where agents iterate.

**Workaround:** Always set a `--budget` cap. The budget enforcement is *exact* — it tracks actual spend and stops at the limit.

**Plan:** Improved cost prediction using historical task data and ML-based estimation (roadmap item N65).

---

*Last updated: 2026-03-30. This document is updated with each release.*
