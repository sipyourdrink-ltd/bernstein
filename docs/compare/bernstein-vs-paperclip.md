# Bernstein vs. Paperclip

> **tl;dr** — Paperclip is an AI company simulator: org charts, budgets, governance hierarchies for AI agents. Bernstein is an engineering tool: spawn agents, ship code, verify results. They solve different problems. If you need corporate structure for your AI workforce, Paperclip is impressive. If you need to parallelize coding tasks and merge working branches, Bernstein is what you want.

*Last verified: 2026-04-19. Based on `github.com/paperclipai/paperclip` (55k+ stars, launched 2026-03-02) and paperclip.ing.*

---

## What each tool is

**Paperclip** (55k+ stars, MIT, Node.js + React) is an "AI company management" platform, released March 2, 2026. It models AI agents as employees in an organization: they have roles, report to managers, operate within budgets, follow org-chart hierarchies, and receive scheduled heartbeats. The coordinator is an LLM. It advertises Claude Code, Codex, Cursor, "OpenClaw," plus bash and HTTP hooks ("if it can receive a heartbeat, it's hired"). Think of it as an HR and project-management control plane for AI agents.

**Bernstein** (Apache 2.0, Python) is a multi-agent orchestrator for CLI coding agents. It breaks a goal into tasks, spawns agents in isolated git worktrees, verifies their output (tests, lint, file checks), and merges the results. The orchestrator is deterministic Python — zero LLM tokens spent on coordination. It supports 31 CLI adapters and runs anywhere Python runs.

---

## Feature comparison

| Feature | Bernstein | Paperclip |
|---|---|---|
| **Primary focus** | Ship code | Manage AI organizations |
| **Open source** | Yes — Apache 2.0 | Yes — MIT |
| **Language** | Python | Node.js + React |
| **Orchestrator logic** | Deterministic code (no LLM) | LLM-based coordination |
| **Agent adapters** | 31 CLI adapters | Claude Code, Codex, Cursor, OpenClaw + bash / HTTP hooks |
| **Org charts / hierarchies** | No | Yes — core feature |
| **Budget enforcement** | Cost tracking + budget caps | Yes — per-agent and per-team budgets |
| **Task ticketing** | Yes — internal task server | Yes — with goal alignment |
| **Git worktree isolation** | Yes — per agent | No |
| **Result verification** | Janitor (tests, lint, files) | Governance controls |
| **Scheduled heartbeats** | Tick pipeline (deterministic) | Yes — LLM-driven |
| **Multi-company support** | No (multi-repo workspaces) | Yes |
| **Plan files** | YAML stages + steps | Goal hierarchies |
| **Audit trail** | HMAC-chained, file-based | Activity logs |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Protocol support** | MCP, A2A | Not documented |
| **Web UI** | TUI + web dashboard | Yes — React dashboard |
| **Cluster mode** | Yes | Not documented |

---

## Architecture comparison

**Paperclip (AI company simulator):**
```
React dashboard
    │
    ▼
LLM coordinator (manages org structure)
    │
    ├── Team A (budget: $50/day)
    │   ├── Manager agent (Claude)
    │   ├── Worker agent (Codex)
    │   └── Worker agent (Cursor)
    │
    └── Team B (budget: $30/day)
        ├── Manager agent (Claude)
        └── Worker agent (OpenClaw)

Heartbeats, goal alignment, governance controls
```

The coordinator uses LLM calls to manage agent relationships, delegate work through hierarchies, and enforce organizational policies. The metaphor is a company with departments, managers, and employees.

**Bernstein (engineering orchestrator):**
```
bernstein -g "goal"  (terminal, CI, SSH)
    │
    ▼
Task server (local FastAPI, deterministic Python)
    │
    ├── Task A → claude  (isolated worktree) → janitor → merge
    ├── Task B → codex   (isolated worktree) → janitor → merge
    └── Task C → gemini  (isolated worktree) → janitor → merge

State: .sdd/ files (backlog, runtime, metrics, config)
```

The orchestrator is deterministic code. Agents are short-lived processes that execute one task, get verified, and exit. No hierarchies, no org charts — just a task queue and a verification step.

---

## The fundamental difference

Paperclip answers: "How do I organize and govern a fleet of AI agents like a company?"

Bernstein answers: "How do I get code shipped faster using multiple agents in parallel?"

These are genuinely different problems. Paperclip cares about organizational structure — who reports to whom, what budget each team has, how goals cascade through a hierarchy. Bernstein cares about engineering output — did the tests pass, did the linter pass, can this branch merge cleanly.

---

## Where Paperclip is better

**Organizational modeling.** If you're running dozens of AI agents across multiple projects with different budgets, teams, and governance requirements, Paperclip's org-chart model gives you structure that Bernstein doesn't attempt. Bernstein has no concept of "teams" or "reporting lines."

**Web UI.** Paperclip ships a React dashboard. Bernstein ships both a TUI (`bernstein live`) and a web dashboard (`bernstein dashboard`). Paperclip's React UI is more polished for non-technical stakeholders; Bernstein's dashboard is developer-oriented (logs, traces, cost).

**Community size.** 55k+ stars means a large ecosystem of contributors, integrations, and community support. More eyes on bugs, more plugins, more documentation.

**Multi-company support.** If you're managing AI agents across multiple organizations (consultancy, agency, MSP use cases), Paperclip has first-class support. Bernstein's multi-repo workspaces are not the same thing.

**Budget governance.** Paperclip's budget enforcement is hierarchical — team budgets, per-agent limits, approval workflows. Bernstein tracks costs and has a global budget cap, but doesn't model organizational budget hierarchies.

---

## Where Bernstein is better

**Actually shipping code.** Bernstein's entire pipeline is optimized for one thing: take a goal, break it into tasks, execute them in parallel, verify the results, merge working code. Git worktree isolation, janitor verification (tests + lint + file checks), and deterministic merge ordering exist because the goal is a working codebase, not an org chart.

**Zero LLM overhead on coordination.** Paperclip uses LLM calls for coordination — managing hierarchies, routing tasks through org structures, heartbeat processing. Every coordination decision costs tokens. Bernstein's orchestrator is ~800 lines of deterministic Python. Coordination cost is zero.

**Agent breadth.** 31 adapters vs. 4 official. If you use Gemini, OpenAI Agents SDK v2, Aider, Amp, Kilo, Kiro, Qwen, Goose, or OpenCode as first-class adapters rather than bash/HTTP shims, Bernstein supports them out of the box.

**Git-native isolation.** Each Bernstein agent works in its own git worktree. Conflicts are detected at merge time, not at runtime. Paperclip doesn't provide git-level isolation.

**Verification before merge.** The janitor runs your test suite, linter, and file-existence checks before any agent's work is merged. This is not governance — it's engineering verification. Paperclip's governance controls are organizational (budget, hierarchy), not technical (tests pass).

**No web UI to maintain.** This is a tradeoff, not a pure win. But for engineers working in terminals, SSH sessions, and CI pipelines, a CLI tool that doesn't require a React app is simpler to deploy and operate.

**Self-evolution.** `bernstein --evolve` analyzes past run metrics and improves prompts, routing, and templates. Paperclip doesn't have an equivalent.

---

## When to use Paperclip

- **You're managing AI agents as a business function.** Multiple teams, budgets, approval chains, governance requirements. The org-chart metaphor maps to how your organization thinks about AI agent deployment.
- **You want a visual dashboard.** Non-technical stakeholders need to see what agents are doing, what they're costing, and how they're organized.
- **You need multi-company support.** You're a consultancy or MSP deploying AI agents across client organizations.
- **The problem is coordination, not code.** Your agents do diverse work (not just coding), and you need organizational structure around them.

---

## When to use Bernstein

- **You want to ship code.** Your goal is "implement these 10 features in parallel and merge them all into a working branch." Bernstein does this. Paperclip doesn't try to.
- **You want zero coordination overhead.** No LLM tokens spent on figuring out which agent should do what. Deterministic task assignment, deterministic verification.
- **You use diverse CLI agents.** 31 adapters vs. 4 official. Mix Claude, Codex, OpenAI Agents SDK v2, Gemini, and Aider in the same session without dropping to bash/HTTP shims.
- **You want git-native safety.** Worktree isolation, conflict detection, janitor verification. The output is a tested, linted branch.
- **You work in terminals.** CLI-native, works over SSH, runs in CI, no browser required.
- **You don't need org charts.** If the concept of "reporting lines for AI agents" doesn't map to your problem, you don't need Paperclip's primary feature.

---

## The complementary case

These tools could coexist. Paperclip could manage the organizational layer — which teams exist, what budgets they have, what governance policies apply — while Bernstein handles the engineering execution within each team. Paperclip decides "Team Backend gets $200/day and works on the API refactor." Bernstein takes that goal, spawns 5 agents in isolated worktrees, verifies their output, and merges working code.

This isn't a theoretical integration — it's a recognition that "manage AI agents as a company" and "ship code with parallel agents" are different layers of the same stack.

---

## See also

- [Bernstein vs. GitHub Agent HQ](./bernstein-vs-github-agent-hq.md)
- [Full comparison index](./README.md)
