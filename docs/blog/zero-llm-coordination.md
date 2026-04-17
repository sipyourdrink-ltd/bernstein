# The Bernstein Architecture: Zero LLM Tokens on Coordination

**Published:** [DATE]
**Target:** Dev.to, Hashnode, cross-post to HN
**Reading time:** ~8 minutes

---

Most multi-agent frameworks make a quiet assumption: the LLM should decide who does what. The scheduler is a prompt. The coordinator is a model call. This feels natural — LLMs are good at reasoning, so use them to reason about task allocation.

I built Bernstein differently, and this post explains why.

---

## The problem with LLM-based orchestration

When I first prototyped Bernstein, the orchestrator was an LLM that received the task queue, the agent roster, and the current context, then decided what to do next. It "felt" intelligent. It could re-prioritize dynamically, explain its decisions, even refuse tasks that seemed underdetermined.

It was also the most unreliable part of the system.

Failure modes I saw in three weeks of testing:

**Hallucinated dependencies.** The scheduling LLM would decide that task B depended on task A even when the task graph said otherwise. This wasn't a bug — the LLM was making a plausible inference. But it was wrong, and it blocked execution.

**Inconsistent re-assignments.** The same task, presented in the same context twice, would get assigned to different agents. This made runs non-reproducible. Debugging a failure required reading LLM reasoning traces, not stack traces.

**Token overhead.** With 10 tasks in flight, each scheduling step made an LLM call. At $0.003/1K tokens with 1000-token context, each run spent $0.05–0.15 just on coordination — before any agent did any actual work.

---

## What replaced it

The Bernstein orchestrator is a priority queue over a dependency graph. Here's the actual core:

```python
def get_ready_tasks(tasks: list[Task]) -> list[Task]:
    completed_ids = {t.id for t in tasks if t.status == "done"}
    return [
        t for t in tasks
        if t.status == "open"
        and all(dep in completed_ids for dep in t.dependencies)
    ]

def tick(state: OrchestratorState) -> list[Task]:
    ready = get_ready_tasks(state.tasks)
    ready.sort(key=lambda t: t.priority, reverse=True)
    slots = state.max_parallel - state.running_count
    return ready[:slots]
```

That's scheduling. It's 12 lines of Python. Same inputs produce the same execution order, every time. No tokens spent on "what should I do next."

---

## Where LLMs actually live

Removing the LLM from coordination doesn't mean removing LLMs from the system. It means being precise about where they add value:

**Goal decomposition (once, at start).** When a user provides a natural-language goal (`bernstein -g "Add JWT auth, tests, and docs"`), an LLM breaks it into a typed task graph: roles, priorities, dependencies, effort estimates. This happens once. The output is a structured JSON object that the rest of the system treats as ground truth.

**Inside each agent.** This is where the real work happens. Each spawned agent (Claude Code, Codex, Gemini — whatever you configure) uses its full context window on a single, bounded task. No coordination overhead. The agent only thinks about the work in front of it.

**Verification summaries.** After a task completes, the janitor runs deterministic checks (tests, linter, file existence). If verification needs a human-readable summary, that's a cheap model call against structured data — not a reasoning-heavy orchestration decision.

The heuristic: LLMs do thinking. Python does coordination.

---

## The agent isolation model

Each agent runs in its own git worktree — a separate checkout of the repository in a temp directory. This has a few consequences:

1. **No file conflicts.** Agents genuinely run in parallel without stepping on each other.
2. **Clean rollback.** A failed agent's worktree gets deleted. The main branch is never touched until verification passes.
3. **Reproducible diffs.** Each agent produces a clean branch. The merge strategy is explicit, not left to the agent.

The worktree approach came from a frustration with Docker-based isolation: it requires a daemon, it's slow to spin up, and it doesn't give you a nice `git diff` at the end. Worktrees are 50ms to create, require no daemon, and integrate naturally with the repo.

---

## Model routing

Not all tasks need the same model. Bernstein routes based on task complexity:

| Task type | Default model | Reasoning |
|-----------|---------------|-----------|
| Architecture / design | Claude Sonnet | High reasoning, complex decisions |
| Feature implementation | Claude Haiku | Capable for most code tasks |
| Tests | Claude Haiku | Repetitive structure, cheap |
| Documentation | Claude Haiku | Structured output, low reasoning |

This is configurable. The routing rules live in `src/bernstein/core/router.py` — a plain Python function, not a model call.

On a representative pilot (JWT auth + tests + docs), this routing reduced token cost meaningfully compared to sending all tasks to Sonnet, with no measurable quality difference on the test and docs tasks. Exact savings vary by workload; your mileage will differ.

---

## The verification pass

Agents make mistakes. The janitor is Bernstein's answer to this.

After each agent completes, a verification pass runs:
1. `pytest` — does the test suite still pass?
2. `ruff check` — is the linter clean?
3. File existence checks — did the agent actually create what was requested?
4. No regressions — does the baseline suite pass against this branch?

If any check fails, the task is marked failed and optionally retried. The merge to main only happens when all checks pass.

In practice, the verification pass catches a meaningful fraction of failures — cases where an agent produced code that passed its own tests but broke something elsewhere. That is exactly the class of bugs that reviews catch. We haven't run a controlled benchmark with enough sample size to publish pass-rate numbers, but the directional effect is real.

---

## Tradeoffs

This architecture makes real sacrifices.

**No dynamic re-prioritization.** If mid-run context changes (an agent discovers a subtask, the user changes the goal), Bernstein doesn't adapt. You'd need to stop the run and re-plan.

**No emergent agent collaboration.** Agents don't communicate. They can't ask each other questions. This is a feature in most cases — agent-to-agent communication is a common source of cascading failures in other frameworks — but it means you can't build certain patterns.

**Task decomposition quality matters.** The initial goal decomposition is load-bearing. If the LLM produces a bad task graph (wrong dependencies, ambiguous deliverables), the orchestrator faithfully executes it, badly. We've put most of our prompt engineering into this step.

If your use case needs dynamic re-planning or agent collaboration, Bernstein is probably not the right tool. LangGraph or AutoGen would serve you better.

---

## The numbers

Scheduling overhead is genuinely zero. The orchestrator spends no tokens on coordination — it's pure Python. Agent work costs more when you run three agents in parallel than one sequentially, because you're doing more work concurrently. The scheduling cost stays at $0 regardless of scale.

We haven't published a controlled benchmark yet. The `benchmarks/` directory has infrastructure for reproducible runs if you want to measure against your own workloads.

---

## Get started

```bash
pipx install bernstein
bernstein init
bernstein -g "add tests for the auth module"
```

The orchestrator source lives in `src/bernstein/core/orchestration/`. It's the best documentation of the architecture.

GitHub: [link]

---

*The self-evolution loop (Bernstein running agents on its own codebase) is a separate post. The 30-day results are in `docs/blog/self-evolution-30-days.md`.*
