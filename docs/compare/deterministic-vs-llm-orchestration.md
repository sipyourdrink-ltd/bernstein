# Deterministic vs. LLM-Based Orchestration

A side-by-side comparison of two approaches to multi-agent coordination, with
architecture diagrams showing the structural differences.

---

## The two approaches

### LLM-based orchestration

An LLM (the "manager agent") sits in the control plane. It reads status from
running agents, reasons about priorities, assigns tasks, and decides when work
is complete. This is the model used by CrewAI's hierarchical process, AutoGen's
GroupChat, and LangGraph's supervisor node.

```mermaid
graph TD
    Goal["User Goal\n(vague or structured)"]
    Manager["Manager LLM\n(reads agent status, assigns tasks,\ndecides priorities)"]
    A1["Agent 1\n(persistent session)"]
    A2["Agent 2\n(persistent session)"]
    A3["Agent 3\n(persistent session)"]
    Bus["Message Bus / Bulletin\n(agents signal status, hunger,\nblockers to manager)"]
    Done["Done?\n(manager decides)"]

    Goal --> Manager
    Manager -->|"assign tasks"| A1 & A2 & A3
    A1 & A2 & A3 -->|"status updates,\nhunger signals"| Bus
    Bus --> Manager
    Manager --> Done

    classDef llm fill:#f9c74f,stroke:#f3722c,color:#000
    classDef agent fill:#90be6d,stroke:#43aa8b,color:#000
    classDef problem fill:#f94144,stroke:#c1121f,color:#fff
    class Manager llm
    class A1,A2,A3 agent
```

**What goes wrong at scale:**

```mermaid
graph TD
    PA["PAPA (Manager LLM)\nfalls asleep →\nall queues drain"]
    MUFFY["MUFFY\n283 BULLETIN messages\n0 code commits\n138 STARVING/DYING/FEED ME"]
    SMARTY["SMARTY\n2 real commits / 40 claimed\nconfused its own work\nwith others' after hours of drift"]
    Phantom["5 phantom agents\n(pido, phoenix, penny, victor, goku)\n200+ noise messages\n0 useful output)"]
    Fail["Result: 737 tasks / 47 hours\n~3 of 12 agents did real work"]

    PA --> MUFFY & SMARTY & Phantom --> Fail

    classDef bad fill:#f94144,stroke:#c1121f,color:#fff
    class PA,MUFFY,SMARTY,Phantom,Fail bad
```

---

### Deterministic orchestration (Bernstein)

A Python process owns the control plane. It is a scheduler — it applies rules
mechanically, makes no LLM calls, and has no concept of "reasoning." Agents are
spawned with pre-assigned tasks, execute them, and exit. There is no idle state.

```mermaid
graph TD
    Goal["User Goal\nor plans/task.yaml"]
    Manager["Manager LLM\n(optional — only for\ngoal decomposition)"]
    TaskServer["Task Server\nREST API :8052\nFile-backed state in .sdd/"]
    Orch["Orchestrator\n(deterministic Python)\nno LLM calls"]
    TP["Tick Pipeline\nfetch · batch · prioritize"]
    TL["Task Lifecycle FSM\nOPEN → CLAIMED → IN_PROGRESS\n→ DONE → CLOSED"]
    AL["Agent Lifecycle\nheartbeat · crash detection · reap"]
    Spawner["Spawner\nbuild prompt · select adapter\nlaunch in git worktree"]
    WT1["Agent A\ngit worktree\n1-3 tasks → exit"]
    WT2["Agent B\ngit worktree\n1-3 tasks → exit"]
    WT3["Agent C\ngit worktree\n1-3 tasks → exit"]
    QG["Quality Gates\nlint · typecheck · tests · PII"]
    Janitor["Janitor\nverify concrete signals\n(file exists, tests pass,\nnot agent claims)"]
    Git["Git\ncommit / PR / merge"]

    Goal -->|"with plan file:\nskip LLM"| TaskServer
    Goal -->|"without plan file:\none-time call"| Manager --> TaskServer
    TaskServer --> Orch
    Orch --> TP & TL & AL
    TP & TL & AL --> Spawner
    Spawner --> WT1 & WT2 & WT3
    WT1 & WT2 & WT3 --> QG --> Janitor --> Git

    classDef det fill:#4361ee,stroke:#3a0ca3,color:#fff
    classDef agent fill:#90be6d,stroke:#43aa8b,color:#000
    classDef verify fill:#7b2d8b,stroke:#560bad,color:#fff
    classDef optllm fill:#f9c74f,stroke:#f3722c,color:#000
    class Orch,TP,TL,AL,Spawner,TaskServer det
    class WT1,WT2,WT3 agent
    class QG,Janitor,Git verify
    class Manager optllm
```

---

## Task state machine (deterministic FSM)

The task lifecycle is a state machine implemented in Python. Every transition has
a defined trigger. There are no judgment calls.

```mermaid
stateDiagram-v2
    [*] --> PLANNED: plan loaded
    PLANNED --> OPEN: stage deps satisfied
    OPEN --> CLAIMED: agent calls claim_next()
    CLAIMED --> IN_PROGRESS: agent begins execution
    IN_PROGRESS --> DONE: agent reports completion
    DONE --> CLOSED: janitor verification passed + merged
    IN_PROGRESS --> ORPHANED: heartbeat timeout / crash
    ORPHANED --> OPEN: retry (max_retries default=3)
    CLAIMED --> OPEN: claim expired (agent died before starting)
    IN_PROGRESS --> FAILED: max retries exceeded
    OPEN --> BLOCKED: blocking dependency added
    BLOCKED --> OPEN: blocker resolved
    OPEN --> CANCELLED: explicit cancellation
    DONE --> OPEN: janitor rejected (signals failed)
    IN_PROGRESS --> WAITING_FOR_SUBTASKS: auto-decomposed
    WAITING_FOR_SUBTASKS --> IN_PROGRESS: subtasks CLOSED
```

---

## Agent lifecycle (short-lived by design)

Agents are born with work and die when done. There is no idle state, no hunger
state, no polling loop.

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant S as Spawner
    participant A as Agent (git worktree)
    participant J as Janitor
    participant G as Git

    O->>O: fetch open tasks, group by role
    O->>S: spawn(tasks=[T1, T2, T3], role=backend)
    S->>A: build prompt + inject tasks + create worktree
    activate A
    A->>A: execute T1
    A->>A: execute T2
    A->>A: execute T3
    A->>O: POST /tasks/T1/complete
    A->>O: POST /tasks/T2/complete
    A->>O: POST /tasks/T3/complete
    deactivate A
    Note over A: agent exits — no idle state

    O->>J: verify T1, T2, T3
    J->>J: check concrete signals<br/>(file exists, tests pass, lint clean)
    J->>G: merge worktree to main
    J->>O: tasks CLOSED

    O->>O: next tick — spawn fresh agent for next batch
```

---

## Structural comparison

| Property | LLM-based | Deterministic (Bernstein) |
|----------|-----------|--------------------------|
| **Control plane** | LLM agent (manager/PAPA) | Python scheduler (no LLM calls) |
| **Scheduling decisions** | LLM reasoning on each tick | Code: priority queue + role grouping |
| **Token cost of coordination** | Thousands/tick (manager context) | Zero |
| **Agent lifetime** | Persistent session (indefinite) | Bounded: 1-3 tasks then exit |
| **Idle state** | Yes — agents poll, spam hunger signals | No — agents are dead when not working |
| **Sleep failure mode** | Critical — unrecoverable without human | Impossible — dead agents cannot sleep |
| **Context drift** | Yes — long sessions accumulate stale context | No — fresh context on every spawn |
| **Scheduling reproducibility** | Non-deterministic (LLM sampling) | Deterministic — same input = same decision |
| **Debuggability** | Cannot reproduce scheduling bugs | Unit-testable state machine |
| **Verification** | Agent claims ("I finished X") | Concrete signals (file exists, tests pass) |
| **Manager failure mode** | Cascading — all agents starve | N/A — no manager |
| **Scalability** | O(agents × tasks) in manager context | O(1) per agent per tick |
| **Coordination auditability** | LLM response logs (non-reproducible) | Python call stack (fully traceable) |

---

## Token cost model

### LLM-based orchestration

```
Per scheduling tick:
  Manager context  ≈ 5,000–20,000 tokens (grows with task count and agent count)
  Manager response ≈ 500–2,000 tokens

Per agent per idle hour:
  Hunger polling   ≈ 500–5,000 tokens (spinning, spam, status checks)
  MUFFY example:     ~50,000 tokens on hunger signaling, 0 code commits

For 737 tasks over 47 hours, 12 agents:
  Coordination overhead: tens of millions of tokens
  Useful work ratio:      ~3/12 agents = 25%
```

### Deterministic orchestration

```
Per scheduling tick:
  Orchestrator CPU ≈ <1ms, 0 tokens

Per agent spawn:
  System prompt    ≈ 1,500–3,000 tokens (one-time per batch)
  Amortized (3 tasks/batch) ≈ 500–1,000 tokens per task

For 737 tasks over 47 hours, 12 agents:
  Coordination overhead: 0 tokens
  Useful work ratio:      ~100% (agents only run when they have work)
```

---

## When each approach makes sense

### Use LLM-based orchestration when:

- Task count is small (< ~20) and agent count is small (< 4)
- Tasks are loosely structured and require creative routing decisions
- You want a demo that explains itself in plain language
- The orchestration logic itself is your product (you're selling the reasoning)

### Use deterministic orchestration when:

- Running more than a handful of parallel agents
- Task definitions are concrete (clear acceptance criteria)
- You need predictable cost (budget limits, production use)
- You need the system to run unattended for hours without human babysitting
- Debugging and auditability matter
- You're using agents as workers, not collaborators

---

## Related documents

- [ADR-001: Agent Lifecycle Model](../decisions/001-agent-lifecycle.md) — Full analysis of hunger vs. pull vs. short-lived models with rag_challenge data
- [ADR-006: No Embedded LLM in the Orchestrator](../decisions/006-no-embedded-llm.md) — Why the control plane is deterministic code
- [Why Deterministic Orchestration](../WHY_DETERMINISTIC.md) — Narrative explainer with first-principles reasoning
- [Architecture](../ARCHITECTURE.md) — Full system diagram and module breakdown
