"""Untracked overlay layer for run-scoped configuration overrides.

A run needs to override parts of the project's configuration for its own
duration - the role/model policy it was launched with, the internal LLM
provider, a quality-gate switch.  Writing those overrides into the tracked
``bernstein.yaml`` in the work tree made them part of every commit an agent
produced with a staged tree, so a pull request could propose rewriting the
repository's committed configuration for everyone.  Restoring the file just
before publishing only fixed the one publishing path that remembered to do
it; the leak came back the moment a second path appeared.

This module removes the failure mode instead of compensating for it: run
overrides are resolved from a layer git does not track.  The overlay lives
inside the repository's git directory (or wherever :data:`ENV_CONFIG_OVERLAY`
points, so a sandbox can hand the run a location of its own), never in the
work tree.  :func:`resolve_effective_mapping` merges it over the mapping
parsed from the committed file, so the committed file is *read* by a run and
never *written* by one.

Precedence, lowest to highest:

1. the committed configuration file in the work tree;
2. the overlay file (:data:`ENV_CONFIG_OVERLAY`, else ``<git-dir>/bernstein/run-overlay.yaml``);
3. the inline override mapping in :data:`ENV_CONFIG_OVERRIDE`;
4. explicit CLI flags, which callers apply to the parsed config afterwards.

With no overlay file and neither environment variable set, the merge is the
identity function: a setup that edits the committed file directly keeps
working exactly as before.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

#: Absolute path to the overlay file.  Set by a sandbox (or an operator) that
#: needs the overlay somewhere other than the repository's git directory - a
#: read-only bind-mounted work tree, for instance.
ENV_CONFIG_OVERLAY: Final[str] = "BERNSTEIN_CONFIG_OVERLAY"

#: Inline override mapping, as YAML or JSON.  Highest-precedence layer below
#: explicit CLI flags; useful when a caller has a single knob to set and no
#: sensible place to keep a file.
ENV_CONFIG_OVERRIDE: Final[str] = "BERNSTEIN_CONFIG_OVERRIDE"

#: Overlay location relative to the repository's git directory when
#: :data:`ENV_CONFIG_OVERLAY` is unset.  Inside ``.git/`` by construction, so
#: it cannot be staged, cannot show up in ``git status``, and cannot be
#: carried by ``git commit -a``.
OVERLAY_RELATIVE_TO_GIT_DIR: Final[str] = "bernstein/run-overlay.yaml"

#: Work-tree paths that carry run configuration and must therefore never
#: appear in a commit an orchestrated run produces.  This is the single
#: source of truth for the invariant: the merge-preflight deny list, the
#: per-worktree local excludes, and the commit gate all derive from it
#: rather than repeating it, so they cannot drift apart.
#:
#: ``.claude/mcp.json`` is the bridge manifest Claude Code reads from a fixed
#: project-local path; see :func:`bernstein.core.git.local_exclude.register_run_excludes`
#: for why that one file is written into the work tree anyway and how it is
#: kept out of the index.
RUN_CONFIG_PATHS: frozenset[str] = frozenset(
    {
        "bernstein.yaml",
        "bernstein.yml",
        ".bernstein/bernstein.yaml",
        ".bernstein/bernstein.yml",
        ".claude/mcp.json",
        ".mcp.json",
    }
)

#: Marker git leaves in every work tree: a directory in a normal clone, a
#: file containing ``gitdir: <path>`` in a linked worktree or a submodule.
_GIT_MARKER = ".git"


class RunOverlayError(RuntimeError):
    """Raised when the overlay layer is present but unusable.

    A malformed or misplaced overlay is never ignored: silently falling back
    to the committed file would run the job under a configuration nobody
    asked for.
    """


# ---------------------------------------------------------------------------
# Git-directory resolution
# ---------------------------------------------------------------------------
#
# Resolved from the filesystem rather than by shelling out to ``git``.
# :func:`resolve_effective_mapping` runs on every configuration load,
# including the orchestrator's per-tick hot-reload check, and a subprocess
# per tick is a cost the overlay does not need to impose. The walk below is
# what git itself does: look for the ``.git`` marker in the directory and
# then in each parent.


def _git_dir_from_marker(marker: Path) -> Path | None:
    """Resolve a ``.git`` marker to the git directory it denotes."""
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    # Linked worktree / submodule: ``.git`` is a file holding a pointer.
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("Cannot read git marker file %s: %s", marker, exc)
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("gitdir:"):
            continue
        target = Path(stripped.removeprefix("gitdir:").strip())
        if not target.is_absolute():
            target = marker.parent / target
        return Path(os.path.normpath(target))
    logger.debug("Git marker file %s has no gitdir: pointer", marker)
    return None


def _walk_to_repo(workdir: Path) -> tuple[Path, Path] | None:
    """Return ``(work_tree_root, git_dir)`` for *workdir*, or ``None``.

    ``$GIT_DIR`` wins when set, matching git's own precedence, so a caller
    that has redirected git already has the overlay follow it.
    """
    try:
        start = workdir.resolve()
    except OSError as exc:
        logger.debug("Cannot resolve %s: %s", workdir, exc)
        return None

    env_git_dir = os.environ.get("GIT_DIR", "").strip()
    if env_git_dir:
        git_dir = Path(env_git_dir).expanduser()
        if not git_dir.is_absolute():
            git_dir = start / git_dir
        env_work_tree = os.environ.get("GIT_WORK_TREE", "").strip()
        root = Path(env_work_tree).expanduser() if env_work_tree else start
        if not root.is_absolute():
            root = start / root
        return Path(os.path.normpath(root)), Path(os.path.normpath(git_dir))

    for candidate in (start, *start.parents):
        git_dir = _git_dir_from_marker(candidate / _GIT_MARKER)
        if git_dir is not None:
            return candidate, git_dir
    return None


def git_dir_for(workdir: Path) -> Path | None:
    """Resolve the git directory that owns *workdir*.

    For a linked worktree this is that worktree's own
    ``.git/worktrees/<id>`` directory, so two worktrees of one clone get two
    independent overlays rather than fighting over a shared file.
    """
    found = _walk_to_repo(workdir)
    return None if found is None else found[1]


def work_tree_root_for(workdir: Path) -> Path | None:
    """Resolve the work-tree root that owns *workdir*."""
    found = _walk_to_repo(workdir)
    return None if found is None else found[0]


# ---------------------------------------------------------------------------
# Overlay location
# ---------------------------------------------------------------------------


def _env_overlay_path() -> Path | None:
    raw = os.environ.get(ENV_CONFIG_OVERLAY, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def resolve_overlay_path(config_path: Path) -> Path | None:
    """Return the overlay file location for the config at *config_path*.

    ``$BERNSTEIN_CONFIG_OVERLAY`` wins when set.  Otherwise the overlay lives
    at ``<git-dir>/bernstein/run-overlay.yaml``.  Returns ``None`` when the
    config file is not inside a git repository and no explicit location was
    given - there is then no place to put an overlay that the work tree does
    not also contain, and the caller falls back to the committed file alone.

    The returned path is not required to exist.
    """
    explicit = _env_overlay_path()
    if explicit is not None:
        return explicit
    git_dir = git_dir_for(config_path.parent)
    if git_dir is None:
        return None
    return git_dir / OVERLAY_RELATIVE_TO_GIT_DIR


def _resolved(path: Path, *, relative_to: Path) -> Path:
    """Absolute, symlink-resolved form of *path* (it need not exist)."""
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = relative_to / candidate
    return candidate.resolve()


def assert_outside_work_tree(overlay_path: Path, *, workdir: Path) -> None:
    """Refuse an overlay location git would report as a work-tree change.

    The whole point of the overlay is that no run can put configuration into
    a commit, so an overlay pointed at the work tree is not a smaller version
    of the guarantee - it is the original defect with a new filename.  Paths
    inside the git directory are fine (git never tracks its own directory);
    everything else under the work-tree root is refused.
    """
    root = work_tree_root_for(workdir)
    if root is None:
        return
    resolved = _resolved(overlay_path, relative_to=workdir)
    git_dir = git_dir_for(workdir)
    if git_dir is not None and resolved.is_relative_to(git_dir.resolve()):
        return
    if resolved.is_relative_to(root.resolve()):
        raise RunOverlayError(
            f"Refusing to use {resolved} as the run-configuration overlay: it is inside the work tree "
            f"({root}), so a run could commit it. Point {ENV_CONFIG_OVERLAY} at a path outside the work "
            f"tree, or leave it unset to use <git-dir>/{OVERLAY_RELATIVE_TO_GIT_DIR}."
        )


# ---------------------------------------------------------------------------
# Reading and merging
# ---------------------------------------------------------------------------


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """Merge *over* onto *base*, recursing into nested mappings.

    Scalars and sequences replace wholesale; only mappings merge key by key.
    A list that half-merged with the committed value would be impossible to
    reason about (``constraints``, ``context_files``, ``org_policies`` are
    all ordered lists whose meaning depends on the whole sequence), so an
    overlay that sets a list means exactly that list.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _as_mapping(loaded: object, *, origin: str) -> dict[str, Any]:
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RunOverlayError(f"{origin} must be a mapping, got {type(loaded).__name__}")
    return dict(loaded)


def load_overlay_mapping(overlay_path: Path | None) -> dict[str, Any]:
    """Load the overlay file, or ``{}`` when there is none.

    Raises:
        RunOverlayError: if the file exists but is unreadable or is not a
            YAML mapping.  A run must not silently proceed on the committed
            configuration when an overlay was meant to apply.
    """
    if overlay_path is None or not overlay_path.exists():
        return {}
    try:
        raw_text = overlay_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunOverlayError(f"Cannot read configuration overlay {overlay_path}: {exc}") from exc
    try:
        loaded: object = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RunOverlayError(f"Invalid YAML in configuration overlay {overlay_path}: {exc}") from exc
    return _as_mapping(loaded, origin=f"Configuration overlay {overlay_path}")


def load_inline_override() -> dict[str, Any]:
    """Load the inline override mapping from :data:`ENV_CONFIG_OVERRIDE`.

    The value is parsed as YAML, which also accepts JSON, so a caller can use
    whichever is easier to quote.

    Raises:
        RunOverlayError: if the variable is set to something that is not a
            mapping.
    """
    raw = os.environ.get(ENV_CONFIG_OVERRIDE, "").strip()
    if not raw:
        return {}
    try:
        loaded: object = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RunOverlayError(f"Invalid YAML/JSON in ${ENV_CONFIG_OVERRIDE}: {exc}") from exc
    return _as_mapping(loaded, origin=f"${ENV_CONFIG_OVERRIDE}")


def resolve_effective_mapping(base: Mapping[str, Any], *, config_path: Path) -> dict[str, Any]:
    """Return *base* merged with the overlay and the inline override.

    Args:
        base: The mapping parsed from the committed configuration file.
        config_path: Where that file lives; used to locate the overlay.

    Returns:
        A new mapping.  *base* is never mutated, and the file it came from is
        never written.

    Raises:
        RunOverlayError: if an overlay or inline override is present but
            unusable.
    """
    overlay_path = resolve_overlay_path(config_path)
    overlay = load_overlay_mapping(overlay_path)
    inline = load_inline_override()
    if not overlay and not inline:
        return dict(base)
    merged = deep_merge(base, overlay)
    merged = deep_merge(merged, inline)
    logger.debug(
        "Applied run-configuration overlay over %s (overlay=%s keys=%s, inline keys=%s)",
        config_path,
        overlay_path,
        sorted(overlay),
        sorted(inline),
    )
    return merged


def effective_mtime(config_path: Path) -> float:
    """Newest mtime across the committed file and its overlay.

    Callers that hot-reload configuration by watching a file's mtime must
    watch both layers, otherwise an overlay write - the only kind of write a
    run is allowed to make - would never be noticed.  Missing files
    contribute ``0.0``.
    """
    newest = 0.0
    for candidate in (config_path, resolve_overlay_path(config_path)):
        if candidate is None:
            continue
        try:
            newest = max(newest, candidate.stat().st_mtime)
        except OSError:
            continue
    return newest


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


_OVERLAY_HEADER = (
    "# Bernstein run-configuration overlay - generated, not tracked by git.\n"
    "# Merged over the committed configuration file at load time so a run\n"
    "# never has to write the tracked file. Safe to delete when no run is\n"
    "# active; the next run recreates whatever it needs.\n"
)


def write_overlay(updates: Mapping[str, Any], *, config_path: Path) -> Path:
    """Merge *updates* into the overlay for *config_path* and persist it.

    This is the only supported way for a run to change its own effective
    configuration.  The committed file is not opened for writing here, or
    anywhere else in the run.

    Returns:
        The overlay path that was written.

    Raises:
        RunOverlayError: when no overlay location can be resolved (the
            config file is not in a git repository and ``$BERNSTEIN_CONFIG_OVERLAY``
            is unset), when the resolved location is inside the work tree, or
            when the write fails.
    """
    overlay_path = resolve_overlay_path(config_path)
    if overlay_path is None:
        raise RunOverlayError(
            f"No configuration overlay location for {config_path}: it is not inside a git repository. "
            f"Set {ENV_CONFIG_OVERLAY} to a writable path outside the work tree."
        )
    assert_outside_work_tree(overlay_path, workdir=config_path.parent)

    merged = deep_merge(load_overlay_mapping(overlay_path), updates)
    try:
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a reader mid-run either sees the previous overlay
        # or the new one, never a half-written document.
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=overlay_path.parent,
            prefix=overlay_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(_OVERLAY_HEADER)
            yaml.safe_dump(merged, handle, default_flow_style=False, sort_keys=True)
            tmp_path = Path(handle.name)
        tmp_path.replace(overlay_path)
    except OSError as exc:
        raise RunOverlayError(f"Cannot write configuration overlay {overlay_path}: {exc}") from exc
    return overlay_path


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


def normalize_repo_path(path: str) -> str:
    """Normalise a git-reported path for comparison against the path set."""
    norm = path.strip().replace("\\", "/")
    # Strip only a leading "./", not a stray leading dot: ``lstrip("./")``
    # would turn ``.bernstein/x`` into ``bernstein/x``.
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def is_run_config_path(path: str) -> bool:
    """Return True when *path* is one of the run-configuration paths."""
    return normalize_repo_path(path) in RUN_CONFIG_PATHS


def find_run_config_paths(paths: Iterable[str]) -> list[str]:
    """Return the run-configuration paths among *paths*, order preserved."""
    return [p for p in (normalize_repo_path(raw) for raw in paths if raw.strip()) if p in RUN_CONFIG_PATHS]


def describe_overlay_remedy(paths: Iterable[str]) -> str:
    """Human-readable remedy naming the offending files."""
    named = ", ".join(paths)
    return (
        f"Run configuration must never be part of a change: {named}. "
        f"Put run overrides in the untracked overlay instead "
        f"(<git-dir>/{OVERLAY_RELATIVE_TO_GIT_DIR}, or ${ENV_CONFIG_OVERLAY}), "
        f"then restore the tracked file with: git restore --staged --worktree -- {named}"
    )
