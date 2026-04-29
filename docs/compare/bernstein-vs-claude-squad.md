# Bernstein vs. Claude Squad

> **tl;dr** — Claude Squad is a tmux-based TUI that lets a single developer juggle several Claude Code (or Codex / Aider / Gemini) sessions side-by-side, each in its own git worktree. Bernstein is a multi-agent orchestrator: a deterministic Python scheduler that decomposes a goal into tasks, hands them to one of 31+ CLI agents, runs quality gates, and merges the results. Claude Squad is a session manager. Bernstein is an orchestrator. If you want a richer interactive control panel for hand-driven parallel work, Claude Squad is solid. If you want a goal turned into shipped, verified code with no human in the inner loop, Bernstein is the right shape.

*Last verified: 2026-04-27. Based on `github.com/smtg-ai/claude-squad` (~7.2k stars, AGPL-3.0, Go, last release v1.0.17, last commit 2026-03-28, project active).*

---

## What each tool is

**Claude Squad** (AGPL-3.0, Go, installed as `cs`) is a terminal app for managing multiple AI coding sessions in parallel. It launches each agent inside its own tmux session and its own git worktree, then renders a TUI with a session list, a preview pane, and a diff tab. Default agent is `claude`, but `cs -p codex`, `cs -p aider ...`, `cs -p gemini`, and a config-file `profiles` array let you choose what runs in each session. Keybindings drive the workflow: `n` new session, `N` new with prompt, `↵` attach, `s` commit + push, `c` checkout-and-pause, `r` resume, `D` kill. Configuration lives in `~/.claude-squad/config.json`. Prerequisites are `tmux` and `gh`. There is no plan format, no cross-session scheduler, no automated verification — the human is the orchestrator, and the TUI is the control surface.

**Bernstein** (Apache 2.0, Python 3.12+) is a multi-agent orchestrator. Given a goal or a YAML plan with stages and steps, it decomposes work into tasks, spawns short-lived agents in isolated worktrees, runs quality gates (tests, lint, types, file checks) via the janitor module, retries with model escalation on failure, and merges what passes. The orchestrator itself is deterministic Python — no LLM tokens spent on coordination. State is file-based under `.sdd/`. It ships 31 cooperating CLI agent adapters plus 2 leaf-node delegation adapters (Composio, Ralphex), an MCP server, a task server (FastAPI on `127.0.0.1:8052`), cost tracking with budget caps, and a per-step `cli:` field so a plan can switch CLIs between stages.

---

## Feature comparison

| Feature | Bernstein | Claude Squad |
|---|---|---|
| **Primary focus** | Goal → tasks → verified merged code | Multiple agent sessions in one TUI |
| **License** | Apache 2.0 | AGPL-3.0 |
| **Language** | Python 3.12+ | Go |
| **Install** | `pip install bernstein` (or `uv`) | `brew install claude-squad` / `install.sh` |
| **Interface** | CLI + TUI + web dashboard + MCP | TUI (tmux-backed) |
| **Orchestrator logic** | Deterministic Python scheduler | None — the human picks tasks |
| **Plan format** | YAML stages + steps, `depends_on`, `complexity` | None |
| **Agent adapters** | 31 cooperating + 2 delegation (Claude, Codex, Gemini, Aider, Amp, Goose, OpenCode, Cursor, Cody, Continue, Kilo, Kiro, Qwen, Ollama, generic, …) | Any agent, but as a launch command per session (`-p "claude"`, `-p "codex"`, `-p "aider …"`) |
| **Per-stage agent switching** | Yes — `cli:` field per step (PR #965) | No — one program per session at creation time |
| **Git worktree isolation** | Yes — per task | Yes — per session |
| **Parallel execution model** | Scheduler dispatches N tasks across worktrees, ticks deterministically | tmux panes, human switches between them |
| **Quality gates** | Janitor: tests, lint, types, file existence; blocks merge on fail | None — diffs shown in TUI for human review |
| **Automated retry** | Yes — with model escalation on failure | No |
| **Cross-agent messaging** | Bulletin board (`POST /bulletin`) | None |
| **State storage** | `.sdd/` files (backlog, runtime, metrics, config) | `~/.claude-squad/` config + tmux session state |
| **Cost tracking** | Per-task tokens + USD, budget caps, anomaly detection | None |
| **MCP server** | First-class (`bernstein mcp serve`) | None |
| **Headless / CI** | Yes — runs without TTY, no tmux dependency | No — requires interactive tmux + TTY |
| **Multi-repo** | Yes — workspace orchestration across repos | One repo per `cs` invocation |
| **Cloud execution** | Cloudflare Workers adapter | None |
| **Self-evolution** | `bernstein --evolve` | No |
| **Audit trail** | HMAC-chained file logs | None |
| **Auto-PR with cost summary** | `bernstein pr` | `s` keybind: commit + push to GitHub via `gh` |
| **Yolo / auto-accept** | Per-task policy + tool-call approval | `--autoyes` flag (experimental) |
| **Prereqs** | Python 3.12+, git | tmux, `gh` |

---

## Architecture comparison

**Claude Squad (tmux session manager):**
```
cs (TUI, Go)
    │
    ├── tmux session A → claude   (worktree A) ── human attaches via Enter
    ├── tmux session B → codex    (worktree B) ── human attaches via Enter
    └── tmux session C → aider    (worktree C) ── human attaches via Enter

State: ~/.claude-squad/config.json + live tmux sessions
Coordination: human keyboard input
```

The human decides what each session works on, attaches to inspect or re-prompt, and presses `s` to commit and push. There is no scheduler — sessions run independently, and the only cross-session structure is the list rendered in the TUI.

**Bernstein (deterministic orchestrator):**
```
bernstein run plan.yaml   (terminal, CI, SSH, MCP)
    │
    ▼
Task server (FastAPI 127.0.0.1:8052) + deterministic tick pipeline
    │
    ├── Task A → claude   (worktree A) → janitor (tests/lint/types) → merge
    ├── Task B → codex    (worktree B) → janitor (tests/lint/types) → merge
    └── Task C → gemini   (worktree C) → janitor (tests/lint/types) → retry → escalate model → merge

State: .sdd/ files; cost: per-task tokens + USD with budget caps
Coordination: deterministic Python (no LLM tokens spent on scheduling)
```

A plan defines stages and steps. The scheduler picks tasks, picks a model and effort per task, dispatches to the right adapter, runs quality gates on the result, retries failures, and merges what passes. The human sets the goal; the orchestrator runs the loop.

---

## The fundamental difference

Claude Squad answers: *"How do I keep five Claude Code sessions in front of me without losing my mind?"*

Bernstein answers: *"How do I turn a goal into shipped, tested code without sitting at the keyboard?"*

Both use git worktrees for isolation. Both can run multiple agents at once. The difference is who drives. In Claude Squad, the human is the scheduler — picking which session to attend to, when to commit, when to abandon a branch. In Bernstein, the scheduler is deterministic Python code, and the human reads results.

---

## When Claude Squad is the right tool

- **Single-developer interactive workflows.** You want to babysit a few agents, watch their output, prompt them mid-flight, and decide on the spot what to keep. The TUI is purpose-built for this.
- **Lightweight, no-config, no-server.** No daemon, no API server, no plan files. Install, run `cs`, press `n`. Total config is one JSON file.
- **You like tmux.** If your existing workflow lives in tmux and you want AI sessions as additional panes, Claude Squad fits naturally. The diff tab and preview pane are well-designed for terminal-native review.
- **One repo at a time.** If your work is bounded by a single repository and a handful of parallel branches, the simplicity is a feature, not a limitation.
- **You don't need automated verification.** You're going to read every diff anyway; the human eye is the quality gate.

This is a real niche. For a developer who wants parallelism with full manual control, Claude Squad is a better fit than Bernstein — Bernstein's machinery (plan files, quality gates, task server, cost tracking) is overhead you don't need.

---

## When Bernstein is the right tool

- **Provider breadth.** 31 cooperating adapters plus 2 leaf-node delegation adapters. You can mix Claude, Codex, Gemini, Aider, Amp, Kilo, Kiro, Qwen, Goose, OpenCode, Cody, Continue, and Ollama in the same run. Claude Squad treats every agent as a launch command — fine for one-at-a-time, but with no shared task model across them.
- **Deterministic scheduling.** The orchestrator is Python code, not an LLM. No tokens spent deciding which agent gets which task. Reproducible runs, replayable from `.sdd/`.
- **Quality gates before merge.** The janitor runs tests, lint, type checks, and file-existence assertions. Failures retry, optionally with model escalation. Claude Squad shows a diff and lets you decide.
- **Plan files.** YAML with `stages`, `steps`, `depends_on`, `goal`, `role`, `priority`, `scope`, `complexity`. Encodes "build the API, then the migrations, then the tests" once and replays it deterministically. Claude Squad has no equivalent.
- **Headless / CI execution.** Bernstein runs without a TTY, with no tmux dependency, in GitHub Actions or any CI runner. Claude Squad needs an interactive tmux session.
- **MCP server first-class.** Other tools and Claude itself can drive Bernstein via MCP. Claude Squad has no MCP surface.
- **Multi-repo orchestration.** One run can span multiple repositories. `cs` is one repo per invocation.
- **Cost tracking with budgets.** Per-task token counts, per-task USD, anomaly detection, hard budget caps. Claude Squad doesn't track cost.
- **Cross-agent communication.** The bulletin board lets agents post findings or blockers another task can read. Claude Squad sessions are siloed.
- **Cloud execution.** Cloudflare Workers adapter for ephemeral remote execution.

---

## How to migrate from Claude Squad to Bernstein

If you already use Claude Squad and want to try Bernstein for a specific run rather than as a wholesale replacement, the path is short.

1. **Keep using `cs` for interactive work.** Bernstein doesn't replace the TUI loop; it replaces the goal-decomposition-and-verification loop. Run both.
2. **Translate one repeated workflow into a plan file.** If you find yourself spinning up the same three sessions every Monday — "refactor the auth module, write the tests, update the docs" — that's a plan. Write it as `plans/auth-refactor.yaml` with three steps and a `depends_on`.
   ```yaml
   stages:
     - name: refactor
       steps:
         - goal: "Refactor auth module to use the new session abstraction"
           role: backend
           cli: claude
     - name: tests
       depends_on: [refactor]
       steps:
         - goal: "Add unit tests for the refactored auth module"
           role: qa
           cli: codex
     - name: docs
       depends_on: [refactor]
       steps:
         - goal: "Update auth.md to reflect the new API"
           role: docs
   ```
3. **Run it.** `bernstein run plans/auth-refactor.yaml`. Worktrees, quality gates, cost tracking, and merge happen without you attaching to any session.
4. **Keep the human-driven sessions in `cs`.** When a task is exploratory and you want to drive it yourself, that's still Claude Squad's job.

The two tools coexist cleanly because they answer different questions. `cs` is for "I want to drive five agents at once." `bernstein run` is for "I want this goal shipped while I do something else."

---

## See also

- [Bernstein vs. Crystal](./bernstein-vs-crystal.md) — another tmux-adjacent multi-agent TUI
- [Bernstein vs. Conductor](./bernstein-vs-conductor.md) — orchestrator comparison
- [Full comparison index](./README.md)
