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
1. Read all listed files; run `git diff` to see recent changes in context
2. Check for: correctness, type safety, missing error handling at system boundaries, test coverage
3. Classify each finding: `blocking` (must fix before merge) or `non-blocking` (suggestion)
4. Provide concrete fix suggestions with code, not just descriptions of the problem
5. Run the test suite to verify current state: `uv run python scripts/run_tests.py -x`
6. Write findings as a structured report; post blockers to BULLETIN if critical

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
  -d '{"result_summary": "{{TASK_TITLE}}: <N blocking, M non-blocking findings>"}'
```
