"""Privacy-redacted publish flow for hash-chained journals (#1799).

Privacy default is local-only. ``publish_receipt`` is the only path that
ever writes outside ``.sdd/runtime/``; callers must explicitly pass
``opt_in=True`` so an accidental invocation cannot leak a chain.

Publishing redacts the fields listed in :class:`RedactionPolicy` (the
default policy clears ``prompt`` and ``tool_result``) and **re-anchors**
the chain to the redacted payloads:

* The redacted step is hashed with the same ``compute_step_hash``
  primitive used during recording, against the prior redacted
  ``step_hash`` (not the original).
* The resulting chain still verifies offline using the same
  ``verify_receipt`` walker. Only the head hash changes - and the
  ``ExportResult`` carries both the original and the redacted head so
  the operator can correlate the two surfaces.

Redaction is intentionally a one-way transform: there is no key that
recovers the cleartext from a published receipt. The orchestrator keeps
the unredacted journal under ``.sdd/runtime/journal/`` so the operator
can still replay locally.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.journal import (
    JournalReader,
    compute_step_hash,
)
from bernstein.core.persistence.journal_export import (
    _JOURNAL_PREFIX,  # type: ignore[reportPrivateUsage]
    MANIFEST_NAME,
    ExportResult,
    ReceiptError,
    ReceiptManifest,
    _add_bytes,  # type: ignore[reportPrivateUsage]
    _detect_version,  # type: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    import base64 as _  # noqa: F401  - imported only for type sake below
    from pathlib import Path

    from bernstein.core.persistence.lineage_signer import LineageSigner

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """Raised for publish failures (missing opt-in, empty journal, etc.)."""


#: Placeholder substituted for redacted fields. Chosen so it is
#: recognisable in human review and so that two redacted fields with
#: different cleartexts collide (the receipt deliberately destroys the
#: signal a length-based fingerprint might recover).
REDACTED_PLACEHOLDER = "<<redacted>>"


@dataclass(frozen=True)
class RedactionPolicy:
    """Which fields of a journal entry to scrub before publish.

    Attributes:
        redact_fields: Names of the canonical hashed fields to redact.
            Currently the only fields safe to redact are ``prompt`` and
            ``tool_result``; redacting ``input_hash`` or ``prev_hash``
            would break chain semantics and is rejected at policy
            construction.
    """

    redact_fields: frozenset[str] = field(default_factory=lambda: frozenset({"prompt", "tool_result"}))

    _ALLOWED: frozenset[str] = field(
        default_factory=lambda: frozenset({"prompt", "model", "tool_call", "tool_result"}),
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        illegal = self.redact_fields - self._ALLOWED
        if illegal:
            msg = (
                f"cannot redact fields {sorted(illegal)}; only "
                f"{sorted(self._ALLOWED)} may be redacted (others are "
                "load-bearing for chain semantics)"
            )
            raise PublishError(msg)

    @classmethod
    def default(cls) -> RedactionPolicy:
        return cls()


@dataclass(frozen=True)
class PublishResult:
    """Returned by :func:`publish_receipt`.

    Attributes:
        path: Filesystem path of the published receipt tarball.
        head_hash: Head hash of the *re-anchored* (redacted) chain.
        original_head_hash: Head hash of the original local chain.
        steps: Number of steps in the chain.
        signed: True when a detached signature was bundled.
    """

    path: Path
    head_hash: str
    original_head_hash: str
    steps: int
    signed: bool


def _redact_value(value: Any) -> Any:
    """Return a one-way redacted form of *value*.

    Strings collapse to the literal placeholder. Containers collapse to
    the placeholder string too - we deliberately do not recurse, because
    structure-preserving redaction would still leak length and shape
    fingerprints to a curious observer.
    """
    if value is None:
        return None
    return REDACTED_PLACEHOLDER


def _redact_row(row: dict[str, Any], policy: RedactionPolicy) -> dict[str, Any]:
    """Return a copy of *row* with redaction policy applied."""
    redacted = row.copy()
    for field_name in policy.redact_fields:
        if field_name in redacted:
            redacted[field_name] = _redact_value(redacted[field_name])
    return redacted


def publish_receipt(
    agent_dir: Path,
    receipt_path: Path,
    *,
    agent_id: str,
    policy: RedactionPolicy,
    opt_in: bool,
    signer: LineageSigner | None = None,
) -> PublishResult:
    """Redact the journal and emit a publish-quality receipt.

    Args:
        agent_dir: Per-agent journal directory.
        receipt_path: Where to write the receipt tarball.
        agent_id: Identifier baked into the manifest.
        policy: :class:`RedactionPolicy` selecting which fields to scrub.
        opt_in: Must be ``True``. Required because publish is the only
            code path that ever writes outside ``.sdd/runtime/`` and a
            silent default would defeat the privacy contract.
        signer: Optional :class:`LineageSigner`; when set, the receipt
            carries a detached Ed25519 signature over the canonical
            manifest bytes of the *redacted* chain.

    Returns:
        :class:`PublishResult` carrying both the redacted and the
        original head hashes plus the receipt path.

    Raises:
        PublishError: When ``opt_in`` is false, the journal is empty,
            the chain fails its self-verification, or the file write
            fails.
    """
    if not opt_in:
        msg = (
            "publish_receipt requires opt_in=True; the local-only default "
            "exists because publish is the only path that ever writes "
            "outside .sdd/runtime/"
        )
        raise PublishError(msg)

    reader = JournalReader(agent_dir)
    entries = list(reader.entries())
    if not entries:
        msg = f"refusing to publish empty journal at {agent_dir}"
        raise PublishError(msg)

    verification = reader.verify()
    if not verification.ok:
        msg = f"journal at {agent_dir} fails self-verification before publish: " + "; ".join(verification.errors)
        raise PublishError(msg)

    # Re-anchor the chain over the redacted payloads.
    original_head = verification.head_hash
    redacted_lines: list[str] = []
    prev_hash = "0" * 64
    for entry in entries:
        row = entry.to_dict()
        redacted = _redact_row(row, policy)
        redacted["prev_hash"] = prev_hash
        new_step_hash = compute_step_hash(
            prev_hash=prev_hash,
            input_hash=str(redacted.get("input_hash", "")),
            model=redacted.get("model"),
            prompt=redacted.get("prompt"),
            tool_call=redacted.get("tool_call"),
            tool_result=redacted.get("tool_result"),
            # Re-anchor over the effort dimension too: a redacted row keeps
            # its ``effort`` key (get -> None on legacy rows), so the
            # published receipt does not silently drop the effort dimension
            # from the chain.
            effort=redacted.get("effort"),
        )
        redacted["step_hash"] = new_step_hash
        # Blob refs may name cleartext blobs; drop them in published receipts.
        redacted["blob_refs"] = []
        redacted_lines.append(json.dumps(redacted, sort_keys=True, separators=(",", ":")))
        prev_hash = new_step_hash

    redacted_head = prev_hash

    manifest = ReceiptManifest(
        agent_id=agent_id,
        head_hash=redacted_head,
        steps=len(entries),
        bernstein_version=_detect_version(),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        blob_digests=[],
    )

    signature_bytes: bytes | None = None
    if signer is not None:
        signature_bytes = signer.sign(manifest.canonical_bytes())

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    import tarfile

    try:
        with tarfile.open(receipt_path, "w") as tar:
            _add_bytes(tar, MANIFEST_NAME, manifest.canonical_bytes())
            if signature_bytes is not None:
                import base64

                _add_bytes(
                    tar,
                    "manifest.sig",
                    base64.b64encode(signature_bytes),
                )
            _add_bytes(
                tar,
                f"{_JOURNAL_PREFIX}/{reader.bucket_path.name}",
                ("\n".join(redacted_lines) + "\n").encode("utf-8"),
            )
    except OSError as exc:
        msg = f"publish write failed: {exc}"
        raise PublishError(msg) from exc

    logger.info(
        "Published redacted receipt for agent %s: original_head=%s redacted_head=%s",
        agent_id,
        original_head[:12],
        redacted_head[:12],
    )

    return PublishResult(
        path=receipt_path,
        head_hash=redacted_head,
        original_head_hash=original_head,
        steps=len(entries),
        signed=signature_bytes is not None,
    )


# Re-export of ``ExportResult`` for callers that handle both surfaces uniformly.
__all__ = [
    "REDACTED_PLACEHOLDER",
    "ExportResult",
    "PublishError",
    "PublishResult",
    "ReceiptError",
    "RedactionPolicy",
    "publish_receipt",
]
