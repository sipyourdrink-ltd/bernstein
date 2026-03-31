"""Quality-gate plugin discovery and registration."""

from __future__ import annotations

import importlib.util
import inspect
import logging
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.gate_runner import GateResult

logger = logging.getLogger(__name__)


class GatePlugin(ABC):
    """Base class for user-defined quality gates."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique gate name."""

    @property
    def required(self) -> bool:
        """Whether this gate blocks merge on failure."""
        return True

    @property
    def condition(self) -> str:
        """The default execution condition for the gate."""
        return "always"

    @abstractmethod
    def run(
        self,
        changed_files: list[str],
        run_dir: Path,
        task_title: str,
        task_description: str,
    ) -> GateResult:
        """Execute the gate and return a result."""


class GatePluginRegistry:
    """Discover and manage quality-gate plugins."""

    def __init__(self, workdir: Path, *, built_in_names: set[str] | frozenset[str] | None = None) -> None:
        self._workdir = workdir
        self._built_in_names = set(built_in_names or set())
        self._plugins: dict[str, GatePlugin] = {}
        self._discovered = False

    def discover(self) -> None:
        """Load gate plugins from the workdir and Python entry points."""
        if self._discovered:
            return
        self._load_file_plugins(self._workdir / ".bernstein" / "gates")
        self._load_entrypoint_plugins()
        self._discovered = True

    def get(self, name: str) -> GatePlugin | None:
        """Return a discovered plugin by name."""
        self.discover()
        return self._plugins.get(name)

    def all_plugins(self) -> list[GatePlugin]:
        """Return all discovered plugins."""
        self.discover()
        return [self._plugins[name] for name in sorted(self._plugins)]

    def register(self, plugin: GatePlugin) -> None:
        """Register a plugin instance after validating its name."""
        name = plugin.name.strip()
        if not name:
            raise ValueError("Gate plugin name cannot be empty")
        if name in self._built_in_names:
            raise ValueError(f"Gate plugin name {name!r} collides with a built-in gate")
        if name in self._plugins:
            raise ValueError(f"Duplicate gate plugin name: {name!r}")
        self._plugins[name] = plugin

    def _load_file_plugins(self, gates_dir: Path) -> None:
        """Load plugins from ``.bernstein/gates/*.py``."""
        if not gates_dir.is_dir():
            return
        for py_file in sorted(gates_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            module_name = f"bernstein_gate_{py_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                logger.warning("Skipping gate plugin %s: unable to create import spec", py_file)
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # pragma: no cover - exercised through tests
                logger.warning("Failed to load gate plugin %s: %s", py_file, exc)
                continue
            self._register_module_plugins(module, source=str(py_file))

    def _load_entrypoint_plugins(self) -> None:
        """Load plugins from the ``bernstein.gates`` entry-point group."""
        for entry_point in entry_points(group="bernstein.gates"):
            try:
                loaded = entry_point.load()
                if inspect.isclass(loaded) and issubclass(loaded, GatePlugin):
                    self.register(loaded())
                elif isinstance(loaded, GatePlugin):
                    self.register(loaded)
                else:
                    logger.warning(
                        "Ignoring entry-point gate plugin %s: unsupported object %r",
                        entry_point.name,
                        loaded,
                    )
            except Exception as exc:  # pragma: no cover - exercised through tests
                logger.warning("Failed to load entry-point gate plugin %s: %s", entry_point.name, exc)

    def _register_module_plugins(self, module: object, *, source: str) -> None:
        """Register all plugin definitions exported by a loaded module."""
        for obj in vars(module).values():
            plugin: GatePlugin | None = None
            if inspect.isclass(obj) and issubclass(obj, GatePlugin) and obj is not GatePlugin:
                plugin = obj()
            elif isinstance(obj, GatePlugin):
                plugin = obj
            if plugin is None:
                continue
            try:
                self.register(plugin)
            except ValueError as exc:
                logger.warning("Skipping gate plugin from %s: %s", source, exc)
