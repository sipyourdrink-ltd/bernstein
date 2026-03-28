---
id: stretch-001
title: Refactor taxonomy to support custom categories
role: architect
expected_files_modified:
  - src/bernstein/eval/taxonomy.py
  - src/bernstein/eval/harness.py
  - tests/unit/test_eval_harness.py
completion_signals:
  - type: test_passes
    value: "uv run pytest tests/unit/test_eval_harness.py -x -q"
  - type: file_contains
    value: "src/bernstein/eval/taxonomy.py :: register_category"
max_cost_usd: 2.00
max_duration_s: 600
owned_files:
  - src/bernstein/eval/taxonomy.py
  - src/bernstein/eval/harness.py
  - tests/unit/test_eval_harness.py
---

Extend the failure taxonomy to support user-defined custom failure categories.
Add a `register_category` function that accepts a name, description, and severity.
The `classify_failure` function should check custom categories before the default set.
Update the harness and tests accordingly.
