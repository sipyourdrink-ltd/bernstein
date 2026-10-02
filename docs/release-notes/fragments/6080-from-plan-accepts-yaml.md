## `run --from-plan` runs the YAML plan it is given

`bernstein run --from-plan plan.yaml` failed with "Could not extract goal from
plan file" on any YAML plan. Reading the goal fixed that, but the command then
re-planned from the plan's name alone: every stage, step, per-step model pin and
`depends_on` was dropped while it printed "Loaded plan from" and exited 0.

A YAML plan is now loaded and run exactly as `bernstein run plan.yaml` runs it,
so the steps, their models and their dependencies reach the task server. The
plan's `cli` and `budget` are applied (the `--cli` and `--budget` flags win). A
file that is not a staged plan, such as a seed with a top-level `goal:`, is
refused with a message naming `--seed`. A plan that sets `max_agents`, `repos`
or `constraints` is refused too, because `bernstein run` has no way to apply
them and running anyway would execute a different plan from the one written
(#6080).
