---
name: bernstein-completion-protocol
description: Report task completion to the Bernstein orchestrator
whenToUse: When you have finished all assigned tasks and are ready to report completion
---

Mark each task complete with the Bernstein CLI. It resolves the task-server port and your agent token itself, so there is no host, auth header or JSON body to hand-quote:

{{COMPLETE_CMDS}}

Then commit your changes and exit:

```bash
git add -A && git commit -m "feat: <brief summary of what you did>"
exit 0
```

The command retries by itself while the server is restarting, and exits non-zero with the reason if the server stays unreachable or rejects the token. Do not treat a task as done unless it succeeds.
