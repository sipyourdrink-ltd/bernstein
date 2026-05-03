# Bernstein Plugin for Cursor

Orchestrate parallel AI coding agents from Cursor. Monitor tasks, agents, costs, approve plans — all from the chat.

## Skills

| Skill | Trigger | What it does |
|-------|---------|--------------|
| `/bernstein-status` | "what's the status?" | Dashboard overview — agents, tasks, costs, alerts |
| `/bernstein-create-task` | "create a task for..." | Queue work for an agent |
| `/bernstein-agents` | "what agents are running?" | List, inspect, kill agents |
| `/bernstein-approve` | "any pending approvals?" | Review and approve/reject tasks and plans |
| `/bernstein-cost` | "how much have we spent?" | Cost breakdown by model and agent |
| `/bernstein-quality` | "which model is best?" | Success rates, pass rates, completion times |
| `/bernstein-alerts` | "any problems?" | Failed tasks, stalled agents, budget warnings |
| `/bernstein-plan` | "plan this out" | Create multi-step execution plans |

## Install

### Local (development)

```bash
ln -s /path/to/bernstein/packages/cursor-plugin ~/.cursor/plugins/local/bernstein
```

Restart Cursor or run `Developer: Reload Window`.

### From GitHub

In Cursor Settings → Rules → Add Rule → Remote Rule (GitHub) → paste:
```
https://github.com/sipyourdrink-ltd/bernstein
```

## Configuration

Set environment variables (optional):

```bash
export BERNSTEIN_API_URL="http://127.0.0.1:8052"  # default
export BERNSTEIN_API_TOKEN="your-token"             # if auth enabled
```

## Requirements

- [Bernstein](https://github.com/sipyourdrink-ltd/bernstein) running locally (`bernstein run`)
- Cursor 2.4+
- `curl`, `python3` (for script execution)

## License

Apache-2.0
