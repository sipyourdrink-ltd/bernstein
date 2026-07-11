# Bernstein Performance Benchmarks

Simulated DAG scheduling, not measured runs. Clear methodology, reproducible results.

---

## Headline: 1.78× faster than single-agent

Across 10 tasks with realistic dependency graphs, Bernstein with 3 agents completes **1.78× faster** on average than a single agent working sequentially. With 5 agents: **2.18× faster**. Model mixing (Haiku for QA/docs, Sonnet for backend) reduces cost by **23%**.

This is a **simulation** - it models scheduling behavior on realistic DAGs, not real agent execution. Treat it as a capacity planning estimate, not a leaderboard claim.

### Results table

| Task | Category | Subtasks | Single (min) | 3-Agent (min) | 5-Agent (min) | Speedup 3× | Speedup 5× | Cost Savings | Quality Δ |
|------|----------|----------|:------------:|:-------------:|:-------------:|:----------:|:----------:|:------------:|:---------:|
| Add REST endpoints (3 routes) | feature | 4 | 41 | 25 | 25 | **1.64×** | **1.64×** | 20% | +8pp |
| Refactor module into clean architecture | refactor | 6 | 72 | 49 | 49 | **1.47×** | **1.47×** | 13% | +14pp |
| Add auth middleware + tests + docs | feature | 6 | 67 | 39 | 39 | **1.72×** | **1.72×** | 27% | +14pp |
| Fix 5 linting violations | maintenance | 5 | 20 | 8 | 4 | **2.50×** | **5.00×** | −10% | +11pp |
| Add error handling to all endpoints | reliability | 6 | 40 | 22 | 16 | **1.82×** | **2.50×** | 11% | +14pp |
| Write integration test suite | testing | 5 | 57 | 33 | 27 | **1.73×** | **2.11×** | 72% | +11pp |
| Add rate limiting + tests | feature | 6 | 61 | 43 | 43 | **1.42×** | **1.42×** | 20% | +14pp |
| Create OpenAPI spec from code | docs | 6 | 54 | 38 | 38 | **1.42×** | **1.42×** | 57% | +14pp |
| Add logging and monitoring hooks | observability | 8 | 68 | 32 | 30 | **2.12×** | **2.27×** | 17% | +20pp |
| Security audit + fixes | security | 10 | 97 | 49 | 43 | **1.98×** | **2.26×** | 8% | +26pp |

**What this means for you:** a task that takes one agent 67 minutes (auth middleware + tests + docs) drops to 39 minutes with 3 agents - saving 28 minutes of your day. The lint-fix task (20 min → 8 min) saves 12 minutes. Across 10 tasks, you save roughly 40% of your wait time.

### Methodology

**Task definitions.** Each of the 10 benchmark tasks is a DAG of subtasks with explicit role assignments (backend, qa, docs, security) and dependency edges. Definitions live in `benchmarks/tasks/` as YAML files.

**Scheduling model.** Single-agent runs all subtasks sequentially. Multi-agent uses a greedy list scheduler: at each time step, all subtasks whose dependencies are satisfied are dispatched to idle agents. This gives the minimum possible wall-clock time with N agents.

**Cost model.** Token consumption estimated at 320 tokens/minute. Single agent uses Claude Sonnet for all roles. Multi-agent uses model mixing: Sonnet for backend/security, Haiku for QA/docs. A 10% overhead is added to multi-agent runs for orchestration (task decomposition, janitor verification).

**Quality model.** Single-agent test pass rate starts at 82% and degrades by 3pp per subtask beyond four (context dilution). Multi-agent maintains 90%+ through focused per-agent contexts and role specialization.

### Reproduce

```bash
uv run python benchmarks/run_benchmark.py --output benchmarks/results/
```

The run writes a JSON results file and a Markdown report under
`benchmarks/results/`. That directory is generated on demand and is not
committed to the repository.

---

## SWE-Bench Lite

SWE-Bench is the standard benchmark for autonomous code understanding and generation. Bernstein runs against SWE-Bench Lite using a verified evaluation harness.

### Current status: **preview artifacts**

The results in `benchmarks/swe_bench/results/` are **mock preview artifacts** - not verified eval runs. They demonstrate the harness format and output structure but should not be used for public benchmark claims.

| Scenario | Source type | Verified | Sample size |
|---|---|---|---:|
| Solo Sonnet | mock | No | 300 |
| Solo Opus | mock | No | 300 |
| Bernstein 3× Sonnet | mock | No | 300 |
| Bernstein Mixed | mock | No | 300 |

### Run a verified eval

```bash
uv run python benchmarks/swe_bench/run.py eval \
    --scenarios solo-sonnet solo-opus bernstein-sonnet bernstein-mixed \
    --limit 50

uv run python benchmarks/swe_bench/run.py report
```

Only artifacts marked `verified=true` from `benchmarks/swe_bench/run.py eval` are eligible for public benchmark claims. Public comparisons are kept narrow - Bernstein versus solo-agent baselines on SWE-Bench Lite. Cross-framework numbers belong only on harnesses we run end-to-end.

---

## Component benchmarks

These measure internal subsystems, not end-to-agent-end performance. Useful for capacity planning on your hardware.

### Orchestrator tick latency

Measures `Orchestrator.tick()` execution time with a 100-task backlog.

- **Idle (no spawn):** sub-millisecond tick latency
- **Under load:** latency dominated by spawn/external process interactions

```bash
uv run python benchmarks/bench_orchestrator.py
```

### Task store throughput

Measures raw throughput of the JSONL-backed task store.

- High write throughput on local SSD
- Low flush latency

```bash
uv run python benchmarks/bench_task_store.py
```

### Quality gate verification

Measures `verify_task` latency with increasing completion signal count.

- Near-linear scaling as signal count increases

```bash
uv run python benchmarks/bench_quality_gates.py
```

### Startup latency

End-to-end time from orchestrator initialization to first tick completion.

- Generally fast in local developer environments

```bash
uv run python benchmarks/bench_startup.py
```

---

## What these numbers don't tell you

Benchmarks measure scheduling efficiency, not code quality. A fast wrong answer is still wrong. Bernstein's janitor and quality gates ensure the output is correct before it lands - which adds overhead but saves you from debugging agent mistakes.

The real metric that matters: **how much of your day do you save?** If a single agent would take 4 hours on your backlog and Bernstein finishes it in 2.5 hours with verified output, you got back 1.5 hours. That compounds across every run.

---

## Community-submitted benchmarks

Real runs from the community. Submit yours via [issue #787](https://github.com/sipyourdrink-ltd/bernstein/issues/787) or open a PR adding a row here.

### Component benchmarks — Intel i3-6006U, Linux, Python 3.14 (pre-release)

**Hardware:** Intel Core i3-6006U @ 2.00GHz, 4 cores, 3.7 GB RAM, Ubuntu (kernel 6.17.0-35-generic), SSD

**Bernstein version:** v2.7.0

**Python:** 3.14.5 (pre-release dev build, inside project venv — `uv venv`)

**Submitted by:** [@Om-Rohilla](https://github.com/Om-Rohilla)

| Benchmark | Result | Command |
|-----------|--------|---------|
| Orchestrator tick latency (100-task backlog) — avg | 5.89 ms | `uv run python benchmarks/bench_orchestrator.py` |
| Orchestrator tick latency (100-task backlog) — max | 7.35 ms | `uv run python benchmarks/bench_orchestrator.py` |
| Task store: creations | 251.83 tasks/sec | `uv run python benchmarks/bench_task_store.py` |
| Task store: claims | 253.38 tasks/sec | `uv run python benchmarks/bench_task_store.py` |
| Task store: completions | 162.19 tasks/sec | `uv run python benchmarks/bench_task_store.py` |
| Task store: flush latency (buffer=1) | 3.32 ms | `uv run python benchmarks/bench_task_store.py` |
| Quality gate verify_task — 1 signal | 0.038 ms | `uv run python benchmarks/bench_quality_gates.py` |
| Quality gate verify_task — 10 signals | 0.231 ms | `uv run python benchmarks/bench_quality_gates.py` |
| Quality gate verify_task — 50 signals | 1.134 ms | `uv run python benchmarks/bench_quality_gates.py` |
| Quality gate verify_task — 100 signals | 1.915 ms | `uv run python benchmarks/bench_quality_gates.py` |
| Startup latency (avg, 5 runs) | 3048.61 ms | `uv run python benchmarks/bench_startup.py` |

**Notes:** Low-end consumer laptop (budget i3, 2016 generation, 3.7 GB RAM). Startup latency is higher than expected — likely cold import overhead from running a Python 3.14 pre-release build; expect lower on stable Python 3.12/3.13. Orchestrator tick and task store throughput look normal for this hardware class. Quality gate scaling is near-linear as the docs describe.
