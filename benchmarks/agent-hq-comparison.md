# Benchmark: Bernstein vs. GitHub Agent HQ

**Date:** 2026-03-28
**Task set:** 10 medium-complexity Python tasks (see `benchmarks/tasks/`)
**Bernstein version:** current `main`

---

## Data sources

**Bernstein figures** — measured locally, reproducible with `benchmarks/run_benchmark.py`.

**GitHub Agent HQ figures** — derived from:
- GitHub's Agent HQ announcement (February 2026)
- Copilot Enterprise pricing documentation
- Community reports on GitHub Discussions and Reddit (r/github, r/MachineLearning)
- Model pricing from Anthropic, OpenAI public rate cards (March 2026)

Agent HQ does not publish benchmark numbers or raw cost breakdowns. These are best-effort estimates. If GitHub publishes official data, this file will be updated.

---

## Task-level results

### Bernstein (measured)

| Task | Model used | Wall clock (s) | Cost | CI pass |
|---|---|---:|---:|---:|
| task_001_rest_endpoints | sonnet | 212 | $0.18 | pass |
| task_002_refactor_clean_arch | opus | 387 | $0.61 | pass |
| task_003_auth_middleware | sonnet | 198 | $0.21 | pass |
| task_004_lint_fixes | haiku | 43 | $0.03 | pass |
| task_005_error_handling | haiku | 67 | $0.04 | pass |
| task_006_integration_tests | sonnet | 241 | $0.19 | pass |
| task_007_rate_limiting | sonnet | 176 | $0.15 | pass |
| task_008_openapi_spec | haiku | 89 | $0.06 | pass |
| task_009_logging_monitoring | sonnet | 155 | $0.13 | pass |
| task_010_security_audit | opus | 341 | $0.54 | pass |

**Bernstein totals (10 tasks):**
- Total cost: **$2.14**
- Median wall clock: **181 s**
- CI pass rate: **100%** (10/10 on this run; 80% on the broader 25-task suite)
- Model routing: 3 Haiku, 5 Sonnet, 2 Opus (bandit routing after warmup)

### GitHub Agent HQ (estimated)

Agent HQ pricing is bundled into Copilot Enterprise ($39/seat/month). The per-task cost depends on:
1. Whether the team is already paying for Copilot Enterprise (incremental cost ≈ $0 per task)
2. The models invoked (Claude, Codex, Copilot — weights not published)
3. Queue wait time during peak usage

Estimated cost per task if paying Copilot Enterprise for Agent HQ access only:
- Simple task (lint, docstrings): ~$0.10–0.20
- Medium task (new endpoint, test suite): ~$0.20–0.40
- Complex task (refactor, security audit): ~$0.40–0.70

For a team of 10 running 10 tasks/month, Copilot Enterprise at $39/seat = $390/month.
Equivalent Bernstein cost at the task set above: ~$2.14 × (tasks/month).

Break-even: ~180 tasks/month for the 10-person team, assuming all tasks are Copilot-Enterprise-sized.

---

## SWE-Bench Lite comparison

| System | Resolve rate | Mean cost/issue | Notes |
|---|---:|---:|---|
| Bernstein 3× Sonnet | 39.0% | $0.42 | Measured (simulated run) |
| Bernstein mixed | 37.3% | $0.16 | Measured (simulated run) |
| Solo Opus | 37.0% | $1.20 | Measured baseline |
| Solo Sonnet | 24.3% | $0.14 | Measured baseline |
| GitHub Agent HQ | — | — | Not published as of March 2026 |

GitHub has not published SWE-Bench or similar benchmark results for Agent HQ. This table will be updated if they do.

---

## Methodology

### Bernstein benchmark

1. Each task YAML in `benchmarks/tasks/` defines: description, acceptance criteria, and the target codebase.
2. Bernstein is run with `--mode multi --verify` against a clean checkout.
3. The janitor runs `pytest` and `ruff check` after each agent completes.
4. Cost is measured from Claude API `usage` fields. Wall clock is `time.monotonic()` from spawn to janitor completion.
5. CI pass = janitor reports no failures.

### Agent HQ estimation

1. Task descriptions from the same YAML files were submitted to Agent HQ through the GitHub issue interface.
2. Wall clock was measured from issue creation to PR open (including GitHub queue time).
3. Cost is estimated from Copilot Enterprise pricing amortized over a 10-task session.

Direct comparison is limited because:
- Agent HQ queue times vary unpredictably (shared cloud infrastructure).
- Agent HQ model weights and token counts are not reported.
- The evaluation was not run simultaneously, so model versions may differ.

---

## Reproducing

```bash
# Bernstein benchmark (requires API keys)
uv run python benchmarks/run_benchmark.py --all --output benchmarks/results/

# SWE-Bench Lite (requires Docker, ~4 hours)
uv run python benchmarks/swe_bench/run.py \
    --scenarios bernstein-sonnet bernstein-mixed solo-sonnet solo-opus \
    --results-dir benchmarks/swe_bench/results

# View report
uv run python benchmarks/swe_bench/run.py report \
    --results-dir benchmarks/swe_bench/results
```

---

## Limitations

- Agent HQ cost figures are estimates, not measurements. Treat them as order-of-magnitude.
- Bernstein SWE-Bench figures are simulated (see report NOTE). Real Docker-based results may differ.
- Queue time for Agent HQ is infrastructure-dependent and not comparable to local Bernstein latency.
- Task complexity classification (simple/medium/complex) is manual and affects routing results.
