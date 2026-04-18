"""Config source checksum watcher for detecting drift during runs.

Monitors all configuration source files (bernstein.yaml, ~/.bernstein/config.yaml,
.bernstein/config.yaml, .sdd/config/*.json) and detects when any file changes
after the orchestrator run has started.  Designed to be called periodically from
the orchestrator tick loop.

The watcher is purely deterministic -- it computes SHA-256 checksums of config
files at snapshot time and compares them on each check.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_CONFIG_YAML_FILENAME = "config.yaml"

logger = logging.getLogger(__name__)

DriftSeverity = Literal["info", "warning", "error"]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileChecksum:
    """Checksum of a single config source file at a point in time.

    Attributes:
        path: Absolute path to the config file.
        label: Human-readable source label (e.g. "project", "user").
        checksum: SHA-256 hex digest, or empty string if the file was missing.
        exists: Whether the file existed at snapshot time.
    """

    path: str
    label: str
    checksum: str
    exists: bool


@dataclass(frozen=True, slots=True)
class DriftEvent:
    """A detected config drift event.

    Attributes:
        path: Absolute path to the changed config file.
        label: Source label.
        kind: What changed -- "modified", "created", or "deleted".
        old_checksum: Checksum at snapshot time.
        new_checksum: Checksum at check time.
        severity: Suggested severity level.
        detected_at: Unix timestamp when drift was detected.
    """

    path: str
    label: str
    kind: Literal["modified", "created", "deleted"]
    old_checksum: str
    new_checksum: str
    severity: DriftSeverity
    detected_at: float

    def summary(self) -> str:
        """Return a single-line human-readable summary."""
        return f"config drift [{self.severity}]: {self.label} ({self.path}) {self.kind}"


@dataclass
class DriftReport:
    """Aggregated result of a drift check.

    Attributes:
        drifted: Whether any config source has changed.
        events: Individual drift events per file.
        checked_at: Unix timestamp of the check.
        snapshot_at: Unix timestamp of the baseline snapshot.
    """

    drifted: bool
    events: list[DriftEvent] = field(default_factory=list[DriftEvent])
    checked_at: float = 0.0
    snapshot_at: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-safe dict."""
        return {
            "drifted": self.drifted,
            "events": [
                {
                    "path": e.path,
                    "label": e.label,
                    "kind": e.kind,
                    "old_checksum": e.old_checksum,
                    "new_checksum": e.new_checksum,
                    "severity": e.severity,
                    "detected_at": e.detected_at,
                    "summary": e.summary(),
                }
                for e in self.events
            ],
            "checked_at": self.checked_at,
            "snapshot_at": self.snapshot_at,
        }


# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """Return SHA-256 hex digest for *path*, or empty string when missing/unreadable."""
    if not path.exists():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _severity_for_label(label: str) -> DriftSeverity:
    """Choose drift severity based on the config source importance.

    project/user configs changing mid-run is a warning (operators may
    intentionally edit them).  Managed/CLI override files changing is an
    error because they should only be written by the system.
    """
    if label in ("managed", "cli_overrides"):
        return "error"
    return "warning"


# ---------------------------------------------------------------------------
# Watcher class
# ---------------------------------------------------------------------------


def discover_config_paths(workdir: Path) -> list[tuple[str, Path]]:
    """Return all config source file paths that should be watched.

    Each entry is (label, absolute_path).  The list always contains all
    candidate paths regardless of whether the file currently exists -- this
    allows the watcher to detect *creation* of new config files mid-run.

    The returned list has six entries, one per cascade layer actually consumed
    by :func:`bernstein.core.config.config_schema.load_and_validate`: user,
    project, project_alt, local, cli_overrides, managed.  The legacy
    ``sdd_project`` slot (``.sdd/config.yaml``) was dropped in audit-157 because
    no loader reads it -- the earlier ``settings_cascade`` reference was
    removed by audit-151.

    Args:
        workdir: Project root directory.

    Returns:
        List of (label, path) tuples covering every cascade layer.
    """
    paths: list[tuple[str, Path]] = [
        ("user", Path.home() / ".bernstein" / _CONFIG_YAML_FILENAME),
        ("project", workdir / "bernstein.yaml"),
        ("project_alt", workdir / "bernstein.yml"),
        ("local", workdir / ".bernstein" / _CONFIG_YAML_FILENAME),
        ("cli_overrides", workdir / ".sdd" / "config" / "cli_overrides.json"),
        ("managed", workdir / ".sdd" / "config" / "managed_settings.json"),
    ]
    return paths


@dataclass
class ConfigWatcher:
    """Watches config source files for checksum drift during a run.

    Usage::

        watcher = ConfigWatcher.snapshot(workdir)
        # ... later, in the tick loop ...
        report = watcher.check()
        if report.drifted:
            for event in report.events:
                logger.warning(event.summary())

    Attributes:
        workdir: Project root directory.
        baseline: Baseline checksums captured at snapshot time.
        snapshot_at: Unix timestamp when the baseline was captured.
        acknowledged: Set of paths whose drift has been acknowledged (suppressed
            from future checks until the file changes again).
    """

    workdir: Path
    baseline: list[FileChecksum] = field(default_factory=list[FileChecksum])
    snapshot_at: float = 0.0
    acknowledged: dict[str, str] = field(default_factory=dict[str, str])

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def snapshot(cls, workdir: Path) -> ConfigWatcher:
        """Create a watcher and capture the current baseline checksums.

        Args:
            workdir: Project root directory.

        Returns:
            A new :class:`ConfigWatcher` with the current state as baseline.
        """
        now = time.time()
        paths = discover_config_paths(workdir)
        baseline: list[FileChecksum] = []
        for label, path in paths:
            checksum = _hash_file(path)
            baseline.append(
                FileChecksum(
                    path=str(path),
                    label=label,
                    checksum=checksum,
                    exists=path.exists(),
                )
            )
        watcher = cls(workdir=workdir, baseline=baseline, snapshot_at=now)
        logger.debug(
            "Config watcher snapshot: %d sources, %d existing",
            len(baseline),
            sum(1 for b in baseline if b.exists),
        )
        return watcher

    # ------------------------------------------------------------------
    # Drift checking
    # ------------------------------------------------------------------

    def check(self) -> DriftReport:
        """Compare current file checksums against the baseline.

        Returns:
            A :class:`DriftReport` describing any detected drift.
        """
        now = time.time()
        events: list[DriftEvent] = []

        for entry in self.baseline:
            path = Path(entry.path)
            current_checksum = _hash_file(path)
            current_exists = path.exists()

            # Skip if nothing changed.
            if current_checksum == entry.checksum:
                continue

            # Skip if already acknowledged at this checksum.
            if self.acknowledged.get(entry.path) == current_checksum:
                continue

            # Determine kind of change.
            if not entry.exists and current_exists:
                kind: Literal["modified", "created", "deleted"] = "created"
            elif entry.exists and not current_exists:
                kind = "deleted"
            else:
                kind = "modified"

            severity = _severity_for_label(entry.label)
            event = DriftEvent(
                path=entry.path,
                label=entry.label,
                kind=kind,
                old_checksum=entry.checksum,
                new_checksum=current_checksum,
                severity=severity,
                detected_at=now,
            )
            events.append(event)
            logger.warning(event.summary())

        return DriftReport(
            drifted=bool(events),
            events=events,
            checked_at=now,
            snapshot_at=self.snapshot_at,
        )

    def acknowledge(self, path: str, checksum: str) -> None:
        """Suppress future drift reports for *path* at *checksum*.

        Call this after a successful reload to avoid repeating the same
        warning on every tick until the file changes again.

        Args:
            path: Absolute path to the config file.
            checksum: The current checksum to acknowledge.
        """
        self.acknowledged[path] = checksum

    def acknowledge_report(self, report: DriftReport) -> None:
        """Acknowledge all events in a drift report.

        Convenience method to suppress future warnings for every file
        that drifted in *report*.

        Args:
            report: The drift report whose events should be acknowledged.
        """
        for event in report.events:
            self.acknowledge(event.path, event.new_checksum)

    def re_snapshot(self) -> None:
        """Replace the baseline with the current file state.

        Call this after a successful config reload to reset the watcher.
        """
        now = time.time()
        paths = discover_config_paths(self.workdir)
        baseline: list[FileChecksum] = []
        for label, path in paths:
            checksum = _hash_file(path)
            baseline.append(
                FileChecksum(
                    path=str(path),
                    label=label,
                    checksum=checksum,
                    exists=path.exists(),
                )
            )
        self.baseline = baseline
        self.snapshot_at = now
        self.acknowledged.clear()
        logger.debug("Config watcher re-snapshot: %d sources", len(baseline))

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def source_chain(self) -> list[dict[str, object]]:
        """Return the current baseline as an inspectable source chain.

        Useful for ``bernstein status --config`` or trace embedding.

        Returns:
            List of dicts with path, label, checksum, and exists fields.
        """
        return [
            {
                "path": entry.path,
                "label": entry.label,
                "checksum": entry.checksum[:12] + "..." if entry.checksum else "",
                "exists": entry.exists,
            }
            for entry in self.baseline
        ]
