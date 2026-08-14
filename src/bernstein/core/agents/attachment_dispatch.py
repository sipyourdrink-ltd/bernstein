"""Spawn-path dispatch for operator attachments (issue #1797, #3555).

:mod:`bernstein.core.agents.multimodal_attestation` owns the attachment
primitives -- CAS storage, the ``multimodal.attach`` audit-chain event,
the worktree pin, and the authenticated resolver. This module is the
seam that calls them from the agent spawn path, the way
:mod:`bernstein.core.agents.context_attachments` is the seam for declared
context files.

Two operator surfaces feed one dispatch:

* ``bernstein run --attach <path>`` sets :data:`ENV_RUN_ATTACHMENTS`, a
  run-level list that applies to every worker the run spawns.
* A plan step's ``attachments:`` list lands on ``Task.attachments`` and
  applies only to the workers spawned for that task.

Both are collected by :func:`collect_declared_attachments` in a stable
order -- run-level first, then per-task in task order, deduplicated by
path -- so the digests a run records are a function of its inputs and not
of dict iteration order.

Where the state lives
---------------------
CAS and the audit chain are rooted at the *run* root (``<workdir>/.sdd``),
never at the per-session worktree, while the pin recorded in the event is
the worktree id of the spawning worker. That split is what makes the pin
mean anything: one shared chain that every worktree reads, with each
attach row naming the single worktree allowed to resolve it. Per-worktree
chains would turn a cross-worktree read into "no such attachment" instead
of the refusal :class:`WorktreeAccessDenied` is there to raise.

Resume
------
:func:`rebuild_context_for_resume` rebuilds a crashed worker's context
from CAS through :func:`resolve_attachment_for_worker` rather than
re-reading the operator's files. The source file may have been edited or
deleted since the original spawn; the CAS bytes are the ones the chain
attests, so the resumed turn sends byte-identical input and the resolve
is subject to the same worktree pin as any other read.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bernstein.core.agents.multimodal import (
    ModalityType,
    MultiModalContext,
    MultiModalInput,
    detect_modality,
    primary_modality,
)
from bernstein.core.agents.multimodal_attestation import (
    AttachmentChainUnverified,
    WorktreeAccessDenied,
    build_attachment_context,
    resolve_attachment_for_worker,
)
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.skills.catalog.lockfile import worktree_id_for

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.core.agents.multimodal_attestation import AttachmentResolution
    from bernstein.core.tasks.models import AgentSession, Task

logger = logging.getLogger(__name__)

#: Run-level attachment paths exported by ``bernstein run --attach``,
#: ``os.pathsep``-joined. Written by the CLI, read here.
ENV_RUN_ATTACHMENTS = "BERNSTEIN_RUN_ATTACHMENTS"

#: Key under which resolved attachment provenance is stamped onto
#: ``Task.metadata``. Mirrors the ``injected_skills`` stamp so the
#: completion and resume paths can read back what the spawn recorded
#: without re-hashing files that may since have changed.
ATTACHMENT_METADATA_KEY = "multimodal_attachments"

#: Turn sequence recorded for the spawn-time attach. A resumed worker
#: resolves the existing rows rather than appending new ones, so this is
#: the only turn that attaches.
_SPAWN_TURN_SEQ = 0


class AttachmentDispatchError(RuntimeError):
    """Raised when declared attachments cannot be dispatched to a worker.

    Carried to the operator instead of spawning a worker whose input
    silently lacks the bytes -- or the provenance -- that were declared.
    """


@dataclass(frozen=True)
class DispatchedAttachments:
    """What a spawn recorded for its attachments.

    Attributes:
        context: The context to hand the adapter.
        resolutions: Per-attachment provenance in declared order.
        worktree_id: The worktree the attachments are pinned to.
    """

    context: MultiModalContext
    resolutions: tuple[AttachmentResolution, ...]
    worktree_id: str

    @property
    def digests(self) -> list[str]:
        """SHA-256 digests in declared order."""
        return [r.sha256 for r in self.resolutions]


def collect_declared_attachments(
    tasks: Sequence[Task],
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return every attachment path declared for *tasks*, deduplicated.

    Run-level ``--attach`` entries come first (in flag order), then each
    task's declared ``attachments`` in task order. A path declared twice
    -- run-level and again by a task, or by two grouped tasks -- is kept
    once, at its first position, so the same inputs always produce the
    same digest order.

    Args:
        tasks: The tasks being spawned as one worker.
        env: Environment to read :data:`ENV_RUN_ATTACHMENTS` from;
            defaults to ``os.environ``.

    Returns:
        Declared attachment paths in stable order.
    """
    source = os.environ if env is None else env
    declared: list[str] = []
    seen: set[str] = set()

    raw_run_level = source.get(ENV_RUN_ATTACHMENTS, "")
    for entry in raw_run_level.split(os.pathsep):
        path = entry.strip()
        if path and path not in seen:
            declared.append(path)
            seen.add(path)

    for task in tasks:
        for entry in getattr(task, "attachments", ()) or ():
            path = str(entry).strip()
            if path and path not in seen:
                declared.append(path)
                seen.add(path)

    return declared


def _require_readable(declared: Sequence[str]) -> None:
    """Refuse the spawn when a declared attachment is not a readable file.

    ``build_multimodal_context`` logs and skips a missing path, which is
    the right behaviour for an optional context file but the wrong one
    for an explicit ``--attach``: the worker would run text-only, with no
    attach event, and nothing but a log line to say so. Plan-declared
    paths never pass through Click's ``exists=True`` check, so this is
    the only place a typo in ``attachments:`` is caught.
    """
    missing = [p for p in declared if not Path(p).is_file()]
    if missing:
        raise AttachmentDispatchError("declared attachment(s) not found or not a file: " + ", ".join(sorted(missing)))


def dispatch_for_spawn(
    *,
    declared: Sequence[str],
    session_id: str,
    worktree_path: Path,
    run_root: Path,
) -> DispatchedAttachments:
    """Store, attest, and pin the attachments declared for a spawn.

    Args:
        declared: Attachment paths from :func:`collect_declared_attachments`.
            Taken as an argument rather than re-derived so the caller's
            emptiness check and this call cannot disagree about what was
            declared if the environment changes between them.
        session_id: The worker id recorded as the attaching actor.
        worktree_path: The worktree the worker will run in; its id is the
            pin recorded in the audit-chain event.
        run_root: The run's working directory. CAS and the audit chain
            live under ``<run_root>/.sdd`` so every worktree shares one
            chain.

    Returns:
        The dispatch record.

    Raises:
        AttachmentDispatchError: *declared* is empty, a declared path is
            missing, or the provenance could not be recorded.
    """
    if not declared:
        raise AttachmentDispatchError("dispatch_for_spawn called with no declared attachments")

    _require_readable(declared)

    sdd = Path(run_root) / ".sdd"
    worktree_id = worktree_id_for(Path(worktree_path))
    try:
        result = build_attachment_context(
            attachments=list(declared),
            worker_id=session_id,
            turn_seq=_SPAWN_TURN_SEQ,
            worktree_id=worktree_id,
            cas=CASStore(sdd / "cas"),
            audit_chain=AuditChainStore(sdd / "audit"),
        )
    except OSError as exc:
        # A failure to store the bytes or append the event means the
        # worker would consume an image with no provenance behind it.
        # That is the exact gap this path exists to close, so it fails
        # the spawn rather than proceeding unattested.
        raise AttachmentDispatchError(f"could not record attachment provenance: {exc}") from exc

    if len(result.resolutions) != len(declared):
        recorded = {r.source_path for r in result.resolutions}
        dropped = [p for p in declared if str(Path(p)) not in recorded]
        raise AttachmentDispatchError("attachment(s) could not be encoded or attested: " + ", ".join(sorted(dropped)))

    logger.info(
        "Attached %d file(s) for session %s pinned to worktree %s: %s",
        len(result.resolutions),
        session_id,
        worktree_id,
        ", ".join(r.sha256[:12] for r in result.resolutions),
    )
    return DispatchedAttachments(
        context=result.context,
        resolutions=result.resolutions,
        worktree_id=worktree_id,
    )


def stamp_dispatch(
    session: AgentSession,
    tasks: Sequence[Task],
    dispatched: DispatchedAttachments,
) -> None:
    """Record *dispatched* on the session and every task it covers.

    The stamp is what later phases read instead of re-deriving digests
    from disk: :func:`attachment_digests_for_tasks` supplies them to the
    signed lineage entry at artifact-completion time, and
    :func:`rebuild_context_for_resume` reads it to resolve the bytes back
    out of CAS. Mirrors the ``injected_skills`` stamp, and inherits its
    limitation: the record is only as durable as the last persisted task
    write.
    """
    record = [
        {
            "sha256": r.sha256,
            "mime": r.mime,
            "worktree_id": r.worktree_id,
            "source_path": r.source_path,
            "modality": str(detect_modality(r.source_path)),
        }
        for r in dispatched.resolutions
    ]
    session.multimodal_attachments = [dict(entry) for entry in record]
    for task in tasks:
        task.metadata[ATTACHMENT_METADATA_KEY] = [dict(entry) for entry in record]


def _stamped_records(task: Task) -> list[dict[str, str]]:
    """Return the attachment records stamped on *task*, or an empty list.

    Tolerant of a malformed stamp: task metadata round-trips through the
    backlog YAML, so an operator-edited file can put anything here. A
    non-conforming payload yields no records rather than raising in the
    completion path.
    """
    raw: object = task.metadata.get(ATTACHMENT_METADATA_KEY)
    if not isinstance(raw, list):
        return []
    records: list[dict[str, str]] = []
    for entry in cast("list[object]", raw):
        if isinstance(entry, dict):
            records.append({str(k): str(v) for k, v in cast("dict[object, object]", entry).items()})
    return records


def attachment_digests_for_tasks(tasks: Sequence[Task]) -> list[str]:
    """Return the attachment digests stamped across *tasks*, deduplicated."""
    digests: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        for entry in _stamped_records(task):
            digest = str(entry.get("sha256", ""))
            if digest and digest not in seen:
                digests.append(digest)
                seen.add(digest)
    return digests


def rebuild_context_for_resume(
    *,
    tasks: Sequence[Task],
    worktree_path: Path,
    run_root: Path,
) -> MultiModalContext | None:
    """Rebuild a resumed worker's attachment context from attested bytes.

    Reads through :func:`resolve_attachment_for_worker`, so the resume is
    held to the same worktree pin and the same authenticated-chain
    requirement as any other attachment read: a resume in a different
    worktree, or against a chain whose HMAC linkage no longer holds, gets
    no bytes rather than unverified ones.

    Args:
        tasks: The tasks being resumed.
        worktree_path: The preserved worktree the worker resumes in.
        run_root: The run's working directory.

    Returns:
        The rebuilt context, or ``None`` when the tasks recorded no
        attachments.

    Raises:
        AttachmentDispatchError: The recorded bytes could not be
            resolved back for this worktree.
    """
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for task in tasks:
        for entry in _stamped_records(task):
            digest = str(entry.get("sha256", ""))
            if digest and digest not in seen:
                records.append(entry)
                seen.add(digest)
    if not records:
        return None

    sdd = Path(run_root) / ".sdd"
    cas = CASStore(sdd / "cas")
    chain = AuditChainStore(sdd / "audit")
    requesting_worktree_id = worktree_id_for(Path(worktree_path))

    inputs: list[MultiModalInput] = []
    for entry in records:
        digest = str(entry["sha256"])
        try:
            raw = resolve_attachment_for_worker(
                sha256=digest,
                requesting_worktree_id=requesting_worktree_id,
                cas=cas,
                audit_chain=chain,
            )
        except (WorktreeAccessDenied, AttachmentChainUnverified, FileNotFoundError) as exc:
            raise AttachmentDispatchError(f"could not resolve attachment {digest[:12]}... on resume: {exc}") from exc
        source_path = str(entry.get("source_path", ""))
        raw_modality = str(entry.get("modality", ""))
        try:
            modality = ModalityType(raw_modality)
        except ValueError:
            modality = detect_modality(source_path) if source_path else ModalityType.TEXT
        inputs.append(
            MultiModalInput(
                modality=modality,
                content_path=Path(source_path) if source_path else None,
                content_base64=base64.b64encode(raw).decode("ascii"),
                mime_type=str(entry.get("mime", "application/octet-stream")),
                description=Path(source_path).name if source_path else digest[:12],
            )
        )

    return MultiModalContext(inputs=tuple(inputs), primary_modality=primary_modality(inputs))


__all__ = [
    "ATTACHMENT_METADATA_KEY",
    "ENV_RUN_ATTACHMENTS",
    "AttachmentDispatchError",
    "DispatchedAttachments",
    "attachment_digests_for_tasks",
    "collect_declared_attachments",
    "dispatch_for_spawn",
    "rebuild_context_for_resume",
    "stamp_dispatch",
]
