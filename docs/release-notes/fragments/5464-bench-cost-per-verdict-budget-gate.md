## Benchmark cost per verdict, bundle comparison, and budget gate

Submission bundles now record token count, cost in USD, and duration per task alongside
verdict breakdowns (`cost_per_verdict` and `tokens_per_verdict`) with bundle schema version 2.
`bernstein bench compare` enables side-by-side comparison of submission bundles across
accuracy, cost, tokens, and wall time. `bernstein bench run` adds `--budget` to enforce a
maximum spend limit for CI benchmark runs, halting execution and recording budget status
when exceeded (#5464).
