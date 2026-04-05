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
1. Read each proposal carefully
2. Evaluate against feasibility, ROI, risk, user demand, and dependencies
3. Score each proposal and produce a clear verdict
4. For APPROVED proposals, decompose into concrete implementation tasks
5. Write evaluations to the output path specified in your task

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <N proposals evaluated, M approved>"}'
```
