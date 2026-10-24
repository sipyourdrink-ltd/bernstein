## Complete the bare-task MCP worker loop

Tasks created with `bernstein_create_task` or `POST /api/v1/tasks` can now be
acquired by `bernstein_claim` and completed through the same TaskStore lifecycle.
Claims preserve dependency, role, tenant and task-scope boundaries, include a
verifiable snapshot, and create no duplicate backlog rows. HTTP task creation
records its fields in the install-local HMAC audit chain without requiring an
orchestrator audit listener.
