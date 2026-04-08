# ADR-007: Pluggy for the Plugin System

**Status**: Accepted  
**Date**: 2026-03-22  
**Context**: Bernstein multi-agent orchestration system

---

## Problem

Bernstein needs an extension point that lets users add custom behavior at key
orchestration events — task created, agent spawned, task completed, cost
threshold exceeded — without modifying core code.

The questions are:
1. Should we build our own plugin system or use an existing one?
2. How should plugins be registered and discovered?
3. How do we ensure a misbehaving plugin can't crash the orchestrator?

---

## Decision

**Use [pluggy](https://pluggy.readthedocs.io/) as the plugin infrastructure.**

Pluggy is the hook system used by pytest and tox. It provides named hooks,
ordered hook calling, error isolation, and pip-installable plugin discovery — all
the machinery we need, battle-tested by one of Python's most-used testing tools.

---

## Options evaluated

### Option A: Custom callback registry

```python
class EventEmitter:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, handler: Callable) -> None:
        self._handlers[event].append(handler)

    def emit(self, event: str, **kwargs) -> None:
        for handler in self._handlers[event]:
            try:
                handler(**kwargs)
            except Exception:
                logger.exception(f"Handler error for {event}")
```

**Pros**: Simple, no dependencies, immediately understandable.

**Cons**:
- No discovery mechanism — plugins must be registered manually in user code.
- No hook specification — no way to define the canonical signature for an event.
  Users guess at keyword arguments.
- No ordering control — all handlers are equal, no way to say "run before
  the default handler" or "run last."
- No pytest integration — custom event names can't be type-checked or
  auto-completed.

**Verdict**: Good for internal use; insufficient for a public plugin API.

### Option B: Python entry points only (no library)

```python
import importlib.metadata

def load_plugins() -> list[Any]:
    plugins = []
    for ep in importlib.metadata.entry_points(group='bernstein.plugins'):
        plugin_class = ep.load()
        plugins.append(plugin_class())
    return plugins
```

**Pros**: Pure stdlib, no additional dependency. Familiar to experienced Python
packaging authors.

**Cons**:
- Entry points alone don't define hook signatures — still need a way to
  specify what methods plugins can implement.
- Manual discovery only — no way for users to register plugins without packaging
  them.
- No error isolation built in.

**Verdict**: This is the discovery layer, not the complete solution. Pluggy uses
entry points internally for pip-based discovery; we'd be reinventing the rest.

### Option C: Pluggy (chosen)

Pluggy provides three things:
1. **Hook specifications** (`@hookspec`) — define the canonical signature and
   documentation for each hook.
2. **Hook implementations** (`@hookimpl`) — plugins annotate their methods,
   pluggy calls them with the right arguments.
3. **Discovery** — register plugins manually (`register(MyPlugin())`) or via
   pip entry points (plugins installed via `pip install` are auto-discovered).

Error isolation is trivially added with a `_safe_call` wrapper:
```python
def _safe_call(self, hook_name: str, **kwargs) -> None:
    try:
        getattr(self._pm.hook, hook_name)(**kwargs)
    except Exception:
        logger.exception(f"Plugin hook {hook_name!r} failed")
```

**Why pluggy over a custom system:**

- **Proven at scale.** pytest has hundreds of plugins using pluggy. The hook
  semantics are well-understood by a large population of Python developers.
  Saying "Bernstein plugins work like pytest plugins" is a meaningful reference.
- **Hook specification as documentation.** `@hookspec` with docstrings is the
  canonical plugin API contract. Users read the hookspecs to understand what they
  can hook into; they don't have to read the orchestrator source.
- **Call ordering.** Pluggy supports `firstresult=True` (first non-None return
  wins) and `tryfirst`/`trylast` markers for ordering. Useful for override plugins
  that need to run before the default behavior.
- **Entry point discovery.** A plugin distributed via pip just needs:
  ```toml
  [project.entry-points."bernstein"]
  my_plugin = "my_package:MyPlugin"
  ```
  Bernstein discovers and loads it automatically on startup.

**Cons:**
- One additional dependency (`pluggy>=1.5`). It's small (< 100 lines of actual
  logic) and has no transitive dependencies.

---

## Hooks defined

```python
# src/bernstein/plugins/hookspecs.py

class BernsteinSpec:
    @hookspec
    def on_task_created(self, task: Task) -> None:
        """Called when a new task enters the open queue."""

    @hookspec
    def on_task_claimed(self, task: Task, agent_id: str) -> None:
        """Called when an agent claims a task."""

    @hookspec
    def on_task_completed(self, task: Task, result: TaskResult) -> None:
        """Called when a task passes all quality gates and is marked done."""

    @hookspec
    def on_task_failed(self, task: Task, error: str) -> None:
        """Called when a task fails (quality gate failure or agent error)."""

    @hookspec
    def on_agent_spawned(self, agent_id: str, role: str, adapter: str) -> None:
        """Called when a CLI agent process is launched."""

    @hookspec
    def on_agent_died(self, agent_id: str, reason: str) -> None:
        """Called when an agent process exits (clean or crash)."""

    @hookspec
    def on_cost_threshold(self, total_usd: float, threshold_usd: float) -> None:
        """Called when cumulative cost exceeds a configured threshold."""

    @hookspec
    def on_quality_gate_failure(self, task: Task, gate: str, output: str) -> None:
        """Called when a quality gate (lint, tests, type-check) fails."""
```

---

## Consequences

### Benefits

**Plugin authors get a familiar API.** Anyone who has written a pytest plugin
understands the `@hookimpl` pattern. The barrier to writing a Bernstein plugin is
low.

**Error isolation is built in.** A plugin that raises an exception does not crash
the orchestrator. The `_safe_call` wrapper catches, logs, and discards plugin
errors. A misbehaving Slack notifier cannot take down an active run.

**pip-installable plugins.** `pip install bernstein-slack-notifier` is sufficient.
No manual registration. No import in user config. The plugin announces itself via
entry points.

**Hook specifications are the API contract.** The `hookspecs.py` file is the
definitive documentation of what events Bernstein exposes. When we add a new hook,
we add it here first — the spec is the contract.

### Costs

**One additional dependency.** `pluggy>=1.5` is required. It is small and stable.

**Hook signature changes are breaking.** Adding a required keyword argument to a
hookspec is a breaking change for all existing plugin implementations. We mitigate
this by always adding new arguments as optional kwargs with sensible defaults.

---

## References

- Implementation: `src/bernstein/plugins/`
- Hook specs: `src/bernstein/plugins/hookspecs.py`
- Plugin manager: `src/bernstein/plugins/manager.py`
- Plugin SDK docs: [plugin-sdk.md](../plugin-sdk.md)
- pluggy docs: https://pluggy.readthedocs.io/
