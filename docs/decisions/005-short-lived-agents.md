# ADR-005: Short-Lived Agent Lifecycle

**Status**: Accepted  
**Date**: 2026-03-22  
**Context**: Bernstein multi-agent orchestration system  
**Supersedes**: ADR-001 (extends it with implementation conclusions)

---

## Problem

When orchestrating multiple CLI coding agents in parallel, agents that finish
their current work and have nothing immediately queued enter an idle state. In
practice, this idle state is indistinguishable from the agent being stuck,
confused, or dead. The result: wasted tokens, phantom work claims, and
orchestrator complexity.

The alternative is to simply kill agents when they have no work. But killing
processes is disruptive and spawning new ones is expensive. How do you find
the right balance?

ADR-001 evaluated four lifecycle models in detail. This ADR documents the final
implemented model and its rationale, building on the data from ADR-001's
production analysis of the rag_challenge system (12 agents, 737 tasks, 47 hours).

---

## Decision

**Agents are short-lived. They spawn with a batch of 1–3 tasks, execute them
sequentially, and exit cleanly. The orchestrator spawns new agents as work
becomes available.**

There is no idle state. There is no polling loop. There are no "is anyone there?"
pings. An agent either has work or it doesn't exist.

### Lifecycle

```
Orchestrator has open tasks
         │
         ▼
Batch assembler groups 1–3 related tasks
         │
         ▼
Spawner launches CLI agent in isolated git worktree
  - Provides: identity, role prompt, task list, context files
  - Starts heartbeat monitoring
         │
         ▼
Agent executes tasks (reads files, writes code, runs tests)
         │
         ▼
Agent exits (success: all tasks complete, or failure: error/timeout)
         │
         ▼
Orchestrator collects results, runs quality gates, spawns next batch
```

### Hard constraints

1. **Max lifetime**: Agents are killed after a configurable wall-clock limit
   (default: 30 minutes) regardless of claimed progress. Incomplete tasks return
   to the queue. This prevents the context drift observed in long-lived sessions.

2. **Max tasks per batch**: 1–3 tasks. Above 3, context accumulates enough
   stale information that the agent's performance degrades measurably. Below 1
   is vacuous.

3. **No idle state**: An agent that completes its batch exits. It does not poll
   for more tasks. The orchestrator is responsible for deciding whether to spawn
   a new agent or wait.

4. **No inter-agent messaging during execution**: Agents write results to files
   and exit. The orchestrator reads results between spawns. This eliminates the
   "hunger spam" problem (138 `STARVING/DYING/FEED ME` messages from the
   rag_challenge analysis).

---

## Rejected alternatives

### Option A: Persistent sessions (the rag_challenge model)

Agents run indefinitely. They poll a task queue. When the queue is empty, they
either sleep or post status messages.

**Why rejected:**

The rag_challenge data is definitive. 12 named agents, 47 hours of operation.
Observed failure modes:

- **Sleep problem**: Agents that exhausted their task queue stopped working
  silently. Anti-sleep instructions in the system prompt ("NEVER sleep, NEVER
  stop") did not reliably prevent this. The failure mode is a fundamental
  property of long-lived LLM sessions, not a prompt engineering problem.
- **Hunger spam**: MUFFY produced 283 bulletin messages, the vast majority
  being idle status reports (`STARVING/DYING/FEED ME`). Zero code commits.
  ~50,000 tokens consumed on signaling rather than work.
- **Context drift**: After hours of operation, agents confused their own work
  with others' (SMARTY claimed credit for PAPA's commits), accumulated stale
  context, and degraded in output quality. There is no mechanism to "refresh"
  an agent's context without killing and respawning.
- **LLM orchestrator single point of failure**: The PAPA manager agent was
  responsible for keeping all worker queues filled. When PAPA fell asleep, the
  entire system stalled. This is not fixable with better prompts — it is an
  architectural single point of failure.

Only 3 of 12 agents (FRANKY, SISSY, ROCKY) were reliable producers. The rest
were net consumers of tokens.

### Option B: Pure pull (spawn per task, exit after one)

Agent spawns for a single task, completes it, exits. Maximum freshness,
zero idle waste.

**Why not chosen as the primary model:**

At 737 tasks and ~3–5K tokens per spawn for context loading, pure pull costs
~2–4M tokens in spawn overhead alone. Many tasks are related (e.g., "implement
function X" followed by "write tests for function X") — context learned on the
first task is directly applicable to the second. Batching 2–3 related tasks
amortizes spawn cost and preserves useful context.

Pure pull is available as a configuration option (`batch_size: 1`) for cases
where task isolation matters more than efficiency.

---

## Consequences

### Benefits

**The sleep problem is architecturally eliminated.** An agent that has no work
simply doesn't exist. There is no idle state to drift into.

**Token efficiency.** Spawn overhead is ~3–5K tokens per batch. Amortized over
2–3 tasks, that's ~1.5–2.5K tokens per task. The rag_challenge model spent
more than this on idle signaling per agent.

**Bounded context degradation.** A 30-minute wall-clock limit ensures agents
never accumulate the stale context that caused identity drift in rag_challenge.
Fresh agents read current file state; they don't carry stale assumptions.

**Deterministic orchestration.** The orchestrator is Python code, not an LLM.
It cannot fall asleep. It cannot misunderstand a task. It cannot be confused by
a long conversation history. Every scheduling decision is auditable in the source
code.

**Correct failure handling.** If an agent crashes mid-task, the heartbeat timeout
detects it and the task returns to the queue. A new agent starts fresh with the
same task. The agent's partial work remains in the git worktree and can be
inspected; the clean branch is unaffected.

### Costs

**Per-batch spawn latency.** Each new batch takes 5–30 seconds to spawn a CLI
agent process. For workloads with many tiny tasks (< 1 minute each), this overhead
is significant. Mitigation: keep tasks at the right granularity (10–30 minute
estimated duration).

**No context accumulation across batches.** If a long-running feature requires
deep understanding of a complex codebase, each new batch re-reads the relevant
files. This is usually not a problem — CLI agents like Claude Code read files as
part of their normal operation — but it is a difference from persistent sessions.

**Batch assembly complexity.** The orchestrator must decide which tasks to group
in a batch. Currently: group by role (same role type), then by code proximity
(tasks touching similar file paths). This is a heuristic and can be tuned.

---

## Implementation

The spawner (`core/spawner.py`) builds the spawn prompt with:
1. Role identity and system prompt from `templates/roles/<role>.md`
2. Task list (1–3 tasks with full goal descriptions)
3. Context file list (relevant code files for the task batch)
4. Exit instructions (what to do when all tasks are done)

The agent lifecycle module (`core/agent_lifecycle.py`) monitors heartbeat files.
If the heartbeat goes stale beyond the configured timeout, the agent process is
killed and its claimed tasks return to the open queue.

The batch assembler (part of `core/tick_pipeline.py`) groups open tasks by role
and code area. Related tasks (same directory, same module) are grouped together
to maximize context reuse within a batch.
