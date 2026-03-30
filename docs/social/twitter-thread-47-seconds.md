# Twitter Thread: "3 agents, 47 seconds"

**Platform:** Twitter/X
**Goal:** Show the concrete speed and cost advantage of parallel agents
**When to post:** Day 1 of launch

---

## Thread

**1/**
I built a declarative agent orchestrator. Here's what 3 agents did in 47 seconds that takes one agent 3 minutes.

(The orchestrator is deterministic Python — zero LLM tokens on coordination)

🧵

---

**2/**
The task: add JWT auth, write 12 tests, update the API docs.

One agent approach:
- Backend agent writes auth → waits → tests → waits → docs
- Sequential. One bottleneck blocks everything.
- Wall clock: ~3 minutes. Cost: ~$0.18

---

**3/**
Three-agent approach with Bernstein:

```
bernstein -g "Add JWT auth, tests, and API docs"
```

Bernstein decomposes the goal → spawns 3 agents in parallel, each in its own git worktree → runs verification → commits.

Wall clock: 47 seconds. Cost: $0.42.

---

**4/**
Wait — it costs *more* but you're saying it's faster?

Yes. 1.25x the cost, 3.8x faster.

If your bottleneck is wall-clock time (CI/CD pipelines, iterative dev loops), that tradeoff is usually worth it.

If you're cost-constrained, run one agent. Bernstein supports both modes.

---

**5/**
The verification pass is what makes it trustworthy.

After agents finish, a janitor process runs:
- Tests pass? ✓
- Linter clean? ✓
- No regressions against baseline? ✓

If any check fails, the task is retried, not silently merged.

---

**6/**
Model selection per task:

- Backend (JWT middleware): Claude Sonnet — complex logic
- QA (12 tests): Haiku — repetitive, cheap
- Docs: Haiku — structured output, low reasoning needed

Total: $0.42. Heavy model only where it earns its cost.

---

**7/**
What makes the 47 seconds possible: git worktrees.

Each agent gets its own checkout. No file conflicts. No agent waiting for another to release a lock. They genuinely run in parallel.

When they're done, Bernstein verifies and merges in dependency order.

---

**8/**
Full methodology:

- Benchmark: 10 runs on the same task, fresh repo each time
- CI pass rate: 80% (single agent: 52%)
- 28 pp improvement. The verification pass catches what the agent misses.
- Raw data: `benchmarks/` in the repo

---

**9/**
Works with any CLI agent: Claude Code, Codex, Gemini CLI, Aider, Qwen.

No API key plumbing. No SDK wrappers. If it runs in a terminal, Bernstein can orchestrate it.

Mix agents in a single run. Use the cheap one for tests, the capable one for architecture.

---

**10/**
Install:

```
pipx install bernstein
bernstein init
bernstein -g "your goal here"
```

GitHub: [link]
Demo: [YouTube link]

Built this because babysitting one agent at a time doesn't scale.
