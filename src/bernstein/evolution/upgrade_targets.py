"""Category-to-target-file mapping shared by the upgrade executor and task spawn.

An upgrade proposal's :class:`~bernstein.evolution.detector.UpgradeCategory`
determines exactly which files applying it touches:
:class:`~bernstein.evolution.applicator.FileUpgradeExecutor` writes one config
or template file per category. Auto-spawned upgrade tasks must declare the
same files as ``owned_files`` so the file-collision guards stop no-opping on
them (issue #3398) - and the declaration must never drift from what the
executor actually writes. This module is that single source of truth: the
executor resolves its write targets and the task-spawn path renders its
``owned_files`` from the same table.

Detector component labels ("model_routing", "policy", backend names) are
deliberately not part of this mapping: they are risk metadata, not paths, and
writing them into ``owned_files`` would defeat the guards' empty-means-no-scope
short-circuit while still matching no real file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from bernstein.evolution.detector import UpgradeCategory

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "UPGRADE_CATEGORY_TARGETS",
    "upgrade_owned_files",
    "upgrade_target_paths",
]

_Anchor = Literal["state_dir", "repo_root"]

#: Per-category target files, each as ``(anchor, relative_path)``. Paths
#: anchored on ``state_dir`` live under the runtime state directory (``.sdd``
#: by the evolution loop's contract); paths anchored on ``repo_root`` live
#: under its parent, the working directory.
UPGRADE_CATEGORY_TARGETS: dict[UpgradeCategory, tuple[tuple[_Anchor, str], ...]] = {
    UpgradeCategory.POLICY_UPDATE: (("state_dir", "config/policies.yaml"),),
    UpgradeCategory.ROUTING_RULES: (("state_dir", "config/routing.yaml"),),
    UpgradeCategory.MODEL_ROUTING: (("state_dir", "config/routing.yaml"),),
    UpgradeCategory.PROVIDER_CONFIG: (("state_dir", "config/providers.yaml"),),
    UpgradeCategory.ROLE_TEMPLATES: (("repo_root", "templates/roles/PROPOSED_UPGRADES.jsonl"),),
}


def upgrade_target_paths(category: UpgradeCategory, state_dir: Path) -> tuple[Path, ...]:
    """Return the file paths an upgrade of *category* writes, anchored on *state_dir*.

    The repository root is ``state_dir.parent``, matching the contract the
    evolution loop and :class:`FileUpgradeExecutor` already assume. The
    returned paths are absolute exactly when *state_dir* is absolute; no
    ``resolve()`` is applied, preserving the executor's pre-existing
    anchoring semantics.

    Args:
        category: The upgrade category being applied.
        state_dir: The runtime state directory (the ``.sdd`` directory).

    Returns:
        The anchored target paths; empty only for a category missing from
        :data:`UPGRADE_CATEGORY_TARGETS`.
    """
    repo_root = state_dir.parent
    return tuple(
        (state_dir if anchor == "state_dir" else repo_root) / rel
        for anchor, rel in UPGRADE_CATEGORY_TARGETS.get(category, ())
    )


def upgrade_owned_files(category: UpgradeCategory, state_dir_name: str = ".sdd") -> list[str]:
    """Return workdir-relative ``owned_files`` for a task spawned from *category*.

    Args:
        category: The upgrade category of the spawning proposal.
        state_dir_name: Name of the runtime state directory under the workdir.

    Returns:
        Workdir-relative path strings, in table order; empty only for a
        category missing from :data:`UPGRADE_CATEGORY_TARGETS` - callers
        record that instead of spawning a silently scopeless task.
    """
    return [
        f"{state_dir_name}/{rel}" if anchor == "state_dir" else rel
        for anchor, rel in UPGRADE_CATEGORY_TARGETS.get(category, ())
    ]
