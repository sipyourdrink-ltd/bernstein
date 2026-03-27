# bernstein-sdk

Python integration SDK for the [Bernstein](https://github.com/sasha-labs/bernstein) multi-agent orchestration system.

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

- **Jira** — convert issues to tasks, sync status back via transitions
- **Linear** — GraphQL-backed issue sync
- **Slack** — Block Kit notifications on task events
- **Teams** — Adaptive Card notifications
- **GitHub Actions** — create fix tasks from CI failures

## State mapping

```python
from bernstein_sdk.state_map import JiraToBernstein, BernsteinToJira
from bernstein_sdk.models import TaskStatus

status = JiraToBernstein.map("In Progress")   # → TaskStatus.IN_PROGRESS
label  = BernsteinToJira.map(TaskStatus.DONE) # → "Done"
```

## Async client

```python
from bernstein_sdk.client import AsyncBernsteinClient

async with AsyncBernsteinClient("http://127.0.0.1:8052") as client:
    task = await client.create_task(title="Add rate limiting")
    await client.complete_task(task.id, result_summary="Implemented token bucket")
```

## License

MIT
