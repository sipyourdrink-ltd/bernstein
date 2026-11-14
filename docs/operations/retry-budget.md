# Retry budget (criterion-aware)

Audience: operators tired of identical retries that re-burn the same
budget and produce the same failure.

## Overview

The default retry path reruns with the same model, prompt, and gate
criteria - so attempt #2 typically fails the same way as attempt #1.

The criterion-aware retry budget instead dials down a *named criterion*
on each retry. The first retry degrades the first criterion, the
second retry the second, and so on. Once every criterion has been
degraded the floor is held: further retries are permitted but produce
no additional degradation.

Source:

- `src/bernstein/core/cost/retry_budget.py`
- `src/bernstein/cli/run_bootstrap.py` (the `--retry-budget` flag)

## Spec syntax

```
N retries, degrade: <criterion1>[>criterion2[>criterion3...]]
```

Examples:

| Spec | Behaviour |
|------|-----------|
| `3 retries, degrade: coverage>tests>style` | Attempt 1: coverage degraded. Attempt 2: tests degraded. Attempt 3: style degraded. |
| `2 retries, degrade: coverage` | Attempt 1: coverage degraded. Attempt 2: no further degradation (held at floor). |
| `0 retries, degrade: coverage` | Never retries; useful as a "fail fast" pin. |

Parser: `parse_retry_budget_spec()`. Errors raised eagerly at CLI
parse time (before any agent spawns):

- `UnknownCriterionError` - unknown criterion name
- `DuplicateCriterionError` - same criterion listed twice
- `RetryBudgetError` - generic malformed spec

## CLI

```bash
bernstein run plan.yaml --retry-budget "3 retries, degrade: coverage>tests>style"
```

The parsed spec is validated eagerly, then exported to the orchestrator
via `BERNSTEIN_RETRY_BUDGET_SPEC`.

## Criterion semantics

Each criterion is a `(name, level, min_level, max_level)` tuple. Each
degradation step decrements the level by `1` until `min_level`
(`is_at_floor` returns `True` at that point). The actual
interpretation of a criterion name (e.g. `coverage`, `tests`, `style`)
is owned by the gating layer that consumes the snapshot.

## Snapshot returned per retry

`RetryDecision` (frozen dataclass) is the snapshot returned for a retry
position:

| Field | Use |
|-------|-----|
| `should_retry` | Whether the decision says to retry. On `peek()` this is only a preview, not an eligibility check or authorisation; see below. |
| `attempt_index` | Zero-based retry position (attempt 0 is the first retry). |
| `degraded_criterion` | The criterion whose level was lowered, the selected criterion already at its minimum, or `None` if no criterion is assigned to this position. |
| `degradation_kind` | `LOWERED`, `FLOORED`, or `NONE`; says what happened to the criterion. |
| `criteria_snapshot` | Current `(name -> level)` mapping after the step (for a preview, the proposed step). |
| `reason` | Human-readable rationale for logs / decisions. |

These three criterion outcomes are distinct:

| Outcome | `degraded_criterion` | `degradation_kind` | Meaning |
|---------|----------------------|--------------------|---------|
| Lowered | The resulting criterion, at its new lower level | `LOWERED` | A selected criterion was above its minimum and was lowered by one. |
| Selected criterion already at minimum | The selected criterion, still at its minimum | `FLOORED` | A criterion was assigned to this retry position, but could not be lowered further. |
| No criterion assigned to this retry position | `None` | `NONE` | The retry position has no corresponding criterion in the configured policy; the retry can still be allowed. |

A small deterministic preview illustrates all three outcomes:

```python
from bernstein.core.cost.retry_budget import Criterion, DegradationKind, RetryBudget

lowered = RetryBudget.from_names(retries=1, names=["coverage"], max_level=1, min_level=0).peek()
assert lowered.degradation_kind is DegradationKind.LOWERED
assert lowered.degraded_criterion is not None and lowered.degraded_criterion.level == 0

floored = RetryBudget(
    retries=1,
    criterion_degradation=[Criterion("coverage", level=0, min_level=0, max_level=1)],
).peek()
assert floored.degradation_kind is DegradationKind.FLOORED
assert floored.degraded_criterion is not None and floored.degraded_criterion.level == 0

unassigned = RetryBudget.from_names(retries=1, names=[]).peek()
assert unassigned.degradation_kind is DegradationKind.NONE
assert unassigned.degraded_criterion is None
```

## Preview versus eligibility

`peek()` is read-only: it previews the next position without advancing
`attempts_used` or changing criteria. It does not check whether the retry
budget has any attempts left, so its decision can have
`should_retry=True` even when `attempts_left == 0`. Check `attempts_left`
(or `is_exhausted`) as well as a preview when deciding eligibility, or
use `consume()` as the authoritative consuming decision. `consume()`
advances the retry and criterion state when eligible; if the budget is
exhausted, it returns `should_retry=False` without mutating state.

For example, with one retry available, the first consume uses it; a
subsequent preview can still describe the next policy position positively,
but another consume denies it:

```python
from bernstein.core.cost.retry_budget import RetryBudget

budget = RetryBudget.from_names(retries=1, names=["coverage"])
first = budget.consume()
assert first.should_retry and budget.attempts_left == 0

preview = budget.peek()
assert preview.should_retry  # preview is not eligibility
assert budget.attempts_left == 0
rejected = budget.consume()
assert not rejected.should_retry  # consuming decision checks eligibility
```

## Errors

| Class | Cause |
|-------|-------|
| `RetryBudgetError` | Base class for the sealed hierarchy |
| `CriterionExhaustedError` | A criterion below `min_level` was asked to degrade |
| `DuplicateCriterionError` | Two entries with the same name |
| `UnknownCriterionError` | Spec references a criterion the consumer never registered |
| `RetryBudgetExhaustedError` | All retries already consumed |

## Examples

Tighten the budget for a flaky pipeline:

```bash
bernstein run plan.yaml --retry-budget "2 retries, degrade: coverage>tests"
```

Disable retries entirely for a deterministic CI run:

```bash
bernstein run plan.yaml --retry-budget "0 retries, degrade: coverage"
```

## Troubleshooting

**`invalid --retry-budget value`.** Click rejected the spec eagerly.
The error string names the offending token (unknown criterion,
duplicate name, or malformed `>` chain).

**Retries happen but the criterion never seems to degrade.** Confirm
the consumer reads `BERNSTEIN_RETRY_BUDGET_SPEC` and calls
`RetryBudget.consume()` rather than just `peek()`. The peek path
returns the next decision but does not advance the cursor.
