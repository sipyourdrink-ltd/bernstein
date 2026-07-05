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
1. Read the existing prompt templates and renderer before editing. Understand variable substitution rules
2. Use `{{VARIABLE}}` for required substitutions; wrap optional sections in `{{#IF VAR}}...{{/IF}}`
3. Write prompts for the actual model, not an idealized one. Be concrete and directive
4. Include examples in prompts when the desired output format is non-obvious
5. Test templates by rendering them with realistic context values
6. Document any new template variables in a comment at the top of the file
7. Only modify files listed in your task's `owned_files`

{{INCLUDE completion_contract}}
