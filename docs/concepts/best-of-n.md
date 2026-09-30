# Best-of-N delegation

For tasks tagged "complex" or "ambiguous", Bernstein can spawn K
parallel candidate agents in isolated worktrees, score each candidate
with automated signals (tests pass, lint clean, runtime) plus an
LLM-as-judge rubric, and merge only the winner. Losing candidates'
worktrees are cleaned up automatically.

## Why it exists

`task_retry` retries serially: fail → escalate → retry. That works
for transient errors. It does **not** work for genuinely ambiguous
tasks where serial retries compound the same wrong assumption. For
those, K parallel attempts in isolated sandboxes plus a judge is
cheaper in wall-clock and tokens than serial retries.

The infrastructure to spawn parallel candidates already existed (git
worktrees, sandbox backends, adaptive parallelism). This module is
the candidate-selection layer on top.

## How to use it

The plan loader and the task-server API do not accept `best_of_n` yet; a
`best_of_n:` key on a plan step is ignored. Set it on a `Task` object in
Python and drive the fan-out with `BestOfNRunner` directly:

```python
task = Task(
    id="...",
    title="Migrate the auth module",
    description="Migrate the auth module from Flask to FastAPI",
    role="backend",
    best_of_n=3,
)
```

The orchestrator does not fan out best-of-N tasks yet: nothing in `src/`
constructs `BestOfNRunner`, and `tick_pipeline.partition_best_of_n` (a
helper that separates best-of-N tasks) has no caller. A caller that runs
`BestOfNRunner.run(task, n)` gets:

1. The `spawner` callback invoked for n candidates (clamped to
   `max_candidates`).
2. The `awaiter` callback returning `CandidateResult`s.
3. The optional `judge` callback scoring candidates that have a diff
   (skipped when `judge` is None or `judge_enabled` is false).
4. `select_best` picking the highest blended score (tests, lint, runtime,
   judge).
5. The optional `reclaimer` called for each loser.

It returns a `BestOfNOutcome`; merging the winner is left to the caller.

The cross-model verifier still runs **on the winner** before merge.
Best-of-N does not replace verification - it picks a candidate to
verify.

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.BEST_OF_N.enabled` | `false` | Master switch; tasks must set `best_of_n=K` *and* this must be on to fan out. |
| `defaults.BEST_OF_N.default_candidates` | `1` (off) | Declared but currently unread. |
| `defaults.BEST_OF_N.max_candidates` | `5` | Hard cap, regardless of what a plan asks for. |
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
