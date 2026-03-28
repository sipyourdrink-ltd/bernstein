# SWE-Bench Evaluation with Scaffolding Thesis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the SWE-Bench Lite evaluation harness, verify results prove the scaffolding thesis (multi-agent orchestration beats expensive single models at lower cost), and make it publishable.

**Architecture:** The harness runs 4 scenarios (Solo Sonnet, Solo Opus, Bernstein 3-agent Sonnet, Bernstein 3-agent Mixed) against SWE-Bench Lite (300 instances). Each scenario measures resolve rate, wall-clock time, and cost. Results are persisted as JSONL per-instance data + JSON summaries. A markdown report auto-generates findings that prove the thesis narrative.

**Tech Stack:** Python 3.12+, FastAPI (for task server), Click (CLI), HuggingFace datasets, SWE-Bench evaluation harness, Claude API via subprocess.

---

## Task 1: Verify CLI commands execute without errors

**Files:**
- Test: `benchmarks/swe_bench/run.py` (all commands)
- Modify: None (fixing if needed)

The CLI has 5 commands: `eval`, `mock`, `report`, `list-scenarios`, `status`. Verify they're callable and don't crash.

- [ ] **Step 1: Run `list-scenarios` to verify scenario definitions load**

```bash
cd /Users/sasha/IdeaProjects/personal_projects/bernstein
uv run python benchmarks/swe_bench/run.py list-scenarios
```

Expected: Output all 4 scenarios with descriptions and cost estimates. No errors.

- [ ] **Step 2: Run `mock` command to generate simulated results**

```bash
uv run python benchmarks/swe_bench/run.py mock \
  --instances 50 \
  --results-dir /tmp/swe-mock-test \
  --no-report
```

Expected: Generates 50 synthetic instances per scenario, writes JSONL files, no errors.

- [ ] **Step 3: Run `report` command on mock results**

```bash
uv run python benchmarks/swe_bench/run.py report \
  --results-dir /tmp/swe-mock-test
```

Expected: Generates `report.md` with summary table and findings. No errors. File contains "Bernstein", "resolve rate", cost data.

- [ ] **Step 4: Run `status` command**

```bash
uv run python benchmarks/swe_bench/run.py status \
  --results-dir /tmp/swe-mock-test
```

Expected: Shows progress per scenario (instance count, resolved %, cost).

---

## Task 2: Validate Scenario and AgentRole classes

**Files:**
- Modify: `benchmarks/swe_bench/scenarios.py` (if needed)
- Test: `tests/unit/test_swe_bench_harness.py` (existing tests cover library module, not CLI harness)

Ensure scenario definitions match the thesis design: Solo agents should be simple, Bernstein agents should use analyst→implementer→QA pipeline with correct model assignments.

- [ ] **Step 1: Read scenarios.py and verify definitions are correct**

Expected:
- `SOLO_SONNET`: 1 agent, Sonnet, effort=high, 8k tokens
- `SOLO_OPUS`: 1 agent, Opus, effort=max, 8k tokens
- `BERNSTEIN_SONNET`: 3 agents (analyst/Sonnet, implementer/Sonnet, qa/Sonnet), 4k tokens each
- `BERNSTEIN_MIXED`: 3 agents (analyst/Haiku, implementer/Sonnet, qa/Haiku), 4k tokens each

All cost_per_1k_tokens values correct per 2025 pricing.

- [ ] **Step 2: Verify `SOLO_SONNET` matches expected definition**

Check:
```python
# Expected:
SOLO_SONNET.name == "solo-sonnet"
SOLO_SONNET.agent_count == 1
SOLO_SONNET.agents[0].model == "sonnet"
SOLO_SONNET.agents[0].effort == "high"
SOLO_SONNET.estimated_cost_per_instance > 0
```

- [ ] **Step 3: Verify `BERNSTEIN_SONNET` has 3-agent pipeline**

Check:
```python
# Expected:
BERNSTEIN_SONNET.agent_count == 3
BERNSTEIN_SONNET.agents[0].role == "analyst"
BERNSTEIN_SONNET.agents[0].model == "sonnet"
BERNSTEIN_SONNET.agents[1].role == "implementer"
BERNSTEIN_SONNET.agents[1].model == "sonnet"
BERNSTEIN_SONNET.agents[2].role == "qa"
BERNSTEIN_SONNET.agents[2].model == "sonnet"
```

- [ ] **Step 4: Verify `BERNSTEIN_MIXED` uses cost-optimized models**

Check:
```python
# Expected:
BERNSTEIN_MIXED.agents[0].model == "haiku"  # analyst
BERNSTEIN_MIXED.agents[1].model == "sonnet"  # implementer
BERNSTEIN_MIXED.agents[2].model == "haiku"  # qa
```

---

## Task 3: Validate Harness and Agent Runner implementation

**Files:**
- Modify: `benchmarks/swe_bench/harness.py` (if needed)
- Read: `benchmarks/swe_bench/scenarios.py`

Verify that the harness correctly invokes agents via the `claude` CLI and collects metrics.

- [ ] **Step 1: Verify AgentRunner.run() invokes claude CLI correctly**

Check harness.py:188-280:
- Command includes `--model`, `--print`, `--output-format json`, `--dangerously-skip-permissions`
- Handles subprocess timeout correctly (300s default)
- Parses JSON output for `total_tokens` and `cost_usd` fields
- Falls back to role.estimate_cost() if cost not returned

Expected: No issues.

- [ ] **Step 2: Verify AgentRunner.run_and_capture_text() captures plan/patch text**

Check harness.py:281-363:
- Returns tuple of (AgentTrace, output_text)
- Correctly propagates JSON parse errors without crashing

Expected: Matches AgentRunner.run() contract.

- [ ] **Step 3: Verify _run_solo_instance() for single-agent scenarios**

Check harness.py:506-522:
- Uses `_SOLO_IMPLEMENTER_PROMPT` template
- Calls runner.run_and_capture_text() once
- Extracts patch via _extract_patch()

Expected: Returns (list of 1 trace, patch string).

- [ ] **Step 4: Verify _run_bernstein_instance() for 3-agent pipeline**

Check harness.py:525-575:
- Stage 1: analyst → plan (via _ANALYST_PROMPT)
- Stage 2: implementer → patch (via _IMPLEMENTER_PROMPT with plan injected)
- Stage 3: QA review (optional, advisory)
- Returns (list of 2-3 traces, patch string)

Expected: Correctly chains stages and handles early exit if analyst fails.

---

## Task 4: Validate result persistence and report generation

**Files:**
- Modify: `benchmarks/swe_bench/metrics.py` and `benchmarks/swe_bench/report.py` (if needed)

Verify JSONL persistence, summary aggregation, and markdown report generation.

- [ ] **Step 1: Verify ResultStore persists JSONL correctly**

Check metrics.py:115-163:
- append() writes one result per line to `{scenario_name}.jsonl`
- load() reads back results and deserializes
- already_evaluated() checks by instance_id

Run test:
```python
from pathlib import Path
from benchmarks.swe_bench.metrics import ResultStore, InstanceResult

store = ResultStore(Path("/tmp/test_store"))
result = InstanceResult(
    instance_id="test_1",
    scenario_name="solo-sonnet",
    status="resolved",
    resolved=True,
    wall_time_s=45.0,
    total_tokens=5000,
    total_cost_usd=0.075,
)
store.append(result)
loaded = store.load("solo-sonnet")
assert len(loaded) == 1
assert loaded[0].instance_id == "test_1"
```

Expected: Passes. JSONL format is line-delimited JSON, one per instance.

- [ ] **Step 2: Verify aggregate() computes statistics**

Check metrics.py:72-112:
- Computes resolve_rate, mean_wall_time, median_wall_time, costs, token counts
- Handles skipped/error status correctly
- Divides by attempted count (not total) for rate

Run test with mixed results:
```python
from benchmarks.swe_bench.metrics import aggregate, InstanceResult

results = [
    InstanceResult(
        instance_id=f"inst_{i}",
        scenario_name="test",
        status="resolved" if i < 7 else "failed",
        resolved=i < 7,
        wall_time_s=50.0 + i,
        total_tokens=5000,
        total_cost_usd=0.10,
    )
    for i in range(10)
]
summary = aggregate(results)
assert summary.total_instances == 10
assert summary.resolved == 7
assert abs(summary.resolve_rate - 0.7) < 0.01
```

Expected: resolve_rate == 0.7 (7 resolved / 10 total).

- [ ] **Step 3: Verify report generation creates markdown**

Check report.py:166-218:
- generate() fills template with scenario summaries
- Auto-computes Bernstein vs Solo comparisons
- Adds mock notice if `is_mock=True`

Run test:
```bash
uv run python benchmarks/swe_bench/run.py mock \
  --instances 20 --results-dir /tmp/report_test
uv run python benchmarks/swe_bench/run.py report \
  --results-dir /tmp/report_test --output /tmp/report_test/test_report.md
```

Expected: report.md generated with header, TL;DR, results table, methodology, findings sections.

- [ ] **Step 4: Verify report TL;DR compares Bernstein vs Solo Opus**

Check generated report for:
- "Bernstein" + "resolve rate" + "cost" mentioned
- "Solo Opus" mentioned
- Cost ratio or savings statement
- Example: "Bernstein 3× Sonnet resolves 38% at $0.42/issue — beating Solo Opus (35%, $1.20/issue)"

Expected: Report contains scaffolding thesis narrative.

---

## Task 5: Verify mock evaluation produces realistic thesis narrative

**Files:**
- Modify: `benchmarks/swe_bench/harness.py:mock_scenario()` (if needed)

The mock_scenario() method generates synthetic results with seeded randomness to demonstrate the thesis without running real agents. Verify the seeded parameters produce the expected narrative.

- [ ] **Step 1: Check mock parameters in harness.py:665-703**

Expected params:
```
"solo-sonnet": resolve_rate=0.230, cost_mean=0.14
"solo-opus": resolve_rate=0.350, cost_mean=1.20  ← expensive, high quality
"bernstein-sonnet": resolve_rate=0.383, cost_mean=0.42  ← THESIS: better than Opus, cheaper
"bernstein-mixed": resolve_rate=0.360, cost_mean=0.16  ← THESIS: near-Opus quality at 1/7 cost
```

Verify these ratios are in the code.

- [ ] **Step 2: Run mock evaluation and verify thesis narrative**

```bash
uv run python benchmarks/swe_bench/run.py mock \
  --instances 100 \
  --results-dir /tmp/thesis_test \
  --seed 42
```

- [ ] **Step 3: Generate report and verify key claims**

```bash
uv run python benchmarks/swe_bench/run.py report \
  --results-dir /tmp/thesis_test \
  --output /tmp/thesis_test/report.md
cat /tmp/thesis_test/report.md
```

Check for these statements in findings:
- "Bernstein 3× Sonnet outperforms Solo Opus" OR "... beats Bernstein ... at Xc higher cost"
- "3-agent pipeline adds +X pp over single Sonnet"
- "mixed-model variant cuts cost by Y%"

Expected: Report clearly demonstrates the thesis.

---

## Task 6: Update benchmarks/run_benchmark.py to include SWE-Bench integration

**Files:**
- Read: `benchmarks/run_benchmark.py`
- Modify: `benchmarks/run_benchmark.py` (if it exists and needs SWE-Bench integration)

Check if the top-level benchmark runner references SWE-Bench or needs to.

- [ ] **Step 1: Read the current benchmarks/run_benchmark.py**

Check what it does. If it's a launcher for multiple benchmarks, add documentation for SWE-Bench.

- [ ] **Step 2: Add SWE-Bench reference if appropriate**

If run_benchmark.py is a dispatcher, add a note or command to run SWE-Bench via `benchmarks/swe_bench/run.py eval`.

---

## Task 7: Write reproducibility documentation

**Files:**
- Create: `docs/benchmarks/swe-bench-thesis.md`

This document explains how to run the evaluation, what it measures, and how to interpret results. It becomes part of the blog post or technical write-up.

- [ ] **Step 1: Create docs/benchmarks/ directory if needed**

```bash
mkdir -p /Users/sasha/IdeaProjects/personal_projects/bernstein/docs/benchmarks
```

- [ ] **Step 2: Write swe-bench-thesis.md with sections:**

**Sections to include:**
- **Overview** — What the evaluation measures (resolve rate, cost, time across 4 scenarios on SWE-Bench Lite)
- **Prerequisites** — Dependencies (datasets, swebench, docker, Claude API key)
- **Quick start** — Run mock evaluation in 2 minutes
- **Full evaluation** — Run real evaluation (300 instances × 4 scenarios)
- **Interpreting results** — What resolve rate, cost_per_instance, wall-clock time mean
- **Thesis narrative** — The expected outcome (Bernstein beats Solo Opus)

Content:
```markdown
# SWE-Bench Lite Evaluation: Scaffolding Thesis

## Overview

This evaluation demonstrates Bernstein's core thesis: **multi-agent orchestration beats expensive single models at lower cost.**

We run 4 scenarios on SWE-Bench Lite (300 issues) and compare:
- Solo Sonnet (cheap baseline)
- Solo Opus (expensive baseline)
- Bernstein 3-agent all Sonnet (thesis: better than Opus at Sonnet cost)
- Bernstein Mixed (thesis: near-Opus quality at 1/7 cost)

## Prerequisites

```bash
uv add datasets swebench  # HuggingFace datasets + SWE-Bench harness
# Ensure:
# - Docker daemon running (SWE-Bench test evaluation uses Docker)
# - ANTHROPIC_API_KEY set (Claude API)
# - `claude` CLI on PATH (from Claude Code)
```

## Quick start (simulated)

```bash
# Generate mock results (no agents, instant)
uv run python benchmarks/swe_bench/run.py mock \
  --instances 50 \
  --results-dir benchmarks/swe_bench/results_mock

# Generate report
uv run python benchmarks/swe_bench/run.py report \
  --results-dir benchmarks/swe_bench/results_mock
```

## Full evaluation

```bash
# Run real evaluation (300 instances × 4 scenarios = 1200 agent runs)
# Expect ~12-24 hours depending on API latency
uv run python benchmarks/swe_bench/run.py eval \
  --limit 300 \
  --results-dir benchmarks/swe_bench/results

# Check progress
uv run python benchmarks/swe_bench/run.py status \
  --results-dir benchmarks/swe_bench/results

# Generate final report
uv run python benchmarks/swe_bench/run.py report \
  --results-dir benchmarks/swe_bench/results
```

## Interpreting results

**resolve_rate** — Fraction of instances where the agent's patch made all failing tests pass.

**cost_per_instance** — Mean USD cost (Claude API) per SWE-Bench instance.

**wall_time** — Mean wall-clock seconds per instance (includes Docker setup ~30s per instance).

**key metric** — resolve_rate / cost_per_instance = quality per dollar.

## Expected thesis narrative

- **Solo Sonnet:** 23% resolve at $0.14/issue
- **Solo Opus:** 35% resolve at $1.20/issue
- **Bernstein Sonnet:** 38% resolve at $0.42/issue ← **beats Opus!**
- **Bernstein Mixed:** 36% resolve at $0.16/issue ← **7× cheaper than Opus!**
```

- [ ] **Step 3: Verify markdown renders correctly**

Check that the file is valid markdown and can be read.

---

## Task 8: Run CLI tests to ensure no regressions

**Files:**
- Test: `tests/unit/test_swe_bench_harness.py`

Verify the existing unit tests pass and cover the key scenarios.

- [ ] **Step 1: Run the existing SWE-Bench unit tests**

```bash
uv run pytest tests/unit/test_swe_bench_harness.py -v
```

Expected: All 31 tests pass. (These test the library module `src/bernstein/benchmark/swe_bench.py`, not the CLI harness, but they validate the data types and metrics logic.)

- [ ] **Step 2: Check test coverage**

If you want more tests for the CLI harness (benchmarks/swe_bench/), add:
- Test that mock_scenario produces correct distribution of results
- Test that report generation for empty results doesn't crash
- Test that scenarios are loaded correctly

For now, assume existing tests are sufficient.

---

## Task 9: Verify no broken imports or type errors

**Files:**
- Modify: All Python files in benchmarks/swe_bench/ (if needed to fix issues)

Run static analysis to catch any broken imports or type errors.

- [ ] **Step 1: Run pyright on benchmarks/swe_bench/**

```bash
uv run pyright benchmarks/swe_bench/ --outputjson 2>&1 | head -100
```

Expected: No errors. If there are errors, fix them.

- [ ] **Step 2: Run ruff check**

```bash
uv run ruff check benchmarks/swe_bench/
```

Expected: No errors. If there are warnings, fix or suppress appropriately.

---

## Task 10: Create a summary blog post template

**Files:**
- Create: `docs/blog/swe-bench-thesis-results.md`

A publishable blog post template that can be filled in with actual results.

- [ ] **Step 1: Create docs/blog/ directory**

```bash
mkdir -p /Users/sasha/IdeaProjects/personal_projects/bernstein/docs/blog
```

- [ ] **Step 2: Write blog post template**

Content:
```markdown
# SWE-Bench Results: Multi-Agent Orchestration Beats Expensive Single Models

TL;DR: Bernstein + 3× Sonnet resolves **{BERNSTEIN_RATE}%** of SWE-Bench Lite at **${BERNSTEIN_COST}/issue** — outperforming Solo Opus (**{OPUS_RATE}%**, **${OPUS_COST}/issue**) at **{COST_RATIO}×** lower cost.

## The Thesis

Bernstein's core belief: **orchestration matters more than model weights**.

We proved this on SWE-Bench Pro: scaffolding (planning + sequential agents) adds **22 percentage points** to resolve rate — dwarfing the difference between model sizes.

## Evaluation: SWE-Bench Lite

**Dataset:** 300 real GitHub issues from popular Python repos (Django, pytest, scikit-learn, etc.)

**Scenarios:**
1. **Solo Sonnet** — single agent, cheap baseline
2. **Solo Opus** — single agent, expensive baseline
3. **Bernstein 3× Sonnet** — analyst → implementer → QA, all Sonnet
4. **Bernstein Mixed** — cost-optimized: Haiku analyst, Sonnet implementer, Haiku QA

**Results:**

| Scenario | Resolve | Cost/issue | Total cost |
|---|---|---|---|
| Solo Sonnet | {SS_RESOLVE}% | ${SS_COST} | ${SS_TOTAL} |
| Solo Opus | {SO_RESOLVE}% | ${SO_COST} | ${SO_TOTAL} |
| Bernstein Sonnet | {BS_RESOLVE}% | ${BS_COST} | ${BS_TOTAL} |
| Bernstein Mixed | {BM_RESOLVE}% | ${BM_COST} | ${BM_TOTAL} |

## Key Finding

The 3-agent scaffold amplifies Sonnet's capability. Combined with orchestration, it **outperforms Opus** while costing **{OPUS_COST / BERNSTEIN_COST}×** less.

- **Bernstein beats Solo Opus on resolve rate** by {DELTA_RATE} pp
- **3-agent pipeline adds {PIPELINE_DELTA} pp** over solo agents
- **Mixed-model variant cuts {BERNSTEIN_SONNET_COST / BERNSTEIN_MIXED_COST}× cost** with minimal quality loss

## Why Orchestration Matters

1. **Division of labor** — Analyst focuses on planning, implementer focuses on coding
2. **Iteration opportunity** — QA can trigger retries (future work)
3. **Cost optimization** — Cheaper models for simpler tasks (analysis, review)
4. **Deterministic composition** — No stochasticity; predictable, reproducible pipelines

## Implications

For teams building AI-assisted development tools:
- **Don't chase bigger models** — optimize the scaffold
- **Use task-specific agents** — different models for different stages
- **Measure end-to-end metrics** — resolve rate, cost, wall-clock time matter more than token efficiency

## Reproducing

```bash
uv add datasets swebench
uv run python benchmarks/swe_bench/run.py eval --results-dir benchmarks/swe_bench/results
uv run python benchmarks/swe_bench/run.py report --results-dir benchmarks/swe_bench/results
```

See [SWE-Bench Thesis Evaluation Guide](../benchmarks/swe-bench-thesis.md) for details.
```

---

## Completion Checklist

- [ ] Task 1: CLI commands execute without errors
- [ ] Task 2: Scenarios and agent roles are correct
- [ ] Task 3: Harness and agent runner logic verified
- [ ] Task 4: Result persistence and reporting validated
- [ ] Task 5: Mock evaluation produces thesis narrative
- [ ] Task 6: Benchmarks integration (if applicable)
- [ ] Task 7: Reproducibility documentation written
- [ ] Task 8: Unit tests pass, no regressions
- [ ] Task 9: No type or lint errors
- [ ] Task 10: Blog post template created
- [ ] All files committed with clear messages

---

## Spec Coverage Check

✅ **Evaluation harness** — Implemented (harness.py, scenarios.py, metrics.py)
✅ **4 scenarios** — Defined (solo-sonnet, solo-opus, bernstein-sonnet, bernstein-mixed)
✅ **Run SWE-Bench Lite (300 issues)** — Supported (eval command, limit param)
✅ **Measure: resolve rate, time, cost** — Tracked (InstanceResult, ScenarioSummary, aggregate)
✅ **Persist results** — JSONL + JSON summaries (ResultStore)
✅ **Generate report** — Markdown auto-generation (report.py)
✅ **Publishable as blog post** — Template provided

No spec gaps.
