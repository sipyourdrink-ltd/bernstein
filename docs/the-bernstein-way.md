# The Bernstein Way

How Bernstein thinks about multi-agent orchestration. These are defaults, not laws — every tenet has an escape hatch.

Bernstein is infrastructure for **agentic engineering** — the discipline of orchestrating AI coding agents while humans own architecture, quality, and intent. The term was [coined by Andrej Karpathy](https://x.com/karpathy/status/1886192184808149383) as the rigorous successor to "vibe coding": you are not writing code directly, you are directing agents who do.

Addy Osmani [distinguishes](https://addyosmani.com/blog/future-agentic-coding/) two modes: **conductor** (one agent, synchronous, pair-programming) and **orchestrator** (multiple agents, parallel, asynchronous). Bernstein is an orchestrator. You describe what needs to happen; the system handles decomposition, scheduling, agent lifecycle, verification, and integration.

## Default lifecycle

```
Goal
  │
  ▼
Decompose ─── LLM breaks goal into tasks with roles, priorities, dependencies
  │
  ▼
Spawn agents ─ one fresh CLI process per task, in its own git worktree
  │
  ▼
Execute ────── agents work in parallel, isolated checkouts, no conflicts
  │
  ▼
Verify ─────── janitor checks: tests pass, linter clean, files exist
  │
  ▼
Merge ──────── verified branches integrate into main
  │
  ▼
Done ───────── clean git history, passing CI, cost report
```

---

## 1. Agents are disposable

Agents spawn, do 1-3 tasks, and exit. No long-running sessions, no accumulated context, no drift. A fresh agent starts with exactly the state it needs — the task description, the relevant files, and a role-specific system prompt.

This solves the "agent sleep" problem we hit running 12 agents for 47 hours: long-running agents stop picking up new work, lose track of project state, and waste tokens re-reading context they've already seen. Short-lived agents don't have these failure modes.

**Escape hatch:** Set `estimated_minutes` high on a task and the spawner will keep the session alive longer. You can also batch related tasks into one session via task grouping.

## 2. The orchestrator is code, not AI

Scheduling, dependency resolution, model routing, and budget tracking are deterministic Python. Zero LLM tokens spent on coordination. The orchestrator reads task metadata (scope, complexity, priority, dependencies) and makes routing decisions with simple rules.

This matters because LLM-based schedulers are slow, expensive, and non-deterministic. When agent A finishes and three tasks become unblocked, you want the next spawn to happen in milliseconds, not after a 2-second API round-trip that might hallucinate a different plan.

**Escape hatch:** The planning step (goal decomposition) does use an LLM — that's where judgment is needed. Everything after planning is code.

## 3. Git worktree isolation

Each agent gets its own git worktree at `.sdd/worktrees/{session_id}` on a branch named `agent/{session_id}`. Two agents editing the same file in the same checkout is the fastest way to corrupt a codebase. Worktrees eliminate this by giving every agent a full, independent working copy.

The tradeoff is disk space — each worktree is a shallow copy of the repo. For most codebases this is negligible. For monorepos with large binary assets, it adds up.

**Escape hatch:** Disable worktrees with `worktree: false` in `bernstein.yaml`. Agents will share the main checkout and rely on file ownership declarations in task metadata to avoid conflicts. This is faster to set up but less safe.

## 4. Verification before merge

The janitor verifies every completed task before its branch merges. Verification checks are defined per-task in `completion_signals`:

- `test_passes` — run a test command, assert exit code 0
- `path_exists` — confirm expected files were created
- `lint_clean` — run the project linter
- `llm_judge` — optionally use an LLM to review the diff against the spec

Nothing merges unchecked. If verification fails, the task is marked failed and can be retried or routed back as a new task. This is the difference between "agents wrote some code" and "agents produced working code."

**Escape hatch:** Set `skip_verification: true` on a task to bypass the janitor. Useful for exploratory or documentation-only tasks where there's nothing to verify.

## 5. Cost budgeting

Every run has a dollar cap (`--budget 5.00` or `budget_usd` in config). When the cap is reached, no new agents spawn. The orchestrator tracks spending per task and per model.

Cheap models handle boilerplate (docs, formatting, simple tests). Heavy models handle architecture and complex logic. The router picks the model based on task `scope` and `complexity` fields — small/low tasks get Sonnet-class models at normal effort, large/high tasks get Opus-class at max effort.

**Escape hatch:** Set `budget_usd: 0` for unlimited spending (not recommended). Override model routing per-task with explicit `model` and `effort` fields in the task definition.

## 6. File-based state

`.sdd/` is the entire runtime state of a Bernstein run. Backlog, active tasks, metrics, agent logs, worktree metadata, evolution proposals — all plain files. No database, no message queue, no hidden state in memory.

This means you can inspect, edit, or script against the state with standard Unix tools. `cat .sdd/backlog.yaml` shows the task queue. `ls .sdd/worktrees/` shows active agents. If something goes wrong, the state is right there.

**Escape hatch:** None needed. This is a hard constraint. If state doesn't live in files, it doesn't exist.

## 7. Provider agnostic

Bernstein works with any CLI agent that accepts a prompt and writes to stdout. The adapter interface is four methods: `spawn`, `kill`, `is_alive`, `name`. Built-in adapters cover Claude Code, Codex, Gemini CLI, and Qwen. The `GenericAdapter` handles anything else.

You can mix providers in a single run — Claude for architecture tasks, Codex for tests, Gemini for docs. The orchestrator does not care which agent handles which task. Your prompts, task graphs, and roles are portable across providers.

**Escape hatch:** Write a custom adapter by subclassing `CLIAdapter` from `bernstein.adapters.base`. Implement `spawn` and `name`, and the rest works.

## 8. CI feedback loop

Every push triggers CI. When CI fails, the failure is routed back as a new task: "fix the lint error in `src/auth.py:42`" or "test `test_rate_limit` is failing after the last merge." The janitor catches regressions locally before push, but CI is the final gate.

This creates a self-healing loop: agents write code, CI catches what the janitor missed, failures become tasks, agents fix them. The loop runs until CI is green or the budget runs out.

**Escape hatch:** Disable CI integration by omitting the `ci` section from `bernstein.yaml`. The system works without it — you just lose the automatic failure-to-task routing.

---

## Summary of tradeoffs

| Tenet | You get | You give up |
|-------|---------|-------------|
| Disposable agents | No context drift, no sleep problem | Startup cost per task (~2-5s) |
| Code orchestrator | Speed, determinism, zero coordination cost | Can't do fuzzy re-planning mid-run |
| Worktree isolation | No merge conflicts during execution | Disk space per worktree |
| Verification | Nothing merges broken | Slower cycle time per task |
| Cost budgeting | Predictable spend | Hard stop when budget hits |
| File-based state | Full inspectability | No query language (just grep) |
| Provider agnostic | No lock-in | Least-common-denominator features |
| CI feedback | Self-healing | CI minutes / additional cost |

These are the defaults. They work for most projects. When they don't, the escape hatches are there. But start here.
