## `run --from-plan` accepts the YAML plans the docs show

`bernstein run --from-plan plan.yaml` failed with "Could not extract goal from
plan file" on any YAML plan, including one carrying a top-level `goal:`. The
goal was read before the plan was, and that reader knew only JSON and a markdown
`**Goal:**` line, so the YAML loader behind it was never reached -- and per-step
model routing, which has no other entry point, was unreachable from the CLI. A
staged plan's `name` and a top-level `goal` are both read now (#6080).
