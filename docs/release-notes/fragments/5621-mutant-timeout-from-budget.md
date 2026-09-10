## Derive mutant run timeout from module budget and stop scoring timeouts as kills

`scripts/mutmut_critical.py` scored timed-out mutant runs as kills, inflating
the kill rate when slow test suites hit their cap without actually asserting
mutant detection. In addition, mutant runs were capped at a flat 180-second
constant regardless of the module's configured `budget_seconds`. Mutant runs now
derive their timeout from `budget_seconds` (matching the baseline policy),
timed-out mutants are no longer counted towards `killed`, and runs with a
material share of timeouts report as unmeasured (#5621).
