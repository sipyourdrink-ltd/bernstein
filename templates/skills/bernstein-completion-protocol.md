---
name: bernstein-completion-protocol
description: Report task completion to the Bernstein orchestrator
whenToUse: When you have finished all assigned tasks and are ready to report completion
---

Mark each task complete by posting to the Bernstein task server:

{{COMPLETE_CMDS}}

Then commit your changes and exit:

```bash
git add -A && git commit -m "feat: <brief summary of what you did>"
exit 0
```

If the task server is unreachable, retry up to 3 times with a 2-second delay before giving up.
