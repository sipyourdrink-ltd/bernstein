# Bernstein — Comparison Pages

How Bernstein compares to other tools in the multi-agent coding space.

*Last verified: 2026-04-19*

---

## Table A — Python agent frameworks

| Feature | Bernstein | CrewAI | AutoGen | LangGraph |
|---|---|---|---|---|
| Orchestrator | Deterministic code | LLM-driven | LLM-driven (maintenance mode) | Graph + LLM |
| Works with | Any CLI agent (31 adapters) | Python SDK classes | Python agents | LangChain nodes |
| Git isolation | Worktrees per agent | No | No | No |
| Pluggable sandboxes | Worktree, Docker, E2B, Modal | No | No | No |
| Per-task verification | Janitor + quality gates | Test harness only | No | Conditional edges |
| Cost tracking | Built-in | External (Langfuse/AgentOps) | External (AgentOps) | External (LangSmith) |
| State model | File-based (`.sdd/`) | In-memory + SQLite checkpoint | In-memory | SQLite/Postgres checkpoint |
| Remote artifact sinks | S3, GCS, Azure Blob, R2 | No | No | No |
| Self-evolution | Built-in (`--evolve`) | No | No | No |
| Declarative plans | YAML with `depends_on` | YAML (agents + tasks) | Partial (Studio JSON) | Code or JSON config |
| Model routing per task | Bandit router | Per-agent | Per-agent | Manual per-node |
| MCP support | Client + server | Client (since 1.0) | Client (McpWorkbench) | Client + server (Platform) |
| Agent-to-agent chat | Bulletin board | Yes | Yes | No |
| Web UI | TUI + web dashboard | Yes (AMP) | Yes (Studio) | Yes (Studio + LangSmith) |
| Cloud hosted | Yes (Cloudflare) | Yes (AMP) | Via Microsoft Agent Framework | Yes (LangSmith Deployment) |
| Built-in RAG | Yes (codebase FTS5 + BM25) | Yes | Yes | Yes |

*Last verified: 2026-04-19. AutoGen entered maintenance mode in 2025; successor is Microsoft Agent Framework 1.0 (April 3, 2026).*

## Table B — CLI coding orchestrators

|  | Bernstein | [Stoneforge](./bernstein-vs-stoneforge.md) | [Agent HQ](./bernstein-vs-github-agent-hq.md) | [Conductor](./bernstein-vs-conductor.md) | [Crystal](./bernstein-vs-crystal.md) | [Parallel Code](./bernstein-vs-parallel-code.md) | [Dorothy](./bernstein-vs-dorothy.md) | [Single agent](./bernstein-vs-single-agent.md) |
|--|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **Parallel execution** | yes | yes | yes | yes | yes (worktrees) | yes | yes | no |
| **CLI agent support** | 31 adapters | no | Claude/Codex/Copilot | no | Claude/Codex | Claude/Codex/Gemini | Claude/Codex/Gemini/local | yes |
| **Pluggable sandbox** | worktree/docker/e2b/modal | no | no | no | worktree | no | no | no |
| **Result verification** | janitor (tests+lint) | provider-native | GitHub CI | none | none | manual | none | none |
| **Task planning from goal** | yes | no | yes | no | no | no | via Super Agent | no |
| **Self-evolution** | yes | no | no | no | no | no | no | no |
| **Model routing** | bandit | no | no | no | no | no | no | no |
| **Headless / overnight** | yes | limited | GitHub Actions | yes | no (Electron) | no | app must run | no |
| **IDE integration** | no | VS Code, JetBrains | GitHub UI | no | no | desktop app | desktop app | no |
| **Chat bridges (Telegram / Discord / Slack)** *(only Bernstein)* | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ~ | ✗ |
| **SSH remote sandbox** *(only Bernstein)* | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Lifecycle hooks (pre/post task, merge, spawn)** *(only Bernstein)* | ✓ | ✗ | ~ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Auto-PR generation with gate + cost summary** *(only Bernstein)* | ✓ | ✗ | ~ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Tunnel wrapper (4 providers, one CLI)** *(only Bernstein)* | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Interactive tool-call approval** *(only Bernstein)* | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ~ | ✗ |
| **Daemon / service install (systemd / launchd)** *(only Bernstein)* | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Open source** | Apache 2.0 | Apache 2.0 | no | Apache 2.0 | MIT (deprecated 2026-02 → Nimbalyst) | MIT | MIT | varies |

*Last verified: 2026-04-29. Stoneforge launched 2026-03-03. Paperclip (launched 2026-03-04) is covered on its [own page](./bernstein-vs-paperclip.md); it is an AI-company control plane, not a CLI orchestrator, and is not compared in this table.*

## Table C — Multi-session and swarm orchestrators

A second wave of tools focuses on managing multiple sessions of a single coding agent (usually Claude Code) — tmux-based, Claude-Code-shaped, or workforce-style. Bernstein's wedge against this group is provider breadth (31 cooperating CLI adapters, not Claude-only) and a deterministic Python scheduler (no LLM-driven swarm coordination).

|  | Bernstein | [Claude Squad](./bernstein-vs-claude-squad.md) | [claude-flow / Ruflo](./bernstein-vs-claude-flow.md) | [AgentsMesh](./bernstein-vs-agentsmesh.md) |
|--|:---:|:---:|:---:|:---:|
| **Primary scope** | provider-agnostic CLI orchestrator | tmux session manager | Claude Code plugin pack + swarm | self-hosted agent workforce platform |
| **Built-in agent breadth** | 31 cooperating + 2 delegated | Claude Code (extra via `-p`) | Claude Code | Claude/Codex/Gemini/Aider/OpenCode |
| **Scheduler** | deterministic Python | human picks per session | LLM-driven hooks + queen agent | service that dispatches AgentPods |
| **Plan files (`depends_on`)** | yes (YAML stages/steps) | no | partial (SPARC) | no |
| **Quality gates / janitor** | yes (tests + lint + types + files) | no | safety scans only (PII/CVE) | no |
| **MCP server (first-class)** | yes | no | yes (built-in MCP server) | no |
| **Headless / CI** | yes | no (needs tmux) | yes (npm CLI) | server-based |
| **State** | files (`.sdd/`) | per-session worktrees | vector memory store + Claude state | PostgreSQL + Redis + MinIO |
| **Cost tracking with budgets** | yes | no | no | partial |
| **License** | Apache 2.0 | AGPL-3.0 | MIT | BSL-1.1 (→ GPL-2.0-or-later in 2030) |

---

## Benchmark data

Early pilot on 25 real GitHub issues (run 2026-03-28) showed 40% resolve for single-agent vs 48% for Bernstein multi-agent (+8 pp, p=0.569, described as a "negligible effect" at n=25). A larger evaluation under the Bernstein SWE-Bench Lite harness is pending. See [`benchmarks/README.md`](../../benchmarks/README.md) for methodology and raw data.

Earlier drafts of this page cited larger deltas that were not reproducible at n=25; they have been removed.

---

## Choosing a tool

**Use Bernstein if:**
- You want to run multiple coding tasks in parallel and verify each result independently
- You want to mix AI providers (Claude + Codex + Gemini) in the same run
- You want the orchestrator to plan the task breakdown from a plain-language goal
- You need headless, overnight, or CI-integrated operation
- You want self-evolution (the system improves its own prompts and routing over time)

**Consider alternatives if:**
- You need production workflow orchestration for non-coding workloads (→ Conductor)
- You want deep IDE integration and are committed to one provider (→ Stoneforge)
- You want a desktop app to run a few agents side by side and resolve conflicts manually (→ Parallel Code or Crystal/Nimbalyst)
- You want an AI-company control plane with org charts, budgets, and governance on top of your agents (→ Paperclip)
- You want a Kanban-style desktop dashboard to delegate work between a few agents (→ Dorothy)
- You want a tmux-based multi-session manager for one Claude Code account (→ Claude Squad)
- Your stack is Claude-Code-only and you want a swarm/SPARC layer on top (→ claude-flow / Ruflo)
- You need a multi-tenant agent workforce platform with RBAC, SSO, and audit logs (→ AgentsMesh)
- Your task is simple and well-scoped (→ single agent, no orchestration needed)

---

## Detailed comparison pages

### Python agent frameworks
- [Bernstein vs. CrewAI](./bernstein-vs-crewai.html) — deterministic CLI orchestration vs Python-native LLM-driven scheduling
- [Bernstein vs. AutoGen](./bernstein-vs-autogen.html) — file-state CLI orchestration vs in-memory multi-agent conversation (AutoGen in maintenance mode)
- [Bernstein vs. LangGraph](./bernstein-vs-langgraph.html) — CLI agents vs LangChain graph-based LLM nodes

### CLI orchestrators and baselines
- [Bernstein vs. Single Agent](./bernstein-vs-single-agent.md) — one CLI agent vs an orchestrator
- [Bernstein vs. Parallel Code](./bernstein-vs-parallel-code.md) — orchestrated parallel vs manual multi-terminal
- [Bernstein vs. Stoneforge](./bernstein-vs-stoneforge.md) — provider-agnostic vs provider-integrated (Stoneforge launched March 3, 2026)
- [Bernstein vs. GitHub Agent HQ](./bernstein-vs-github-agent-hq.md) — open-source alternative to GitHub's multi-agent system (Universe 2025)
- [Bernstein vs. Crystal](./bernstein-vs-crystal.md) — provider-agnostic CLI orchestrator vs Electron desktop app for Claude Code / Codex worktrees (Crystal deprecated Feb 2026)
- [Bernstein vs. Conductor](./bernstein-vs-conductor.md) — workflow engine vs coding agent orchestrator (Netflix Conductor and forks)

### Multi-session and swarm orchestrators
- [Bernstein vs. Claude Squad](./bernstein-vs-claude-squad.md) — orchestrator with plan files and quality gates vs tmux session manager
- [Bernstein vs. claude-flow](./bernstein-vs-claude-flow.md) — provider-agnostic deterministic scheduler vs Claude Code plugin pack with LLM-driven swarm coordination
- [Bernstein vs. AgentsMesh](./bernstein-vs-agentsmesh.md) — single-process file-based orchestrator vs server-stack agent workforce platform

### Agent management overlays
- [Bernstein vs. Paperclip](./bernstein-vs-paperclip.md) — engineering orchestrator vs AI-company control plane (Paperclip launched March 4, 2026)
- [Bernstein vs. Dorothy](./bernstein-vs-dorothy.md) — task dispatch vs desktop Kanban orchestrator with Super Agent

### Deep dive
- [Deterministic vs LLM orchestration](./deterministic-vs-llm-orchestration.md) — architecture comparison

---

## Broader ecosystem comparison

For a comparison against Python agent frameworks beyond Table A (OpenAI Agents SDK, Google ADK, Microsoft Agent Framework 1.0), see [docs/reference/competitive-matrix.md](../reference/competitive-matrix.md).
