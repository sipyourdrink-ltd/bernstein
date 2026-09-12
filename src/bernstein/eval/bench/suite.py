"""
bernstein-bench: content-addressed task suite.

A suite is a versioned, content-addressed set of tasks derived from
``golden.py`` curation and ``yaml_runner.py`` spec format.  Two runners
on the same ``suite_hash`` provably ran the same task set; a changed task
changes the hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.compliance.controls import ControlRegistry

# ---------------------------------------------------------------------------
# Task spec (mirrors yaml_runner task shape, kept dependency-free here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchTask:
    """A single, content-addressed benchmark task."""

    id: str
    description: str
    # Ordered list of steps the adapter must complete.
    steps: tuple[str, ...]
    # Expected artefacts / assertions (opaque to the harness — verified by
    # harness.py scoring machinery after replay).
    assertions: tuple[dict[str, Any], ...]
    # Optional human-readable category tag (e.g. "file_io", "refactor").
    category: str = ""

    def content_hash(self) -> str:
        """Deterministic SHA-256 of the canonical task bytes."""
        canonical = json.dumps(
            {
                "id": self.id,
                "description": self.description,
                "steps": list(self.steps),
                "assertions": list(self.assertions),
                "category": self.category,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------


@dataclass
class BenchSuite:
    """
    A versioned, content-addressed collection of :class:`BenchTask` objects.

    ``suite_hash`` is derived from the *ordered* sequence of per-task hashes,
    so adding, removing, or reordering any task changes the suite identity.
    If private holdout tasks are configured, ``holdout_hash`` is bound to
    the manifest without publishing the private task specifications. A
    non-empty ``controls`` declaration is bound the same way, so a suite
    that declares none keeps the hash it was published with.
    """

    version: str
    tasks: list[BenchTask] = field(default_factory=list)
    holdout_hash: str = ""
    holdout_tasks: list[BenchTask] = field(default_factory=list, repr=False)
    # Compliance control IDs this suite claims to exercise (#5455). Bound
    # into ``suite_hash`` only when non-empty -- see ``_compute_hash``.
    controls: list[str] = field(default_factory=list)

    # Computed lazily and cached.
    _suite_hash: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.holdout_hash and self.holdout_tasks:
            self.holdout_hash = self.compute_holdout_hash()

    def compute_holdout_hash(self) -> str:
        """Deterministic SHA-256 of the canonical holdout task hashes."""
        if not self.holdout_tasks:
            return self.holdout_hash
        task_hashes = [t.content_hash() for t in self.holdout_tasks]
        payload = json.dumps(
            {"task_hashes": task_hashes},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @property
    def suite_hash(self) -> str:
        if self._suite_hash is None:
            self._suite_hash = self._compute_hash()
        return self._suite_hash

    def _compute_hash(self) -> str:
        task_hashes = [t.content_hash() for t in self.tasks]
        payload_dict: dict[str, Any] = {"task_hashes": task_hashes, "version": self.version}
        effective_holdout = self.holdout_hash or (self.compute_holdout_hash() if self.holdout_tasks else "")
        if effective_holdout:
            payload_dict["holdout_hash"] = effective_holdout
        # Same rule as holdout_hash: a declaration the suite does not make is
        # not in the payload, so every suite published before #5455 -- and
        # every receipt, bundle and leaderboard row that names its hash --
        # stays valid. A suite that does declare controls commits to them.
        if self.controls:
            # Canonical: the same declaration in any order, with any
            # repeats, is one identity -- as holdout_hash is one digest.
            payload_dict["controls"] = sorted(set(self.controls))
        payload = json.dumps(
            payload_dict,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def validate_controls(self, registry: ControlRegistry | None = None) -> None:
        """Validate that the suite declares at least one control and all controls are registered."""
        if not self.controls:
            raise ValueError(
                f"BenchSuite {self.version!r} must declare at least one control ID from the compliance registry."
            )
        if registry is None:
            from bernstein.compliance.controls import get_default_registry

            registry = get_default_registry()
        invalid = registry.validate_control_ids(self.controls)
        if invalid:
            raise ValueError(
                f"BenchSuite {self.version!r} declares unregistered control IDs: {invalid}. "
                "All controls must be registered in bernstein.compliance.controls."
            )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "suite_hash": self.suite_hash,
            "tasks": [
                {
                    "id": t.id,
                    "description": t.description,
                    "steps": list(t.steps),
                    "assertions": list(t.assertions),
                    "category": t.category,
                    "task_hash": t.content_hash(),
                }
                for t in self.tasks
            ],
        }
        effective_holdout = self.holdout_hash or (self.compute_holdout_hash() if self.holdout_tasks else "")
        if effective_holdout:
            d["holdout_hash"] = effective_holdout
        if self.controls:
            d["controls"] = list(self.controls)
        return d

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> BenchSuite:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tasks = [
            BenchTask(
                id=t["id"],
                description=t["description"],
                steps=tuple(t["steps"]),
                assertions=tuple(t["assertions"]),
                category=t.get("category", ""),
            )
            for t in raw["tasks"]
        ]
        holdout_hash = raw.get("holdout_hash", "")
        controls = raw.get("controls", [])
        # The field is bound into suite identity, so its shape is checked here
        # rather than surfacing later as a per-character "unregistered id"
        # error (a string) or a silently accepted mapping (a dict).
        if not isinstance(controls, list) or not all(isinstance(c, str) and c for c in controls):
            raise ValueError(
                f"Suite {raw.get('version')!r}: 'controls' must be a list of non-empty strings, got {controls!r}"
            )
        suite = cls(version=raw["version"], tasks=tasks, holdout_hash=holdout_hash, controls=controls)
        # Integrity check: stored hash must match recomputed hash.
        if suite.suite_hash != raw["suite_hash"]:
            raise ValueError(
                f"Suite hash mismatch: stored {raw['suite_hash']!r} "
                f"!= recomputed {suite.suite_hash!r}. "
                "The suite file may have been tampered with."
            )
        return suite
