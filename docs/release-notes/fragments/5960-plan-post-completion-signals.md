## A plan step's completion signals now reach the task, and gate it

The planner built the task body field by field and never forwarded
`completion_signals`, so every task a plan posted (`bernstein run <plan.yaml>`,
`bernstein cook`) reached the server with none, and was judged by the default
verification heuristics instead of the witnesses the step declared. They are
now forwarded, so a step's declared signals decide when its task is done.

For that to work on correct work, the documented `file_contains` form,
`{type: file_contains, path: ..., contains: ...}`, is now kept whole. The plan
loader used to keep the path and drop the text, which the janitor can never
pass. It now builds the janitor's `"<path> :: <text>"` spec, and a
`file_contains` missing either half is refused when the plan loads.

Two plans that the task server refuses are now refused by `load_plan` and
`bernstein plan validate` instead of partway through a run, after earlier steps
had already been created: a step that declares both a non-default
`artifact_spec` and an `llm_judge` signal, and a step with more than 100
completion signals (#5960).
