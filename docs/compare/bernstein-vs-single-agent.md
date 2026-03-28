# Bernstein vs. Single Agent

> **tl;dr** — Running a single coding agent is the right call for simple, well-scoped tasks. Bernstein exists for the cases where a single agent bottlenecks on sequential work, lacks external verification, or needs to run overnight without supervision. The benchmark data shows multi-agent wins on CI pass rate for medium-to-high complexity tasks, with a modest cost premium. For simple tasks, save the overhead.

*This comparison uses benchmark data from `benchmarks/README.md` — 25 real GitHub issues across 10 popular Python repos, run 2026-03-28 with Claude Code as the underlying agent.*

---

## The actual question

Multi-agent orchestration adds overhead: task planning, worker spawn/teardown, result verification, merge coordination. That overhead is worth paying when:

1. The problem decomposes into independent subtasks that can run in parallel
2. You need external verification (test runner, linter) before trusting agent output
3. The task runs unattended and you can't supervise failures
4. The work is too complex for a single context window

For a 2-minute fix with a clear scope, none of those conditions hold. Single agent is faster, cheaper, and simpler.

---

## Benchmark results

25 GitHub issues, categorized by complexity:

| Task type | Single agent (Claude Sonnet) | Bernstein (3× mixed) | Winner |
|---|---:|---:|---|
| **Simple** (1–2 file changes, clear scope) | $0.08 / 94 s | $0.12 / 110 s | Single agent |
| **Medium** (3–6 file changes, some planning) | $0.12 / 234 s | $0.15 / 181 s | Bernstein |
| **Complex** (7+ files, multiple concerns) | $0.29 / 480 s | $0.25 / 320 s | Bernstein |

| Metric | Single agent | Bernstein |
|---|---:|---:|
| **CI pass rate (all tasks)** | 52% | **80%** |
| **CI pass rate (medium/complex only)** | 47% | **84%** |
| **Median cost per task** | **$0.121** | $0.150 |
| **Median wall clock** | 234 s | **181 s** |
| **Linter delta (median)** | 0 | **−2** |
| **Merge conflicts** | 1 | **0** |

The 28 pp CI pass rate difference on medium/complex tasks is the headline finding. For simple tasks, the single agent wins on cost and is within noise on time.

---

## What causes the CI pass rate gap?

Three structural reasons single agents fail more:

**1. No external verification step.** When you run `claude --prompt "fix issue #42"`, the agent marks the task done when it thinks it's done — not when the tests pass. Bernstein's janitor runs `pytest` and the linter after every task. If either fails, the task goes back to the queue. The agent's self-assessment is not trusted.

**2. Context window saturation on complex tasks.** A single agent working a 7-file change accumulates context from all the wrong paths, backtracking, and self-corrections. The signal-to-noise ratio degrades. Bernstein decomposes the task into subtasks with isolated contexts — each agent starts clean with a focused scope.

**3. No second pass on assumptions.** Single agents commit to an approach early. Bernstein's manager agent can assign a verification subtask to a second agent that reads only the output, not the reasoning path.

---

## Feature comparison

| Feature | Single agent | Bernstein |
|---|---|---|
| **Setup complexity** | None — just run the CLI | Low — `pip install bernstein`, `bernstein init` |
| **Task decomposition** | Manual (you provide the prompt) | Automatic from natural language goal |
| **Parallelism** | None | Multiple agents in parallel |
| **External verification** | None | Janitor: tests, linter, file checks |
| **Model routing** | Fixed to your CLI install | Cost-aware bandit across providers |
| **Headless operation** | Manual restart on failure | `--headless` with retry + budget cap |
| **Self-evolution** | None | `--evolve` mode |
| **Audit trail** | Terminal output / session logs | `.sdd/` files, per-task cost + quality metrics |
| **Failure recovery** | Manual | Automatic retry, cross-run quarantine |
| **Cost visibility** | Provider dashboard | Per-task cost logged and aggregated |

---

## When single agent is clearly better

- **The task takes under 5 minutes.** Orchestration overhead is 15–30 seconds of spawn time plus verification. For a quick fix, this isn't worth it.
- **You're iterating interactively.** If you're reading agent output and adjusting the prompt in real time, a single agent in interactive mode is faster than Bernstein's batch flow.
- **The task is hard to decompose.** Some problems are fundamentally sequential — you can't parallelize "refactor this function" because each step depends on the previous one.
- **You need the agent's full reasoning visible.** Bernstein's agents write to files; you don't see their chain of thought in real time unless you tail the trace files.
- **You want zero infrastructure.** One command, one process, done. No task server, no janitor, no signal files.

---

## When Bernstein is better

- **The task decomposes into 3+ independent subtasks.** Adding auth middleware, writing tests, and updating the docs can all happen in parallel. Single agent does them sequentially.
- **You need verified output.** "It looks right" is not the same as "the tests pass." Bernstein's janitor is non-negotiable — agents can't self-certify.
- **You're running overnight or in CI.** `bernstein --headless --budget 20.00` runs until the backlog is empty or the $20 is gone, retrying failures automatically. A single agent left overnight either finishes or hangs silently.
- **You have mixed task complexity.** Bernstein routes simple tasks to cheap models (Haiku, free-tier Gemini) and escalates complex ones. A flat single-agent setup uses the same model for everything.
- **You want a record.** `.sdd/` files log every task, every cost, every outcome. The evolution engine uses this data to improve over time.

---

## The honest answer

Use a single agent. Then, when you notice the CI failing after "done" tasks, or spending an hour rerunning a single long session because it drifted off track halfway through, or leaving work queued overnight that needs babysitting — that's when Bernstein is the right tool.

Most users start with single-agent Claude Code or Codex. Bernstein solves specific problems that emerge after you've been using those tools for a while.

---

## See also

- [Bernstein benchmark methodology and raw data](../../benchmarks/README.md)
- [Full comparison matrix](./README.md)
- [Zero lock-in: model-agnostic orchestration](../zero-lock-in.md)
