"""Engagement playbook library for deterministic security engagement projection.

Loads engagement playbooks from YAML templates, providing dataclass views
for the engagement projection engine. Mirrors the scenario_library pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ScannerConfig:
    """Adapter-specific configuration for a scanner phase."""

    adapter: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngagementPhase:
    """One phase of an engagement playbook.

    Attributes:
        name: Phase name (e.g. "Recon", "Scan", "Verify", "Report").
        action: Action type — "scanner", "verify", or "report".
        scanners: ScannerAdapter references (only for action="scanner").
        scope_ref: Content-addressed grant reference to the EngagementMandate.
        config: Phase-specific configuration (output_format, risk_threshold, etc.).
    """

    name: str
    action: str
    scanners: tuple[ScannerConfig, ...]
    scope_ref: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngagementPlaybook:
    """A reusable engagement playbook for deterministic task graph projection."""

    playbook_id: str
    name: str
    description: str
    version: str
    tags: tuple[str, ...]
    scope_ref: str
    phases: tuple[EngagementPhase, ...]


@dataclass(frozen=True)
class EngagementLibrary:
    """In-memory library of engagement playbooks indexed by id."""

    playbooks: dict[str, EngagementPlaybook]

    def get(self, playbook_id: str) -> EngagementPlaybook | None:
        return self.playbooks.get(playbook_id)


def load_engagement_library(root: Path) -> EngagementLibrary:
    """Load all engagement YAML files under *root* recursively."""
    playbooks: dict[str, EngagementPlaybook] = {}
    if not root.exists():
        return EngagementLibrary(playbooks={})

    files = sorted(list(root.rglob("*.yaml")) + list(root.rglob("*.yml")))
    for path in files:
        playbook = _load_playbook_file(path)
        if playbook is None:
            continue
        playbooks[playbook.playbook_id] = playbook
    return EngagementLibrary(playbooks=playbooks)


def _load_playbook_file(path: Path) -> EngagementPlaybook | None:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    data = cast("dict[str, object]", loaded)

    playbook_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    phases_raw = data.get("phases")
    if not playbook_id or not name:
        return None

    phases: list[EngagementPhase] = []
    if isinstance(phases_raw, list):
        for phase_item in cast("list[object]", phases_raw):
            phase = _parse_phase(phase_item)
            if phase is not None:
                phases.append(phase)

    if not phases:
        return None

    scope_ref = str(data.get("scope_ref", "")).strip()
    tags_raw = data.get("tags", [])
    tags = (
        tuple(str(t).strip() for t in cast("list[object]", tags_raw) if str(t).strip())
        if isinstance(tags_raw, list)
        else ()
    )

    return EngagementPlaybook(
        playbook_id=playbook_id,
        name=name,
        description=description,
        tags=tags,
        version=str(data.get("version", "1.0")).strip() or "1.0",
        scope_ref=scope_ref,
        phases=tuple(phases),
    )


def _parse_phase(data: object) -> EngagementPhase | None:
    if not isinstance(data, dict):
        return None
    phase_data = cast("dict[str, object]", data)

    name = str(phase_data.get("name", "")).strip()
    action = str(phase_data.get("action", "")).strip()
    if not name or not action:
        return None

    scanners: list[ScannerConfig] = []
    scanners_raw = phase_data.get("scanners")
    if action == "scanner" and isinstance(scanners_raw, list):
        for sc_item in cast("list[object]", scanners_raw):
            sc_config = _parse_scanner_config(sc_item)
            if sc_config is not None:
                scanners.append(sc_config)

    scope_ref = str(phase_data.get("scope_ref", "")).strip()
    config = phase_data.get("config", {})

    if not scope_ref:
        return None

    return EngagementPhase(
        name=name,
        action=action,
        scanners=tuple(scanners),
        scope_ref=scope_ref,
        config=dict(config) if isinstance(config, dict) else {},
    )


def _parse_scanner_config(data: object) -> ScannerConfig | None:
    if not isinstance(data, dict):
        return None
    item = cast("dict[str, object]", data)

    adapter = str(item.get("adapter", "")).strip()
    config = item.get("config", {})
    if not adapter:
        return None

    return ScannerConfig(
        adapter=adapter,
        config=dict(config) if isinstance(config, dict) else {},
    )
