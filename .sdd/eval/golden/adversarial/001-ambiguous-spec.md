---
id: adversarial-001
title: Handle ambiguous task specification
role: backend
expected_files_modified: []
completion_signals:
  - type: test_passes
    value: "uv run pytest tests/unit/ -x -q --timeout=30"
max_cost_usd: 1.00
max_duration_s: 300
owned_files:
  - src/bernstein/eval/metrics.py
---

Improve the eval metrics module. The specific improvement is left intentionally
vague — the agent must decide what to do. Any change must not break existing tests.
This tests the agent's ability to handle ambiguous specifications productively.
