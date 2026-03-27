# Bernstein vs. Dorothy

> **tl;dr** — Dorothy is a graph-based multi-agent conversation framework: agents deliberate, pass messages, and build shared understanding over multiple turns. Bernstein is a task dispatch system: agents get isolated tasks, work them independently, and exit. Dorothy is better when the problem requires multi-turn reasoning between agents. Bernstein is better when the problem decomposes into parallel independent subtasks with external verification requirements.

*This comparison is based on publicly available documentation as of March 2026.*

---

## What each tool is

**Dorothy** is a multi-agent framework built around conversation graphs. You define agents as nodes and message-passing edges. Agents can deliberate — passing context back and forth over multiple turns until they reach a result. It's designed for problems where the reasoning process itself is the output: research synthesis, design deliberation, multi-perspective analysis.

**Bernstein** is a task dispatch orchestrator for CLI coding agents. It decomposes a goal into tasks, assigns each task to a short-lived coding agent (Claude Code, Codex, Gemini CLI), verifies the result against external criteria (tests, linter), and merges the output. The orchestrator is deterministic Python — no LLM makes scheduling decisions.

The core architectural difference: Dorothy coordinates agents that talk to each other. Bernstein coordinates agents that don't.

---

## Feature comparison

| Feature | Bernstein | Dorothy |
|---|---|---|
| **Agent communication** | None — agents are isolated | Multi-turn message passing between agents |
| **Task model** | Discrete tasks with completion signal | Conversational turns until convergence |
| **Verification** | External (tests, linter, file checks) | Internal (agent consensus, reflection loops) |
| **CLI agent support** | Yes — wraps installed CLI tools | Typically SDK-based agents |
| **Parallel execution** | Yes — independent tasks run concurrently | Depends on graph topology |
| **Deterministic coordinator** | Yes — Python, no LLM for scheduling | Graph structure defines flow |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Model routing** | Cost-aware bandit across providers | Configurable per node |
| **Headless / overnight** | Yes — `--headless` flag | Depends on deployment |
| **Open source** | Apache 2.0 | MIT |
| **Primary use case** | Parallel coding tasks | Multi-perspective reasoning and deliberation |

---

## Architecture comparison

**Dorothy (conversation graph):**
```
User goal
    │
    ▼
Orchestrator agent (LLM-based)
    │
    ├── Researcher agent  ←──┐
    │       │                │ (multi-turn message passing)
    │       ▼                │
    ├── Analyst agent  ──────┤
    │       │                │
    │       ▼                │
    └── Synthesizer agent ───┘
         │
         ▼
    Final output (after convergence)
```

Dorothy's agents communicate within the framework. The researcher can ask the analyst a clarifying question. The synthesizer reads all previous messages. Completion is defined by the graph reaching a terminal node, not by external test results.

**Bernstein (isolated task dispatch):**
```
bernstein -g "goal"  (terminal)
    │
    ▼
Task server (deterministic Python)
    │
    ├── Task A → claude  (no knowledge of B/C) → janitor → merge
    ├── Task B → codex   (no knowledge of A/C) → janitor → merge
    └── Task C → gemini  (no knowledge of A/B) → janitor → merge

Verification: pytest + ruff pass rate, not agent consensus
```

Bernstein's agents don't communicate. Each one reads its task description and the codebase, works independently, and exits. The janitor verifies the output against objective criteria. There's no deliberation — just execution and verification.

---

## When isolation beats deliberation

For coding tasks, isolation has specific advantages:

**Hallucination containment.** If Agent A takes a wrong turn and reasons incorrectly about an API, that incorrect reasoning doesn't propagate to Agent B and C. Each agent starts from the codebase, not from other agents' assumptions.

**Parallelism.** Independent agents can run simultaneously. Dorothy's conversation graphs have sequential dependencies by design — Agent B can't respond until Agent A sends its message.

**Objective verification.** "The tests pass" is a verifiable criterion. "The agents reached consensus" is not. For coding tasks, Bernstein's external janitor provides a ground truth that agent self-assessment can't.

**Speed.** Multi-turn deliberation is slow. A Dorothy workflow with 3 agents doing 4 turns each is 12 LLM calls before any code is written. Bernstein dispatches coding agents immediately and measures results.

---

## When deliberation beats isolation

**Research and synthesis.** "Analyze this codebase from a security perspective, cross-check with performance implications, and produce a recommendation" — this problem benefits from agents challenging each other's assumptions. Dorothy's message passing is designed for this.

**Ambiguous requirements.** If the task requires agents to negotiate what "done" means before starting, a conversation graph is better than discrete task dispatch. Bernstein assumes requirements are clear enough to write a task description.

**Multi-perspective review.** Code review where a security agent, a performance agent, and a readability agent all comment on the same diff — and respond to each other's objections — is a deliberation problem. Bernstein's agents don't see each other's output.

**Problems without external verification.** If there's no test suite, no linter, and no objective completion criterion, Bernstein's janitor has nothing to check. Dorothy's agent consensus becomes the verification mechanism.

---

## Cost and performance

Dorothy's multi-turn model means more LLM calls per outcome. If 3 agents do 4 turns each, that's 12 API calls before completion. For coding tasks with clear requirements and a test suite, this is overhead without benefit.

Bernstein benchmark (25 GitHub issues, 2026-03-28):

| Metric | Bernstein | Typical multi-turn deliberation |
|---|---:|---|
| **API calls per task** | 1 per agent (3–5 total) | 10–20 (3 agents × 4–6 turns) |
| **Median cost per task** | $0.150 | Varies (2–5× higher for equivalent complexity) |
| **CI pass rate** | 80% | Depends on task type |
| **Verification method** | External: pytest, ruff | Internal: agent consensus |

The cost comparison is task-dependent. For deliberation-appropriate tasks (research, analysis, design review), Dorothy's additional API calls produce qualitatively better output. For implementation tasks with clear specs, those extra turns are wasted.

---

## When to use Dorothy instead

- **The task requires agents to challenge and refine each other's reasoning.** Research synthesis, design review, threat modeling, competitive analysis — problems where multiple passes improve quality.
- **Requirements are ambiguous and need negotiation.** If the agents need to decide what to build before building it, a conversation graph is better than a task queue.
- **There's no external verification criterion.** If success is "the output is good" rather than "the tests pass," deliberation is the verification mechanism.
- **You want visible agent reasoning.** Dorothy surfaces the full conversation between agents. Bernstein shows task outcomes, not reasoning paths.
- **You're building an agent application, not automating coding tasks.** Dorothy is a framework for building products. Bernstein is a tool for running software development workflows.

---

## When to use Bernstein instead

- **The task decomposes into parallel independent subtasks.** REST endpoints + tests + docs can all happen simultaneously. Dorothy's sequential message passing doesn't help here.
- **You need external verification.** Tests either pass or fail — agent consensus is irrelevant. Bernstein's janitor enforces this. Dorothy doesn't have an equivalent.
- **You want CLI agent support.** Bernstein wraps Claude Code, Codex, Gemini CLI, and Qwen as installed CLI tools. Dorothy typically uses SDK-based agent construction, requiring more integration work.
- **You want cost-aware model routing.** Bernstein's bandit router assigns cheap models to simple tasks and escalates complexity. Dorothy's model selection is per-node configuration.
- **You want headless, overnight operation.** `bernstein --headless --budget 20.00` runs until the backlog is empty or the budget runs out, retrying failures automatically.

---

## See also

- [Bernstein benchmark: multi-agent vs single-agent](../../benchmarks/README.md)
- [Full comparison matrix](./README.md)
- [Bernstein vs. single agent](./bernstein-vs-single-agent.md)
