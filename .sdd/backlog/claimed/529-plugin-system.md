# 529 — Plugin system for custom evolution strategies and agent roles

**Role:** architect
**Priority:** 3 (medium)
**Scope:** medium

## Problem

Everything is hardcoded in the Bernstein package. Users can't add custom agent
roles, evolution strategies, or task processors without forking. For community
adoption, extensibility is essential.

## Design

### Plugin types
1. **Agent roles**: custom system prompts + task templates (drop into templates/roles/)
2. **Evolution strategies**: custom proposal generators (implement EvolutionStrategy ABC)
3. **Adapters**: custom CLI agent adapters (implement BaseAdapter ABC)
4. **Hooks**: pre/post task execution callbacks
5. **Reporters**: custom metric exporters (Datadog, New Relic, etc.)

### Discovery
- Python entry points: `[project.entry-points."bernstein.plugins"]`
- Local plugins: `bernstein.yaml` -> `plugins: [./my_plugin.py]`
- Package plugins: `pip install bernstein-plugin-datadog`

### Plugin API
```python
from bernstein.plugins import hookimpl

class MyPlugin:
    @hookimpl
    def on_task_complete(self, task: Task, result: TaskResult) -> None:
        """Called after every task completion."""
        send_to_datadog(task.metrics)

    @hookimpl
    def custom_evolution_strategy(self, metrics: Metrics) -> list[Proposal]:
        """Generate evolution proposals using custom logic."""
        ...
```

### Implementation
- Use `pluggy` (same as pytest uses) for hook-based plugin system
- Minimal overhead: plugins only loaded if configured
- Plugin template: `bernstein plugin create my-plugin`

## Files to modify
- `pyproject.toml` — pluggy dependency, entry points
- New: `src/bernstein/plugins/` — plugin API, hookspec, manager
- `src/bernstein/core/orchestrator.py` — hook invocations
- `src/bernstein/evolution/loop.py` — strategy hook

## Completion signal
- Third-party plugin can be installed and discovered
- `bernstein plugins list` shows active plugins
- Example plugin in `examples/plugins/`
