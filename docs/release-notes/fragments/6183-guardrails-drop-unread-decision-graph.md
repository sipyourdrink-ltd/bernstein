## `run_guardrails` no longer builds a decision graph it never reads

`run_guardrails` built a `DecisionGraph`, added every guardrail decision to
it, and returned without evaluating it, while a comment said the graph was
being populated to build the results. Nothing consulted it: each result has
always been judged on its own decision. The construction and the per-decision
`add_decision` call are gone, and the comment says what the function actually
does. `DecisionGraph` keeps its own tests for when a caller needs its
precedence (#6183).
