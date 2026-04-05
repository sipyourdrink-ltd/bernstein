# Task: {{TASK_TITLE}}

## Description
{{TASK_DESCRIPTION}}

{{#IF FILES}}
## Files to work with
{{FILES}}
{{/IF}}

{{#IF CONTEXT}}
## Context
{{CONTEXT}}
{{/IF}}

## Instructions
1. Read the failure context and identify the root cause
2. Make the smallest change that fixes the failure
3. Verify locally:
   - `uv run ruff check src/` (lint)
   - `uv run ruff format --check src/` (format)
   - `uv run python scripts/run_tests.py -x` (tests)
4. Commit only the files you changed: `fix: resolve CI failure — <brief description>`

## If stuck or blocked
- If you cannot determine the fix, mark the task as failed:
  ```bash
  curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/fail \
    -H "Content-Type: application/json" \
    -d '{"reason": "<describe what went wrong and what you tried>"}'
  ```

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <what was fixed>"}'
```
