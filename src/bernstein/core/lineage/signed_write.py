"""The supported signed-lineage write path (Ed25519 detached JWS + operator HMAC).

This module is the single, non-deprecated home for *sealing* a lineage entry:
computing the content hash, chaining it to the artefact's current tip, wrapping
it in the operator-HMAC envelope, signing the JCS-canonical bytes with the
agent's Ed25519 key, and handing the ``(entry, jws)`` pair to
:class:`~bernstein.core.lineage.store.LineageStore`.

Relationship to :class:`~bernstein.core.lineage.spine.LineageSpine`
------------------------------------------------------------------

The spine is the always-on Merkle+HMAC provenance chain that every adapter
artifact write routes through. It is deliberately keyless beyond the operator
HMAC: it proves *ordering and integrity* for a whole run.

A signed lineage entry proves something the spine cannot: **attributable
non-repudiation**. It carries

* an Ed25519 detached JWS over the entry's canonical bytes, verifiable offline
  against a published Agent Card by someone who holds no operator secret;
* an operator-HMAC envelope over every field, so a substitution attack after
  signing is caught independently of the signature;
* a caller-controlled ``span_id``, which the receipt subsystems repurpose as a
  *binding digest* so receipt-core fields are covered by both the signature and
  the HMAC.

Those three properties are what the datasource query receipts and the payment
transaction receipts verify. They are not a superset or a subset of the spine -
they are a different proof. Both substrates stay, and this module is the
supported entry point for the signed one.

What is deprecated
------------------

The *class* wrapper that used to own this logic
(:class:`bernstein.core.lineage.recorder.LineageRecorder`) is deprecated and
reduced to a shim over :class:`SignedLineageLog` here; the WAL-backed
:class:`bernstein.core.persistence.lineage.LineageWriter` is deprecated for new
code. The signed-append operation itself is not deprecated and lives here.
``tests/unit/lineage/test_spine_deprecations.py`` enforces that
:meth:`LineageStore.append` is reached from nowhere else in ``src/``, so a
signed write cannot silently regress onto a deprecated substrate.

Artifact keys and path safety
-----------------------------

``artefact_path`` is an *artifact key* (issue #2559): either a repo-relative
POSIX path -- the implicit default scheme, and what every historical entry
carries -- or a canonical URI from the closed scheme set in
:mod:`bernstein.core.lineage.artifact_uri` (``pr``, ``pkg``, ``deploy``,
``doc``), which lets a signed entry attribute an artifact whose bytes never
lived in the worktree.

The key is validated before any HMAC or signature is computed, through the same
decision function the spine boundary uses, so the two write boundaries cannot
drift apart. Absolute paths and ``..`` traversal are rejected, so a caller that
controls the key cannot anchor an artefact outside the repo; an unknown or
non-canonical URI is rejected rather than stored as if it were a filename.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

from bernstein.core.lineage.artifact_uri import (
    REASON_ABSOLUTE,
    REASON_EMPTY,
    REASON_MALFORMED_URI,
    REASON_NON_CANONICAL,
    REASON_TRAVERSAL,
    REASON_UNKNOWN_SCHEME,
    artifact_key_rejection_reason,
)
from bernstein.core.lineage.entry import LineageEntry, canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.identity import sign_detached

if TYPE_CHECKING:
    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.lineage.store import LineageStore

logger = logging.getLogger(__name__)

#: Boundary wording for each rejection code from
#: :func:`bernstein.core.lineage.artifact_uri.artifact_key_rejection_reason`.
#: The three legacy codes keep their pre-#2559 message verbatim, so a caller
#: matching on the error text is unaffected.
_REJECTION_MESSAGES = {
    REASON_EMPTY: "empty artefact_path",
    REASON_ABSOLUTE: "absolute artefact_path not allowed",
    REASON_TRAVERSAL: "path traversal in artefact_path",
    REASON_UNKNOWN_SCHEME: "unknown artefact URI scheme",
    REASON_MALFORMED_URI: "malformed artefact URI",
    REASON_NON_CANONICAL: "non-canonical artefact URI",
}


def _is_unsafe_path(artefact_path: str) -> str | None:
    """Return a reason string if the key is unsafe; ``None`` otherwise.

    Delegates the decision to
    :func:`bernstein.core.lineage.artifact_uri.artifact_key_rejection_reason`,
    the single function the spine boundary also routes through, so the two
    write boundaries cannot drift apart on what a legal artifact key is
    (issue #2559).

    Repo-relative paths keep their previous verdict exactly:

      * absolute paths (``/...`` or ``C:\\...``) are rejected - a repo lineage
        key is a repo-relative POSIX string;
      * any segment equal to ``..`` is rejected (path traversal), treating
        ``\\`` as a separator too (defence in depth);
      * empty keys are rejected.

    In addition, a canonical artifact URI from the closed scheme set is now
    accepted, and a string carrying ``://`` with an unknown scheme or a
    non-canonical spelling is rejected instead of being stored as a filename.
    """
    reason = artifact_key_rejection_reason(artefact_path)
    if reason is None:
        return None
    return _REJECTION_MESSAGES[reason]


def seal_write(
    store: LineageStore,
    operator_hmac_key: bytes,
    *,
    artefact_path: str,
    new_content: bytes,
    agent_id: str,
    agent_card: AgentCard,
    private_key_pem: str,
    tool_call_id: str,
    span_id: str,
    artefact_kind: str = "file",
    trust_class: str | None = None,
    extra_parents: list[str] | None = None,
    ts_ns: int | None = None,
) -> str:
    """Seal a single signed lineage write into ``store``. Returns the entry hash.

    This is the supported signed-write primitive. It:

    1. Computes ``content_hash = sha256(new_content)``.
    2. Looks up the current tip(s) for the artefact via the store.
    3. Builds a :class:`~bernstein.core.lineage.entry.LineageEntry` with the
       appropriate ``parent_hashes`` - empty for genesis, the single current
       tip for a linear successor, plus any explicit ``extra_parents``. Merges
       are never invented on the agent's behalf.
    4. Computes the HMAC envelope with the operator key over the entry's
       canonical bytes minus the ``operator_hmac`` field itself.
    5. Signs the canonical bytes (RFC 7515 + RFC 8037 detached JWS, EdDSA) with
       the agent's Ed25519 private key.
    6. Hands everything to the store, which fsyncs + flocks the log and writes
       the ``signatures/<aa>/<full>/<entry-hash>.jws`` sidecar.
    7. Emits an OpenTelemetry span (no-op when telemetry is not initialised).

    Args:
        store: The lineage store the entry is appended to.
        operator_hmac_key: Operator secret for the HMAC envelope.
        artefact_path: Repo-relative POSIX path of the artefact written.
        new_content: The bytes that just landed on disk.
        agent_id: Bernstein agent slug (e.g. ``agent:claude-worker-3``).
        agent_card: Agent Card with the public key the auditor will use.
        private_key_pem: PEM-encoded Ed25519 private key for the agent.
        tool_call_id: Cross-link to the originating audit entry.
        span_id: OTel span hex; carried verbatim in the signed entry body. The
            receipt subsystems repurpose it as a receipt-core *binding digest*
            so those fields are covered by the signature and the HMAC. This
            function neither opens a span with it nor emits it, so the
            repurposing has no telemetry side effect.
        artefact_kind: One of ``ARTEFACT_KINDS``; defaults to ``file``.
        trust_class: Optional provenance trust class (issue #2513). Set on
            tool-result records so the signed entry itself carries the label;
            taint propagation is a projection over these entries.
        extra_parents: Optional additional ``parent_hashes`` recorded on top of
            the artefact's own tip. This is the cross-artefact lineage edge that
            anchors a derived artefact (or a quarantine extraction) back to the
            tainted source it was produced from.
        ts_ns: Optional deterministic entry timestamp (nanoseconds). When
            ``None`` the wall clock is stamped. Artifact-mode callers pass a
            logical timestamp so two operators with equal inputs produce a
            byte-identical signed entry - the deterministic projection of
            ``(task, inputs)`` (issue #2608).

    Raises:
        ValueError: When ``artefact_path`` is absolute or contains a
            path-traversal segment.
    """
    unsafe = _is_unsafe_path(artefact_path)
    if unsafe is not None:
        raise ValueError(unsafe)

    content_hash = "sha256:" + hashlib.sha256(new_content).hexdigest()
    tips = store.tip_set(artefact_path)
    # Only ever chain to the single current tip. Forks are surfaced upstream;
    # merges are emitted by the Steward via an explicit multi-parent
    # ``record_merge`` call (out of scope for v1 core).
    parent_hashes: list[str] = list(tips.get("open", []))[:1]
    # Cross-artefact edges (provenance/quarantine lineage) are appended after
    # the tip parent, preserving order and dropping duplicates so the same
    # source is never named twice.
    if extra_parents:
        for ph in extra_parents:
            if ph not in parent_hashes:
                parent_hashes.append(ph)

    entry_ts_ns = time.time_ns() if ts_ns is None else int(ts_ns)

    # Build the entry with an empty ``operator_hmac`` field, compute the
    # canonical HMAC over its JCS bytes, then materialise the final immutable
    # entry with the digest. The HMAC binds every field of the entry so a
    # substitution attack post-signing is caught by both the JWS and the HMAC
    # envelope independently. The shared :func:`compute_operator_hmac` helper is
    # the single source of truth used by both this path and the CI gate - see
    # ADR-009 §5.2.
    unsigned_entry = LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind=artefact_kind,
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent_id,
        agent_card_kid=agent_card.kid,
        tool_call_id=tool_call_id,
        span_id=span_id,
        ts_ns=entry_ts_ns,
        operator_hmac="",
        trust_class=trust_class,
    )
    operator_hmac = compute_operator_hmac(unsigned_entry, operator_hmac_key)

    entry = LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind=artefact_kind,
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent_id,
        agent_card_kid=agent_card.kid,
        tool_call_id=tool_call_id,
        span_id=span_id,
        ts_ns=entry_ts_ns,
        operator_hmac=operator_hmac,
        trust_class=trust_class,
    )

    # Sign the JCS-canonical entry bytes. The auditor verifies the same bytes
    # via :func:`bernstein.core.lineage.identity.verify_detached` - see
    # ADR-009 §5.2.
    canonical = canonicalise(entry)
    jws = sign_detached(canonical, private_key_pem, kid=agent_card.kid)

    h = store.append(entry, jws=jws)

    # Best-effort OTel emission. ``start_span`` is a no-op when telemetry has
    # not been initialised, so this is safe in tests.
    try:
        from bernstein.core.observability.telemetry import start_span

        with start_span(
            "lineage.record_write",
            attributes={
                "lineage.artefact_path": artefact_path,
                "lineage.entry_hash": h,
                "lineage.agent_id": agent_id,
                "lineage.tool_call_id": tool_call_id,
                "lineage.parent_hashes_count": len(parent_hashes),
            },
        ):
            pass
    except Exception as exc:  # pragma: no cover - telemetry must never break recording
        logger.debug("lineage OTel span emission failed: %s", exc)

    # Sanity check: the entry hash returned by the store must equal what we'd
    # recompute from the canonical bytes.
    assert h == entry_hash(entry), "store.append entry_hash mismatch"

    return h


class SignedLineageLog:
    """A lineage store bound to an operator key, writing signed entries.

    The supported object form of :func:`seal_write`, for callers that seal many
    writes against one store and one operator secret (the artifact sink, the
    provenance recorder, the computer-use attestor). It is stateless beyond its
    two dependencies; sharing one instance across threads is safe because
    serialisation of writes is enforced by the store's ``flock``.
    """

    def __init__(
        self,
        store: LineageStore,
        *,
        operator_hmac_key: bytes,
    ) -> None:
        self.store: LineageStore = store
        self._hmac_key: bytes = operator_hmac_key

    def record_write(
        self,
        *,
        artefact_path: str,
        new_content: bytes,
        agent_id: str,
        agent_card: AgentCard,
        private_key_pem: str,
        tool_call_id: str,
        span_id: str,
        artefact_kind: str = "file",
        trust_class: str | None = None,
        extra_parents: list[str] | None = None,
        ts_ns: int | None = None,
    ) -> str:
        """Seal one artefact write into the bound store. Returns the entry hash.

        Arguments and failure modes are exactly those of :func:`seal_write`;
        ``store`` and ``operator_hmac_key`` come from the instance.
        """
        return seal_write(
            self.store,
            self._hmac_key,
            artefact_path=artefact_path,
            new_content=new_content,
            agent_id=agent_id,
            agent_card=agent_card,
            private_key_pem=private_key_pem,
            tool_call_id=tool_call_id,
            span_id=span_id,
            artefact_kind=artefact_kind,
            trust_class=trust_class,
            extra_parents=extra_parents,
            ts_ns=ts_ns,
        )


__all__ = ["SignedLineageLog", "seal_write"]
