## A plan step's completion signals now reach the server

`bernstein run --from-plan` posts each task to the task server with a body built
field by field, and `completion_signals` had no forward in it. Every task
created that way arrived with no witnesses and was verified by the default
heuristics instead of by the signals the plan declared -- silently, since an
empty list is also what a task with nothing declared looks like. The signals are
now forwarded when present (#5960).
