# Release Note Fragment: 5458-bench-ci-sarif

## Summary
Introduced benchmark CI surface featuring SARIF v2.1.0 report generation, GitHub Check Run scorecard posting, and regression delta analysis against signed verified baseline bundles.

## Key Changes
- `src/bernstein/eval/bench/sarif.py`: Added `bundle_to_sarif` converting bundle failures to SARIF 2.1.0 diagnostic reports for GitHub Code Scanning.
- `src/bernstein/eval/bench/ci.py`: Added `BenchScorecard`, `evaluate_ci_scorecard`, and `post_bench_check_run` computing pass rate and score deltas against verified baselines. Missing or unverified baselines yield neutral conclusions, while regressions exceeding threshold fail the check.
- `src/bernstein/github_app/check_runs.py`: Added `create_bench_check_run` helper to publish native GitHub Check Runs with Markdown scorecard tables.
- `src/bernstein/eval/bench/bench_cli.py`: Added `--ci`, `--sarif-out`, `--baseline`, and `--regression-threshold` options to `bernstein bench run`.
- `tests/unit/eval/bench/test_bench_ci.py`: Comprehensive test matrix covering SARIF schemas, scorecard evaluations, check run posting, and CLI integration.
- `docs/eval/bench.md`: Updated architecture diagrams, CLI walkthrough, and Python API references.
