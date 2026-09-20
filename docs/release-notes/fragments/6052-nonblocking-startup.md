## `bernstein -g GOAL` no longer crashes on slow agent discovery

The bare `bernstein` entry point ran agent discovery in a background thread
and then joined it with `_splash_future.result(timeout=10)`. On a machine
where discovery takes longer than ten seconds that wait raised
`TimeoutError`, so `bernstein -g "..." --plan-only` died with a traceback
instead of rendering the plan.

The startup results are cosmetic and not read downstream, so the join is
gone and the background thread is never awaited. The run callback and
`--plan-only` now execute regardless of how long discovery takes.
