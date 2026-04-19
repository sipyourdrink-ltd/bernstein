# Bernstein vs. Crystal

> **tl;dr** — Crystal uses iterative self-review loops: an agent writes code, a reviewer agent critiques it, the writer revises, repeat until quality gates pass. Bernstein uses external verification: the janitor runs tests and the linter, and trusts those results over agent self-assessment. Crystal is better when objective test suites are thin or absent. Bernstein is better when tests are the ground truth and you want fast, deterministic verification.

*Last verified: 2026-04-19.*

---

## What each tool is

**Crystal** is a multi-agent coding framework that wraps each coding task in a review loop. A writer agent produces output; a reviewer agent critiques it against a rubric; the writer revises; the cycle continues until the reviewer approves or a max-iteration limit is reached. The verification is agent-to-agent, internal to the system.

**Bernstein** separates writing from verification entirely. A coding agent (Claude Code, Codex, Gemini CLI) produces output in an isolated git worktree. A deterministic janitor — not another LLM — runs `pytest`, `ruff`, and file existence checks. If any check fails, the task goes back to the queue. The agent's self-assessment is not trusted.

The core difference: Crystal trusts agent judgment to improve quality through iteration. Bernstein trusts test results to define quality.

---

## Feature comparison

| Feature | Bernstein | Crystal |
|---|---|---|
| **Verification approach** | External: tests, linter, file checks | Internal: LLM reviewer agent |
| **Reviewer type** | Deterministic (pytest, ruff) | LLM-based (another agent) |
| **Review cost** | Near-zero (test runner is cheap) | Additional LLM API calls per review cycle |
| **Task isolation** | Git worktree per agent, no shared context | Shared context within review cycle |
| **Parallelism** | Yes — multiple tasks concurrently | Within-task review cycles are sequential |
| **CLI agent support** | Yes — wraps installed CLI tools | SDK-based integration |
| **Model routing** | Cost-aware bandit across providers | Configurable per role |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Headless operation** | Yes — `--headless` flag | Depends on deployment |
| **Open source** | Apache 2.0 | Varies |
| **Primary use case** | Parallel coding tasks with test verification | High-quality single-task output via review loops |

---

## Architecture comparison

**Crystal (internal review loop):**
```
Task
    │
    ▼
Writer agent (LLM) ──→ code output
    │
    ▼
Reviewer agent (LLM) ──→ critique + rubric check
    │
    ├── Approved → output
    └── Rejected → writer agent (revised prompt with critique)
         │
         └── (loop, max N iterations)
```

The review cycle is internal: two LLMs in dialogue. The quality gate is "reviewer LLM approves," not "test suite passes." This can catch issues that tests don't cover (code readability, architectural consistency, edge case reasoning) but can also miss issues that tests would catch deterministically.

**Bernstein (external verification):**
```
Task
    │
    ▼
Coding agent (Claude Code / Codex / Gemini CLI)
    │
    ▼
Janitor (deterministic)
    ├── pytest → pass/fail
    ├── ruff → lint violations count
    └── file existence checks → pass/fail
    │
    ├── All pass → commit + merge
    └── Any fail → task back to queue (retry, or quarantine after N failures)
```

The quality gate is objective: either the tests pass or they don't. No LLM reviewer is consulted. The janitor can't be fooled by confident-sounding but wrong reasoning.

---

## The reviewer hallucination problem

Crystal's LLM reviewer can approve incorrect code if the reviewer hallucinates correctness. This is the same failure mode as asking "does this look right?" — if the reviewer doesn't actually run the code, it's making an educated guess.

Bernstein's janitor is not a language model. It runs `pytest` and reads exit codes. It cannot be convinced by an argument that the code is correct if the tests fail.

This matters most for:

- **Logic errors that look syntactically correct.** A reviewer LLM may not catch off-by-one errors, incorrect algorithm choices, or wrong API usage. Tests catch these.
- **Integration failures.** Code that works in isolation but breaks when integrated. Tests that import and exercise the code catch these; a reviewing LLM reading the diff may not.
- **Regressions.** Changes that break unrelated functionality. A regression test suite catches these automatically. A reviewer LLM doesn't know what previously worked.

Crystal's review loop catches things tests don't cover — code organization, naming consistency, documentation quality, architectural alignment. For these concerns, LLM review adds value that deterministic checks can't provide.

---

## Cost comparison

Crystal's review loop adds API calls. For a 3-iteration loop with writer + reviewer:

| Review cycles | API calls | Approximate additional cost |
|---|---:|---|
| 1 (write only, no review) | 1 | $0 |
| 2 (write + one review) | 2–3 | +$0.10–0.20 |
| 3 (write + two reviews) | 4–5 | +$0.20–0.40 |
| 4 (write + three reviews) | 6–7 | +$0.30–0.60 |

Bernstein adds no extra LLM calls for verification — the janitor runs `pytest` and `ruff` and reads exit codes. Crystal's review loop adds one or more LLM calls per iteration. For a fair head-to-head on the same task mix, we are holding back quantitative claims until the Bernstein SWE-Bench Lite harness lands. See [`benchmarks/README.md`](../../benchmarks/README.md) for the current pilot (n=25, +8 pp, p=0.569).

The cost premium for Crystal is task-type-dependent. For tasks with good test coverage, Bernstein's test-based verification is faster and cheaper while providing stronger guarantees. For tasks without test coverage, Crystal's review loop provides quality signal that Bernstein can't.

---

## When test coverage changes the calculus

| Scenario | Better choice |
|---|---|
| Good test suite (80%+ coverage) | Bernstein — external verification is definitive |
| Thin test suite (< 40% coverage) | Crystal — LLM review catches what tests miss |
| No tests at all | Crystal or manual review — janitor has nothing to check |
| Tests exist but are slow (> 5 min) | Crystal may be faster despite extra API calls |
| Legacy codebase with fragile tests | Crystal — LLM review can reason about intent |
| New greenfield project with tests | Bernstein — tests are ground truth |

---

## When to use Crystal instead

- **Your test coverage is low.** If tests cover only 30–40% of the codebase, Bernstein's janitor misses most quality issues. Crystal's review loop adds value where tests don't exist.
- **Code quality is harder to define than correctness.** API design consistency, documentation completeness, architectural alignment — these require judgment that deterministic checks don't have.
- **You're working in a domain where tests are hard to write.** ML model training, infrastructure code, database migrations — areas where test execution is expensive or impractical.
- **You want the agent to self-correct before verification.** Crystal's iterative loop can catch obvious mistakes before they hit the test suite, reducing wasted CI time.

---

## When to use Bernstein instead

- **Your test suite is the ground truth.** Tests either pass or fail — agent consensus about correctness is irrelevant.
- **You want parallel task execution.** Bernstein runs multiple independent tasks concurrently. Crystal's review cycles are sequential per task.
- **You want faster verification.** Running `pytest` takes seconds. Three LLM review cycles takes minutes.
- **You want deterministic quality gates.** A test suite gives the same result every time. LLM reviewers don't.
- **You want cost transparency.** Bernstein logs per-task API costs. Crystal's review loop costs are harder to predict.
- **You want model-agnostic CLI agent support.** Bernstein wraps Claude Code, Codex, Gemini CLI, and Qwen as installed tools. Crystal typically requires SDK-based integration.

---

## Combining both approaches

Some teams use both: Bernstein for the primary coding loop (test-verified) and a Crystal-style LLM review as an additional gate before merge. This is the highest-confidence configuration: objective test verification plus qualitative LLM review.

The Bernstein roadmap includes optional review agents as a post-janitor step — not replacing the test-based verification, but supplementing it for codebases where qualitative review matters.

---

## See also

- [Bernstein benchmark: multi-agent vs single-agent](../../benchmarks/README.md)
- [Full comparison matrix](./README.md)
- [Bernstein vs. single agent](./bernstein-vs-single-agent.md)
