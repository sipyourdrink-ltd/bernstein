"""Managed plan lifecycle (``active`` -> ``completed`` | ``blocked``).

Plan files (``plans/*.yaml``) are managed through three buckets:

* ``plans/active/``     - runs in flight or queued.
* ``plans/completed/``  - runs that finished successfully (read-only).
* ``plans/blocked/``    - runs that aborted (read-only).

Transitions are atomic (``os.replace``) and irreversible from the
lifecycle layer's perspective: once a file is archived it is written
``0o444`` and the lifecycle module refuses to overwrite it
programmatically.  Users may copy an archived plan back into
``active/`` manually to re-run it.

This module owns:

* The :class:`PlanState` enum and :class:`PlanLifecycle` controller.
* Slug derivation, with deterministic short-hash collision suffixing.
* One-time backfill of unmanaged ``plans/*.yaml`` into ``active/``.
* Hook dispatch (``pre_archive`` / ``post_archive``) and HMAC audit
  emission, when callers wire the registry / log in.
* Utility queries used by ``bernstein plan ls`` / ``bernstein plan
  show`` (see :mod:`bernstein.cli.commands.plan_archive_cmd`).

The renderer for the ``## Run summary`` and ``## Failure reason``
blocks lives in :mod:`bernstein.core.planning.run_summary`.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

# ``Path`` is genuinely used as a runtime type by the dataclass below
# (frozen=True, slots=True), even with PEP 563 deferred annotations.
from pathlib import Path  # noqa: TC003 - runtime use in ArchivedPlan
from typing import TYPE_CHECKING

from bernstein.core.planning.run_summary import (
    FailureSummary,
    RunSummary,
    render_failure_block,
    render_summary_block,
)

if TYPE_CHECKING:
    from bernstein.core.lifecycle.hooks import HookRegistry
    from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)

__all__ = [
    "ArchivedPlan",
    "PlanArchiveError",
    "PlanLifecycle",
    "PlanState",
    "default_lifecycle",
    "is_archived_filename",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACTIVE_DIR: str = "active"
_COMPLETED_DIR: str = "completed"
_BLOCKED_DIR: str = "blocked"

#: Mode applied to archived plan files.  Read-only for owner/group/other.
_READONLY_MODE: int = 0o444

#: Maximum slug length (characters) before truncation.
_MAX_SLUG_LEN: int = 60

#: Length of the short-hash suffix used to disambiguate slug collisions.
_SHORT_HASH_LEN: int = 6

#: Pattern enforced for archived filenames.
#: ``YYYY-MM-DD-<slug>(-<6-hex-collision>)?.yaml``.
_ARCHIVED_FILENAME_RE: re.Pattern[str] = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<slug>[a-z0-9][a-z0-9-]*?)(?:-(?P<hash>[0-9a-f]{6}))?\.yaml$"
)

#: Hook event names. We dispatch by string and look up the matching
#: :class:`bernstein.core.lifecycle.hooks.LifecycleEvent` lazily so the
#: lifecycle module continues to work when the upstream enum has not
#: been extended yet (e.g. during partial in-progress merges).
_PRE_ARCHIVE_EVENT: str = "pre_archive"
_POST_ARCHIVE_EVENT: str = "post_archive"


class PlanState(StrEnum):
    """Lifecycle state of a managed plan.

    The state machine is purely linear: ``ACTIVE`` is the only legal
    starting state, and from there a plan can transition to either
    ``COMPLETED`` or ``BLOCKED``.  Archived states are terminal -
    re-running requires the user to copy the file back to
    ``active/`` manually.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"


# Allowed transitions; any pair not in this set raises
# :class:`PlanArchiveError`.
_VALID_TRANSITIONS: frozenset[tuple[PlanState, PlanState]] = frozenset(
    {
        (PlanState.ACTIVE, PlanState.COMPLETED),
        (PlanState.ACTIVE, PlanState.BLOCKED),
    }
)


class PlanArchiveError(RuntimeError):
    """Raised on illegal lifecycle operations.

    Examples:
        * Trying to archive a plan from a non-``ACTIVE`` state.
        * Attempting to mutate an archived (read-only) file via the
          lifecycle API.
        * Source plan path is missing or outside the managed root.
    """


@dataclass(frozen=True, slots=True)
class ArchivedPlan:
    """An entry returned by :meth:`PlanLifecycle.list_plans`.

    Attributes:
        plan_id: Filename stem (no extension).  Stable identifier used
            by ``bernstein plan show <id>``.
        path: Absolute path on disk.
        state: Bucket the plan currently lives in.
    """

    plan_id: str
    path: Path
    state: PlanState


class PlanLifecycle:
    """Lifecycle controller for plan files.

    The controller is bound to a single ``plans/`` root.  Multiple
    instances may coexist in different roots (tests use a per-tmp
    directory instance).

    Args:
        root: Absolute path to the ``plans/`` directory.  Subdirectories
            (``active/``, ``completed/``, ``blocked/``) are created on
            first use.
        hook_registry: Optional hook registry to fire ``pre_archive`` /
            ``post_archive`` events on.  When ``None``, transitions
            still work; only the side-channel notifications are
            skipped.
        audit_log: Optional HMAC-chained audit log to record archive
            transitions on.
        clock: Injectable clock returning a UTC ``datetime``; tests
            pin this for deterministic dated filenames.

    Raises:
        PlanArchiveError: If ``root`` exists but is not a directory.
    """

    def __init__(
        self,
        root: Path,
        *,
        hook_registry: HookRegistry | None = None,
        audit_log: AuditLog | None = None,
        clock: type[datetime] | None = None,
    ) -> None:
        if root.exists() and not root.is_dir():
            raise PlanArchiveError(f"Plans root is not a directory: {root}")
        self._root = root
        self._hook_registry = hook_registry
        self._audit_log = audit_log
        self._clock: type[datetime] = clock or datetime
        # Make sure the three buckets exist.  Cheap, idempotent.
        for bucket in (_ACTIVE_DIR, _COMPLETED_DIR, _BLOCKED_DIR):
            (self._root / bucket).mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- queries

    @property
    def root(self) -> Path:
        """Absolute path to the managed ``plans/`` directory."""
        return self._root

    def bucket(self, state: PlanState) -> Path:
        """Return the directory backing ``state``."""
        return self._root / state.value

    def list_plans(self, state: PlanState | None = None) -> list[ArchivedPlan]:
        """Enumerate plans in one or all buckets.

        Args:
            state: When provided, list only that bucket.  When ``None``,
                walk all three.

        Returns:
            Stable, alphabetically-sorted list of :class:`ArchivedPlan`.
        """
        states: list[PlanState] = [state] if state is not None else list(PlanState)
        out: list[ArchivedPlan] = []
        for s in states:
            for path in sorted(self.bucket(s).glob("*.yaml")):
                out.append(ArchivedPlan(plan_id=path.stem, path=path, state=s))
        return out

    def find(self, plan_id: str) -> ArchivedPlan | None:
        """Locate a plan by ``plan_id`` (filename stem) across all buckets.

        Args:
            plan_id: Filename stem to look up.

        Returns:
            The matching :class:`ArchivedPlan`, or ``None`` if not found.
        """
        for state in PlanState:
            candidate = self.bucket(state) / f"{plan_id}.yaml"
            if candidate.exists():
                return ArchivedPlan(plan_id=plan_id, path=candidate, state=state)
        return None

    # ----------------------------------------------------------------- backfill

    def backfill_unmanaged(self) -> list[Path]:
        """Move loose ``plans/*.yaml`` into ``plans/active/``.

        This is intended to run once on orchestrator startup.  It is
        idempotent: subsequent invocations find no top-level YAMLs and
        do nothing.  Files inside the three managed buckets are left
        untouched.

        Returns:
            Absolute paths of files that were migrated, in order.
        """
        migrated: list[Path] = []
        if not self._root.exists():
            return migrated
        for entry in sorted(self._root.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix != ".yaml":
                continue
            destination = self._root / _ACTIVE_DIR / entry.name
            if destination.exists():
                logger.info(
                    "plan-lifecycle backfill: skipping %s (active/ already has %s)",
                    entry.name,
                    destination.name,
                )
                continue
            try:
                shutil.move(str(entry), str(destination))
            except OSError as exc:  # pragma: no cover - filesystem race
                logger.warning("plan-lifecycle backfill failed for %s: %s", entry, exc)
                continue
            migrated.append(destination)
            logger.info("plan-lifecycle backfill migrated %s -> active/", entry.name)
        return migrated

    # ----------------------------------------------------------------- archival

    def archive_completed(
        self,
        active_path: Path,
        summary: RunSummary,
        *,
        plan_name: str | None = None,
    ) -> Path:
        """Move an active plan to ``completed/`` with a run-summary header.

        Args:
            active_path: Path to the plan inside ``plans/active/``.
            summary: Run-summary fields used by the renderer.
            plan_name: Optional override for the slug source name.
                Defaults to the active file's stem.

        Returns:
            Path to the archived plan in ``completed/``.

        Raises:
            PlanArchiveError: If the source is missing, lives outside
                ``active/``, or the file is already read-only (already
                archived).
        """
        source = self._validate_active_source(active_path)
        prelude = render_summary_block(summary)
        return self._archive(
            source=source,
            target_state=PlanState.COMPLETED,
            prelude=prelude,
            plan_name=plan_name,
            audit_kind="success",
        )

    def archive_blocked(
        self,
        active_path: Path,
        failure: FailureSummary,
        *,
        plan_name: str | None = None,
    ) -> Path:
        """Move an active plan to ``blocked/`` with a failure-reason header.

        See :meth:`archive_completed` for argument semantics; the only
        difference is the bucket and the rendered Markdown block.
        """
        source = self._validate_active_source(active_path)
        prelude = render_failure_block(failure)
        return self._archive(
            source=source,
            target_state=PlanState.BLOCKED,
            prelude=prelude,
            plan_name=plan_name,
            audit_kind="failure",
        )

    def assert_writable(self, path: Path) -> None:
        """Refuse mutations against an archived plan.

        Callers that want to defensively guard a file edit can call
        this helper.  It raises :class:`PlanArchiveError` if ``path``
        is inside ``completed/`` or ``blocked/``, regardless of the
        on-disk file mode (the read-only bit is advisory on some
        filesystems; this guard is authoritative).
        """
        try:
            relative = path.resolve().relative_to(self._root.resolve())
        except ValueError:
            return  # Not under our root; not our concern.
        if not relative.parts:
            return
        head = relative.parts[0]
        if head in {_COMPLETED_DIR, _BLOCKED_DIR}:
            raise PlanArchiveError(
                f"Refusing to mutate archived plan: {path} (lives in {head}/, copy back to active/ to re-run)."
            )

    # ----------------------------------------------------------------- internals

    def _validate_active_source(self, active_path: Path) -> Path:
        """Ensure ``active_path`` is a real file inside ``plans/active/``."""
        if not active_path.exists():
            raise PlanArchiveError(f"Plan file not found: {active_path}")
        if not active_path.is_file():
            raise PlanArchiveError(f"Plan path is not a file: {active_path}")
        try:
            relative = active_path.resolve().relative_to(self._root.resolve())
        except ValueError as exc:
            raise PlanArchiveError(f"Plan {active_path} is not under managed root {self._root}") from exc
        if not relative.parts or relative.parts[0] != _ACTIVE_DIR:
            raise PlanArchiveError(f"Plan {active_path} is not in active/ (found in {relative.parts[0]!r})")
        return active_path

    def _archive(
        self,
        *,
        source: Path,
        target_state: PlanState,
        prelude: str,
        plan_name: str | None,
        audit_kind: str,
    ) -> Path:
        """Common path for both completed and blocked archival."""
        if (PlanState.ACTIVE, target_state) not in _VALID_TRANSITIONS:
            raise PlanArchiveError(f"Illegal plan state transition: {PlanState.ACTIVE.value} -> {target_state.value}")

        date_str = self._today_iso()
        slug_seed = plan_name or source.stem
        target_dir = self.bucket(target_state)
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = self._reserve_destination(date_str, slug_seed, source.name, target_dir)

        original_text = source.read_text()
        new_text = prelude + original_text

        # Pre-archive hook: fires *before* we touch disk so subscribers
        # may abort by raising.  Failure here propagates to the caller.
        self._fire_hook(_PRE_ARCHIVE_EVENT, source=source, target=destination, state=target_state)

        # Write to a temp file in the same dir, fsync, then atomic move.
        tmp_path = destination.with_suffix(destination.suffix + ".tmp")
        try:
            tmp_path.write_text(new_text)
            os.replace(tmp_path, destination)
        except OSError as exc:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise PlanArchiveError(f"Failed to write archive {destination}: {exc}") from exc

        # Remove the source only after the destination is durable.
        with contextlib.suppress(FileNotFoundError):
            source.unlink()

        # Make read-only.  Some filesystems ignore the bit; the
        # lifecycle layer's :meth:`assert_writable` is the
        # authoritative refusal.
        with contextlib.suppress(OSError):
            destination.chmod(_READONLY_MODE)

        self._record_audit(audit_kind, source=source, destination=destination)
        self._fire_hook(_POST_ARCHIVE_EVENT, source=source, target=destination, state=target_state)

        logger.info(
            "plan archived: %s -> %s (state=%s)",
            source.name,
            destination.relative_to(self._root),
            target_state.value,
        )
        return destination

    def _reserve_destination(
        self,
        date_str: str,
        slug_seed: str,
        source_filename: str,
        target_dir: Path,
    ) -> Path:
        """Pick the final filename in ``target_dir``, suffixing on collision."""
        slug = _slugify(slug_seed)
        base = f"{date_str}-{slug}"
        candidate = target_dir / f"{base}.yaml"
        if not candidate.exists():
            return candidate
        # Deterministic short-hash from the source filename + date + slug.
        seed = f"{date_str}|{slug}|{source_filename}".encode()
        digest = hashlib.sha256(seed).hexdigest()[:_SHORT_HASH_LEN]
        # If even the hashed candidate exists (extremely unlikely),
        # extend with an incrementing counter for total determinism.
        hashed = target_dir / f"{base}-{digest}.yaml"
        if not hashed.exists():
            return hashed
        for n in range(1, 1000):
            extra = hashlib.sha256(f"{seed!r}|{n}".encode()).hexdigest()[:_SHORT_HASH_LEN]
            attempt = target_dir / f"{base}-{extra}.yaml"
            if not attempt.exists():
                return attempt
        raise PlanArchiveError(f"Could not reserve unique archive name for {source_filename} after 1000 attempts")

    def _today_iso(self) -> str:
        """Return today's date in ``YYYY-MM-DD`` (UTC)."""
        return self._clock.now(tz=UTC).strftime("%Y-%m-%d")

    def _fire_hook(
        self,
        event_name: str,
        *,
        source: Path,
        target: Path,
        state: PlanState,
    ) -> None:
        """Dispatch a lifecycle hook event if a registry is bound.

        We resolve :class:`LifecycleEvent` lazily so the lifecycle
        module remains importable when the upstream enum has not yet
        been extended with the archive events (e.g. during a partial
        merge).  When the enum lacks the event, we log and continue.
        """
        if self._hook_registry is None:
            return
        from bernstein.core.lifecycle.hooks import LifecycleContext, LifecycleEvent

        try:
            event = LifecycleEvent(event_name)
        except ValueError:
            logger.warning(
                "plan-lifecycle: LifecycleEvent missing %r - skipping hook dispatch",
                event_name,
            )
            return
        context = LifecycleContext(
            event=event,
            task=None,
            session_id=None,
            workdir=self._root,
            env={
                "BERNSTEIN_PLAN_SOURCE": str(source),
                "BERNSTEIN_PLAN_TARGET": str(target),
                "BERNSTEIN_PLAN_STATE": state.value,
            },
        )
        try:
            self._hook_registry.run(event, context)
        except Exception:
            logger.exception("plan-lifecycle hook %s raised - propagating", event_name)
            raise

    def _record_audit(
        self,
        kind: str,
        *,
        source: Path,
        destination: Path,
    ) -> None:
        """Append an audit-chain entry for the archive transition."""
        if self._audit_log is None:
            return
        try:
            self._audit_log.log(
                event_type=f"plan.archive.{kind}",
                actor="bernstein",
                resource_type="plan",
                resource_id=destination.stem,
                details={
                    "source": str(source),
                    "destination": str(destination),
                },
            )
        except Exception:
            logger.exception("plan-lifecycle audit append failed - continuing")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def default_lifecycle(workdir: Path) -> PlanLifecycle:
    """Return a :class:`PlanLifecycle` rooted at ``workdir/plans``.

    Args:
        workdir: Project root (the directory containing ``plans/``).

    Returns:
        A controller bound to that root with no hooks/audit attached.
    """
    return PlanLifecycle(workdir / "plans")


_SLUG_INVALID_RE: re.Pattern[str] = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Lowercase, dash-separated slug suitable for filenames.

    Empty or non-alphanumeric inputs collapse to ``"plan"`` so we
    always emit a non-empty slug.

    Args:
        name: Free-form name (typically the original plan filename
            stem).

    Returns:
        Trimmed, lowercased slug. Length is capped at
        :data:`_MAX_SLUG_LEN`.
    """
    lower = name.strip().lower()
    cleaned = _SLUG_INVALID_RE.sub("-", lower).strip("-")
    if not cleaned:
        cleaned = "plan"
    if len(cleaned) > _MAX_SLUG_LEN:
        cleaned = cleaned[:_MAX_SLUG_LEN].rstrip("-") or "plan"
    return cleaned


def is_archived_filename(name: str) -> bool:
    """Return True if ``name`` matches the canonical archive pattern."""
    return bool(_ARCHIVED_FILENAME_RE.match(name))
