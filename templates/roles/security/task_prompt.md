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
1. Read all listed files before auditing — understand data flow from entry point to storage
2. Check in priority order: injection (SQL, command, path traversal), auth bypass, data exposure, secrets in code
3. Classify each finding by severity: critical / high / medium / low / informational
4. Provide concrete fixes with code — not just "validate input", but exactly how
5. If a critical finding is found, post immediately to BULLETIN before fixing
6. Verify fixes do not break existing tests: `uv run python scripts/run_tests.py -x`
7. Never introduce new secrets into source code

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
  -d '{"result_summary": "{{TASK_TITLE}}: <severity summary, e.g. 0 critical, 1 high fixed>"}'
```
