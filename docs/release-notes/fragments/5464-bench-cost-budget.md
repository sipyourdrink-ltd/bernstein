# Release Note Fragment: 5464-bench-cost-budget

## Summary
Added per-task and total token, cost (USD), and duration accounting to benchmark submission bundles (`SubmissionBundle` / `TaskResult`, bumped schema to v2).
Introduced `bernstein bench compare <bundle_a> <bundle_b>` for side-by-side delta reporting on pass rate, accuracy score, USD cost, tokens, and wall-clock duration.
Added `--budget <usd>` flag to `bernstein bench run` which gates runner execution when cumulative spend exceeds the budget threshold and records verifiable refusal receipts.

## Key Changes
- `src/bernstein/eval/bench/bundle.py`: Added `tokens`, `cost_usd`, and `duration_seconds` to `TaskResult`; added `bundle_schema_version`, `total_tokens`, `total_cost_usd`, and `total_duration_seconds` properties to `SubmissionBundle`.
- `src/bernstein/eval/bench/compare.py`: Created `compare_bundles`, `CompareResult`, and `TaskComparison` supporting markdown and JSON reporting.
- `src/bernstein/eval/bench/runner.py`: Implemented budget gate in `BenchRunner` with refusal receipt generation.
- `src/bernstein/eval/bench/bench_cli.py`: Added `compare` command and `--budget` option to `run` command.
- `tests/unit/eval/bench/test_bench_cost_budget.py`: Comprehensive test matrix covering cost accounting, comparison calculations, CLI commands, and budget gate enforcement.
- `docs/eval/bench.md`: Updated architecture diagram, CLI walkthrough, bundle JSON format, and Python API examples.
