"""Prompt versioning and A/B testing for agent prompts.

Stores versioned prompts in ``.sdd/prompts/`` with structured metadata.
Supports A/B assignment of tasks to prompt variants and tracks per-version
metrics (success rate, quality score, cost).  Auto-promotes the winning
variant after a configurable number of observations.

Prompt files live under ``.sdd/prompts/<name>/v<N>.md`` with a sibling
``meta.json`` tracking version metadata and metrics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_OBSERVATIONS_FOR_PROMOTION: int = 20
CONFIDENCE_THRESHOLD: float = 0.05  # winner must be 5% better in success_rate


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class VersionMetrics:
    """Accumulated metrics for a single prompt version."""

    observations: int = 0
    successes: int = 0
    total_quality_score: float = 0.0
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.observations if self.observations else 0.0

    @property
    def avg_quality(self) -> float:
        return self.total_quality_score / self.observations if self.observations else 0.0

    @property
    def avg_cost(self) -> float:
        return self.total_cost_usd / self.observations if self.observations else 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency_s / self.observations if self.observations else 0.0

    def record(
        self,
        success: bool,
        quality_score: float = 0.0,
        cost_usd: float = 0.0,
        latency_s: float = 0.0,
    ) -> None:
        self.observations += 1
        if success:
            self.successes += 1
        self.total_quality_score += quality_score
        self.total_cost_usd += cost_usd
        self.total_latency_s += latency_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "successes": self.successes,
            "total_quality_score": self.total_quality_score,
            "total_cost_usd": self.total_cost_usd,
            "total_latency_s": self.total_latency_s,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> VersionMetrics:
        return cls(
            observations=d.get("observations", 0),
            successes=d.get("successes", 0),
            total_quality_score=d.get("total_quality_score", 0.0),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            total_latency_s=d.get("total_latency_s", 0.0),
        )


def _ab_winner(m1: VersionMetrics, m2: VersionMetrics, v1: int, v2: int) -> str:
    """Determine A/B test winner from two version metrics."""
    if m2.success_rate > m1.success_rate + CONFIDENCE_THRESHOLD:
        return f"v{v2}"
    elif m1.success_rate > m2.success_rate + CONFIDENCE_THRESHOLD:
        return f"v{v1}"
    else:
        return "no clear winner"


@dataclass
class PromptVersion:
    """A single versioned prompt."""

    name: str  # e.g. "plan", "review"
    version: int
    content: str
    content_hash: str  # SHA-256 of content for integrity
    created_at: float = field(default_factory=time.time)
    author: str = "system"
    description: str = ""
    metrics: VersionMetrics = field(default_factory=VersionMetrics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "author": self.author,
            "description": self.description,
            "metrics": self.metrics.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], content: str = "") -> PromptVersion:
        return cls(
            name=d["name"],
            version=d["version"],
            content=content,
            content_hash=d.get("content_hash", ""),
            created_at=d.get("created_at", 0.0),
            author=d.get("author", "system"),
            description=d.get("description", ""),
            metrics=VersionMetrics.from_dict(d.get("metrics", {})),
        )


@dataclass
class PromptMeta:
    """Metadata for a prompt name — tracks all versions and the active one."""

    name: str
    active_version: int = 1
    ab_enabled: bool = False
    ab_versions: list[int] = field(default_factory=list)  # versions in A/B test
    ab_traffic_split: float = 0.5  # fraction of tasks that get version B
    versions: dict[int, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "active_version": self.active_version,
            "ab_enabled": self.ab_enabled,
            "ab_versions": self.ab_versions,
            "ab_traffic_split": self.ab_traffic_split,
            "versions": {str(k): v for k, v in self.versions.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PromptMeta:
        versions = {int(k): v for k, v in d.get("versions", {}).items()}
        return cls(
            name=d["name"],
            active_version=d.get("active_version", 1),
            ab_enabled=d.get("ab_enabled", False),
            ab_versions=d.get("ab_versions", []),
            ab_traffic_split=d.get("ab_traffic_split", 0.5),
            versions=versions,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


class PromptRegistry:
    """Manages versioned prompts stored under ``.sdd/prompts/``.

    Directory layout::

        .sdd/prompts/
          plan/
            meta.json       # PromptMeta (active version, A/B config, metrics)
            v1.md           # Version 1 content
            v2.md           # Version 2 content
          review/
            meta.json
            v1.md
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._prompts_dir = sdd_dir / "prompts"
        self._prompts_dir.mkdir(parents=True, exist_ok=True)

    @property
    def prompts_dir(self) -> Path:
        return self._prompts_dir

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_prompts(self) -> list[str]:
        """Return sorted list of prompt names that have a meta.json."""
        names: list[str] = []
        if not self._prompts_dir.exists():
            return names
        for child in sorted(self._prompts_dir.iterdir()):
            if child.is_dir() and (child / "meta.json").exists():
                names.append(child.name)
        return names

    def get_meta(self, name: str) -> PromptMeta | None:
        """Load metadata for a prompt name."""
        meta_path = self._prompts_dir / name / "meta.json"
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return PromptMeta.from_dict(data)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Bad meta.json for prompt %r: %s", name, exc)
            return None

    def get_version(self, name: str, version: int) -> PromptVersion | None:
        """Load a specific prompt version."""
        meta = self.get_meta(name)
        if meta is None:
            return None
        ver_dict = meta.versions.get(version)
        if ver_dict is None:
            return None
        content_path = self._prompts_dir / name / f"v{version}.md"
        content = ""
        if content_path.exists():
            content = content_path.read_text(encoding="utf-8")
        return PromptVersion.from_dict(ver_dict, content=content)

    def get_active_content(self, name: str) -> str | None:
        """Return the content of the active version for a prompt name."""
        meta = self.get_meta(name)
        if meta is None:
            return None
        ver = self.get_version(name, meta.active_version)
        return ver.content if ver else None

    def list_versions(self, name: str) -> list[int]:
        """Return sorted list of version numbers for a prompt."""
        meta = self.get_meta(name)
        if meta is None:
            return []
        return sorted(meta.versions.keys())

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_version(
        self,
        name: str,
        content: str,
        *,
        author: str = "system",
        description: str = "",
        set_active: bool = False,
    ) -> PromptVersion:
        """Add a new version of a prompt.

        Args:
            name: Prompt name (e.g. "plan", "review").
            content: Full prompt template content.
            author: Who created this version.
            description: Human-readable description of changes.
            set_active: If True, immediately make this the active version.

        Returns:
            The created PromptVersion.
        """
        prompt_dir = self._prompts_dir / name
        prompt_dir.mkdir(parents=True, exist_ok=True)

        meta = self.get_meta(name) or PromptMeta(name=name)
        new_version = max(meta.versions.keys(), default=0) + 1

        pv = PromptVersion(
            name=name,
            version=new_version,
            content=content,
            content_hash=_hash_content(content),
            author=author,
            description=description,
        )

        # Write content file
        content_path = prompt_dir / f"v{new_version}.md"
        content_path.write_text(content, encoding="utf-8")

        # Update meta
        meta.versions[new_version] = pv.to_dict()
        if set_active or meta.active_version == 0:
            meta.active_version = new_version

        self._save_meta(name, meta)
        return pv

    def _save_meta(self, name: str, meta: PromptMeta) -> None:
        meta_path = self._prompts_dir / name / "meta.json"
        meta_path.write_text(
            json.dumps(meta.to_dict(), indent=2),
            encoding="utf-8",
        )

    def promote_version(self, name: str, version: int) -> bool:
        """Manually promote a version to active.

        Returns:
            True if promoted, False if version does not exist.
        """
        meta = self.get_meta(name)
        if meta is None or version not in meta.versions:
            return False
        meta.active_version = version
        meta.ab_enabled = False
        meta.ab_versions = []
        self._save_meta(name, meta)
        return True

    # ------------------------------------------------------------------
    # A/B testing
    # ------------------------------------------------------------------

    def start_ab_test(
        self,
        name: str,
        version_a: int,
        version_b: int,
        traffic_split: float = 0.5,
    ) -> bool:
        """Start an A/B test between two versions.

        Args:
            name: Prompt name.
            version_a: Control version.
            version_b: Treatment version.
            traffic_split: Fraction of tasks assigned to version_b.

        Returns:
            True if test started successfully.
        """
        meta = self.get_meta(name)
        if meta is None:
            return False
        if version_a not in meta.versions or version_b not in meta.versions:
            return False
        meta.ab_enabled = True
        meta.ab_versions = [version_a, version_b]
        meta.ab_traffic_split = traffic_split
        self._save_meta(name, meta)
        logger.info(
            "A/B test started for %r: v%d vs v%d (%.0f%% traffic to v%d)",
            name,
            version_a,
            version_b,
            traffic_split * 100,
            version_b,
        )
        return True

    def stop_ab_test(self, name: str) -> bool:
        """Stop an active A/B test without promoting either version."""
        meta = self.get_meta(name)
        if meta is None or not meta.ab_enabled:
            return False
        meta.ab_enabled = False
        meta.ab_versions = []
        self._save_meta(name, meta)
        return True

    def select_version(self, name: str, task_id: str = "") -> int | None:
        """Select a prompt version for a task.

        If A/B testing is active, deterministically assigns based on
        task_id hash (consistent assignment per task).  Otherwise returns
        the active version.

        Args:
            name: Prompt name.
            task_id: Task ID for deterministic assignment.

        Returns:
            Version number, or None if prompt not found.
        """
        meta = self.get_meta(name)
        if meta is None:
            return None

        if not meta.ab_enabled or len(meta.ab_versions) != 2:
            return meta.active_version

        # Deterministic split: hash task_id to get consistent assignment
        if task_id:
            hash_val = int(hashlib.md5(task_id.encode(), usedforsecurity=False).hexdigest(), 16)
            fraction = (hash_val % 10000) / 10000.0
        else:
            fraction = random.random()

        if fraction < meta.ab_traffic_split:
            return meta.ab_versions[1]  # treatment
        return meta.ab_versions[0]  # control

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        name: str,
        version: int,
        *,
        success: bool,
        quality_score: float = 0.0,
        cost_usd: float = 0.0,
        latency_s: float = 0.0,
    ) -> None:
        """Record a task outcome for a prompt version.

        Args:
            name: Prompt name.
            version: Version that was used.
            success: Whether the task succeeded.
            quality_score: Quality score (0.0-1.0) from review.
            cost_usd: Cost incurred.
            latency_s: Task duration in seconds.
        """
        meta = self.get_meta(name)
        if meta is None or version not in meta.versions:
            logger.warning("Cannot record outcome: %r v%d not found", name, version)
            return

        ver_dict = meta.versions[version]
        metrics = VersionMetrics.from_dict(ver_dict.get("metrics", {}))
        metrics.record(
            success=success,
            quality_score=quality_score,
            cost_usd=cost_usd,
            latency_s=latency_s,
        )
        ver_dict["metrics"] = metrics.to_dict()
        self._save_meta(name, meta)

    def check_auto_promote(
        self,
        name: str,
        min_observations: int = MIN_OBSERVATIONS_FOR_PROMOTION,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> int | None:
        """Check if an A/B test has a clear winner and auto-promote.

        Returns the promoted version number, or None if no promotion.
        """
        meta = self.get_meta(name)
        if meta is None or not meta.ab_enabled or len(meta.ab_versions) != 2:
            return None

        va, vb = meta.ab_versions
        metrics_a = VersionMetrics.from_dict(meta.versions.get(va, {}).get("metrics", {}))
        metrics_b = VersionMetrics.from_dict(meta.versions.get(vb, {}).get("metrics", {}))

        # Need enough observations on both sides
        if metrics_a.observations < min_observations or metrics_b.observations < min_observations:
            return None

        # Compare success rates
        diff = metrics_b.success_rate - metrics_a.success_rate
        if abs(diff) < confidence_threshold:
            return None  # No clear winner yet

        winner = vb if diff > 0 else va
        logger.info(
            "Auto-promoting %r v%d (success_rate=%.2f vs %.2f, %d+%d observations)",
            name,
            winner,
            max(metrics_a.success_rate, metrics_b.success_rate),
            min(metrics_a.success_rate, metrics_b.success_rate),
            metrics_a.observations,
            metrics_b.observations,
        )
        self.promote_version(name, winner)
        return winner

    def compare_versions(self, name: str, v1: int, v2: int) -> dict[str, Any] | None:
        """Compare metrics between two versions.

        Returns:
            Dict with comparison data, or None if versions not found.
        """
        meta = self.get_meta(name)
        if meta is None:
            return None
        if v1 not in meta.versions or v2 not in meta.versions:
            return None

        m1 = VersionMetrics.from_dict(meta.versions[v1].get("metrics", {}))
        m2 = VersionMetrics.from_dict(meta.versions[v2].get("metrics", {}))

        return {
            "name": name,
            "v1": {
                "version": v1,
                "observations": m1.observations,
                "success_rate": round(m1.success_rate, 4),
                "avg_quality": round(m1.avg_quality, 4),
                "avg_cost": round(m1.avg_cost, 6),
                "avg_latency": round(m1.avg_latency, 1),
            },
            "v2": {
                "version": v2,
                "observations": m2.observations,
                "success_rate": round(m2.success_rate, 4),
                "avg_quality": round(m2.avg_quality, 4),
                "avg_cost": round(m2.avg_cost, 6),
                "avg_latency": round(m2.avg_latency, 1),
            },
            "winner": _ab_winner(m1, m2, v1, v2),
            "ab_active": meta.ab_enabled,
            "active_version": meta.active_version,
        }


# ---------------------------------------------------------------------------
# Bootstrap helper
# ---------------------------------------------------------------------------


def seed_prompts_from_templates(sdd_dir: Path, templates_dir: Path) -> int:
    """Seed ``.sdd/prompts/`` with v1 from ``templates/prompts/``.

    Only creates entries for prompts that do not already exist in the
    registry.  Returns the number of prompts seeded.
    """
    registry = PromptRegistry(sdd_dir)
    prompts_src = templates_dir / "prompts"
    if not prompts_src.is_dir():
        return 0

    seeded = 0
    for md_file in sorted(prompts_src.glob("*.md")):
        name = md_file.stem  # e.g. "plan", "review"
        if registry.get_meta(name) is not None:
            continue  # already tracked
        content = md_file.read_text(encoding="utf-8")
        registry.add_version(
            name,
            content,
            author="system",
            description=f"Initial version from templates/prompts/{md_file.name}",
            set_active=True,
        )
        seeded += 1
        logger.info("Seeded prompt %r v1 from %s", name, md_file)
    return seeded
