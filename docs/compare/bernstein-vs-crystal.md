# Bernstein vs. Crystal

> **tl;dr** — Crystal (stravu/crystal) is an Electron desktop app for running parallel Claude Code and Codex sessions in git worktrees, with a friendly visual UI for a single developer watching agents work. Bernstein is a CLI orchestrator with 31 cooperating agent adapters, a deterministic Python scheduler, file-based state, MCP server, plan files, cost budgets, and headless CI execution. They overlap on the worktree-isolation idea and diverge on almost everything else. As of February 2026 Crystal is deprecated in favor of its successor Nimbalyst; this comparison is kept here because it remains the most-cited "parallel agents in worktrees" reference.

*Last verified: 2026-04-27. Based on `github.com/stravu/crystal` (3.0k+ stars, MIT, last release 0.3.5 on 2026-02-26, deprecated in favor of `nimbalyst.com`).*

---

## What is Crystal?

Crystal is an Electron desktop application written in TypeScript (React 19 frontend, Node.js + better-sqlite3 backend, node-pty for process management). Its scope is narrow and intentional:

- **Two agents.** Claude Code and OpenAI Codex, integrated via their SDKs and CLIs.
- **Sessions in worktrees.** Each session creates a git worktree off a project's main branch and runs one (or, since 0.3.4, several) agents inside it.
- **Local SQLite state.** Sessions, conversation messages, prompt history, panel configurations, execution diffs, and run-script logs all live in `~/.crystal/` SQLite tables.
- **Per-session UI.** Output view, raw-message view, diff view, and an editor view. Multiple XTerm.js terminals per session, with 50,000-line scrollback and lazy initialization.
- **Git operations.** Rebase / merge to main, squash, diff visualization, conflict surfacing.
- **Notifications.** Desktop notifications, Web Audio sound cues on status change.

What Crystal does not have: no CLI mode, no headless / CI execution, no MCP server, no plan files, no cost tracking with budgets, no support for any agent beyond Claude Code and Codex, no multi-machine coordination, no automated retries with model escalation, no quality gates beyond what the agent decides itself.

Crystal is a desktop app for one developer running a few parallel agents and watching them in tabs. That is the entire product.

**Project status (April 2026).** Crystal's final release was `0.3.5` on 2026-02-26. The repo is now a migration shim pointing at Nimbalyst (`nimbalyst.com`, `Nimbalyst/nimbalyst`). The historical Crystal architecture and feature set are still useful as a reference point because Nimbalyst's parallel-agents model inherits the same constraints (single machine, desktop-first, Claude Code + Codex focus).

---

## What is Bernstein?

Bernstein (Apache 2.0, Python 3.12+, hatchling) is a CLI orchestrator for multi-agent coding workflows. The orchestrator is deterministic Python — no LLM tokens are spent on coordination. Agents are short-lived processes that pick up a task, execute it in an isolated worktree, and exit. State lives in `.sdd/` files (backlog, runtime, metrics, config), inspectable from the shell.

- 31 cooperating CLI agent adapters (Claude Code, Codex, Cursor, Aider, Amp, Cody, Continue, Gemini CLI, Goose, Kilo, Kiro, Ollama, OpenCode, Qwen, plus a generic adapter), with two leaf-node delegation adapters (Composio, Ralphex) shipping in PR #966.
- Per-step `cli:` field (PR #965) so a plan can pin a specific agent to a step.
- Janitor module — quality gates for tests, lint, types, file checks. Failures go back into the queue; successful work merges.
- MCP server first-class, exposing run / status / cost / approve / tasks tools.
- YAML plan files with stages, dependencies, role assignments — repeatable workflows checked into the repo.
- Cost tracking with budgets, anomaly detection, and per-agent budget enforcement.
- Multi-repo orchestration; cloud execution via Cloudflare Workers; SSH remote sandbox.
- Headless: runs in CI, over SSH, in a tmux session, in a daemon. No GUI required.

---

## Feature comparison

| Feature | Bernstein | Crystal |
|---|---|---|
| **Primary surface** | CLI + optional TUI / web dashboard | Electron desktop app |
| **License** | Apache 2.0 | MIT |
| **Language** | Python 3.12+ | TypeScript (React 19, Node, Electron) |
| **Project status** | Active | Deprecated 2026-02-26 (replaced by Nimbalyst) |
| **Orchestrator logic** | Deterministic Python scheduler | Per-session UI; no cross-session orchestration |
| **Agent adapters** | 31 CLI adapters + 2 leaf delegators | 2 (Claude Code, Codex) |
| **Per-step agent selection** | Yes — `cli:` field in plan steps (PR #965) | Per-session at creation; multi-agent per worktree as of 0.3.4 |
| **Git worktree isolation** | Yes — per task | Yes — per session |
| **Plan files (repeatable workflows)** | YAML stages + steps + `depends_on` | None |
| **Headless / CI mode** | Yes | No — desktop only |
| **MCP server** | First-class (`bernstein mcp`) | None |
| **Cost tracking + budgets** | Yes — per-task cost, budget caps, anomaly detection | Token usage display only |
| **Quality gates / verification** | Janitor: tests, lint, types, file checks | Run-scripts; agent self-judgment |
| **Automated retry with model escalation** | Yes | No |
| **State storage** | `.sdd/` files (inspectable from shell) | `~/.crystal/` SQLite |
| **Multi-repo / multi-project** | Yes — multi-repo workspaces | Multiple projects, single machine |
| **Multi-machine / cluster** | Yes — cluster mode + Cloudflare Workers | No |
| **SSH remote sandbox** | `bernstein remote test/run/forget` | No |
| **Chat bridges (Telegram / Discord / Slack)** | `bernstein chat serve --platform=...` | No |
| **Tunnel wrapper (cloudflared / ngrok / bore / tailscale)** | `bernstein tunnel start/list/stop` | No |
| **Daemon / service install** | `bernstein daemon install/start/stop/status` | n/a (desktop app) |
| **Self-evolution** | `bernstein --evolve` | No |
| **Audit trail** | HMAC-chained, file-based | SQLite tables, not signed |

---

## Architecture comparison

**Crystal (desktop app, single machine):**

```
~/.crystal/ SQLite
        ▲
        │
Electron main process
  ├── SessionManager
  ├── WorktreeManager
  ├── ClaudeCode / Codex SDK
  ├── node-pty PTY processes
  └── Bull task queue (in-memory)
        │
        ▼
React renderer (Sidebar, Terminal panels, Diff view, Editor)
```

Everything runs inside one Electron process. Sessions are visible to one user on one machine. The "orchestration" is a queue of process invocations driven by user clicks in the sidebar.

**Bernstein (CLI orchestrator, deterministic scheduler):**

```
bernstein -g "goal"  (terminal, CI, SSH, daemon)
    │
    ▼
Task server (local FastAPI, deterministic Python tick pipeline)
    │
    ├── Task A → claude  (worktree A) → janitor → merge
    ├── Task B → codex   (worktree B) → janitor → merge
    ├── Task C → gemini  (worktree C) → janitor → merge
    └── Task D → aider   (worktree D) → janitor → retry with stronger model
                                 │
                                 ▼
                       Plan files (YAML), cost budget, audit log
                       State: .sdd/ (file-based, inspectable via shell)
```

The orchestrator is a long-running process (or a one-shot CLI invocation) that schedules tasks across whatever agents are available, verifies their output through the janitor, and merges working branches.

---

## When Crystal is the right tool

Crystal earned its niche, and the niche is real:

- **Single developer, single machine, two agents.** If you're sitting at one Mac, you have Claude Code and Codex installed, and you want to run three or four parallel attempts at the same task and pick the best one — Crystal's UI is genuinely pleasant for that. Sidebar, tabs, diff view, run-script panel.
- **Visual diff and merge workflow.** Watching a worktree's diff change in real time as the agent works, then clicking "merge to main," is a nicer experience in Crystal than scrolling through a terminal log.
- **No CI requirement.** If your work is "explore three approaches to this prompt and keep the one I like," you don't need plan files, budgets, or headless execution. Crystal is lighter weight for that loop.
- **You already use it.** Until Nimbalyst stabilizes, Crystal still works; it just won't get new features.

If your problem is "I want a desktop UI to run a few Claude Code or Codex sessions in worktrees and watch them in tabs," Crystal is the better fit. Bernstein is built for a different shape of problem.

---

## When Bernstein is the right tool

Bernstein's wedge is everything Crystal's scope deliberately excludes:

- **Provider breadth.** 31 CLI adapters versus Crystal's two. Mix Claude Code, Codex, Gemini CLI, Aider, Amp, Goose, Cursor, OpenCode, Qwen, Kilo, Kiro, Ollama in the same run — first-class, not bash shims. If a vendor breaks or a model regresses, switch the `cli:` field on the affected steps.
- **Headless / CI execution.** Crystal can't run in GitHub Actions. Bernstein can. `bernstein run plans/release.yaml` in a CI job spawns agents, runs the janitor, and exits with a status code.
- **Plan files for repeatable workflows.** A YAML plan with stages, `depends_on` edges, and per-step `cli:` and `role:` fields turns "I implemented this with three agents last Tuesday" into a checked-in artifact that runs again identically.
- **Deterministic Python scheduler.** No tokens spent on coordination. The scheduler is code, not an LLM. Failures in scheduling are bugs you can fix; LLM coordination drift is not a class of problem here.
- **File-based state inspectable from the shell.** `.sdd/runtime/tasks.json`, `.sdd/metrics/`, `.sdd/config.yaml` — `cat`, `jq`, `grep`. No SQLite browser required, no Electron app needed to read your own state.
- **MCP server first-class.** Bernstein exposes its run, status, cost, approve, tasks, create-subtask, and load-skill operations over MCP. Other agents can drive Bernstein. Crystal does not expose MCP.
- **Janitor verification.** Tests, lint, types, file existence — checked before merge. A task that breaks the test suite goes back to the queue with retry-and-escalate. Crystal relies on the user's run-script and the agent's own judgment.
- **Cost tracking with budgets.** Per-task cost, anomaly detection, hard budget cap that stops a runaway loop. Crystal displays token usage; it doesn't enforce a budget.
- **Multi-repo orchestration.** A single run can touch several repositories; Crystal binds a session to one project at a time on one machine.
- **Cluster + Cloudflare Workers + SSH remote sandbox.** Bernstein runs across machines. Crystal is local-only by design.

---

## How to migrate from Crystal to Bernstein

If you've been using Crystal and want to keep the worktree-per-task model while gaining headless execution, more agents, and verification, the path is:

1. **Keep your projects where they are.** Bernstein operates on a normal git repo. No data migration. Nothing in `~/.crystal/` needs to move; you can run both in parallel during the transition.
2. **Install Bernstein.** `pip install bernstein` (or `uv tool install bernstein`). State lives in `.sdd/` inside each repo.
3. **Translate "sessions" into "tasks."** What Crystal called a session — one goal, one worktree — Bernstein calls a task. Run `bernstein run -g "your goal"` or `bernstein run plans/your-plan.yaml`.
4. **Pick agents per step.** Where Crystal had a per-session model picker, Bernstein has a per-step `cli:` field. Pin Claude to architectural steps, Codex to refactors, Gemini to docs — whatever maps to your team's habits.
5. **Add a janitor config.** A two-line `pytest` + `ruff check` block in your plan turns "the agent says it's done" into "the test suite says it's done."
6. **Set a budget.** `bernstein config set cost.budget_usd 5` caps a single run. The orchestrator stops when it hits the cap.
7. **Run it in CI.** Add a workflow step that runs `bernstein run plans/nightly-cleanup.yaml`. Crystal could not do this; Bernstein is built for it.
8. **Drop the GUI if you don't miss it.** If you do miss it, `bernstein dashboard` and `bernstein live` (TUI) cover most of what Crystal's UI offered for monitoring runs in progress.

---

## The honest summary

Crystal solved a specific problem well: give one developer a friendly desktop UI for running a couple of Claude Code and Codex sessions in parallel worktrees, with diff view and merge buttons. For that exact shape, it's a fine tool, and its UI is genuinely nicer than running raw `claude` and `codex` in side-by-side terminals.

Bernstein solves a different problem: orchestrate many CLI agents across many tasks, verify their output with deterministic checks, track costs, run headless in CI, and do it across more than one provider and more than one machine. The scopes overlap on "git worktree per task" and diverge from there.

Crystal is deprecated. Nimbalyst is its successor and inherits Crystal's desktop-first model. If your future is "single user, desktop, polished editor experience," Nimbalyst is where to look. If your future is "headless multi-agent automation with verification and budgets," Bernstein.

---

## See also

- [Bernstein vs. Conductor](./bernstein-vs-conductor.md)
- [Bernstein vs. Parallel Code](./bernstein-vs-parallel-code.md)
- [Full comparison index](./README.md)
