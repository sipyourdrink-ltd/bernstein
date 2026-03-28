"""Baseline tracking for eval-gated evolution.

Stores and loads the eval baseline score that proposals must meet or exceed.
The baseline is persisted at ``.sdd/eval/baseline.json``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Default path relative to state_dir.
_BASELINE_FILENAME = "eval/baseline.json"


@dataclass
class EvalBaseline:
    """Tracked eval baseline that proposals must meet.

    Attributes:
        score: Composite score (0.0 - 1.0).
        components: Per-tier or per-dimension scores.
        timestamp: ISO 8601 timestamp of when this baseline was recorded.
        config_hash: Hash of the config state when the baseline was recorded.
    """

    score: float
    components: dict[str, float] = field(default_factory=dict[str, Any])
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    config_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON output."""
        return {
            "score": self.score,
            "components": self.components,
            "timestamp": self.timestamp,
            "config_hash": self.config_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalBaseline:
        """Deserialize from dict."""
        return cls(
            score=float(data["score"]),
            components=dict(data.get("components", {})),
            timestamp=str(data.get("timestamp", "")),
            config_hash=str(data.get("config_hash", "")),
        )


def load_baseline(state_dir: Path) -> EvalBaseline | None:
    """Load the current eval baseline from disk.

    Args:
        state_dir: Path to the .sdd directory.

    Returns:
        EvalBaseline if a baseline file exists, None otherwise.
    """
    path = state_dir / _BASELINE_FILENAME
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return EvalBaseline.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Failed to load eval baseline from %s: %s", path, exc)
        return None


def save_baseline(state_dir: Path, baseline: EvalBaseline) -> None:
    """Save the eval baseline to disk.

    Args:
        state_dir: Path to the .sdd directory.
        baseline: The baseline to save.
    """
    path = state_dir / _BASELINE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Eval baseline saved: score=%.4f at %s", baseline.score, path)


def compute_config_hash(state_dir: Path) -> str:
    """Compute a hash of the current config state.

    Hashes all YAML/JSON files in .sdd/config/ to fingerprint
    the active configuration.

    Args:
        state_dir: Path to the .sdd directory.

    Returns:
        Hex digest of the combined config hash.
    """
    config_dir = state_dir / "config"
    if not config_dir.is_dir():
        return "no-config"

    hasher = hashlib.sha256()
    for path in sorted(config_dir.glob("*")):
        if path.suffix in (".yaml", ".yml", ".json"):
            try:
                hasher.update(path.read_bytes())
            except OSError:
                continue
    return hasher.hexdigest()[:12]
