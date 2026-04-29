# Bernstein vs. claude-flow

> **tl;dr** — claude-flow (recently renamed Ruflo) is a Claude Code plugin pack: 20 plugins, an MCP server, and a swarm coordination layer that turns Claude Code into a multi-agent platform. It is Claude-Code-shaped end to end. Bernstein is provider-agnostic: a deterministic Python orchestrator that drives 31 different CLI agents (Claude, Codex, Gemini, Aider, Goose, Cursor, Qwen, Ollama, and more) in isolated git worktrees with file-based state. If your stack is Claude Code and you want a swarm/SPARC layer on top, claude-flow is the natural fit. If you want to mix providers, ship code through quality gates, and keep the scheduler out of an LLM, Bernstein is the right tool.

*Last verified: 2026-04-27. Based on `github.com/ruvnet/claude-flow` (~34k stars, MIT, npm package `claude-flow` v3.6.9, latest release v3.5.80 on 2026-04-11). The project has been rebranded "Ruflo" inside the same repo.*

---

## What is claude-flow?

**claude-flow / Ruflo** is a TypeScript/Node.js orchestration layer built specifically for Claude Code. It installs three ways: as a Claude Code plugin (`/plugin install ruflo-core@ruflo`), as an npm CLI (`npm install -g ruflo@latest`), or as an MCP server (`claude mcp add ruflo`). The shipped surface is a marketplace of ~20 plugins — `ruflo-core`, `ruflo-swarm`, `ruflo-autopilot`, `ruflo-intelligence`, `ruflo-agentdb`, `ruflo-aidefence`, `ruflo-jujutsu`, `ruflo-wasm`, `ruflo-rag-memory`, etc. — that drop skills, slash commands, agents, and MCP tools into Claude Code. Coordination happens through a "queen-led" topology with hierarchical/mesh/adaptive layouts, an HNSW vector memory store (AgentDB), and a hooks layer that auto-routes tasks. The project is active (1,400+ releases, latest v3.5.80 in April 2026) and licensed MIT.

The README markets several headline numbers ("89% routing accuracy", "150x–12,500x faster search", "SONA self-learning patterns", Raft/Byzantine consensus). These are claims about the runtime; the codebase that implements them is a TypeScript monorepo under `v3/@claude-flow/*` plus optional ruvector / agentdb native bindings. Treat the claims as claims and the architecture as: Node.js process, MCP-server-shaped, Claude-Code-bound.

## What is Bernstein?

**Bernstein** (Apache 2.0, Python 3.12+) is a multi-agent orchestrator for CLI coding agents. It breaks a goal into tasks, spawns short-lived agents in isolated git worktrees, verifies each agent's output through a janitor (tests, lint, types, file checks), and merges the results. The orchestrator is deterministic Python — no LLM in the scheduling loop. It ships 31 cooperating CLI adapters (Claude Code, Codex, Cursor, Gemini, Aider, Amp, Goose, OpenCode, Qwen, Kilo, Kiro, Ollama, Continue, Cody, Cloudflare, IaC, Generic, …) plus 2 leaf-node delegation adapters (Composio, Ralphex). State lives in `.sdd/` files; agents are ~1–3 tasks each and exit. Cost tracking, budget caps, MCP server mode, multi-repo workspaces, Cloudflare Workers cloud execution, and HMAC-chained audit logs are all first-class.

---

## Feature comparison

| Feature | Bernstein | claude-flow / Ruflo |
|---|---|---|
| **Primary focus** | Ship code across many CLI agents | Multi-agent swarm layer for Claude Code |
| **License** | Apache 2.0 | MIT |
| **Language / runtime** | Python 3.12+ | Node.js 20+ (TypeScript) |
| **Install path** | `pipx install bernstein` / `uv tool install` | `npm i -g ruflo` / Claude Code plugin / MCP |
| **Provider scope** | 31 CLI adapters + 2 leaf-node | Claude Code first-class; multi-provider routing via plugins |
| **Orchestrator logic** | Deterministic Python scheduler | LLM + hooks + queen-led swarm coordination |
| **Coordination cost** | Zero LLM tokens for scheduling | Coordination uses LLM calls (hooks, routing, consensus) |
| **State storage** | File-based (`.sdd/`) | AgentDB (HNSW vector) + RVF + ReasoningBank |
| **Git isolation** | Per-agent worktrees | Worktree isolation via `ruflo-swarm` plugin |
| **Quality gates** | Janitor: tests, lint, types, file checks before merge | AIDefence: PII / prompt-injection / CVE scanning |
| **Automated retries** | Yes — failure-aware retry with model escalation | Hooks/learning loop reattempts |
| **Cost tracking** | Per-task, per-run, with budget caps | Not a primary surface |
| **Plan files** | YAML stages + steps + `cli:` per step | SPARC / GOAP / workflow templates per plugin |
| **MCP server** | First-class (`bernstein mcp`) | First-class (`@claude-flow/cli` MCP) |
| **Headless / CI** | Yes — designed for unattended CI | Optimized for interactive Claude Code sessions |
| **Multi-repo** | Yes — multi-repo workspaces | Single project per session |
| **Cloud execution** | Cloudflare Workers adapter | Not shipped |
| **Audit trail** | HMAC-chained `.sdd/audit.jsonl` | Activity logs |
| **Self-evolution** | `bernstein --evolve` rewrites prompts/templates | SONA pattern learning, ReasoningBank trajectories |
| **Headline claims** | Conservative — measured against tests | "89% routing accuracy", "150x–12,500x search", "100+ agents" |

---

## Architecture comparison

**claude-flow / Ruflo (Claude-Code-native swarm layer):**
```
Claude Code session
    │
    ▼
Ruflo MCP server + 27 hooks
    │
    ├── Router (intelligent task dispatch)
    │
    ▼
Swarm coordinator (queen + topology)
    │
    ├── coder, tester, reviewer, architect, security, ...
    │
    ▼
AgentDB (HNSW vector memory) + ReasoningBank
    │
    ▼
LLM providers (Claude / GPT / Gemini / Cohere / Ollama)
```

The system lives inside Claude Code. Hooks intercept tasks, the router decides which agent handles them, the swarm coordinator runs the chosen topology, and shared state flows through a vector memory store. Coordination is LLM-driven and hook-driven.

**Bernstein (provider-agnostic engineering orchestrator):**
```
bernstein -g "goal"   (terminal, CI, SSH, MCP)
    │
    ▼
Task server (FastAPI, deterministic Python)
    │
    ├── Task A → claude   (worktree) → janitor → merge
    ├── Task B → codex    (worktree) → janitor → merge
    ├── Task C → gemini   (worktree) → janitor → merge
    └── Task D → aider    (worktree) → janitor → merge

State: .sdd/  (backlog, runtime, metrics, audit, config)
```

The orchestrator is ~800 lines of deterministic Python. Agents are short-lived processes — one per task — that read the codebase, make changes, and exit. The janitor runs tests/lint/types in the worktree before any merge. No LLM is consulted to decide what to do next.

---

## When claude-flow is the right tool

**You are all-in on Claude Code.** The product is built around it. Plugins drop into `/plugin`, hooks integrate with Claude Code's task lifecycle, and the MCP server speaks that ecosystem fluently. If your team's daily driver is Claude Code and you want to extend it without leaving the session, claude-flow is the more native experience.

**You want a swarm/SPARC methodology layer.** The queen-led hierarchy, mesh/adaptive topologies, and consensus mechanisms are an opinionated take on coordinating many specialized agents (coder, tester, reviewer, architect, security). If that conceptual model maps to how you want to organize work, the plugins are wired up to it.

**You want vector-memory shared context between agents.** AgentDB / HNSW / ReasoningBank are designed for cross-session, cross-agent memory retrieval. Bernstein's file-based state is simpler and cheaper but doesn't index trajectories for semantic recall.

**You want a plugin marketplace.** Twenty plugins covering RAG, browser automation, security audit, doc generation, GOAP planning, WASM sandboxing, etc. is a large pre-built surface to draw from inside Claude Code.

---

## When Bernstein is the right tool

**You use more than one CLI agent.** 31 adapters vs. Claude-Code-as-the-host. If you actually run Codex, Gemini, Aider, Amp, Goose, Qwen, Kilo, Kiro, OpenCode, Cursor, or Ollama — not as bash shims but as first-class adapters with their own auth and cost models — Bernstein meets you where you are. claude-flow can route to other providers through plugins, but the orchestration substrate is Claude Code.

**You want zero LLM tokens in the scheduler.** Bernstein's tick pipeline, task assignment, retries, and merge ordering are deterministic Python. claude-flow's coordination uses hooks + routers + a learning loop, all of which spend tokens to make decisions. Both approaches work; the cost and reproducibility profiles are different.

**You want quality gates, not safety scanning.** Bernstein's janitor is engineering verification: did the tests pass, did the linter pass, did Pyright pass, do the expected files exist. claude-flow's AIDefence is security/safety scanning (PII, prompt injection, CVE). They solve different problems. Bernstein refuses to merge a branch with a failing test; claude-flow refuses to send a prompt with PII in it.

**You run in CI / headless / SSH / cloud.** Bernstein is designed to run unattended: `bernstein run --plan plans/x.yaml`, return exit code, ship a branch. Cloudflare Workers execution is supported. claude-flow is optimized for interactive Claude Code sessions.

**You want file-based reproducible state.** Everything Bernstein does lands in `.sdd/`: backlog, runtime, metrics, HMAC-chained audit log. You can diff two runs, replay a run, or hand `.sdd/` to a teammate. claude-flow's state lives in vector DBs and learning stores designed for retrieval, not byte-level reproducibility.

**You want per-step model and provider selection.** Bernstein's plan YAML supports `cli:` per step (PR #965), so a plan can route the cheap step to Haiku-via-Claude, the heavy refactor to Codex, and the doc pass to Gemini. claude-flow leans on its router to decide.

**You want multi-repo orchestration.** Bernstein workspaces span repos. claude-flow operates per project.

**You want Apache 2.0.** If license matters to your org, Apache 2.0 vs. MIT is a real distinction (patent grant). Both are permissive; pick the one your legal team prefers.

---

## Stitching claude-flow into a Bernstein plan

These are not mutually exclusive. claude-flow's strength is being Claude-Code-native; Bernstein's strength is provider-agnostic orchestration. A pragmatic combination:

- Use Bernstein as the top-level scheduler. It owns the plan, the worktrees, the janitor gate, and the merge.
- For Claude-Code-heavy steps, set `cli: claude` on the step and let Bernstein's Claude adapter spawn the agent. If you want claude-flow's swarm/skills inside that session, the Claude adapter will pick up `.claude/` plugins and MCP servers configured in the workspace.
- Keep deterministic verification at the Bernstein layer (tests + lint + types in the worktree, not inside the swarm). The janitor's pass/fail is what gates the merge.
- Keep cost tracking at the Bernstein layer. claude-flow doesn't surface per-step cost; Bernstein does.

This keeps the scheduler deterministic and cheap, and lets claude-flow do what it does best inside the Claude Code steps that benefit from a swarm.

---

## See also

- [Bernstein vs. Paperclip](./bernstein-vs-paperclip.md)
- [Full comparison index](./README.md)
