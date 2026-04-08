# Why Deterministic Orchestration

Bernstein's control plane is deterministic Python code. It never calls an LLM
to make scheduling decisions. This document explains what that means, why it
matters, and what you give up.

---

## What "deterministic orchestration" means

When you run `bernstein run`, a Python process wakes up and runs a tick loop:

1. Fetch all open tasks from the task server
2. Group them by role (backend, frontend, qa, etc.)
3. Pick the highest-priority batch for each underserved role
4. Spawn a short-lived agent with that batch
5. Monitor heartbeats; reap crashed agents; retry stalled tasks
6. Repeat

There are no LLM calls in this loop. No model decides which task to run next,
which agent to assign it to, or whether the agent is making progress. Those
decisions are made by code — specifically, by a priority queue, a role grouper,
and a state machine with defined transitions.

The orchestrator's code has no `import` of any LLM client. This boundary is
enforced architecturally, not by convention.

---

## Why this matters: the rag_challenge evidence

The design comes from a specific failure. In the `rag_challenge` competition,
Bernstein's predecessor used an LLM agent (called PAPA) as the manager. PAPA
read status from 12 running agents, reasoned about task priorities, and issued
assignments. Here is what happened:

| Metric | Observed |
|--------|----------|
| Total agents | 12 named + 5 phantom |
| Total tasks completed | 737+ over ~47 hours |
| Agents that did real work | ~3 of 12 |
| MUFFY (worker agent) code commits | 0 |
| MUFFY BULLETIN messages | 283 (138 were hunger spam) |
| SMARTY real commits / claimed commits | 2 / 40 |
| Times PAPA fell asleep | Multiple, unrecoverable without human |

PAPA fell asleep. When PAPA fell asleep, every downstream agent starved, because
PAPA was responsible for keeping their task queues full. The system had a
single non-deterministic point of failure, and it failed.

Five "phantom agents" — spawned without identity or tasks — generated 200+
noise messages and zero useful output. Long-running agents (like SMARTY) lost
track of what they had done versus what others had done, and started claiming
credit for commits they did not make.

No amount of prompt engineering fixed it. The system prompt had 350 lines of
anti-sleep instructions. They did not work reliably. Sleep is not a prompt
engineering problem — it is a fundamental property of long-lived LLM sessions.

The conclusion: **the orchestration layer must be code, not a model.**

---

## Four concrete problems with LLM-based orchestration

### 1. The single point of failure

An LLM manager is a single non-deterministic component in the critical path.
If it falls asleep, hallucinates a task assignment, or burns through its context
window, all downstream agents are blocked.

A deterministic scheduler does not fall asleep. It does not forget to check the
queue. If there is a bug in the scheduling code, the bug is reproducible — you
can write a test that catches it and a fix that eliminates it.

### 2. Token cost of coordination

Every tick of an LLM orchestrator costs tokens: the manager reads agent status,
reasons about priorities, issues instructions. In a system running 737 tasks
over 47 hours with 12 agents, that coordination overhead adds up to tens of
millions of tokens — tokens that produce no code, no tests, no value.

The deterministic orchestrator spends zero tokens on scheduling decisions. The
only tokens spent in a Bernstein run are on actual task execution.

### 3. Non-determinism is the enemy of debugging

When an LLM orchestrator makes a wrong decision, you cannot reproduce it. The
response depends on the exact content of the context window, the order of
messages, and sampling temperature. You cannot write a unit test that says
"given this task queue and these agent states, the orchestrator should pick task
X." You can observe the outcome but not reliably replicate the input.

The Bernstein orchestrator is a state machine. Given the same task queue and
agent states, it always makes the same decision. You can unit-test it. You can
trace exactly why task X was assigned at time T.

### 4. Coordination overhead scales non-linearly

An LLM manager reading status from 12 agents has a context window proportional
to the number of agents and outstanding tasks. At 30 agents with 500 open tasks,
that context is enormous and expensive. The manager becomes a bottleneck.

A deterministic scheduler's cost per tick is O(tasks) for the fetch and O(1)
per agent for the spawn decision — a hash lookup and a queue pop. Adding more
agents adds work linearly, not quadratically.

---

## How Bernstein implements it

### The orchestrator is a scheduler, not a reasoner

`core/orchestrator.py` is a thin façade over three subsystems:

- **Tick Pipeline** (`tick_pipeline.py`) — fetch tasks, group by role, compute
  batch assignments using deterministic priority rules
- **Task Lifecycle** (`task_lifecycle.py`) — state machine: OPEN → CLAIMED →
  IN_PROGRESS → DONE → CLOSED, with retry logic on failure or orphan
- **Agent Lifecycle** (`agent_lifecycle.py`) — heartbeat monitoring, crash
  detection, stall detection, dead agent reaping

None of these make LLM calls. They apply rules.

### Agents are short-lived by design

The spawner creates a git worktree, injects a role prompt and 1-3 pre-assigned
tasks, and launches a CLI agent (Claude Code, Codex, Gemini, or any supported
adapter). The agent executes the tasks and exits. There is no idle state, no
polling loop, no hunger mechanism.

This eliminates the sleep problem structurally. A dead agent cannot fall asleep.

### Verification is concrete, not claimed

When an agent reports a task complete, the janitor checks concrete signals —
"does this file exist?", "does the test suite pass?", "does this function appear
in the file?" — rather than trusting the agent's claim. An agent that says
"done" but leaves tests broken gets the task returned to the queue.

### LLMs appear only at explicit leaf nodes

Three places in Bernstein call an LLM, all optional and named:

| Module | Purpose | When called |
|--------|---------|-------------|
| `core/manager.py` | Decompose a high-level goal into tasks | Once per goal, if no plan file is provided |
| `core/reviewer.py` | Review completed code for quality | After janitor verification, if `reviewer.enabled: true` |
| `core/cross_model_verifier.py` | Independent diff verification | For high-stakes tasks, if configured |

None of these are in the scheduling critical path. If the manager falls asleep
mid-decomposition, the orchestrator is unaffected — you re-run the decomposition
and inject the resulting tasks. If the reviewer produces a bad verdict, the task
goes back to the queue; the orchestrator continues.

---

## What you give up

Deterministic orchestration is not free. Here are the real trade-offs:

**Ambiguity must be resolved before tasks enter the queue.** The scheduler
cannot reason about whether "add tests for the auth module" means unit tests,
integration tests, or both. Task definitions need clear acceptance criteria.
If they are vague, the agent will interpret them however seems reasonable,
which may not match what you wanted.

**No dynamic re-planning during execution.** If an agent discovers mid-task
that the original plan was wrong, the orchestrator cannot adapt the plan on its
own. You (or an agent) can add tasks to the queue at any time via `bernstein
add-task`, but the orchestrator will not infer the need to do so.

**Role-based grouping requires up-front task tagging.** The scheduler groups
tasks by role to improve context reuse within a batch. If tasks are not tagged
with a role, they land in a default pool and may be batched suboptimally.

These costs are real. They are also, in practice, the costs of writing good
task specs — which you need anyway for any automated system to do useful work.

---

## The boundary is enforced in code

The `Orchestrator` class has no import of any LLM client. Any LLM call must
go through an explicitly named module (`manager.py`, `reviewer.py`,
`cross_model_verifier.py`). When you read the orchestrator code, there are no
surprise model calls. When you grep for LLM usage, you find exactly three
files, each clearly named for its purpose.

This is not just a policy. The import graph enforces it.

---

## Summary

Deterministic orchestration means the control plane — scheduling, task
assignment, lifecycle management, retry logic — is code with no LLM calls.
Agents are short-lived, spawned with pre-assigned work and no idle state.
Verification is concrete.

This makes the system reliable at scale, predictable in cost, debuggable when
something goes wrong, and auditable after the fact. The trade-off is that it
requires clear task definitions and does not adapt dynamically to ambiguous
inputs.

The design is based on direct evidence from the `rag_challenge` competition,
where the LLM-orchestrated predecessor ran 737 tasks over 47 hours with only
~3 of 12 agents doing real work. Bernstein was built to fix that.

---

## Further reading

- [ADR-001: Agent Lifecycle Model](decisions/001-agent-lifecycle.md) — Full
  scoring analysis of hunger vs. pull vs. short-lived models with raw data
- [ADR-006: No Embedded LLM in the Orchestrator](decisions/006-no-embedded-llm.md) —
  Formal decision record with rejected alternatives
- [Architecture Comparison Diagram](compare/deterministic-vs-llm-orchestration.md) —
  Side-by-side Mermaid diagrams of LLM-based vs. deterministic orchestration
- [Architecture](ARCHITECTURE.md) — Full system diagram and module breakdown
