---
id: standard-002
title: Add --json flag to eval report command
role: backend
expected_files_modified:
  - src/bernstein/cli/main.py
completion_signals:
  - type: file_contains
    value: "src/bernstein/cli/main.py :: --json"
max_cost_usd: 0.50
max_duration_s: 180
owned_files:
  - src/bernstein/cli/main.py
---

Add a `--json` flag to the `bernstein eval report` CLI command that outputs
the report as structured JSON instead of formatted text. Use `click.option`.
