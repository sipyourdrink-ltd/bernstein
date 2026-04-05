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
1. Read the listed files and map current module boundaries before proposing changes
2. Write an ADR if this decision affects module structure, interfaces, or technology choices
3. Never refactor structure and behavior in the same change
4. Document trade-offs explicitly: what you gain, what you give up
5. If you create or move files, update all import paths and references
6. Run tests after structural changes: `uv run python scripts/run_tests.py -x`
7. Mark complete only when the design is implemented, tests pass, and any ADR is committed

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
  -d '{"result_summary": "{{TASK_TITLE}}: <what was designed/restructured>"}'
```
