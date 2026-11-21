# Best-of-N delegation

Best-of-N can run K parallel candidate agents in isolated worktrees and
select a winner using automated signals (tests pass, lint clean, runtime)
and, when configured, an LLM-as-judge rubric. This is not automatic:
setting a task's `best_of_n` field does not itself start candidate fan-out.
Worktree cleanup is also optional, and integrating the selected result is
the caller's responsibility.

## Why it exists

`task_retry` retries serially: fail → escalate → retry. That works
for transient errors. It does **not** work for genuinely ambiguous
tasks where serial retries compound the same wrong assumption. For
those, K parallel attempts in isolated sandboxes plus an optional judge is
cheaper in wall-clock and tokens than serial retries.

The infrastructure to spawn parallel candidates already existed (git
worktrees, sandbox backends, adaptive parallelism). This module is
the candidate-selection layer on top.

## How to use it

The plan loader and task-server API do not accept `best_of_n` yet; a
`best_of_n:` key on a plan step is ignored. In Python, set the field on a
`Task` to opt it in for callers using the task-routing helpers:

```python
task = Task(
    id="...",
    title="Migrate the auth module",
    description="Migrate the auth module from Flask to FastAPI",
    role="backend",
    best_of_n=3,
)
```

`is_best_of_n(task)` recognizes the opt-in only when `task.best_of_n > 1`
and the global `BEST_OF_N.enabled` flag is on. `task_n(task)` returns that
requested count clamped to the configured maximum (or `1` when the task is
not opted in). These helpers support routing; they do not start the runner.
There is currently no orchestration wiring that constructs
`BestOfNRunner`. A caller must explicitly provide a spawner and awaiter and
invoke `BestOfNRunner.run(task, n)`. The runner uses that explicit `n`
(clamped to `max_candidates`); it does not derive the count from the task
field or check the opt-in helpers itself:

```python
runner = BestOfNRunner(spawner=spawner, awaiter=awaiter, reclaimer=reclaimer)
outcome = runner.run(task, n=3)
```

The runner calls the spawner for the requested candidates, waits for their
`CandidateResult`s, then selects the highest-ranked candidate. By default,
selection blends passing tests (0.5), lint (0.2), judge (0.2), and runtime
(0.1); when no judge score is available, the judge weight is redistributed
among the other signals. A judge callback is optional. It is not called if
`judge` is absent or `BEST_OF_N.judge_enabled` is false; candidates are still
ranked without judge scores. When judging is enabled, empty-diff candidates also skip the judge callback
and are assigned a `judge_score` of `0.0`.

Cleanup is optional: only if a `reclaimer` callback is supplied does the
runner call it for each losing candidate. It does not automatically remove
worktrees otherwise. The returned `BestOfNOutcome` identifies the winner
and losers, but applying or merging the winner is left to the caller.

The caller should still run the cross-model verifier **on the winner**
before merging. Best-of-N selects a candidate; it does not replace
verification.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.BEST_OF_N.enabled` | `false` | Enables task opt-in helpers to route tasks with `best_of_n=K` to fan-out; it does not start the runner itself. |
| `defaults.BEST_OF_N.default_candidates` | `1` (off) | Declared but currently unread. |
| `defaults.BEST_OF_N.max_candidates` | `5` | Hard cap on the explicit runner count or helper-derived task count. |
| `defaults.BEST_OF_N.judge_model` | `haiku` | Declared but currently unread; the judge is whatever `judge=` callable is passed to `BestOfNRunner`. |

These are the `best_of_n` section of the `tuning:` block in `bernstein.yaml`.
The code reads only `enabled`, `max_candidates` (via `clamp_n`) and
`judge_enabled` (default `true`). The `score_weight_*` fields on
`BEST_OF_N` are also unread: scoring uses `ScoreWeights` (tests 0.5 /
lint 0.2 / judge 0.2 / runtime 0.1), overridable via `weights=` on
`BestOfNRunner`.
The judge rubric is an in-module default (`_DEFAULT_RUBRIC`); override
it per run by passing `rubric=` to `BestOfNRunner`.

Metrics:

- `bernstein_best_of_n_judge_score{role}` (histogram)
- `bernstein_best_of_n_candidates_total{outcome,role}` - `winner` / `loser`.

## Limitations

- One level of branching per task. No nested best-of-N inside a
  candidate.
- The operator sets `best_of_n` explicitly; there is no auto-escalation.
- All candidates run the same model unless you also set per-candidate
  `mode_profile` overrides.
- K worktrees mean K times the disk and parallel agent budget; the
  existing `adaptive_parallelism` cap still applies.

## Related

- Source: `src/bernstein/core/orchestration/best_of_n.py`
- Tick pipeline: `src/bernstein/core/orchestration/tick_pipeline.py`
- [Adaptive parallelism](../architecture/adaptive-parallelism.md)
- [Quality Pipeline](../architecture/quality-pipeline.md)
- PR #1011
