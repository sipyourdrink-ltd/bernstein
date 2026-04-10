# Bernstein vs. Conductor

> **tl;dr** — Conductor is a workflow orchestration engine for production business processes. Bernstein is a coding agent orchestrator. They solve different problems. If your question is "how do I coordinate AI coding agents on a software development task," Conductor is not designed for that. If your question is "how do I orchestrate microservice workflows with durable execution and human-in-the-loop steps," Bernstein is not designed for that either.

*This comparison is based on publicly available documentation as of March 2026.*

---

## What each tool is

**Conductor** (Netflix Conductor, or the OSS forks/successors) is a workflow orchestration engine designed for production microservice coordination. It has durable execution, state machines, retry policies, human approval steps, and a web UI for workflow visualization. It is used to orchestrate processes like "charge customer → provision account → send welcome email → start onboarding sequence."

**Bernstein** is an orchestrator for CLI coding agents. It takes a natural language goal, decomposes it into subtasks, spawns short-lived AI coding agents (Claude Code, Codex, Gemini CLI) to work the subtasks in parallel, verifies each result, and merges the output. It is used to orchestrate processes like "add REST endpoints + write tests + update docs → run in parallel → verify → commit."

These tools share the word "orchestration" and not much else.

---

## Where the confusion comes from

Multi-agent AI systems are being built on top of general workflow engines. Teams with existing Conductor infrastructure sometimes try to use it as the coordination layer for AI coding agents. The result is usually more complexity than value: Conductor's execution model (persistent workflows, worker polling, workflow versioning) doesn't match the filesystem-native, short-lived process model that coding agents need.

Bernstein is purpose-built for the coding agent use case. No workflow versioning, no worker registration, no durable execution across process restarts — just task files, agent processes, and a janitor.

---

## Feature comparison

| Feature | Bernstein | Conductor |
|---|---|---|
| **Primary use case** | AI coding agent coordination | Production workflow orchestration |
| **Task execution** | Spawned CLI processes (short-lived) | Registered workers (long-running) |
| **State storage** | Plain files (`.sdd/`) | Database-backed (Postgres/Redis/etc.) |
| **Workflow definition** | Natural language → auto-decomposed | YAML/JSON workflow DSL |
| **Human-in-the-loop** | No (headless) | Yes — human task steps |
| **Durable execution** | No — restart from queue if process dies | Yes — survives restarts and failures |
| **Retry logic** | Quarantine + retry via janitor | Configurable per workflow step |
| **Parallel execution** | Yes — multiple agents on independent tasks | Yes — fork/join operators |
| **Web UI** | TUI + local web dashboard | Full workflow visualization UI |
| **Self-evolution** | Yes | No |
| **AI model integration** | First-class (is the point) | Plugin/worker integration |
| **License** | Apache 2.0 | Apache 2.0 |
| **Operational complexity** | Low (runs locally, no external services) | Medium–high (requires backend services) |

---

## Architecture comparison

**Conductor (production workflow engine):**
```
Workflow definition (YAML/JSON)
    │
    ▼
Conductor Server (backend services: Postgres, Redis, etc.)
    │
    ├── Worker A (polls for tasks, long-running process)
    ├── Worker B (polls for tasks, long-running process)
    └── Worker C (polls for tasks, long-running process)
         │
         ▼
    Durable state, retry policies, human approval gates
```

Workers are persistent services that register with the Conductor server and poll for work. Workflow definitions are versioned schemas. State survives restarts because it's in a database.

**Bernstein (coding agent orchestrator):**
```
bernstein -g "goal"  (terminal)
    │
    ▼
Task server (local FastAPI, no external dependencies)
    │
    ├── Task A → claude  (spawned process, exits after 1–3 tasks)
    ├── Task B → codex   (spawned process, exits after 1–3 tasks)
    └── Task C → gemini  (spawned process, exits after 1–3 tasks)
         │
         ▼
    Git worktree → janitor → merge
```

Agents are fresh processes spawned per task. State is plain files. No persistent services required. The "server" is a lightweight FastAPI process that Bernstein starts automatically.

---

## Cost comparison

Conductor requires operational infrastructure: a database, a server process, and worker services. For self-hosted deployments, this means ongoing ops work. Managed Conductor offerings add a subscription cost.

Bernstein requires nothing but the Python package and model API tokens. A typical medium-complexity session costs $1–3 in API tokens. No backend, no subscription.

---

## When Conductor is the right tool

- **You're orchestrating production business workflows**, not development tasks. Order processing, account provisioning, approval chains, notification sequences — these are Conductor's domain.
- **You need durable execution**. The workflow must survive process crashes, server restarts, and network partitions. Conductor's state machine survives these; Bernstein does not claim to.
- **You have human approval steps**. A human must review and approve before the next step proceeds. Conductor has first-class human task support.
- **You need workflow versioning and rollback**. Conductor tracks workflow definition versions and allows in-flight migrations.
- **Your team already runs Conductor**. If you have the infrastructure, adding another AI-native workflow is reasonable.

---

## When Bernstein is the right tool

- **You want to run coding agents on development tasks**. Bernstein is purpose-built for this. No DSL required — just `bernstein -g "add rate limiting to the API"`.
- **You want zero infrastructure**. No database, no server services, no ops burden. Bernstein starts a local process and exits when done.
- **You want model-agnostic agent orchestration**. Mix Claude, Codex, Gemini, and Qwen in the same run, routing each task to the cheapest capable model.
- **You want self-evolution**. Bernstein analyzes its own run metrics and improves prompts and routing over time. No equivalent exists in Conductor.
- **Your state can live in git**. Bernstein's `.sdd/` files are checked in alongside code. The audit trail is the git history.

---

## Can you use both?

Yes. If you run Conductor for production business workflows and want to add AI coding agent orchestration for your development pipeline, Bernstein is not a replacement — it's a different tool for a different problem. They don't compete for the same workload.

Some teams use Conductor for production orchestration and Bernstein for automated development tasks (CI-integrated PR generation, overnight issue triage, scheduled code quality improvements). These are complementary, not conflicting.

---

## See also

- [Bernstein benchmark: multi-agent vs single-agent](../../benchmarks/README.md)
- [Full comparison matrix](./README.md)
- [Bernstein vs. GitHub Agent HQ](./bernstein-vs-github-agent-hq.md)
