"""Lazy import utilities for optimizing CLI startup time."""

from __future__ import annotations

import importlib
import time
from typing import Any


class LazyImport:
    """Lazy import wrapper that defers module loading until first access.

    Usage:
        foo = LazyImport("foo")
        # Module not loaded yet
        result = foo.bar()  # Module loaded here on first access
    """

    def __init__(self, module_name: str, attr: str | None = None) -> None:
        self._module_name = module_name
        self._attr = attr
        self._module: Any = None

    def _load(self) -> Any:
        """Load the module if not already loaded."""
        if self._module is None:
            start = time.perf_counter()
            self._module = importlib.import_module(self._module_name)
            elapsed = (time.perf_counter() - start) * 1000
            if elapsed > 10:  # Log slow imports
                from bernstein.cli.helpers import console

                console.print(f"[dim]Loaded {self._module_name} in {elapsed:.1f}ms[/dim]")

        if self._attr:
            return getattr(self._module, self._attr)
        return self._module

    def __getattr__(self, name: str) -> Any:
        module = self._load()
        return getattr(module, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        func = self._load()
        return func(*args, **kwargs)


def lazy_import(name: str) -> LazyImport:
    """Create a lazy import for a module.

    Args:
        name: Module name to import lazily.

    Returns:
        LazyImport instance.
    """
    return LazyImport(name)


def lazy_import_attr(module: str, attr: str) -> LazyImport:
    """Create a lazy import for a specific attribute from a module.

    Args:
        module: Module name.
        attr: Attribute name.

    Returns:
        LazyImport instance.
    """
    return LazyImport(module, attr)


class StartupTimer:
    """Context manager for measuring CLI startup time."""

    def __init__(self, command: str, threshold_ms: float = 1000.0) -> None:
        self._command = command
        self._threshold_ms = threshold_ms
        self._start: float = 0.0

    def __enter__(self) -> StartupTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        if elapsed_ms > self._threshold_ms:
            from bernstein.cli.helpers import console

            console.print(
                f"[yellow]Warning:[/yellow] {self._command} took {elapsed_ms:.0f}ms "
                f"(target: <{self._threshold_ms:.0f}ms)"
            )
        elif elapsed_ms > 100:
            from bernstein.cli.helpers import console

            console.print(f"[dim]{self._command} started in {elapsed_ms:.0f}ms[/dim]")


def measure_startup(command: str, threshold_ms: float = 1000.0) -> StartupTimer:
    """Create a startup timer for a command.

    Args:
        command: Command name for logging.
        threshold_ms: Warning threshold in milliseconds.

    Returns:
        StartupTimer context manager.
    """
    return StartupTimer(command, threshold_ms)
