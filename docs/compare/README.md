# Bernstein — Comparison Pages

How Bernstein compares to other tools in the multi-agent coding space.

---

## Quick reference matrix

|  | [Single agent](./bernstein-vs-single-agent.md) | [Conductor](./bernstein-vs-conductor.md) | [Dorothy](./bernstein-vs-dorothy.md) | [Parallel Code](./bernstein-vs-parallel-code.md) | [Crystal](./bernstein-vs-crystal.md) | [Stoneforge](./bernstein-vs-stoneforge.md) | [GitHub Agent HQ](./bernstein-vs-github-agent-hq.md) |
|--|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **Parallel execution** | — | ✓ | — | ✓ | — | ✓ | ✓ |
| **CLI agent support** | ✓ | — | — | varies | — | — | — |
| **Result verification** | — | — | — | — | internal | external | GitHub CI |
| **Task planning from goal** | — | — | — | — | — | — | ✓ |
| **Self-evolution** | — | — | — | — | — | — | — |
| **Model routing** | — | — | — | — | — | — | — |
| **Headless / overnight** | — | ✓ | — | — | — | limited | limited |
| **IDE integration** | — | ✓ | — | — | varies | ✓ | GitHub UI |
| **Multi-provider** | ✓ | ✓ | varies | varies | — | — | GitHub-managed |
| **Open source** | ✓ | ✓ Apache 2.0 | ✓ MIT | varies | — | — | — |

✓ = yes, — = no/not applicable, "varies" = depends on configuration

**Bernstein:** ✓ on all rows above

---

## Benchmark data

From `benchmarks/README.md` — 25 real GitHub issues across 10 popular Python repos, run 2026-03-28 with Claude Code as the underlying agent.

| Metric | Single-agent baseline | Bernstein multi-agent |
|---|---:|---:|
| CI pass rate | 52% | **80%** |
| Cost (median/task) | **$0.121** | $0.150 |
| Wall clock (median) | 234 s | **181 s** (ns) |
| Linter delta (median) | 0 | **−2** (p=0.004) |
| Merge conflicts | 1 | **0** |

"ns" = not statistically significant at n=25.

**Key finding:** for medium-to-high complexity tasks, multi-agent Bernstein shows 28 pp higher CI pass rate with ~24% cost premium. For low-complexity tasks, single-agent is cheaper with no quality difference. Full methodology and raw data: [benchmarks/README.md](../../benchmarks/README.md).

---

## Choosing a tool

**Use Bernstein if:**
- You want to run multiple coding tasks in parallel and verify each result independently
- You want to mix AI providers (Claude + Codex + Gemini) in the same run
- You want the orchestrator to plan the task breakdown from a plain-language goal
- You need headless, overnight, or CI-integrated operation
- You want self-evolution (the system improves its own prompts and routing over time)

**Consider alternatives if:**
- Your goal needs multi-turn deliberation between agents (→ Dorothy)
- You need production workflow orchestration for non-coding workloads (→ Conductor)
- You want deep IDE integration and are committed to one provider (→ Stoneforge)
- You need iterative self-review loops per task rather than external verification (→ Crystal)
- You need raw parallelism with minimal tooling and already know your tasks (→ Parallel Code)
- Your task is simple and well-scoped (→ single agent, no orchestration needed)

---

## Detailed comparison pages

- [Bernstein vs. Single Agent](./bernstein-vs-single-agent.md) — the core multi-vs-single question, with benchmark data
- [Bernstein vs. Conductor](./bernstein-vs-conductor.md) — workflow engine vs. coding agent orchestrator
- [Bernstein vs. Dorothy](./bernstein-vs-dorothy.md) — task dispatch vs. graph-based conversation
- [Bernstein vs. Parallel Code](./bernstein-vs-parallel-code.md) — orchestrated parallel vs. manual parallel
- [Bernstein vs. Crystal](./bernstein-vs-crystal.md) — external verification vs. iterative self-review
- [Bernstein vs. Stoneforge](./bernstein-vs-stoneforge.md) — provider-agnostic vs. provider-integrated
- [Bernstein vs. GitHub Agent HQ](./bernstein-vs-github-agent-hq.md) — open-source alternative to GitHub's multi-agent system

---

## Broader ecosystem comparison

For a comparison against Python agent frameworks (CrewAI, AutoGen, LangGraph, OpenAI Agents SDK, Google ADK), see [docs/competitive-matrix.md](../competitive-matrix.md).
