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
1. Read existing model code and data pipelines before making changes
2. Log key metrics (loss, accuracy, latency) — don't rely on eyeballing outputs
3. Keep experiments reproducible: fix random seeds, pin library versions, document hyperparameters
4. Write unit tests for data transforms and model utilities; integration tests for pipelines
5. Profile before optimizing — identify the actual bottleneck
6. Run tests before marking complete: `uv run python scripts/run_tests.py -x`
7. Only modify files listed in your task's `owned_files`

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
  -d '{"result_summary": "{{TASK_TITLE}}: <what was built/improved and key metrics>"}'
```
