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
1. Read the codebase structure and current feature set
2. Review recent metrics and evolution history if available
3. Think from the user's perspective — what friction exists? What's missing?
4. Generate 3-5 bold feature proposals as structured JSON
5. Write proposals to the output path specified in your task

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <N proposals generated>"}'
```
