# ADR-006: No Embedded LLM in the Orchestrator

**Status**: Accepted  
**Date**: 2026-03-22  
**Context**: Bernstein multi-agent orchestration system

---

## Problem

An orchestration system needs to make decisions: which task to run next, which
agent to assign it to, whether a completed task is good enough, when to retry
versus abandon. Should those decisions be made by deterministic code, or by
an LLM?

The seductive answer is "LLM" — it can understand task semantics, resolve
ambiguity, adapt to unexpected situations. The practical answer, learned through
painful experience, is more nuanced.

---

## Decision

**The Bernstein orchestrator core — scheduling, task assignment, lifecycle
management, retry logic — is deterministic Python code. Zero LLM tokens are
spent on coordination.**

LLMs are used only at the explicit leaf nodes of the system:
- `core/manager.py` — task decomposition from high-level goals (optional, only
  when a plan file is not provided)
- `core/reviewer.py` — post-completion quality review (optional, off by default)
- `core/cross_model_verifier.py` — independent verification of completed diffs
  (optional, for high-stakes work)

Everything else — tick loop, task state machine, agent spawning, heartbeat
monitoring, quality gate execution, metrics recording — is deterministic code
with no LLM calls.

---

## Rejected alternative: LLM-as-orchestrator

The alternative model puts an LLM in the control plane loop. The manager LLM
receives task status updates, reasons about priorities, assigns work to agents,
and decides when work is done.

This is the model used by CrewAI's hierarchical process and was the model used in
the rag_challenge competition (the PAPA manager agent).

**Why rejected:**

### 1. The single point of failure problem

In rag_challenge, PAPA (the LLM manager agent) was responsible for keeping all
worker queues filled. PAPA fell asleep regularly. When PAPA fell asleep, every
downstream agent starved. The system had a single non-deterministic point of
failure that could not be fixed with better prompts.

A deterministic scheduler does not fall asleep. It does not forget to check the
queue. It does not hallucinate task assignments. If the scheduling code is buggy,
the bug is reproducible and fixable.

### 2. Token cost of coordination

Every scheduling decision by an LLM costs tokens. In a system running hundreds of tasks over days (the rag_challenge experience), LLM-based scheduling would have spent tens of thousands of tokens on coordination overhead — tokens that produce no code, no tests, no value.

The deterministic orchestrator spends zero tokens on scheduling. The only tokens
spent are on actual task execution.

### 3. Non-determinism is the enemy of debugging

When a LLM orchestrator makes a wrong scheduling decision, you cannot reproduce
it. The LLM's response depends on its context window, the exact phrasing of the
task descriptions, the current state of other agent conversations, and sampling
randomness. You cannot write a unit test for "PAPA will assign the right task
under these conditions."

The deterministic scheduler is a state machine with testable transitions. When it
makes a wrong decision, you can write a test that reproduces the input state and
verify the correct output.

### 4. Coordination overhead grows non-linearly

An LLM manager reading status from 12 agents, reasoning about priorities, and
issuing instructions has a context window proportional to the number of agents
and tasks. At 12 agents with a large task backlog, PAPA's context was enormous and
expensive. At 30 agents, it would be unmanageable.

A deterministic scheduler's coordination cost is O(1) per agent per tick — a
hash lookup and a queue pop. It scales linearly.

---

## Where LLMs are intentionally used

### Task decomposition (`core/manager.py`)

When a user runs `bernstein run --goal "Add OAuth to the API"`, Bernstein calls
the manager to decompose the goal into concrete tasks. This is appropriate because:
- It happens once per goal, not on every tick
- The output is inspectable (a plan written to a file) and correctable
- Users can bypass it entirely by providing a `plans/` YAML file

The manager is optional. If you provide `plans/my-feature.yaml`, it's never
called.

### Post-completion review (`core/reviewer.py`)

After the janitor verifies concrete signals (file exists, tests pass), an optional
LLM reviewer can evaluate code quality. This is appropriate because:
- It runs after the work is done, not during coordination
- It's opt-in (`reviewer.enabled: true` in config)
- It produces a structured verdict (pass/revise/fail) written to the task file

### Cross-model verification (`core/cross_model_verifier.py`)

For high-stakes changes (security code, data migrations), a different model can
independently review the completed diff. This is appropriate because:
- It's a pure quality gate, not a scheduling decision
- It's opt-in and configurable per task type
- It runs after completion, not in the critical path

---

## Consequences

### Benefits

**Zero token cost for coordination.** The orchestrator runs for days without
spending a single token on scheduling decisions.

**Predictable behavior.** The orchestrator's scheduling decisions are
deterministic and reproducible. Given the same task queue and agent states, it
always makes the same decision. This makes debugging possible and unit testing
straightforward.

**Scales linearly.** Adding more agents, more tasks, or more nodes to a cluster
increases the orchestrator's work linearly. There is no "manager bottleneck" as
seen in LLM-orchestrated systems.

**Auditable.** Every scheduling decision is a function call in Python code. You
can trace exactly why task X was assigned to agent Y at time Z.

### Costs

**Less flexible response to ambiguity.** The deterministic scheduler cannot
"reason about" whether two tasks should be parallelized or whether a task
description is ambiguous. It applies rules mechanically. Ambiguous tasks must
be clarified before entering the queue.

**No dynamic re-planning.** If a task reveals that the original plan was wrong,
the deterministic scheduler cannot adapt the plan. The manager LLM must be
invoked explicitly to create a new plan. In practice, the `bernstein add-task`
command handles this — users or agents can add tasks to the queue at any time.

**Requires explicit task definitions.** The LLM-orchestrated approach can infer
task boundaries from a vague goal. Bernstein requires tasks with clear acceptance
criteria. This is a feature for quality (ambiguity is made explicit) but a cost
for convenience.

---

## Implementation note

The boundary between "deterministic orchestration" and "LLM reasoning" is enforced
architecturally: the `Orchestrator` class has no import of `core/llm.py`. Any
LLM call must go through an explicit interface (`manager.py`, `reviewer.py`,
`cross_model_verifier.py`) that is clearly named and documented. When reading the
orchestrator code, you will never encounter a surprise LLM call.
