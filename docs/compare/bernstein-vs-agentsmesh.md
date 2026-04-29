# Bernstein vs. AgentsMesh

> **tl;dr** — AgentsMesh is a self-hosted AI workforce platform: a multi-tenant web console, a Postgres-backed task board, and remote "AgentPods" running CLI agents in PTY sandboxes. It targets organizations giving every team member an AI agent squad. Bernstein is a single-process orchestrator that turns a goal into tasks, runs them in parallel through a deterministic Python scheduler, verifies output with a janitor, and merges working branches. They overlap on "run multiple CLI agents at once" and diverge on almost everything else.

*Last verified: 2026-04-27. Based on `github.com/AgentsMesh/AgentsMesh` (1.8k stars, v0.29.0, repo created 2026-02-28) and agentsmesh.ai.*

---

## What each tool is

**AgentsMesh** (1.8k stars, BSL-1.1 until 2030, Go backend + Next.js frontend) is a self-hosted "AI Agent Workforce Platform." A central control plane (PostgreSQL, Redis, MinIO) manages organizations, teams, and tickets. Self-hosted runners spawn "AgentPods" — long-lived PTY sandboxes that run a CLI agent (Claude Code, Codex CLI, Gemini CLI, Aider, OpenCode, or a custom terminal agent). Operators bind tickets to pods from a Kanban board, watch I/O stream through a WebSocket relay, and review the resulting MRs/PRs. The pitch is enterprise-shaped: multi-tenant org hierarchy with row-level isolation, SSO, RBAC, audit logs, air-gapped deployment, BYOK for API keys.

**Bernstein** (Apache 2.0, Python 3.12+) is a CLI-native orchestrator for coding agents. It breaks a goal into tasks via a deterministic Python scheduler, spawns short-lived agents in isolated git worktrees, verifies their output (tests, lint, type checks, file presence), and merges the results. State lives in `.sdd/` files on disk, inspectable from the shell. It ships 31 cooperating CLI adapters plus 2 leaf-node delegation adapters, an MCP server, plan files with `depends_on` between stages, cost tracking with budget caps, and a `--evolve` mode for self-improvement runs.

---

## Feature comparison

| Feature | Bernstein | AgentsMesh |
|---|---|---|
| **Primary focus** | Ship code from a CLI | Run an AI workforce from a web console |
| **License** | Apache 2.0 | BSL-1.1 (GPL-2.0-or-later in 2030) |
| **Language / runtime** | Python 3.12+ (hatchling) | Go backend + TypeScript/Next.js frontend |
| **Install path** | `pip install bernstein` / `uv` | `curl ... \| sh`, deb/rpm, Docker |
| **Required infrastructure** | None (single process) | PostgreSQL + Redis + MinIO + Relay cluster |
| **CLI agent integrations** | 31 cooperating + 2 delegation adapters | 5 — Claude Code, Codex, Gemini, Aider, OpenCode (+ custom) |
| **Orchestrator logic** | Deterministic Python scheduler | Manual ticket-to-pod binding, queue-based |
| **LLM tokens for coordination** | Zero | Zero (no LLM "manager"; humans assign) |
| **Agent lifetime** | Short-lived per task | Long-lived AgentPods |
| **Sandbox model** | Git worktree per agent | PTY sandbox per pod |
| **State storage** | `.sdd/` files on local disk | PostgreSQL + Redis + object storage |
| **Plan files** | YAML with `stages` + `steps` + `depends_on` | Not documented |
| **Quality gates** | Janitor: tests, lint, types, file checks | Not documented |
| **Cost tracking** | Per-task tracking + global budget cap | BYOK; user-managed |
| **MCP server** | First-class | Not documented |
| **A2A protocol** | Yes | Not documented |
| **Multi-tenant orgs / RBAC / SSO** | No | Yes — core feature |
| **Web console** | Web dashboard + TUI (`bernstein live`) | Web console is the primary UI |
| **Self-evolution** | `bernstein --evolve` | Not documented |
| **Headless / CI** | Yes — runs in CI, over SSH, in containers | Web-console-first; CI integration not documented |
| **Audit log** | HMAC-chained, file-based | Enterprise audit logs (mechanism not documented) |
| **Per-step model/effort routing** | Yes (cascade router, `cli:` field per step in PR #965) | Pod is bound to one agent type |
| **Cloud execution** | Cloudflare Workers adapter | Self-hosted by design |
| **Multi-repo orchestration** | Yes | Per-pod git worktree, no documented multi-repo plan |

---

## Architecture comparison

**AgentsMesh (workforce platform):**
```
Web console (Next.js)
    │
    ▼
Control plane (Go, gRPC + mTLS)
    │
    ├── PostgreSQL  (org, team, tickets, pod lifecycle)
    ├── Redis       (queues, presence)
    └── MinIO       (artifacts)
    │
    ▼
Relay cluster (WebSocket pub/sub for I/O)
    │
    ▼
Self-hosted runner(s)
    │
    ├── AgentPod #1 — PTY sandbox running Claude Code
    ├── AgentPod #2 — PTY sandbox running Codex
    └── AgentPod #3 — PTY sandbox running Aider
```

The unit of work is a long-lived AgentPod. An operator (or a teammate via the Kanban board) binds a ticket to a pod, watches the terminal stream, and reviews the MR/PR the pod opens. Coordination is human-in-the-loop through a web UI.

**Bernstein (CLI orchestrator):**
```
bernstein run plans/feature.yaml   (terminal, CI, SSH)
    │
    ▼
Task server (local FastAPI, deterministic Python scheduler)
    │
    ├── Task A → claude  (worktree A) → janitor → merge
    ├── Task B → codex   (worktree B) → janitor → merge
    └── Task C → gemini  (worktree C) → janitor → merge

State: .sdd/ files (backlog, runtime, metrics, config)
```

The unit of work is a short-lived task. Agents are spawned per task, verified by the janitor, and exited. Coordination is deterministic code reading and writing files on disk.

---

## When AgentsMesh is the right tool

**You need a workforce control plane, not a CLI tool.** AgentsMesh's core value is the org-shaped layer above the agent: tenants, teams, RBAC, SSO, audit logs, ticket boards, MR/PR integration. If your problem is "give 40 engineers their own AI squad and see what each pod is doing in real time," AgentsMesh ships a working product for that. Bernstein doesn't try to.

**You want long-lived interactive sandboxes.** AgentPods are persistent PTY sandboxes you can attach to from the browser. That maps to "park an agent on a problem, walk away, check on it." Bernstein agents are short-lived processes — once a task ships or fails, they're gone.

**You're deploying enterprise-style.** Air-gapped install, mTLS between runner and control plane, row-level multi-tenancy, BYOK, SSO. These are first-class. Bernstein has none of them — it's a single-user CLI by default.

**Moonshot-adjacent / actively developed.** v0.29.0 was published April 17, 2026; the repo has 56 releases since February 2026. Steady release cadence and a hosted SaaS at agentsmesh.ai.

**Five-agent integration is enough.** If you only run Claude Code, Codex, Gemini, Aider, and OpenCode, AgentsMesh covers the common case out of the box.

---

## When Bernstein is the right tool

**Adapter breadth.** 31 cooperating adapters plus 2 leaf-node delegation adapters (Composio, Ralphex shipping in PR #966) versus AgentsMesh's five built-ins. If you need Amp, Cursor, Cody, Continue, Goose, Kilo, Kiro, Qwen, Ollama, OpenCode, IaC, or a generic adapter alongside Claude/Codex/Gemini, Bernstein already wires them in. AgentsMesh's "custom terminal-based agent" hook covers this in principle but doesn't ship the integrations.

**Deterministic scheduler with reproducible runs.** Bernstein's orchestrator is Python code, not a hosted service. Ticks, retries, fair scheduling, and cost decisions are all in-process and replayable from the WAL. Audit logs are HMAC-chained on disk. No relay cluster, no Postgres, no Redis.

**File-based state inspectable from the shell.** `.sdd/backlog.json`, `.sdd/runtime/`, `.sdd/metrics/`, `.sdd/config/` — `cat`, `jq`, `grep`. No SQL queries to write to answer "what is task 47 doing." AgentsMesh keeps state in PostgreSQL behind the control plane.

**Plan files with dependencies.** `templates/plan.yaml` describes multi-stage projects with `stages`, `steps`, `depends_on`, per-step `goal`, `role`, `priority`, `scope`, `complexity`, and the new per-step `cli:` field landing in PR #965. AgentsMesh's Kanban model is task-by-task; declarative dependency graphs are not documented.

**MCP server first-class.** Bernstein exposes its task server as an MCP server, so other Claude Code / Codex agents can drive it directly. AgentsMesh's README does not mention MCP.

**Quality gates before merge.** The janitor runs tests, lint, type checks, and file-existence checks before any agent's work is merged. AgentsMesh provides MR/PR integration but doesn't document automated pre-merge verification.

**Headless and `--evolve`.** Runs in CI, over SSH, in a container. `bernstein --evolve` reads past run metrics and improves prompts and routing. AgentsMesh is web-console-first; headless and self-improvement loops aren't part of its surface.

**Cost tracking with budgets.** Per-task cost tracking, anomaly detection, and global budget enforcement at the orchestrator. AgentsMesh's cost story is BYOK — you bring keys and absorb costs yourself.

**Single-process install.** `pip install bernstein` and you're running. AgentsMesh requires PostgreSQL, Redis, MinIO, a relay cluster, and a runner before it does anything.

---

## How to evaluate which one you need

Three questions usually settle it:

1. **Is the unit of work a ticket assigned to a teammate's pod, or a task pulled by a scheduler?** First answer points at AgentsMesh. Second points at Bernstein.
2. **Do you need multi-tenant org/RBAC/SSO from day one?** If yes, AgentsMesh ships it. Bernstein doesn't.
3. **Is your usage shape "CI runs Bernstein on a goal, agents merge code, humans review the PR"?** That's Bernstein's primary loop. AgentsMesh works in this loop too but its architecture targets persistent attended pods.

The tools could coexist. AgentsMesh could own the workforce layer (who has access to which pod, where logs live, what the audit trail says) while Bernstein runs inside a pod as the actual scheduler — turning a single AgentPod into a 31-adapter parallel orchestrator with a janitor and plan files. Nothing in either project prevents that.

---

## See also

- [Bernstein vs. Paperclip](./bernstein-vs-paperclip.md)
- [Full comparison index](./README.md)
