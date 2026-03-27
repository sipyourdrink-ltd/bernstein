"""Plugin manager — discovers, loads, and invokes Bernstein plugins."""

from __future__ import annotations

import importlib
import logging
import warnings
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pluggy

from bernstein.plugins.hookspecs import BernsteinSpec

log = logging.getLogger(__name__)

__all__ = ["PluginManager", "get_plugin_manager"]

# Module-level singleton so the same manager is reused within a process.
_manager: PluginManager | None = None


class PluginManager:
    """Discovers, loads, and invokes Bernstein plugins.

    Plugins are discovered from two sources:

    1. **Entry points** — any installed package that registers hooks under
       the ``bernstein.plugins`` entry-point group.
    2. **bernstein.yaml** ``plugins:`` field — a list of dotted import paths
       (``"my_package.my_module:MyPlugin"`` or just ``"my_module"``).

    All hook calls are fire-and-forget: exceptions raised by individual
    plugins are caught, logged, and discarded so a misbehaving plugin cannot
    crash the orchestrator.
    """

    def __init__(self) -> None:
        self._pm = pluggy.PluginManager("bernstein")
        self._pm.add_hookspecs(BernsteinSpec)
        self._registered_names: list[str] = []

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_entry_points(self) -> None:
        """Load all plugins registered via the ``bernstein.plugins`` entry-point group."""
        eps = entry_points(group="bernstein.plugins")
        for ep in eps:
            try:
                plugin = ep.load()
                # Entry points may point to a class or an instance; instantiate if needed.
                if isinstance(plugin, type):
                    plugin = plugin()
                name = ep.name
                self._pm.register(plugin, name=name)
                self._registered_names.append(name)
                log.debug("Loaded entry-point plugin %r from %s", name, ep.value)
            except Exception as exc:
                warnings.warn(
                    f"Failed to load bernstein plugin {ep.name!r} ({ep.value}): {exc}",
                    stacklevel=1,
                )

    def discover_config_plugins(self, config_plugins: list[str]) -> None:
        """Load plugins listed in ``bernstein.yaml`` under the ``plugins:`` key.

        Each entry should be a dotted import path, optionally with a colon
        separating the module from the attribute, e.g.
        ``"my_package.hooks:MyPlugin"``.

        Args:
            config_plugins: List of import-path strings from the config file.
        """
        for spec in config_plugins:
            try:
                if ":" in spec:
                    module_path, attr = spec.rsplit(":", 1)
                    mod = importlib.import_module(module_path)
                    obj = getattr(mod, attr)
                else:
                    mod = importlib.import_module(spec)
                    obj = mod

                plugin = obj() if isinstance(obj, type) else obj
                name = spec
                self._pm.register(plugin, name=name)
                self._registered_names.append(name)
                log.debug("Loaded config plugin %r", name)
            except Exception as exc:
                warnings.warn(
                    f"Failed to load bernstein config plugin {spec!r}: {exc}",
                    stacklevel=1,
                )

    def load_from_workdir(self, workdir: Path | None = None) -> None:
        """Convenience: discover entry points then load any config-listed plugins.

        Reads ``plugins:`` from ``bernstein.yaml`` in *workdir* (or the current
        directory if *workdir* is ``None``).

        Args:
            workdir: Project root directory.  Defaults to ``Path.cwd()``.
        """
        self.discover_entry_points()

        root = workdir or Path.cwd()
        config_path = root / "bernstein.yaml"
        if config_path.exists():
            try:
                import yaml  # type: ignore[import-untyped]

                # yaml.safe_load is untyped; work around via explicit annotation.
                loaded: object = yaml.safe_load(config_path.read_text())
                if not isinstance(loaded, dict):
                    return
                raw_plugins: object = loaded.get("plugins")  # type: ignore[union-attr]
                if isinstance(raw_plugins, list):
                    plugin_strs: list[str] = [str(item) for item in raw_plugins]  # type: ignore[var-annotated]
                    self.discover_config_plugins(plugin_strs)
            except Exception as exc:
                log.warning("Could not read plugins from bernstein.yaml: %s", exc)

    def register(self, plugin: object, name: str) -> None:
        """Register a plugin instance directly (useful in tests and scripts).

        Args:
            plugin: Any object with ``@hookimpl``-decorated methods.
            name: Unique name for this plugin instance.
        """
        self._pm.register(plugin, name=name)
        self._registered_names.append(name)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def registered_names(self) -> list[str]:
        """Names of all successfully registered plugins."""
        return list(self._registered_names)

    def plugin_hooks(self, plugin_name: str) -> list[str]:
        """Return names of hooks implemented by *plugin_name*.

        Args:
            plugin_name: Plugin name as returned by :attr:`registered_names`.

        Returns:
            Sorted list of hook names implemented by the plugin.
        """
        plugin = self._pm.get_plugin(plugin_name)
        if plugin is None:
            return []
        callers = self._pm.get_hookcallers(plugin)
        if callers is None:
            return []
        return sorted(hc.name for hc in callers)

    # ------------------------------------------------------------------
    # Fire methods
    # ------------------------------------------------------------------

    def _safe_call(self, hook_name: str, **kwargs: Any) -> None:
        """Invoke a hook, swallowing all exceptions from individual plugins.

        Args:
            hook_name: Name of the hook attribute on ``self._pm.hook``.
            **kwargs: Arguments forwarded to the hook.
        """
        try:
            hook = getattr(self._pm.hook, hook_name)
            hook(**kwargs)
        except Exception as exc:
            log.warning("Plugin hook %r raised an exception: %s", hook_name, exc)

    def fire_task_created(self, task_id: str, role: str, title: str) -> None:
        """Fire the ``on_task_created`` hook.

        Args:
            task_id: Unique task identifier.
            role: Agent role.
            title: Task title.
        """
        self._safe_call("on_task_created", task_id=task_id, role=role, title=title)

    def fire_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Fire the ``on_task_completed`` hook.

        Args:
            task_id: Unique task identifier.
            role: Agent role.
            result_summary: Short description of the outcome.
        """
        self._safe_call("on_task_completed", task_id=task_id, role=role, result_summary=result_summary)

    def fire_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Fire the ``on_task_failed`` hook.

        Args:
            task_id: Unique task identifier.
            role: Agent role.
            error: Error description.
        """
        self._safe_call("on_task_failed", task_id=task_id, role=role, error=error)

    def fire_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        """Fire the ``on_agent_spawned`` hook.

        Args:
            session_id: Unique session identifier.
            role: Agent role.
            model: Model identifier.
        """
        self._safe_call("on_agent_spawned", session_id=session_id, role=role, model=model)

    def fire_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        """Fire the ``on_agent_reaped`` hook.

        Args:
            session_id: Unique session identifier.
            role: Agent role.
            outcome: Outcome description.
        """
        self._safe_call("on_agent_reaped", session_id=session_id, role=role, outcome=outcome)

    def fire_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        """Fire the ``on_evolve_proposal`` hook.

        Args:
            proposal_id: Unique proposal identifier.
            title: Proposal title.
            verdict: Final verdict string.
        """
        self._safe_call("on_evolve_proposal", proposal_id=proposal_id, title=title, verdict=verdict)


def get_plugin_manager(workdir: Path | None = None, *, reload: bool = False) -> PluginManager:
    """Return the process-level singleton :class:`PluginManager`.

    The manager is initialised lazily on first call, then cached.  Pass
    ``reload=True`` to force re-discovery (useful in tests).

    Args:
        workdir: Project root passed to :meth:`PluginManager.load_from_workdir`.
        reload: If ``True``, discard the cached instance and rebuild.

    Returns:
        The (possibly freshly constructed) :class:`PluginManager`.
    """
    global _manager
    if _manager is None or reload:
        _manager = PluginManager()
        _manager.load_from_workdir(workdir)
    return _manager
