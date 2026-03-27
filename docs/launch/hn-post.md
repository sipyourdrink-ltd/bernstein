# Show HN: Bernstein – one command, multiple AI coding agents in parallel

**Date:** 2026-03-28
**Status:** Reviewed — ready to submit once pre-launch gates #700 and #701 pass
**Channel:** news.ycombinator.com/submit (Show HN)

---

## Submission

**Title:**
```
Show HN: Bernstein – one command, multiple AI coding agents in parallel
```

**Body:**
```
Bernstein takes a goal, spawns parallel AI coding agents (Claude Code,
Codex, Gemini, Aider), verifies the output with tests, and commits
the results. The orchestrator is deterministic Python — zero LLM tokens
on coordination.

Key difference from other multi-agent tools: it's a CLI, not a desktop
app. Runs headless in CI. Has cost budgeting ($5 cap). Works with any
CLI agent.

Benchmark: 28 pp higher CI pass rate (52% → 80%) on medium-complexity
tasks, at ~1.25x the cost. Full methodology in the repo.

https://github.com/chernistry/bernstein

Built this because I got tired of babysitting one agent at a time.
Happy to answer questions about the architecture.
```

---

## Pre-launch checklist

- [ ] README has autoplay GIF demo (`docs/assets/demo.gif` generated via `vhs docs/assets/demo.tape`)
- [ ] `pipx install bernstein && bernstein init && bernstein -g "task"` works on a fresh machine
- [ ] Benchmark data in README (CI pass rate, cost, wall-clock — from `benchmarks/README.md`)
- [ ] Comparison pages exist and are linked from README (`docs/compare/README.md` ✓)
- [ ] YouTube demo video linked from README
- [ ] GitHub topics set: `ai`, `agents`, `coding-agent`, `claude`, `codex`, `gemini`, `multi-agent`, `orchestration`, `cli`, `python`
- [ ] Repository description is < 80 chars and compelling (e.g. "Parallel AI coding agents from a single command. No babysitting.")

---

## Response strategy

**Goal:** answer every comment within 6 hours of posting.

### Technical questions

- **"How does it compare to CrewAI / AutoGen / LangGraph?"** → Link `docs/competitive-matrix.md`. Key difference: we wrap existing CLI tools, zero LLM tokens on coordination, disposable agents.
- **"Benchmark methodology?"** → Link `benchmarks/README.md`. 25 real GitHub issues, 10 popular Python repos, Claude Code as the underlying agent, run 2026-03-28.
- **"Why not just run agents manually in parallel?"** → Scheduling, health checks, janitor verification, result merging, cost budgeting, and model routing are all handled. You'd be building that yourself.
- **"What about context window / agent state?"** → Agents are short-lived (1-3 tasks, then exit). State lives in `.sdd/` files, not in agent memory. Fresh spawn = no context drift.
- **"Architecture deep dive?"** → Link `docs/the-bernstein-way.md` and `docs/DESIGN.md`.

### Skeptical / critical questions

- **"Is this actually faster? Sounds like overhead."** → Be honest: overhead exists. For low-complexity tasks, single agent is cheaper with no quality difference. Multi-agent shines on medium-to-high complexity parallelizable work. Show the benchmark table.
- **"LLM-based schedulers are already pretty good."** → Agree they work. Deterministic scheduler is cheaper, faster, and debuggable. Show the token cost delta.
- **"Doesn't this just multiply your AI bill?"** → 1.25x median cost for 28 pp higher pass rate. Cost cap ($5 default) prevents runaway spend.

### Tone

- Be direct, honest, and technical
- Admit limitations (no multi-turn deliberation, no visual workflow builder, self-evolution is still evolving)
- Don't oversell — let the benchmark data speak
- Share raw data when asked; link to methodology rather than summarizing

---

## Timing

Post between **09:00–11:00 ET on a Tuesday or Wednesday** for maximum visibility. Avoid Fridays and Mondays.

Monitor the thread continuously for the first 3 hours. After that, check hourly for 12 hours.
