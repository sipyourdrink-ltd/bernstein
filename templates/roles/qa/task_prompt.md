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
1. Read the code under test before writing a single test
2. Cover: happy path, edge cases (empty, boundary, None), and error paths
3. Use descriptive test names: `test_<function>_<scenario>_<expected_outcome>`
4. Mock external dependencies (network, filesystem, time); do NOT mock internal logic
5. Run the full suite to check for regressions: `uv run python scripts/run_tests.py -x`
6. If you find a bug while testing, document it as a failing test before fixing

## If stuck or blocked
- If a curl to the task server fails, retry up to 3 times with 2-second delays
- If tests fail after your changes, fix the code — do not skip tests or mark complete with failures
- If you cannot determine the fix, mark the task as failed:
  ```bash
  curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/fail \
    -H "Content-Type: application/json" \
    -d '{"reason": "<describe what went wrong and what you tried>"}'
  ```
- If blocked by another agent's files, post to the bulletin board and move on

## Bulletin board
Post discoveries, new APIs, or blockers so other parallel agents stay informed:
```bash
curl -s -X POST http://127.0.0.1:8052/bulletin \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "{{AGENT_ID}}", "type": "finding", "content": "<what you created or discovered>"}'
```

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <N tests added, coverage areas>"}'
```
