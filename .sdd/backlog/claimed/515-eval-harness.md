# 515 — Evaluation harness: multiplicative scoring, LLM judge, failure taxonomy

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
No objective way to measure if Bernstein is getting better or worse. "Tests pass" measures code health, not orchestration quality. Self-evolution proposes changes blindly — no eval gate to reject regressions. We need what rag_challenge has: a multiplicative scoring harness that forces honesty across all dimensions.

## Design (stolen from rag_challenge/eval/)

### Multiplicative Scoring Formula
```
Score = (0.5 * TaskSuccess + 0.3 * CodeQuality + 0.2 * Efficiency) * Reliability * Safety
```

Where:
- **TaskSuccess** (deterministic): % tasks completed with passing completion signals
- **CodeQuality** (judge): LLM-judged quality of code changes (correctness, style, tests)
- **Efficiency**: cost-per-task and time-per-task vs baseline
- **Reliability** (multiplicative gate): 1.0 if no crashes/orphans, degrades per failure
- **Safety** (multiplicative gate): 1.0 if no test regressions, 0.0 on any regression

Multiplicative gates mean: one test regression = zero score regardless of other metrics. This forces defensive architecture.

### Components

#### 1. Golden benchmark suite (`src/bernstein/eval/golden.py`)
Curated set of 20-30 tasks across difficulty tiers:
- **Smoke** (5 tasks): single-file changes, should always pass
- **Standard** (10 tasks): multi-file features, typical workload
- **Stretch** (10 tasks): cross-module refactoring, architecture changes
- **Adversarial** (5 tasks): ambiguous specs, conflicting requirements

Each task has: description, expected files modified, expected test outcomes, golden completion signals.

Store in `.sdd/eval/golden/` as markdown task files with golden metadata in frontmatter.

#### 2. Custom metrics (`src/bernstein/eval/metrics.py`)
Pydantic BaseModel subclasses (pattern from rag_challenge):

- **TaskCompletionRate**: fraction of tasks passing all completion signals
- **RetryRate**: fraction of tasks requiring retry (lower is better)
- **AgentUtilization**: productive turns / total turns per agent (from log parsing)
- **CostEfficiency**: total cost / tasks completed (normalized to baseline)
- **TimeEfficiency**: wall-clock seconds / tasks completed
- **ContextWaste**: estimated tokens spent on codebase exploration vs actual coding

#### 3. LLM judge (`src/bernstein/eval/judge.py`)
Adapted from rag_challenge pattern:
- **Dual-attempt**: standard prompt first, strict JSON suffix on parse failure
- **Circuit breaker**: stop after 3 consecutive judge failures
- **Retry with backoff**: 2, 4, 8, 16 seconds on transient errors
- **Structured output**:
```python
class JudgeVerdict(BaseModel):
    correctness: int  # 0-5
    style: int  # 0-5
    test_coverage: int  # 0-5
    safety: int  # 0-5
    verdict: Literal["PASS", "FAIL"]
    issues: list[str]
```
- Judge reviews: git diff of agent's changes + task description + test results

#### 4. Failure taxonomy (`src/bernstein/eval/taxonomy.py`)
Classify every failure into a closed set (from rag_challenge/failure_cartography):
- **ORIENTATION_MISS**: agent spent too long understanding codebase
- **SCOPE_CREEP**: agent changed files outside owned_files
- **TEST_REGRESSION**: agent broke existing tests
- **INCOMPLETE**: agent didn't finish all completion signals
- **TIMEOUT**: agent hit max_turns or wall-clock limit
- **CONFLICT**: agent's changes conflict with concurrent agent
- **CONTEXT_MISS**: agent lacked necessary context to complete task
- **HALLUCINATION**: agent created code that doesn't compile or references nonexistent APIs

Track drift across eval runs: same task, different outcomes = instability signal.

#### 5. Telemetry contract (`src/bernstein/eval/telemetry.py`)
Strict schema for agent output metadata (validated by harness):
- `duration_s`, `turns_used`, `files_read`, `files_modified`
- `tokens_input`, `tokens_output`, `cost_usd`
- `tests_run`, `tests_passed`, `tests_failed`
- `completion_signals_checked`, `completion_signals_passed`
- Schema violation = T gate drops to 0.5

### CLI
```bash
bernstein eval run                    # run golden benchmark suite
bernstein eval run --tier smoke       # smoke tier only
bernstein eval run --compare prev     # compare vs previous run
bernstein eval report                 # generate markdown report
bernstein eval failures               # show failure taxonomy breakdown
```

### Output
`.sdd/eval/runs/{timestamp}.json`:
```json
{
  "timestamp": "2026-03-28T12:00:00Z",
  "config": {"model": "sonnet", "effort": "high", "max_agents": 4},
  "score": 0.72,
  "components": {
    "task_success": 0.85,
    "code_quality": 0.78,
    "efficiency": 0.90,
    "reliability": 1.0,
    "safety": 1.0
  },
  "per_tier": {"smoke": 1.0, "standard": 0.80, "stretch": 0.60, "adversarial": 0.40},
  "failures": [{"task": "...", "taxonomy": "CONTEXT_MISS", "details": "..."}],
  "cost_total": 2.34,
  "duration_total": 480
}
```

## Files
- src/bernstein/eval/__init__.py (new package)
- src/bernstein/eval/harness.py (new) — main orchestrator
- src/bernstein/eval/golden.py (new) — golden dataset loader
- src/bernstein/eval/metrics.py (new) — custom metric classes
- src/bernstein/eval/judge.py (new) — LLM judge with resilience
- src/bernstein/eval/taxonomy.py (new) — failure classification
- src/bernstein/eval/telemetry.py (new) — agent output schema validation
- src/bernstein/cli/main.py — add eval command group
- .sdd/eval/golden/ (new) — golden benchmark tasks
- tests/unit/test_eval_harness.py (new)
- tests/unit/test_eval_metrics.py (new)
- tests/unit/test_eval_judge.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_eval_harness.py -x -q
- test_passes: uv run pytest tests/unit/test_eval_metrics.py -x -q
- file_contains: src/bernstein/eval/harness.py :: EvalHarness
- file_contains: src/bernstein/eval/metrics.py :: TaskCompletionRate
- file_contains: src/bernstein/eval/judge.py :: JudgeVerdict
- file_contains: src/bernstein/eval/taxonomy.py :: FailureTaxonomy
