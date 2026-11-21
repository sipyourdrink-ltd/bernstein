# Criterion profiles

Audience: operators who want per-task control over the
correctness / cost / latency / reversibility trade-off without hand-pinning
a model.

## Overview

A criterion profile is a probability simplex over four axes:

| Axis | Weight |
|------|--------|
| `correctness` | how much routing should prefer deeper-tier models |
| `cost` | how aggressively to push toward cheaper tiers |
| `latency` | how aggressively to push toward fast tiers |
| `reversibility` | how tight to clamp the per-task blast radius |

Weights sum to `1.0` within a `1e-3` tolerance. The dominant axis maps
deterministically to a `(forced_model, forced_effort, max_blast_radius)`
bias. The bias slots in **after** an explicit `task.model` /
`task.effort` override and **before** the bandit/cascade router, so a
`safety-first` task never silently lands on Haiku.

Source: `src/bernstein/core/routing/criterion_profile.py`,
`src/bernstein/cli/commands/criterion_profile_cmd.py`,
`templates/criterion_profiles/*.yaml`.

## Bundled presets

| Preset | correctness | cost | latency | reversibility |
|--------|------------:|-----:|--------:|--------------:|
| `safety-first` | 0.6 | 0.1 | 0.1 | 0.2 |
| `balanced`     | 0.25 | 0.25 | 0.25 | 0.25 |
| `speed-first`  | 0.2 | 0.1 | 0.6 | 0.1 |
| `cost-first`   | 0.1 | 0.6 | 0.2 | 0.1 |

Files live in `templates/criterion_profiles/*.yaml` and are bundled
into the wheel via `pyproject.toml`.

## Applying a profile

On a whole-plan basis:

```bash
bernstein run plan.yaml --criterion-profile safety-first
```

On a single task at add time:

```bash
bernstein add-task --criterion-profile cost-first ...
```

Inline override on a task (YAML / API): set
`metadata.criterion_profile` to the preset name or an inline weight
vector. Child tasks inherit the parent profile unless they override.

## Inspecting

```bash
bernstein criterion-profile list                  # bundled presets + paths
bernstein criterion-profile show <task_id>        # resolved weights + preset name
bernstein --json criterion-profile show <task_id> # machine-readable
```

`show` returns the resolved weight vector plus the named preset (or
`"inline"` for ad-hoc vectors).

## Disabling

Set `BERNSTEIN_CRITERION_PROFILE=0` in the orchestrator environment to
revert to the pre-existing routing path. The feature flag is read at
each routing call so CI can flip it without redeploying.

## Validation

| Check | Failure |
|-------|---------|
| Weights sum to 1.0 ± 1e-3 | `CriterionProfileError` |
| Negative weight | `CriterionProfileError` |
| Unknown axis name | `CriterionProfileError` |
| Malformed metadata on a task | logged warning; router falls through to its heuristic path |

## Troubleshooting

**Routing picks the wrong tier despite `safety-first`.** Confirm
`bernstein criterion-profile show <task_id>` reports
`preset=safety-first` and not `inline` with leftover weights.
`metadata.criterion_profile` set on the task wins over the run-level
`--criterion-profile`.

**Sum-tolerance error from a hand-written YAML.** Re-sum the four
weights; rounding to two decimals occasionally puts you outside the
`1e-3` tolerance. Either round to a vector that sums exactly to 1.0 or
use one of the bundled presets.
