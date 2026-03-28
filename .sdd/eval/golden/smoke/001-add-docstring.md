---
id: smoke-001
title: Add docstring to existing function
role: backend
expected_files_modified:
  - src/bernstein/core/models.py
completion_signals:
  - type: file_contains
    value: "src/bernstein/core/models.py :: docstring"
max_cost_usd: 0.10
max_duration_s: 60
owned_files:
  - src/bernstein/core/models.py
---

Add a Google-style docstring to any undocumented public method in
`src/bernstein/core/models.py`. The docstring must include Args and Returns
sections if applicable.
