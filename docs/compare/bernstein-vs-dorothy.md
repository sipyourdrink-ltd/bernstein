# Bernstein vs. Dorothy

> **tl;dr** — Dorothy is a free desktop app that orchestrates Claude Code, Codex, Gemini, and local agents with a Kanban board, a "Super Agent" delegation layer, and Telegram/Slack controls. Bernstein is a headless orchestrator for CLI coding agents that runs in a terminal or CI and stores all state in files. Dorothy is better when you want a GUI to watch and delegate across a few agents. Bernstein is better when you want unattended, budget-capped, file-state runs across 18 adapters.

*Last verified: 2026-04-19. Based on the Dorothy public site and repo (`github.com/Charlie85270/Dorothy`).*

---

## What each tool is

**Dorothy** is a desktop application that presents AI coding agents through a visual Kanban interface. It can launch and monitor Claude Code, Codex, Gemini, and local agents, delegate between them through a "Super Agent" that talks to them via MCP, schedule recurring work with cron, and trigger on GitHub issues/PRs. It integrates Google Workspace as an MCP server and has Telegram/Slack bridges for remote control.

**Bernstein** is a task dispatch orchestrator for CLI coding agents. It decomposes a goal into tasks, assigns each task to a short-lived CLI agent (across 18 adapters: Claude Code, Codex, OpenAI Agents SDK v2, Gemini CLI, Cursor, Aider, Amp, etc.), verifies the result against external criteria (tests, linter), and merges the output. The orchestrator is deterministic Python — no LLM makes scheduling decisions. No GUI.

The core difference: Dorothy gives you a visual control plane. Bernstein gives you a headless, reproducible, file-state control plane.

---

## Feature comparison

| Feature | Bernstein | Dorothy |
|---|---|---|
| **Interface** | CLI + TUI + JSON status endpoint | Desktop app (Kanban, dashboard) |
| **Agent coverage** | 18 CLI adapters | Claude Code, Codex, Gemini, local |
| **Scheduler** | Deterministic Python, no LLM | "Super Agent" (LLM) via MCP |
| **Verification** | Janitor: tests, linter, file checks | None built-in |
| **Parallel execution** | Yes — independent tasks run concurrently | Yes — up to ~10 agents |
| **Git worktree isolation** | Yes — per agent | No |
| **State** | File-based (`.sdd/`, survives crashes) | Application state |
| **Remote control** | CLI, SSH, REST | Telegram, Slack |
| **Trigger sources** | Manual, CI, cron via your own runner | Built-in cron, GitHub issues/PRs |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Model routing** | Cost-aware bandit across providers | Per-agent |
| **Headless / overnight** | Yes — `--headless` + budget cap | Via Telegram/Slack, app must be running |
| **Open source** | Apache 2.0 | MIT |
| **Primary use case** | Unattended parallel coding with verification | Visual orchestration of a small agent fleet |

---

## Architecture comparison

**Dorothy (desktop + Super Agent):**
```
Desktop app (Kanban, dashboard, logs)
    |
    v
Super Agent (LLM) -- talks to agents via MCP
    |
    +-- Claude Code         (one project, one window)
    +-- Codex               (one project, one window)
    +-- Gemini              (one project, one window)
    +-- local agent         (one project, one window)

Triggers: cron, GitHub issue/PR webhook, Telegram/Slack command
```

Dorothy's value is visual delegation: you watch the Kanban board, approve tasks, and let the Super Agent route work to the right agent. The app must run for agents to execute.

**Bernstein (headless task dispatch):**
```
bernstein -g "goal"  (terminal)
    |
    v
Task server (deterministic Python, .sdd/ files)
    |
    +-- Task A -> claude (isolated worktree) -> janitor -> merge
    +-- Task B -> codex  (isolated worktree) -> janitor -> merge
    +-- Task C -> gemini (isolated worktree) -> janitor -> merge

Verification: pytest + ruff + file checks before merge
```

Bernstein's value is unattended operation: `bernstein --headless --budget 20` works the backlog until empty or budget hit. No GUI, no app to keep open.

---

## When GUI + delegation beats headless dispatch

- **You're actively watching.** A Kanban board shows what's running and what's stuck. The TUI shows the same, but Dorothy's GUI is more discoverable.
- **You want to approve individual tasks.** Dorothy's Super Agent asks; Bernstein assumes you encoded acceptance in the plan file and the janitor.
- **You already run Telegram/Slack for team coordination.** Dorothy's bridges slot in.
- **Your agents do diverse work, not just coding.** Dorothy's Google Workspace MCP means an agent can read email, update a doc, book a meeting. Bernstein's janitor expects "code changes + tests pass."

## When headless dispatch beats GUI + delegation

- **The run must complete without anyone watching.** Overnight, weekend, CI, remote server. Bernstein's `--headless --budget` runs until done or broke. Dorothy wants its app running and an approving hand on Telegram.
- **You want file-state you can check into git.** `.sdd/` is text. You can diff it, `grep` it, revive it after a crash. Dorothy's state is in the app.
- **Verification is non-negotiable.** Bernstein won't merge unless the janitor's signals pass. Dorothy leaves that to the agent and the user.
- **You need 18 adapters, not 4.** Cursor, Aider, Amp, Kilo, Kiro, Goose, OpenCode, Qwen, Cody, Continue.dev, Ollama, IAC, OpenAI Agents SDK v2, Cloudflare Agents, generic — Bernstein wraps them all. Dorothy currently advertises Claude Code, Codex, Gemini, and local.

---

## When to use Dorothy instead

- **You want a dashboard.** Kanban view, per-agent status, visible logs.
- **You want to delegate through a Super Agent.** LLM-routed work across a small fleet.
- **You live in Telegram/Slack.** Remote-trigger agents from chat.
- **Your work crosses Google Workspace.** Email, docs, calendar, not just code.

---

## When to use Bernstein instead

- **The task decomposes into parallel independent subtasks.** REST endpoints + tests + docs can all happen simultaneously in isolated worktrees.
- **You need external verification.** Tests either pass or fail — agent consensus is irrelevant. Bernstein's janitor enforces this.
- **You want 17-adapter coverage.** Bernstein wraps Claude Code, Codex, Gemini CLI, Cursor, Aider, Amp, Kilo, Kiro, Goose, OpenCode, Qwen, Cody, Continue.dev, Ollama, IAC, Visionary, and a generic adapter.
- **You want cost-aware model routing.** Bernstein's bandit router assigns cheap models to simple tasks and escalates complexity.
- **You want headless, overnight operation.** `bernstein --headless --budget 20.00` runs until the backlog is empty or the budget runs out, retrying failures automatically.
- **You want a checkable audit trail.** `.sdd/` files, HMAC-chained logs, per-task cost + quality metrics.

---

## See also

- [Benchmark methodology and raw data](../../benchmarks/README.md)
- [Full comparison matrix](./README.md)
- [Bernstein vs. single agent](./bernstein-vs-single-agent.md)
</content>
</invoke>