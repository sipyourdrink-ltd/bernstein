# Quality Pipeline

Audience: developers who want to understand how Bernstein decides whether
agent output is good enough to merge.

## Overview

After every agent finishes a task, Bernstein runs the **janitor**, which
combines two complementary verification surfaces. The first is a
**structured-signal verifier** that evaluates declarative completion signals
attached to the task (file exists, test passes, regex match). The second is
the **gate pipeline** - a configurable sequence of build/lint/type/test/security
gates that runs on the actual diff. Only when both layers agree does the task
move toward merge.

If verification fails, Bernstein doesn't just block - it feeds the failure
back into the cascade-router via `record_and_escalate()`, which retries the
task on a more capable model in the same chain. An optional **cross-model
verifier** runs the diff past a *second* model from a different provider for
A/B-style review; this layer is shipped but disabled by default. The result
is a deterministic, programmable quality gate where escalation cost is
bounded by the cascade order and observable through `.sdd/metrics/`.

## The Janitor

Source: `src/bernstein/core/quality/janitor.py`. The janitor is the
post-completion verification entry point. Inputs: a `Task` and the
worktree path. Outputs: a `JanitorResult` per signal evaluated.

The janitor evaluates each `CompletionSignal` declared on the task
(`janitor.py:48-77`):

| Signal type      | Behaviour                                                               |
| ---------------- | ----------------------------------------------------------------------- |
| `path_exists`    | File or directory exists at the given relative path.                    |
| `glob_exists`    | At least one file matches the glob.                                     |
| `test_passes`    | The named shell command exits 0 (e.g. `pytest tests/foo.py`).            |
| `file_contains`  | A regex matches the file's content.                                     |
| `llm_review`     | Synchronous LLM review against a written rubric.                        |
| `llm_judge`      | Async LLM judge (`judge_task()`, `janitor.py:462`); used for ambiguous tasks. |

`verify_task()` (`janitor.py:80-97`) reduces all signals to a single
pass/fail and a list of failure descriptions. The async
`run_janitor()` entry point (`janitor.py:171-260`) is what the
orchestrator calls after each agent completes. It mixes synchronous
signal evaluation with async LLM judges (`judge_task()`,
`janitor.py:462`), enforces a per-judge `CompletionBudget`, and emits one
`JanitorResult` per evaluated task.

Two LLM-mediated paths exist for ambiguous verification:

- **`llm_review`** - synchronous, runs once per signal, expects a yes/no
  verdict against a rubric (`janitor.py:_check_llm_review`).
- **`llm_judge`** - async with retry. `JUDGE_MODEL = "anthropic/
  claude-sonnet-4-20250514"`, `JUDGE_MAX_TOKENS = 1024`,
  `JUDGE_CONFIDENCE_THRESHOLD = 0.7`; below the threshold, results are
  flagged for human review (`janitor.py:36-44`). The judge prompt template
  lives in `prompts/judge.md`.

The janitor never blocks merge by itself - it produces results that the
orchestrator interprets. A failed janitor verification is the first
escalation trigger consulted by the cascade-router (see
[Pipeline → cascade-router escalation](#pipeline-cascade-router-escalation)
below).

## Gates

Source: `src/bernstein/core/quality/gate_pipeline.py`,
`src/bernstein/core/quality/quality_gates.py`,
`src/bernstein/core/quality/gate_runner.py`,
`src/bernstein/core/quality/gate_plugins.py`.

A **gate** is a discrete check with a unique name, a required/optional
flag, and an execution condition. Gates run on the diff after every agent
completion, in the order the configured pipeline lists them.

The full set of recognised built-in gate names lives in
`gate_pipeline.py:VALID_GATE_NAMES` (`:16-41`). The default pipeline,
synthesised when `quality_gates.pipeline` is not explicitly set, is
`build_default_pipeline()` in `gate_pipeline.py:164-170`, driven by the
table at `gate_pipeline.py:137-161`. Each entry is
`(config_flag, gate_name, required, condition)`.

Default required gates (only those whose `quality_gates.<flag>: true`):

| Gate name                | Default flag                | Default condition  | Default `required`? |
| ------------------------ | --------------------------- | ------------------ | ------------------- |
| `lint`                   | `lint: true`                | `always`           | required            |
| `type_check`             | `type_check: false`         | `python_changed`   | required (if on)    |
| `tests`                  | `tests: false`              | `python_changed`   | required (if on)    |
| `security_scan`          | `security_scan: false`      | `python_changed`   | required            |
| `complexity_check`       | `complexity_check: false`   | `python_changed`   | required            |
| `pii_scan`               | `pii_scan: true`            | `any_changed`      | required            |
| `dlp_scan`               | `dlp_scan: true`            | `any_changed`      | required            |
| `merge_conflict`         | `merge_conflict_check`      | `any_changed`      | required            |
| `coverage_delta`         | `coverage_delta`            | `python_changed`   | required            |
| `dep_audit`              | `dep_audit`                 | `deps_changed`     | required            |
| `import_cycle`           | `import_cycle_check`        | `python_changed`   | required            |
| `intent_verification`    | `intent_verification`       | `any_changed`      | required            |
| `mutation_testing`       | `mutation_testing`          | `python_changed`   | required            |
| `dead_code`              | `dead_code_check: false`    | `python_changed`   | optional            |
| `comment_quality`        | `comment_quality_check`     | `python_changed`   | optional            |
| `auto_format`            | `auto_format`               | `any_changed`      | optional            |
| `large_file`             | `large_file_check`          | `any_changed`      | optional            |
| `integration_test_gen`   | `integration_test_gen`      | `python_changed`   | required            |
| `review_rubric`          | `review_rubric`             | `python_changed`   | required            |
| `test_expansion`         | `test_expansion`            | `python_changed`   | optional            |
| `agent_test_mutation`    | `agent_test_mutation`       | `tests_changed`    | required            |
| `benchmark`              | `benchmark.enabled`         | `always`           | required            |

A failing **required** gate hard-blocks merge. A failing **optional** gate
is reported but does not block.

Gate conditions (`gate_pipeline.py:42`) gate execution by what changed:
`always`, `python_changed`, `tests_changed`, `any_changed`, `deps_changed`.
The legacy condition string `changed_files.any('.py')` is normalised to
`python_changed` (`gate_pipeline.py:74-81`).

### Adding a custom gate

Custom gates plug in through the `bernstein.gates` entry-point group
(`gate_plugins.py:107-120`) or via a Python file dropped into
`.bernstein/gates/*.py` (`gate_plugins.py:87-105`). Both modes load
classes that subclass `GatePlugin` (`gate_plugins.py:20-46`):

```python
from pathlib import Path
from bernstein.core.quality.gate_plugins import GatePlugin
from bernstein.core.quality.gate_runner import GateResult

class NoFooGate(GatePlugin):
    @property
    def name(self) -> str:
        return "no_foo"

    @property
    def required(self) -> bool:
        return True

    @property
    def condition(self) -> str:
        return "any_changed"

    def run(
        self,
        changed_files: list[str],
        run_dir: Path,
        task_title: str,
        task_description: str,
    ) -> GateResult:
        offending = [f for f in changed_files if "foo" in Path(f).read_text()]
        passed = not offending
        return GateResult(
            name=self.name,
            status="pass" if passed else "fail",
            required=self.required,
            blocked=not passed,
            cached=False,
            duration_ms=0,
            details=f"Found 'foo' in {offending}" if offending else "Clean",
        )
```

Register via `pyproject.toml`:

```toml
[project.entry-points."bernstein.gates"]
no_foo = "my_pkg.gates:NoFooGate"
```

The plugin name must not collide with a built-in
(`gate_plugins.py:81-82`). Names are validated and duplicates raise.
File-based plugins under `.bernstein/gates/` are loaded for ad-hoc
project-local checks; they have the same lifecycle but are not packaged.

## Cross-model verifier

Source: `src/bernstein/core/quality/cross_model_verifier.py`. This is the
"writer != reviewer" layer: after an agent finishes, the diff is sent to
a *different* model (a cheap one from a different provider) with a
focused code-review prompt.

The default reviewer mapping (`cross_model_verifier.py:37-43`):

| Writer family contains | Reviewer model                        |
| ---------------------- | ------------------------------------- |
| `claude`               | `google/gemini-flash-1.5`             |
| `gemini`               | `anthropic/claude-haiku-4-5-...`      |
| `gpt` / `codex`        | `gemini-flash-1.5` / `claude-haiku`   |
| `qwen`                 | `claude-haiku`                        |

`CrossModelVerifierConfig` (`:84-106`) is `enabled=True` *as a class
default*, but the orchestrator config wires it off by default - operators
must enable it explicitly via `quality_gates.cross_model.enabled: true`.

The reviewer is asked for one of two verdicts (`:120-123`):

- `approve` - diff is fine.
- `request_changes` - diff has issues. When `block_on_issues=True`
  (default), this prevents merge and creates a fix task; otherwise
  findings are logged only.

For higher-stakes deployments, `voting_config: VotingConfig` lets you
elect multiple reviewer models and apply quorum logic
(`cross_model_verifier.py:106`). A single reviewer is the default
QUORUM(1,1) behaviour.

Cost controls baked into the module (`:29-34`): diff truncated at 12,000
chars, response capped at 512 tokens, `provider="openrouter"`. With
default reviewers this is in the cents-per-task range.

## Pipeline → cascade-router escalation

The whole pipeline exists to feed information back into the cascade-
router so weaker-but-cheaper models can be tried first. The escalation
contract is in `src/bernstein/core/routing/cascade_router.py`.

After the orchestrator records a completed attempt, it calls
`CascadeRouter.record_and_escalate(chain_id, task, attempt,
janitor_passed=..., output=...)` (`cascade_router.py:386-478`). The
function consults `_should_escalate()` (`:639-673`) in this order:

1. **Hard task failure** - `attempt.success=False` with no other context →
   escalate (`:655-657`).
2. **Janitor verification failure** - `janitor_passed=False` → escalate
   (`:660-661`). This is the wire from janitor results into model
   escalation.
3. **Low-confidence regex on agent output** - `detect_low_confidence()`
   scans the last 2,000 chars for phrases like `"I'm not sure"`,
   `"partial implementation"`, `"TODO: escalat"` (`:371-384`,
   `_LOW_CONFIDENCE_PATTERN`). When matched, escalate.
4. **Explicit failure flag** - `attempt.success=False` after the above
   checks (`:670-671`).

If any trigger fires, the cascade list (`_cascade_for_task()`,
`:681-700`) is consulted: standard tasks step `haiku → sonnet → opus`;
high-stakes tasks (role in `manager`/`architect`/`security`, complexity
high, scope large, priority 1) skip haiku and step `sonnet → opus`. When
the current model is already at the top, escalation gives up
(`:448-455`).

The bandit (`EpsilonGreedyBandit` from `core/cost/cost.py`) is updated on
every observation (`cascade_router.py:559-568`). On the next call to
`select()` for a fresh task, the router proactively skips a tier when
`observations >= MIN_OBSERVATIONS` and `success_rate < QUALITY_THRESHOLD`
(`:594-614`) - i.e. the bandit learns "haiku never works for role=qa,
start at sonnet."

Chain reports persist to `.sdd/metrics/cascade_chains.jsonl`
(`save_chain()`, `:518-539`). Each line lists every attempt with
`{model, cost_usd, latency_s, success, escalated, escalation_reason}`,
the final model, total cost, and `saved_vs_direct_opus_usd`.

The full cross-adapter (rather than intra-Claude) story - what happens on
rate-limit / timeout / API error - lives in
`core/routing/cascade.py:CascadeFallbackManager`. Both surfaces are
documented end-to-end in [Model routing](model-routing.md).

## Configuration

All knobs live under `quality_gates.*` in `bernstein.yaml`. The dataclass
that defines them is `QualityGatesConfig`
(`core/quality/quality_gates.py:135-265`). Highlights:

```yaml
quality_gates:
  enabled: true                  # master switch
  lint: true
  lint_command: "ruff check ."
  type_check: false
  type_check_command: "pyright"
  tests: false
  test_command: "uv run python scripts/run_tests.py -x"
  timeout_s: 120
  base_ref: "main"               # base for incremental diff
  cache_enabled: true            # reuse gate results when diff is unchanged
  allow_bypass: false            # whether the CLI can skip gates

  pii_scan: true
  dlp_scan: true
  security_scan: false
  coverage_delta: false
  complexity_check: false
  dead_code_check: false
  comment_quality_check: false
  import_cycle_check: false
  merge_conflict_check: false
  mutation_testing: false
  dep_audit: false
  benchmark:
    enabled: false

  intent_verification:
    enabled: false               # LLM-based "did this satisfy intent?"
    model: "google/gemini-flash-1.5"
    block_on_no: true

  cross_model:                   # cross-model verifier (writer != reviewer)
    enabled: false
```

When `pipeline:` is omitted, Bernstein synthesises one from the booleans
above. To override the order (or insert custom gates), declare an
explicit pipeline:

```yaml
quality_gates:
  pipeline:
    - { name: "lint",       required: true,  condition: "always" }
    - { name: "type_check", required: true,  condition: "python_changed" }
    - { name: "tests",      required: true,  condition: "python_changed" }
    - { name: "no_foo",     required: true,  condition: "any_changed" }  # custom
```

## Observability

Quality endpoints (FastAPI, all in `core/routes/quality.py` and
`file_health.py`):

| Endpoint                              | Returns                                                                  |
| ------------------------------------- | ------------------------------------------------------------------------ |
| `GET /quality`                        | Aggregated success rate, gate pass rate, p50/p90/p99 task duration.     |
| `GET /quality/budget-forecast`        | Forecast of remaining budget given current burn rate (`:376`).          |
| `GET /quality/trend`                  | Time-series of pass/fail counts (`:561`).                                |
| `GET /quality/models`                 | Per-model success metrics (`:625`).                                      |
| `GET /quality/file-health`            | File-level health scores (`file_health.py:31`).                          |
| `GET /quality/file-health/flagged`    | Files currently flagged by gates (`:85`).                                |
| `GET /quality/file-health/{path}`     | Single-file health report (`:107`).                                      |

On-disk artefacts:

- `.sdd/metrics/quality_gates.jsonl` - one line per gate execution
  (`quality_gates.py:1148`).
- `.sdd/metrics/cascade_chains.jsonl` - one line per cascade chain
  completion (`cascade_router.py:533`).
- `.sdd/metrics/tasks.jsonl` - task lifecycle events used by behaviour
  anomaly detection.

Trend reads, file-health rollups, and budget forecasts all stream from
these JSONL files; tail them directly when the API is unavailable. See
[Observability overview](../operations/observability-overview.md) for
how these signals integrate with Prometheus, Grafana, and SLOs.

## Code pointers

| Concern                          | File                                                       |
| -------------------------------- | ---------------------------------------------------------- |
| Janitor                          | `src/bernstein/core/quality/janitor.py`                    |
| Gate pipeline structure          | `src/bernstein/core/quality/gate_pipeline.py`              |
| QualityGatesConfig (yaml schema) | `src/bernstein/core/quality/quality_gates.py`              |
| Gate execution                   | `src/bernstein/core/quality/gate_runner.py`                |
| Custom gate plugin discovery     | `src/bernstein/core/quality/gate_plugins.py`               |
| Cross-model verifier             | `src/bernstein/core/quality/cross_model_verifier.py`       |
| Quality score scoring            | `src/bernstein/core/quality/quality_score.py`              |
| Review pipeline (LLM review)     | `src/bernstein/core/quality/review_pipeline/`              |
| Cascade router (escalation)      | `src/bernstein/core/routing/cascade_router.py`             |
| Cross-adapter cascade fallback   | `src/bernstein/core/routing/cascade.py`                    |
| Quality HTTP routes              | `src/bernstein/core/routes/quality.py`                     |
| File health routes               | `src/bernstein/core/routes/file_health.py`                 |
