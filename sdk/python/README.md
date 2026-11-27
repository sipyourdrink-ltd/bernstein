# bernstein-sdk

Python integration SDK for [Bernstein](https://github.com/sipyourdrink-ltd/bernstein), an open-source governance layer for AI agents.

Connect CI/CD pipelines, issue trackers (Jira, Linear), and chat tools (Slack, Teams) to Bernstein's task server.

## Install

```bash
pip install bernstein-sdk
# With Jira support
pip install bernstein-sdk[jira]
```

## Quick start

```python
from bernstein_sdk import BernsteinClient

with BernsteinClient("http://127.0.0.1:8052") as client:
    task = client.create_task(
        title="Fix login regression",
        role="backend",
        priority=1,
    )
    print(task.id, task.status)
```

## Adapters

- **Jira** - convert issues to tasks, sync status back via transitions
- **Linear** - GraphQL-backed issue sync
- **Slack** - Block Kit notifications on task events
- **Teams** - Adaptive Card notifications
- **GitHub Actions** - create fix tasks from CI failures

## State mapping

```python
from bernstein_sdk.state_map import JiraToBernstein, BernsteinToJira
from bernstein_sdk.models import TaskStatus

status = JiraToBernstein.map("In Progress")  # → TaskStatus.IN_PROGRESS
label = BernsteinToJira.map(TaskStatus.DONE)  # → "Done"
```

## Status summary

```python
status = client.get_status()
print(status.total, status.open, status.done, status.agents, status.cost_usd)
```

`get_status()` reads the aggregate totals from the nested `summary` object when the server includes one, so dashboard responses keep working as the payload shape evolves. Responses from older servers that carry the totals as flat top-level fields are still decoded directly. An explicit zero in the nested summary takes precedence over a non-zero flat value, so a total the dashboard reports as zero is never silently replaced by a stale top-level figure.

## Async client

```python
from bernstein_sdk.client import AsyncBernsteinClient

async with AsyncBernsteinClient("http://127.0.0.1:8052") as client:
    task = await client.create_task(title="Add rate limiting")
    await client.complete_task(task.id, result_summary="Implemented token bucket")
```

## License

MIT
