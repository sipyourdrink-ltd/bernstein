"""Insights persistence: store and load analytics insights."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bernstein.core.persistence.atomic_write import write_atomic_json

_INSIGHTS_FILE = Path(".sdd") / "runtime" / "insights.json"


@dataclass
class InsightsData:
    """Persisted insights data.

    Args:
        timestamp: Unix timestamp when these insights were generated.
        data: Arbitrary insights data (e.g., analytics, patterns, recommendations).
    """

    timestamp: float
    data: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InsightsData:
        """Deserialise from a JSON-parsed dict.

        Args:
            data: Dict with at least a ``timestamp`` key.

        Returns:
            Populated :class:`InsightsData`.

        Raises:
            KeyError: If ``timestamp`` is absent.
            ValueError: If ``timestamp`` cannot be cast to float.
        """
        return cls(
            timestamp=float(cast(int, data["timestamp"])),
            data=dict(cast("dict[str, Any]", data.get("data", {}))),
        )


def save_insights(workdir: Path, data: InsightsData) -> None:
    """Write insights data to ``.sdd/runtime/insights.json``.

    Creates parent directories as needed.  Overwrites any existing file.

    Args:
        workdir: Project root directory.
        data: Insights data to persist.
    """
    insights_path = workdir / _INSIGHTS_FILE
    write_atomic_json(insights_path, data.to_dict())


def load_insights(workdir: Path) -> InsightsData | None:
    """Load insights data from disk, returning None if missing or corrupt.

    Args:
        workdir: Project root directory.

    Returns:
        :class:`InsightsData` if a valid insights file exists; else None.
    """
    insights_path = workdir / _INSIGHTS_FILE
    if not insights_path.exists():
        return None
    try:
        data = json.loads(insights_path.read_text())
        return InsightsData.from_dict(data)
    except (OSError, KeyError, ValueError):
        return None


def delete_insights(workdir: Path) -> None:
    """Remove the insights file so the next load returns None.

    Args:
        workdir: Project root directory.
    """
    insights_path = workdir / _INSIGHTS_FILE
    insights_path.unlink(missing_ok=True)