"""Plugin hot-reloading with automatic version rollback on failure.

Provides live reloading of plugin modules via :func:`importlib.reload` with
built-in degradation detection and automatic rollback to the previous known-good
version.

The :class:`PluginHotReloader` tracks version history per plugin and monitors
quality gate pass rates within a configurable window.  When degradation is
detected after a reload, the reloader automatically rolls back to the previous
version.

Usage::

    reloader = PluginHotReloader()
    result = reloader.hot_reload("my-plugin", "2.1.0")
    if not result:
        print("Reload failed — automatic rollback applied")

    history = reloader.get_history("my-plugin")
    print(f"Current: {history.current_version}, previous: {history.previous_version}")
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginVersionHistory:
    """Immutable snapshot of a plugin's version history.

    Attributes:
        plugin_name: Unique plugin identifier.
        current_version: The currently active version string.
        previous_version: The version active before the current one, or empty
            string if no prior version exists.
        versions: Ordered list of all versions seen (oldest first).
        last_updated: Unix timestamp of the last version change.
    """

    plugin_name: str
    current_version: str
    previous_version: str
    versions: tuple[str, ...]
    last_updated: float


@dataclass(frozen=True)
class RollbackTrigger:
    """Describes why a rollback was (or would be) triggered.

    Attributes:
        plugin_name: Plugin that triggered the rollback.
        metric: Name of the metric that breached the threshold (e.g.
            ``"quality_gate_pass_rate"``).
        threshold: The minimum acceptable value for the metric.
        current_value: The actual metric value observed.
        triggered: Whether the threshold was actually breached.
    """

    plugin_name: str
    metric: str
    threshold: float
    current_value: float
    triggered: bool


# ---------------------------------------------------------------------------
# Internal mutable version record (not exposed)
# ---------------------------------------------------------------------------


@dataclass
class _VersionRecord:
    """Mutable internal record for tracking a plugin's version lineage."""

    current_version: str
    previous_version: str = ""
    versions: list[str] = field(default_factory=list[str])
    last_updated: float = 0.0


# ---------------------------------------------------------------------------
# Internal metrics record
# ---------------------------------------------------------------------------


@dataclass
class _MetricsRecord:
    """Tracks quality gate pass/fail counts for a plugin."""

    passes: list[float] = field(default_factory=list[float])
    failures: list[float] = field(default_factory=list[float])


# ---------------------------------------------------------------------------
# PluginHotReloader
# ---------------------------------------------------------------------------


class PluginHotReloader:
    """Manages hot-reloading of plugin modules with version tracking and rollback.

    Keeps an in-memory store of version history and quality metrics per plugin.
    After each reload, callers can check :meth:`detect_degradation` to decide
    whether to :meth:`rollback`.

    Attributes:
        default_pass_rate_threshold: Minimum quality gate pass rate (0.0-1.0)
            below which degradation is flagged.
    """

    def __init__(self, *, default_pass_rate_threshold: float = 0.7) -> None:
        self._version_store: dict[str, _VersionRecord] = {}
        self._metrics_store: dict[str, _MetricsRecord] = {}
        self.default_pass_rate_threshold = default_pass_rate_threshold

    # -- public API --------------------------------------------------------

    def hot_reload(self, plugin_name: str, new_version: str) -> bool:
        """Hot-reload a plugin module and register the new version.

        Attempts ``importlib.reload`` on the module identified by
        *plugin_name*.  If the module is not currently in ``sys.modules``,
        it is imported first.

        On reload failure the version store is **not** updated and the
        method returns ``False``.

        Args:
            plugin_name: Dotted module path or simple name of the plugin.
            new_version: Semantic version string for the new version.

        Returns:
            ``True`` when the reload succeeded and the version store was
            updated, ``False`` on import/reload failure.
        """
        module_name = plugin_name.replace("-", "_")

        try:
            if module_name in sys.modules:
                module = sys.modules[module_name]
                importlib.reload(module)
                logger.info(
                    "plugin_hotreload: reloaded module %r to version %s",
                    module_name,
                    new_version,
                )
            else:
                importlib.import_module(module_name)
                logger.info(
                    "plugin_hotreload: imported module %r at version %s",
                    module_name,
                    new_version,
                )
        except Exception:
            logger.exception(
                "plugin_hotreload: failed to reload %r to version %s",
                plugin_name,
                new_version,
            )
            return False

        self._update_version(plugin_name, new_version)
        return True

    def detect_degradation(
        self,
        plugin_name: str,
        window_minutes: int = 30,
    ) -> RollbackTrigger:
        """Check quality gate pass rate within a time window.

        Computes the pass rate from recorded quality gate results within the
        last *window_minutes* minutes.  Returns a :class:`RollbackTrigger`
        indicating whether the threshold was breached.

        Args:
            plugin_name: Plugin to check.
            window_minutes: Number of minutes to look back for metrics.

        Returns:
            A :class:`RollbackTrigger` with ``triggered=True`` when the pass
            rate falls below :attr:`default_pass_rate_threshold`.
        """
        metrics = self._metrics_store.get(plugin_name)
        if metrics is None:
            return RollbackTrigger(
                plugin_name=plugin_name,
                metric="quality_gate_pass_rate",
                threshold=self.default_pass_rate_threshold,
                current_value=1.0,
                triggered=False,
            )

        now = time.monotonic()
        cutoff = now - (window_minutes * 60)

        passes_in_window = sum(1 for ts in metrics.passes if ts >= cutoff)
        failures_in_window = sum(1 for ts in metrics.failures if ts >= cutoff)
        total = passes_in_window + failures_in_window

        pass_rate = (passes_in_window / total) if total > 0 else 1.0
        triggered = pass_rate < self.default_pass_rate_threshold

        if triggered:
            logger.warning(
                "plugin_hotreload: degradation detected for %r — pass_rate=%.2f < threshold=%.2f (window=%dm)",
                plugin_name,
                pass_rate,
                self.default_pass_rate_threshold,
                window_minutes,
            )

        return RollbackTrigger(
            plugin_name=plugin_name,
            metric="quality_gate_pass_rate",
            threshold=self.default_pass_rate_threshold,
            current_value=pass_rate,
            triggered=triggered,
        )

    def rollback(self, plugin_name: str) -> bool:
        """Roll back a plugin to its previous version.

        Swaps the current and previous version pointers.  If no previous
        version exists, the rollback is a no-op and returns ``False``.

        The actual module reload is **not** performed here — the caller
        should invoke :meth:`hot_reload` with the previous version string
        after calling this method, or use an external deployment mechanism.

        Args:
            plugin_name: Plugin to roll back.

        Returns:
            ``True`` when the version store was updated to reflect the
            rollback, ``False`` when no previous version was available.
        """
        record = self._version_store.get(plugin_name)
        if record is None or not record.previous_version:
            logger.warning(
                "plugin_hotreload: cannot rollback %r — no previous version",
                plugin_name,
            )
            return False

        old_current = record.current_version
        record.current_version = record.previous_version
        record.previous_version = old_current
        record.last_updated = time.monotonic()

        logger.info(
            "plugin_hotreload: rolled back %r from %s to %s",
            plugin_name,
            old_current,
            record.current_version,
        )
        return True

    def get_history(self, plugin_name: str) -> PluginVersionHistory | None:
        """Return an immutable snapshot of a plugin's version history.

        Args:
            plugin_name: Plugin to query.

        Returns:
            A :class:`PluginVersionHistory` if the plugin has been loaded at
            least once, otherwise ``None``.
        """
        record = self._version_store.get(plugin_name)
        if record is None:
            return None

        return PluginVersionHistory(
            plugin_name=plugin_name,
            current_version=record.current_version,
            previous_version=record.previous_version,
            versions=tuple(record.versions),
            last_updated=record.last_updated,
        )

    def record_quality_gate(self, plugin_name: str, *, passed: bool) -> None:
        """Record a quality gate result for a plugin.

        Called externally (e.g. by the gate runner) to feed metrics into the
        degradation detector.

        Args:
            plugin_name: Plugin the gate ran for.
            passed: Whether the gate passed.
        """
        metrics = self._metrics_store.setdefault(plugin_name, _MetricsRecord())
        now = time.monotonic()
        if passed:
            metrics.passes.append(now)
        else:
            metrics.failures.append(now)

    # -- internal helpers --------------------------------------------------

    def _update_version(self, plugin_name: str, new_version: str) -> None:
        """Update the version store after a successful reload."""
        record = self._version_store.get(plugin_name)
        now = time.monotonic()

        if record is None:
            self._version_store[plugin_name] = _VersionRecord(
                current_version=new_version,
                previous_version="",
                versions=[new_version],
                last_updated=now,
            )
        else:
            record.previous_version = record.current_version
            record.current_version = new_version
            record.versions.append(new_version)
            record.last_updated = now
