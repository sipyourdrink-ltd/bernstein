# Bernstein Plugin SDK

Bernstein exposes a pluggy-based hook system that lets you extend the
orchestrator without modifying core code.  Plugins are plain Python classes
— no base class required, no registration boilerplate.

## Contents

- [How it works](#how-it-works)
- [Writing a plugin](#writing-a-plugin)
- [Available hooks](#available-hooks)
- [Installing a plugin](#installing-a-plugin)
- [Error isolation](#error-isolation)
- [Example plugins](#example-plugins)
  - [Logging notifier](#logging-notifier)
  - [Slack notifier](#slack-notifier)
  - [Metrics collector](#metrics-collector)
  - [Custom quality gate](#custom-quality-gate)
- [Testing plugins](#testing-plugins)
- [Packaging a plugin for distribution](#packaging-a-plugin-for-distribution)

---

## How it works

Bernstein uses [pluggy](https://pluggy.readthedocs.io/) — the same hook
machinery used by pytest.  The orchestrator fires named hooks at key points in
the task and agent lifecycle.  Any installed plugin that implements a hook
receives the call automatically.

```
orchestrator fires hook
    └─▶ PluginManager._safe_call("on_task_created", ...)
            └─▶ pluggy calls every registered plugin that has on_task_created
                    ├─▶ LoggingPlugin.on_task_created(...)
                    ├─▶ SlackNotifier.on_task_created(...)
                    └─▶ MetricsPlugin.on_task_created(...)
```

All hook calls are **fire-and-forget**: exceptions raised by a plugin are
caught, logged, and discarded.  A misbehaving plugin cannot crash the
orchestrator.

---

## Writing a plugin

A plugin is a Python class whose methods are decorated with `@hookimpl`.

```python
from bernstein.plugins import hookimpl

class MyPlugin:
    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        print(f"Task {task_id} done: {result_summary}")
```

Rules:

- **Decorate with `@hookimpl`** — unmarked methods are ignored.
- **Use keyword arguments** — hooks are always called with `**kwargs`, so you
  may safely omit parameters you don't need.
- **Return `None`** — return values from hook implementations are ignored.
- **Don't block** — hooks run synchronously in the orchestrator's main loop.
  Offload slow I/O (HTTP, DB writes) to a background thread or queue.

---

## Available hooks

All hooks are defined in `src/bernstein/plugins/hookspecs.py`.

| Hook | When fired | Parameters |
|------|-----------|------------|
| `on_task_created` | Immediately after a task is added to the task server | `task_id`, `role`, `title` |
| `on_task_completed` | When a task transitions to `done` | `task_id`, `role`, `result_summary` |
| `on_task_failed` | When a task transitions to `failed` | `task_id`, `role`, `error` |
| `on_agent_spawned` | Right after a new agent session is started | `session_id`, `role`, `model` |
| `on_agent_reaped` | When an agent session is collected by the janitor | `session_id`, `role`, `outcome` |
| `on_evolve_proposal` | When an evolution proposal receives a verdict | `proposal_id`, `title`, `verdict` |

### Parameter reference

**`on_task_created`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | `str` | Unique task identifier (e.g. `"a468891b59b5"`) |
| `role` | `str` | Agent role assigned (e.g. `"backend"`, `"qa"`) |
| `title` | `str` | Human-readable task title |

**`on_task_completed`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | `str` | Unique task identifier |
| `role` | `str` | Role that completed the task |
| `result_summary` | `str` | Short description of what was accomplished |

**`on_task_failed`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | `str` | Unique task identifier |
| `role` | `str` | Role that was working the task |
| `error` | `str` | Error message or failure reason |

**`on_agent_spawned`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Unique agent session identifier |
| `role` | `str` | Agent role |
| `model` | `str` | Model identifier (e.g. `"claude-sonnet-4"`) |

**`on_agent_reaped`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `str` | Unique agent session identifier |
| `role` | `str` | Agent role |
| `outcome` | `str` | Outcome string: `"completed"`, `"timed_out"`, `"failed"` |

**`on_evolve_proposal`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `proposal_id` | `str` | Unique proposal identifier |
| `title` | `str` | Proposal title |
| `verdict` | `str` | Final verdict: `"accepted"`, `"rejected"`, `"deferred"` |

---

## Installing a plugin

There are two ways to load a plugin.

### Option A: `bernstein.yaml` (per-project)

Add a `plugins:` list to your `bernstein.yaml`.  Each entry is a dotted import
path, optionally with a colon separating the module from the class name.

```yaml
# bernstein.yaml
plugins:
  - my_package.hooks:SlackNotifier
  - my_package.hooks:MetricsPlugin
```

Bernstein imports the module, instantiates the class, and registers it at
startup.  The package must be importable in the Python environment where
`bernstein run` is executed.

### Option B: entry points (distributable plugins)

Register a `bernstein.plugins` entry point in `pyproject.toml`.  This makes
the plugin auto-load whenever it is installed alongside Bernstein — no
`bernstein.yaml` change required.

```toml
[project.entry-points."bernstein.plugins"]
slack = "my_package.hooks:SlackNotifier"
metrics = "my_package.hooks:MetricsPlugin"
```

Entry points that point to a **class** are instantiated automatically.  Entry
points that point to a **module** are registered as-is.

---

## Error isolation

Every hook call is wrapped in a `try/except` inside `PluginManager._safe_call`.
If your plugin raises an exception it will be:

1. Logged at `WARNING` level.
2. Silently discarded — the orchestrator continues normally.

This means plugin authors can be liberal with exceptions; they won't take down
the system.  However, it also means silent failures are possible, so log
liberally inside your plugins.

---

## Example plugins

### Logging notifier

`examples/plugins/logging_plugin.py` — ships with Bernstein.

```python
from bernstein.plugins import hookimpl

class LoggingPlugin:
    """Prints all lifecycle events to stdout."""

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        print(f"[plugin] Task {task_id} ({role}) created: {title}")

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        print(f"[plugin] Task {task_id} ({role}) completed: {result_summary}")

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        print(f"[plugin] Task {task_id} ({role}) FAILED: {error}")

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        print(f"[plugin] Agent spawned: session={session_id} role={role} model={model}")

    @hookimpl
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        print(f"[plugin] Agent reaped: session={session_id} role={role} outcome={outcome}")

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        print(f"[plugin] Evolve proposal {proposal_id} ({title!r}): {verdict}")
```

Enable it:

```yaml
# bernstein.yaml
plugins:
  - examples.plugins.logging_plugin:LoggingPlugin
```

---

### Slack notifier

`examples/plugins/slack_notifier.py` — sends failure and completion alerts to
a Slack channel via an Incoming Webhook.

```python
from bernstein.plugins import hookimpl

class SlackNotifier:
    """Posts task failure and completion alerts to Slack."""
    ...
```

See `examples/plugins/slack_notifier.py` for the full source.

Enable it:

```yaml
# bernstein.yaml
plugins:
  - examples.plugins.slack_notifier:SlackNotifier

# Pass the webhook URL via environment variable:
#   export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

---

### Metrics collector

`examples/plugins/metrics_plugin.py` — writes per-task and per-agent metrics to
a JSON Lines file at `.sdd/metrics/plugin_events.jsonl`.

Enable it:

```yaml
# bernstein.yaml
plugins:
  - examples.plugins.metrics_plugin:MetricsPlugin
```

---

### Custom quality gate

`examples/plugins/quality_gate_plugin.py` — runs an additional quality check
after every task completes and writes results to `.sdd/metrics/custom_gates.jsonl`.

Enable it:

```yaml
# bernstein.yaml
plugins:
  - examples.plugins.quality_gate_plugin:SecurityScanGate
```

---

## Testing plugins

Because plugins are plain Python classes you can unit-test them without
starting the orchestrator.

```python
from bernstein.plugins.manager import PluginManager
from my_package.hooks import SlackNotifier

def test_slack_notifier_on_failure(monkeypatch, capsys):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json)

    monkeypatch.setattr("requests.post", fake_post)

    pm = PluginManager()
    pm.register(SlackNotifier(webhook_url="https://example.com/hook"), name="slack")
    pm.fire_task_failed(task_id="abc123", role="backend", error="timeout")

    assert len(calls) == 1
    assert "abc123" in calls[0]["text"]
```

You can also use `PluginManager` directly in scripts to drive the full hook
machinery without the rest of the orchestrator:

```python
from bernstein.plugins.manager import PluginManager

pm = PluginManager()
pm.register(MyPlugin(), name="my-plugin")
pm.fire_task_created(task_id="t1", role="qa", title="Run integration tests")
```

---

## Packaging a plugin for distribution

To share a plugin as a pip-installable package:

```
my_bernstein_plugin/
├── pyproject.toml
└── src/
    └── my_bernstein_plugin/
        ├── __init__.py
        └── hooks.py
```

`pyproject.toml`:

```toml
[project]
name = "bernstein-plugin-example"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["bernstein>=0.1"]

[project.entry-points."bernstein.plugins"]
example = "my_bernstein_plugin.hooks:MyPlugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Once installed (`pip install bernstein-plugin-example`), the plugin loads
automatically — no `bernstein.yaml` change required.

---

## Introspection

You can inspect which plugins are loaded and what hooks they implement:

```python
from bernstein.plugins.manager import get_plugin_manager

pm = get_plugin_manager()
print("Loaded plugins:", pm.registered_names)
for name in pm.registered_names:
    print(f"  {name}: {pm.plugin_hooks(name)}")
```

Example output:

```
Loaded plugins: ['slack', 'metrics']
  slack: ['on_task_failed']
  metrics: ['on_agent_reaped', 'on_agent_spawned', 'on_task_completed', 'on_task_created', 'on_task_failed']
```
