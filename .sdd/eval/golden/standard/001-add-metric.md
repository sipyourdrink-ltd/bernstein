---
id: standard-001
title: Add new custom metric class
role: backend
expected_files_modified:
  - src/bernstein/eval/metrics.py
  - tests/unit/test_eval_metrics.py
completion_signals:
  - type: file_contains
    value: "src/bernstein/eval/metrics.py :: class"
  - type: test_passes
    value: "uv run pytest tests/unit/test_eval_metrics.py -x -q"
max_cost_usd: 0.50
max_duration_s: 180
owned_files:
  - src/bernstein/eval/metrics.py
  - tests/unit/test_eval_metrics.py
---

Add a new `ErrorRecoveryRate` metric class to `src/bernstein/eval/metrics.py`.
It should track the fraction of tasks that recovered from an initial failure
via retry. Include a `rate` property and write a test in `tests/unit/test_eval_metrics.py`.
