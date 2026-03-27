# Bernstein vs. Stoneforge

> **tl;dr** — Stoneforge is a provider-specific multi-agent coding framework with strong IDE integration and a polished developer experience. Bernstein is provider-agnostic and CLI-native, trades the IDE UX for model flexibility and headless operation. Neither is better in the abstract — the question is whether provider lock-in matters for your workflow.

*This comparison is based on publicly available documentation. Feature details may have changed since this was written (March 2026).*

---

## What each tool is

**Stoneforge** is a multi-agent coding framework built around a specific AI provider's model. It runs multiple specialized agents within a structured session, with strong IDE plugin support and a first-party UI for viewing agent activity. The tight provider integration enables features like shared context across agents and provider-native tool use.

**Bernstein** is provider-agnostic. It wraps whichever CLI coding agent you have installed — Claude Code, Codex, Gemini CLI, Qwen, or any CLI with a `--prompt` flag. The orchestrator is deterministic Python code. Agent sessions are isolated processes that exit after 1-3 tasks.

---

## Feature comparison

| Feature | Bernstein | Stoneforge |
|---|---|---|
| **Provider flexibility** | Any CLI agent (Claude, Codex, Gemini, Qwen, Generic) | Single provider |
| **CLI agent support** | Yes — wraps installed CLI tools | No — uses provider SDK directly |
| **IDE integration** | No — terminal-native | Yes — VS Code, JetBrains plugins |
| **Task planning** | LLM planner from natural language goal | Structured prompt with agent roles |
| **Agent isolation** | Git worktree per agent, process isolation | Shared session context |
| **Result verification** | External janitor (tests, linter, files) | Provider-native tool verification |
| **Self-evolution** | Yes — `--evolve` mode | No |
| **Model routing** | Cost-aware bandit across providers | Fixed to provider models |
| **Headless operation** | Yes — CI pipelines, overnight runs | Limited |
| **Multi-repo support** | Yes — workspace mode | No |
| **Cost optimization** | Routes cheap models to simple tasks | Fixed to provider pricing |
| **Agent catalogs** | Yes — Agency + custom catalogs | No |
| **Open source license** | Apache 2.0 | Apache 2.0 |

---

## Architecture comparison

**Stoneforge (provider-integrated):**
```
IDE plugin / Stoneforge UI
    │
    ▼
Provider API (single vendor)
    │
    ├── Agent 1 (specialized role, shared session context)
    ├── Agent 2 (specialized role, shared session context)
    └── Agent 3 (specialized role, shared session context)

Context is shared. Agents see each other's output within the provider session.
```

**Bernstein (CLI-native, provider-agnostic):**
```
bernstein -g "goal"  (terminal)
    │
    ▼
Task server (local FastAPI, no external dependencies)
    │
    ├── Task A → claude (isolated worktree, fresh context) → janitor → merge
    ├── Task B → codex  (isolated worktree, fresh context) → janitor → merge  ← any provider
    └── Task C → gemini (isolated worktree, fresh context) → janitor → merge
```

Stoneforge's shared context means agents can reference each other's reasoning — useful for complex decisions but risky if one agent's hallucination propagates. Bernstein's isolation means each agent starts clean — no accumulated state, but also no cross-agent awareness.

---

## Cost comparison

Stoneforge costs are determined by the underlying provider pricing. If you use a single high-end model for all agents, costs reflect that.

Bernstein benchmark (25 GitHub issues, 2026-03-28):

| Mode | Median cost per task | CI pass rate |
|---|---:|---:|
| Single agent | $0.121 | 52% |
| Bernstein | $0.150 | **80%** |

Bernstein's bandit router reduces cost on low-complexity tasks by routing to Haiku or free-tier Gemini. For a mixed workload, the effective cost per task can be lower than running all tasks against a premium model.

---

## Provider lock-in: an honest assessment

Stoneforge's tight provider integration is a genuine advantage — features that require deep access to provider internals (streaming token inspection, native tool use, session-level memory) work better with first-party integration. If you plan to stay with one provider indefinitely, this is a reasonable trade.

The risk: if that provider raises prices, introduces rate limits, or you want to use a newer model from a different lab, switching Stoneforge means rebuilding your orchestration layer. Bernstein's adapters abstract the provider interface — swapping from `--cli claude` to `--cli codex` is one flag change.

---

## When to use Stoneforge instead

- **You're committed to one provider.** If your policy, compliance, or trust model locks you to a specific AI vendor, Stoneforge's first-party integration makes more sense than Bernstein's general adapter layer.
- **You want IDE integration.** Stoneforge's VS Code and JetBrains plugins show agent activity inline, in your existing editor. Bernstein is terminal + TUI only.
- **You need shared context across agents.** Problems where agents need to read each other's output mid-task are better served by Stoneforge's shared session model.
- **First-party support and SLAs matter.** Stoneforge offers commercial support from the vendor. Bernstein is community-supported (Apache 2.0).

---

## When to use Bernstein instead

- **You want provider flexibility.** Mix Claude, Codex, Gemini, and Qwen in the same run. Route tasks to the cheapest capable model. Switch providers if pricing or quality changes.
- **You want no vendor dependency for orchestration logic.** Your task definitions, role templates, janitor rules, and evolution config are plain files — no vendor SDK imports.
- **You want headless, overnight operation.** CI pipelines, scheduled evolution runs, budget-capped overnight sessions.
- **You want self-evolution.** Bernstein analyzes its own metrics and improves prompts, routing rules, and templates over time.
- **You prefer CLI-native tooling.** No IDE extension required. Works in any terminal, including SSH sessions.

---

## See also

- [Bernstein benchmark: multi-agent vs single-agent](../../benchmarks/README.md)
- [Zero lock-in: model-agnostic orchestration](../../docs/zero-lock-in.md)
- [Bernstein vs. single agent](./bernstein-vs-single-agent.md)
