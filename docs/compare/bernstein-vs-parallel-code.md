# Bernstein vs. Parallel Code

> **tl;dr** — Parallel Code lets you manually run multiple coding agent sessions in separate windows or tmux panes. It works fine if you want to manage the parallelism yourself. Bernstein automates the coordination: task planning, model routing, verification, and merge conflict prevention. The question is whether you want to be the orchestrator or have software be the orchestrator.

*Last verified: 2026-04-19. "Parallel Code" covers both the `github.com/johannesjo/parallel-code` desktop app (Claude Code + Codex + Gemini, git worktree per agent) and the broader manual-tmux pattern of running multiple coding agents side by side.*

---

## What each tool is

**Parallel Code** refers to both the specific desktop app at `github.com/johannesjo/parallel-code` (gives every agent its own git branch and worktree, one-click diff viewer, one-click merge, supports Claude Code, Codex, and Gemini) and the broader pattern of running multiple independent coding agent sessions simultaneously — typically via tmux, multiple terminal windows, or a session manager. The parallelism is human-coordinated: you decide what tasks to run, assign them to sessions, and resolve conflicts.

**Bernstein** automates that coordination. You provide a goal in natural language. Bernstein decomposes it into subtasks, assigns each to an agent process with the appropriate model and role, runs them in isolated git worktrees, verifies results with a janitor (tests + linter), and merges the work. The human stays out of the loop unless a task needs escalation.

---

## Feature comparison

| Feature | Bernstein | Parallel Code |
|---|---|---|
| **Task planning** | Automatic from natural language goal | Manual — you write and assign each task |
| **Parallelism** | Automatic — up to N concurrent agents | Manual — you open N sessions |
| **Git isolation** | Per-agent worktrees, automatic | Manual — you manage branches |
| **Result verification** | Automatic janitor (tests, linter, files) | Manual — you check output |
| **Merge coordination** | Automatic with conflict prevention | Manual — you resolve conflicts |
| **Model routing** | Cost-aware bandit (cheap models for simple tasks) | Fixed — same model per session |
| **Failure handling** | Automatic retry, quarantine | Manual — you rerun failed sessions |
| **Cost tracking** | Per-task, per-model, aggregated | None — check provider dashboard |
| **Headless operation** | Yes — `--headless` flag | No — requires human to monitor |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Setup complexity** | Low — `bernstein init`, `bernstein run` | Zero — just open more terminals |

---

## Architecture comparison

**Parallel Code (human-coordinated):**
```
You (the human orchestrator)
    │
    ├── Terminal 1: claude --prompt "add /users endpoint"  ──→ you review output
    ├── Terminal 2: claude --prompt "write tests for auth"  ──→ you review output
    └── Terminal 3: codex  --prompt "update API docs"       ──→ you review output
         │
         ▼
    You merge the output, resolve conflicts, check tests
```

You are the scheduler, the verifier, and the merge coordinator. If a session fails, you notice and restart it. If two sessions modify the same file, you resolve the conflict.

**Bernstein (automated orchestration):**
```
bernstein -g "add user management: endpoints + tests + docs"
    │
    ▼
Task planner (LLM) → 3 tasks: [endpoints, tests, docs]
    │
    ├── Task A → claude  (worktree: feat/task-a) → janitor → merge
    ├── Task B → codex   (worktree: feat/task-b) → janitor → merge
    └── Task C → gemini  (worktree: feat/task-c) → janitor → merge

You return when it's done. Or set --headless and don't return at all.
```

The scheduler, verifier, and merge coordinator are all software. You're not in the loop unless something escalates.

---

## The coordination tax

Managing parallel agent sessions manually costs real attention:

- **Task assignment**: Writing the right prompt for each session, ensuring no overlap
- **Monitoring**: Watching multiple terminal outputs for completion or failure
- **Verification**: Manually running tests after each session completes
- **Merge coordination**: Pulling each session's output into the main branch without conflicts
- **Cost tracking**: None — you find out at the end of the month

For 2–3 sessions on a short task, this coordination tax is acceptable. For 5+ sessions on a complex feature, it consumes most of the time savings from parallelism.

Bernstein's value proposition is eliminating the coordination tax:

| Metric | Manual parallel | Bernstein |
|---|---:|---:|
| **Human attention required** | High (monitoring + verification) | Low (setup + review) |
| **Verification before merge** | Depends on how well you verify | Janitor-enforced (pytest + ruff + file checks) |
| **Merge conflicts** | Frequent without coordination | Worktree isolation + merge queue |
| **Overnight operation** | Not possible (needs supervision) | Yes (`--headless --budget N`) |

Numeric quality claims (early pilot, n=25, +8 pp, p=0.569) live in [`benchmarks/README.md`](../../benchmarks/README.md). Earlier copies of this page cited larger deltas that were not reproducible at n=25 and have been removed.

---

## When manual parallel is better

- **You have 2–3 short tasks you've done before.** If you know exactly what each agent should do and the tasks take under 10 minutes each, the overhead of setting up Bernstein is not worth it.
- **You want to watch the agent work.** Parallel terminal sessions let you see the agent's reasoning in real time. Bernstein's agents write to trace files — less visible unless you're actively tailing them.
- **You need fine-grained control over each session.** If you want to intervene mid-task, redirect the agent, or combine manual and automated work, controlling the terminals directly gives you more flexibility.
- **The tasks aren't independent.** If each session depends on the output of the previous one, parallelism doesn't help and Bernstein's task decomposition won't either.

---

## When Bernstein is better

- **You have 4+ tasks.** Managing 4+ terminal sessions is where the coordination tax starts exceeding the time savings from parallelism. Bernstein scales linearly; human coordination doesn't.
- **You want verified output.** Bernstein's janitor runs your tests after every task. Manual parallel requires you to remember to run tests after every session.
- **You want to run overnight or in CI.** You can't watch 5 terminal sessions while you sleep. `bernstein --headless --budget 15.00` can.
- **You're doing this repeatedly.** One-off parallel sessions are fine manually. If you're running parallel coding agents multiple times a week, the setup investment in Bernstein pays off quickly.
- **You want cost data.** Bernstein logs every token spent per task per model. After 10 sessions, you have data on which task types cost how much. Manual parallel gives you a monthly provider bill with no task-level breakdown.
- **You want model routing.** Bernstein's bandit assigns cheap models (Haiku, Gemini free tier) to simple tasks and escalates complex ones. Manual parallel uses whatever model you launched each session with.

---

## Migration path

Parallel Code users typically adopt Bernstein after one of these experiences:

1. Two sessions modified the same file and creating a messy merge conflict
2. A session "completed" but the tests were failing — noticed only later
3. Left 3 sessions running and came back to find 1 had silently errored out 2 hours ago
4. Tried to run 6 sessions at once and spent more time coordinating them than the tasks took

If you haven't had any of these experiences yet, your tasks might be simple enough that manual parallelism is the right tool. If any of these sound familiar, Bernstein solves exactly those problems.

---

## See also

- [Bernstein benchmark: multi-agent vs single-agent](../../benchmarks/README.md)
- [Full comparison matrix](./README.md)
- [Bernstein vs. single agent](./bernstein-vs-single-agent.md)
