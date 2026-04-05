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
1. Read the existing prompt templates and renderer before editing — understand variable substitution rules
2. Use `{{VARIABLE}}` for required substitutions; wrap optional sections in `{{#IF VAR}}...{{/IF}}`
3. Write prompts for the actual model, not an idealized one — be concrete and directive
4. Include examples in prompts when the desired output format is non-obvious
5. Test templates by rendering them with realistic context values
6. Document any new template variables in a comment at the top of the file
7. Only modify files listed in your task's `owned_files`

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <what prompts were created/improved>"}'
```
