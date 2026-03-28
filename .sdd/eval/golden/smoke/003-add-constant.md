---
id: smoke-003
title: Extract magic number to named constant
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

Find a magic number in `src/bernstein/core/orchestrator.py` and extract it
to a module-level named constant with a descriptive name.
