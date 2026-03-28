---
id: smoke-002
title: Fix typo in log message
role: backend
expected_files_modified:
  - src/bernstein/core/orchestrator.py
completion_signals:
  - type: test_passes
    value: "uv run pytest tests/unit/test_orchestrator.py -x -q"
max_cost_usd: 0.10
max_duration_s: 60
owned_files:
  - src/bernstein/core/orchestrator.py
---

Find and fix any typo in a log message within `src/bernstein/core/orchestrator.py`.
Ensure all existing tests still pass after the change.
