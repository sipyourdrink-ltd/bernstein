"""Image-attachment passthrough with provenance (issue #1797).

This module wires the operator-supplied ``--attach <path>`` flow into:

* :mod:`bernstein.core.agents.multimodal` -- the existing
  :class:`MultiModalContext` and ``encode_input`` helpers stay the
  source of truth for base64 encoding and modality detection.
* :mod:`bernstein.core.persistence.cas_store` -- raw attachment bytes
  are stored once by SHA-256, so duplicate attachments dedupe and a
  replay path retrieves the exact bytes that the model API saw.
* :mod:`bernstein.core.security.audit_chain` -- every attach call
  records an HMAC-chained ``multimodal.attach`` event carrying the
  bytes' SHA-256, MIME, worker identity, turn sequence, worktree id,
  the operator install signature, and the prior chain digest.
* :mod:`bernstein.core.persistence.lineage_signer` -- the worker's
  lineage v1 receipt is augmented with attachment digests in its
  ``parents`` list via :func:`worker_lineage_parents`.

The :func:`refuse_when_incapable` helper performs capability gating
BEFORE any process is launched: if the selected adapter does not
report ``is_multimodal_capable() == True`` and at least one
attachment is present, a :class:`CapabilityRefusal` is raised whose
``suggested_adapters`` field names adapters that do support
attachments. The orchestrator surfaces this as a structured error
rather than a stack trace.

Worktree pinning
----------------
An attachment is stored in CAS at SHA-256 time but only resolves back
to bytes for workers in the same worktree it was attached from. The
worktree id is embedded in the ``multimodal.attach`` event payload;
:func:`resolve_attachment_for_worker` consults the chain on lookup
and raises :class:`WorktreeAccessDenied` for any cross-worktree
attempt. That lookup reads through
:meth:`AuditChainStore.scan_verified`, so the access decision rests
only on rows whose HMAC linkage held: a ``multimodal.attach`` row
appended by a writer without the audit key names a worktree the chain
never authenticated, and the resolve fails closed with
:class:`AttachmentChainUnverified` instead of handing over another
worktree's bytes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path  # runtime use in encode_one
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from bernstein.core.agents.multimodal import (
    MultiModalContext,
    build_multimodal_context,
    encode_input,
    is_multimodal_capable,
)
from bernstein.core.identity.install_rev import get_install_rev
from bernstein.core.persistence.lineage_signer import (
    build_attachment_parent_uri,
)
from bernstein.core.security.audit_chain import (
    EVENT_MULTIMODAL_ATTACH,
    record_multimodal_attach,
)

if TYPE_CHECKING:
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.security.audit import ChainScanCursor
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


#: Adapters that advertise ``is_multimodal_capable() == True``. Kept
#: as a sorted tuple so error messages are deterministic.
_CAPABLE_SUGGESTIONS: tuple[str, ...] = ("claude", "gemini")


class CapabilityRefusal(RuntimeError):
    """Raised when an incapable adapter is asked to consume attachments.

    Attributes:
        adapter_name: The adapter that was asked.
        suggested_adapters: Adapter names that DO support attachments.
    """

    def __init__(self, adapter_name: str, suggested_adapters: tuple[str, ...]) -> None:
        self.adapter_name = adapter_name
        self.suggested_adapters = suggested_adapters
        super().__init__(
            f"Adapter {adapter_name!r} does not support multimodal attachments. "
            f"Suggested capable adapters: {', '.join(suggested_adapters)}."
        )


def refuse_when_incapable(
    *,
    adapter_name: str,
    attachments: list[str] | tuple[str, ...],
) -> None:
    """Raise :class:`CapabilityRefusal` for incapable + non-empty combos.

    Args:
        adapter_name: Registry name of the selected adapter
            (case-insensitive).
        attachments: Iterable of operator-supplied attachment paths.

    Raises:
        CapabilityRefusal: When the adapter is not multimodal-capable
            and at least one attachment is present.
    """
    if not attachments:
        return
    if is_multimodal_capable(adapter_name):
        return
    raise CapabilityRefusal(adapter_name=adapter_name, suggested_adapters=_CAPABLE_SUGGESTIONS)


# ---------------------------------------------------------------------------
# AttachmentResolution dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentResolution:
    """A single attachment, resolved at spawn time.

    Attributes:
        sha256: Hex digest of the attachment bytes.
        mime: MIME type as resolved by :func:`encode_input`.
        worktree_id: The worktree this attachment is pinned to.
        source_path: Original on-disk path (for diagnostics).
    """

    sha256: str
    mime: str
    worktree_id: str
    source_path: str


@dataclass(frozen=True)
class AttachmentBuildResult:
    """Aggregate result returned by :func:`build_attachment_context`.

    Attributes:
        context: The :class:`MultiModalContext` to pass to the adapter.
        resolutions: Per-attachment provenance records in input order.
    """

    context: MultiModalContext
    resolutions: tuple[AttachmentResolution, ...]


# ---------------------------------------------------------------------------
# Operator install identity signature
# ---------------------------------------------------------------------------


def _operator_install_id_sig() -> str:
    """Return a stable per-install identity signature for the audit record.

    The signature is derived from the same install-rev token surfaced by
    :func:`bernstein.core.identity.install_rev.get_install_rev`. The
    returned value is a hex SHA-256 over the install rev so that a raw
    install token never appears in plain text inside the audit chain.
    """
    rev = get_install_rev()
    return hashlib.sha256(rev.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Build context at spawn time
# ---------------------------------------------------------------------------


def build_attachment_context(
    *,
    attachments: list[str] | tuple[str, ...],
    worker_id: str,
    turn_seq: int,
    worktree_id: str,
    cas: CASStore,
    audit_chain: AuditChainStore,
) -> AttachmentBuildResult:
    """Read attachments from disk, store bytes in CAS, and record events.

    For each ``attachments`` entry the helper:

    1. Encodes the file into a :class:`MultiModalInput` via
       :func:`encode_input` (the same code path the adapters use).
    2. Computes the SHA-256 of the *raw* file bytes and stores them in
       *cas* so a replay path can fetch the exact bytes that were sent
       to the model API.
    3. Appends a ``multimodal.attach`` event to *audit_chain* carrying
       the SHA-256, MIME type, operator install signature, worker id,
       turn sequence, worktree id, and the previous chain digest.

    Args:
        attachments: Operator-supplied attachment paths.
        worker_id: Id of the worker that will consume the attachment.
        turn_seq: Monotonic turn sequence number for the worker.
        worktree_id: Worktree the worker runs in.
        cas: Content-addressed blob store (reused, not re-created).
        audit_chain: Audit chain store.

    Returns:
        An :class:`AttachmentBuildResult` carrying the multimodal
        context (ready to pass to an adapter) and the per-attachment
        provenance records.
    """
    if not attachments:
        return AttachmentBuildResult(
            context=build_multimodal_context([]),
            resolutions=(),
        )

    paths: list[str | Path] = list(attachments)
    context = build_multimodal_context(paths)

    resolutions: list[AttachmentResolution] = []
    operator_sig = _operator_install_id_sig()
    for inp in context.inputs:
        if inp.content_path is None or not inp.content_base64:
            # build_multimodal_context skipped a missing file or could
            # not produce a base64 payload; nothing to anchor in CAS /
            # the chain. Skip provenance recording so downstream
            # callers see no resolution for it.
            continue
        # Hash the bytes that will actually travel to the model API,
        # not a separate re-read of the source file. The base64
        # payload in ``content_base64`` IS what the adapter inlines in
        # the request body; decoding it here gives us the identical
        # bytes for CAS + the audit-chain digest, eliminating the race
        # where the on-disk file changes between encode time and
        # attest time. (bot-ack: 3284182756 -- CodeRabbit critical.)
        try:
            raw_bytes = base64.b64decode(inp.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Skipping attachment %s: invalid base64 payload (%s)",
                inp.content_path,
                exc,
            )
            continue
        digest = hashlib.sha256(raw_bytes).hexdigest()
        cas.put(
            raw_bytes,
            content_type=inp.mime_type,
            metadata={
                "source_path": str(inp.content_path),
                "worktree_id": worktree_id,
                "worker_id": worker_id,
            },
        )
        record_multimodal_attach(
            chain=audit_chain,
            sha256=digest,
            mime=inp.mime_type,
            operator_install_id_sig=operator_sig,
            worker_id=worker_id,
            turn_seq=turn_seq,
            worktree_id=worktree_id,
        )
        resolutions.append(
            AttachmentResolution(
                sha256=digest,
                mime=inp.mime_type,
                worktree_id=worktree_id,
                source_path=str(inp.content_path),
            )
        )

    return AttachmentBuildResult(context=context, resolutions=tuple(resolutions))


# ---------------------------------------------------------------------------
# Encode a single file (test convenience)
# ---------------------------------------------------------------------------


def encode_one(file_path: str | Path) -> tuple[str, str, str]:
    """Encode a single attachment for adapter consumption.

    Returns ``(base64_content, mime_type, sha256_digest)``. The digest is
    over the raw file bytes -- so it matches what
    :func:`build_attachment_context` records in the audit chain.
    """
    inp = encode_input(file_path)
    raw = Path(file_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    return (inp.content_base64 or "", inp.mime_type, digest)


# ---------------------------------------------------------------------------
# Worktree-pinned resolver
# ---------------------------------------------------------------------------


class AttachmentChainUnverified(RuntimeError):
    """Raised when the chain backing an attachment lookup does not authenticate.

    The worktree pin is only as strong as the rows it is read from. When the
    HMAC linkage over the audit chain does not hold, no ``multimodal.attach``
    row can be attributed to a worktree, so the resolve refuses rather than
    falling back to whatever the log happens to contain.

    Attributes:
        sha256: Digest whose resolve was refused.
        errors: Per-entry verification errors reported by the chain scan.
    """

    def __init__(self, sha256: str, errors: list[str]) -> None:
        self.sha256 = sha256
        self.errors = errors
        super().__init__(
            f"Refusing to resolve attachment {sha256[:12]}... from an unverified audit chain: " + "; ".join(errors[:3])
        )


class WorktreeAccessDenied(RuntimeError):
    """Raised when a worker in worktree B requests an attachment from A."""

    def __init__(self, sha256: str, attached_worktree: str, requesting_worktree: str) -> None:
        self.sha256 = sha256
        self.attached_worktree = attached_worktree
        self.requesting_worktree = requesting_worktree
        super().__init__(
            f"Attachment {sha256[:12]}... was attached in worktree "
            f"{attached_worktree!r} but worker in worktree "
            f"{requesting_worktree!r} attempted to resolve it. Cross-worktree "
            "access is denied."
        )


class _VerifiedAttachIndex:
    """Attaching worktrees per digest, built only from authenticated rows.

    The index is fed by :meth:`AuditChainStore.scan_verified`, which
    recomputes the HMAC of exactly the bytes it reads and refuses the whole
    scan when the linkage breaks. Keeping the returned cursor here is what
    makes an authenticated read affordable on this path: the first lookup
    walks the chain once, and every later lookup verifies and parses only the
    bytes appended since, so a per-attachment resolve costs O(new rows)
    instead of O(entire chain).

    The index lives beside the cursor because the two must be discarded
    together: when a scan reports ``rescanned`` the history under the cursor
    changed, and any mapping derived from the old prefix is no longer backed
    by rows this process authenticated.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor: ChainScanCursor | None = None
        self._owners: dict[str, set[str]] = {}

    def owners_of(self, chain: AuditChainStore, sha256: str) -> frozenset[str]:
        """Return the worktree ids that verifiably attached *sha256*.

        Raises:
            AttachmentChainUnverified: The chain scan did not authenticate.
        """
        with self._lock:
            result = chain.scan_verified(self._cursor, event_type=EVENT_MULTIMODAL_ATTACH)
            if not result.ok:
                # Leave the cursor where it was by not adopting ``result.cursor``:
                # the failed scan consumed bytes it could not authenticate, so
                # advancing past them would let the next lookup treat the damaged
                # span as verified history and go quiet about a break it already
                # found. ``scan_verified`` walks a copy of what it is handed
                # (:meth:`ChainScanCursor.working_copy`), so ``self._cursor``
                # really is still the pre-scan resume point here and the next
                # lookup re-verifies -- and re-refuses -- the same bytes.
                raise AttachmentChainUnverified(sha256=sha256, errors=result.errors)
            if result.rescanned:
                self._owners.clear()
            for event in result.events:
                if event.event_type != EVENT_MULTIMODAL_ATTACH:
                    continue
                digest = str(event.details.get("sha256", ""))
                if not digest:
                    continue
                self._owners.setdefault(digest, set()).add(str(event.details.get("worktree_id", "")))
            self._cursor = result.cursor
            return frozenset(self._owners.get(sha256, frozenset()))


#: One index per :class:`AuditChainStore` instance. The cursor is only
#: meaningful against the chain it was produced from, so the store is the
#: correct owner; the mapping is weak-keyed so a store that goes out of scope
#: takes its cursor and index with it rather than pinning them for the life of
#: the process.
_ATTACH_INDEXES: WeakKeyDictionary[AuditChainStore, _VerifiedAttachIndex] = WeakKeyDictionary()
_ATTACH_INDEXES_LOCK = threading.Lock()


def _attach_index_for(chain: AuditChainStore) -> _VerifiedAttachIndex:
    """Return (creating on first use) the authenticated attach index for *chain*."""
    with _ATTACH_INDEXES_LOCK:
        index = _ATTACH_INDEXES.get(chain)
        if index is None:
            index = _VerifiedAttachIndex()
            _ATTACH_INDEXES[chain] = index
        return index


def resolve_attachment_for_worker(
    *,
    sha256: str,
    requesting_worktree_id: str,
    cas: CASStore,
    audit_chain: AuditChainStore,
) -> bytes:
    """Return attached bytes if the requesting worktree owns the attach.

    Looks up the ``multimodal.attach`` events matching ``sha256``. If one of
    them pins the attachment to ``requesting_worktree_id`` the bytes are
    returned from CAS; otherwise :class:`WorktreeAccessDenied` is raised.

    Events are read through :meth:`AuditChainStore.scan_verified`, never
    through ``query()``. ``query()`` performs no HMAC checking, so a
    ``multimodal.attach`` row appended by anything that can write the audit
    directory -- without the audit key, and therefore without valid chain
    linkage -- would name any worktree it likes and hand that worktree another
    worktree's attachment bytes. The scan authenticates the rows it returns and
    the lookup fails closed when the linkage does not hold. The cursor behind
    the scan is kept per audit-chain store (see :class:`_VerifiedAttachIndex`)
    so the authenticated read stays incremental on this per-attachment path.

    Args:
        sha256: Hex digest of the requested attachment.
        requesting_worktree_id: The worker's worktree id.
        cas: Content-addressed blob store.
        audit_chain: Audit chain to consult for the attach event.

    Returns:
        The raw attachment bytes.

    Raises:
        AttachmentChainUnverified: The audit chain did not verify, so no
            attach row can be attributed to a worktree.
        WorktreeAccessDenied: The attach event's worktree id differs
            from the requesting worktree id.
        FileNotFoundError: No attach event exists for the SHA, or the
            CAS lookup misses.
    """
    # Resolve by (sha256, worktree_id) so concurrent attaches in
    # different worktrees of the same bytes do not poison each other.
    # If any historical attach in the requesting worktree exists for
    # this digest, allow the resolve; otherwise refuse. The list of
    # attaching worktrees (for the structured error) is built from
    # every historical event so the operator sees the full picture.
    # (bot-ack: 3284182761 -- CodeRabbit major.)
    attaching_worktrees = _attach_index_for(audit_chain).owners_of(audit_chain, sha256)
    if not attaching_worktrees:
        raise FileNotFoundError(f"No multimodal.attach event for {sha256[:12]}...")
    if requesting_worktree_id not in attaching_worktrees:
        seen_worktrees = sorted(w for w in attaching_worktrees if w)
        raise WorktreeAccessDenied(
            sha256=sha256,
            attached_worktree=", ".join(seen_worktrees) or "<unknown>",
            requesting_worktree=requesting_worktree_id,
        )
    blob = cas.get(sha256)
    if blob is None:
        raise FileNotFoundError(f"CAS miss for {sha256[:12]}...")
    return blob


# ---------------------------------------------------------------------------
# Lineage parents
# ---------------------------------------------------------------------------


def worker_lineage_parents(result: AttachmentBuildResult) -> list[str]:
    """Return canonical lineage parent URIs for *result*'s attachments.

    Empty when no attachments were resolved.
    """
    return [build_attachment_parent_uri(r.sha256) for r in result.resolutions]


__all__ = [
    "AttachmentBuildResult",
    "AttachmentChainUnverified",
    "AttachmentResolution",
    "CapabilityRefusal",
    "WorktreeAccessDenied",
    "build_attachment_context",
    "encode_one",
    "refuse_when_incapable",
    "resolve_attachment_for_worker",
    "worker_lineage_parents",
]
