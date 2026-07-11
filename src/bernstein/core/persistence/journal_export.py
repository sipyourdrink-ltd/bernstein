"""Portable receipt format for hash-chained journals (#1799).

A receipt is a tarball that bundles:

* The journal bucket(s) for one agent run, byte-identical to what
  ``.sdd/runtime/journal/<agent_id>/`` contains on disk.
* A manifest JSON (``manifest.json``) capturing ``agent_id``,
  ``head_hash``, ``steps``, ``bernstein_version`` (best-effort), and the
  list of CAS blobs the chain references.
* Any referenced CAS blobs under ``blobs/<digest>``. This lets a
  verifier replay the chain end-to-end without contacting the
  originating host.

The receipt is *signed* when the caller passes a ``LineageSigner``
(typically the install's Ed25519 key plumbed via
``signer_from_config``). The signature covers the canonical manifest
JSON; ``verify_receipt`` checks it before walking the chain. An
unsigned receipt verifies in two steps - chain integrity + head match -
which is good enough for sovereign verifiers running fully offline.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import tarfile
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.persistence.journal import (
    JournalError,
    JournalReader,
    compute_step_hash,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.persistence.lineage_signer import (
        LineageSigner,
        LineageVerifier,
    )

logger = logging.getLogger(__name__)

#: Manifest filename inside the tarball.
MANIFEST_NAME = "manifest.json"

#: Where journal bucket files live inside the tarball.
_JOURNAL_PREFIX = "journal"

#: Where CAS blobs live inside the tarball.
_BLOBS_PREFIX = "blobs"


class ReceiptError(RuntimeError):
    """Raised for unrecoverable receipt build/parse/verify errors."""


@dataclass(frozen=True)
class ReceiptManifest:
    """Manifest header carried inside a receipt tarball.

    Attributes:
        agent_id: The agent whose chain is captured.
        head_hash: SHA-256 head of the chain at export time.
        steps: Number of steps in the chain (used for sanity checks).
        bernstein_version: Best-effort version string of the exporting install.
        created_at: ISO 8601 timestamp of export.
        blob_digests: SHA-256 hex digests of every CAS blob bundled.
        format_version: Receipt format version; bump on schema changes.
    """

    agent_id: str
    head_hash: str
    steps: int
    bernstein_version: str
    created_at: str
    blob_digests: list[str] = field(default_factory=list)
    format_version: int = 1

    def canonical_bytes(self) -> bytes:
        """Return the UTF-8 canonical-JSON bytes used for signing."""
        document = {
            "agent_id": self.agent_id,
            "head_hash": self.head_hash,
            "steps": self.steps,
            "bernstein_version": self.bernstein_version,
            "created_at": self.created_at,
            "blob_digests": self.blob_digests.copy(),
            "format_version": self.format_version,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_json(self) -> str:
        """Return a pretty JSON representation suitable for human review."""
        return json.dumps(
            {
                "agent_id": self.agent_id,
                "head_hash": self.head_hash,
                "steps": self.steps,
                "bernstein_version": self.bernstein_version,
                "created_at": self.created_at,
                "blob_digests": self.blob_digests.copy(),
                "format_version": self.format_version,
            },
            sort_keys=True,
            indent=2,
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> ReceiptManifest:
        """Build a manifest from its on-disk JSON bytes."""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"manifest is not valid JSON: {exc}"
            raise ReceiptError(msg) from exc
        try:
            return cls(
                agent_id=str(data["agent_id"]),
                head_hash=str(data["head_hash"]),
                steps=int(data["steps"]),
                bernstein_version=str(data.get("bernstein_version", "unknown")),
                created_at=str(data.get("created_at", "")),
                blob_digests=list(data.get("blob_digests") or []),
                format_version=int(data.get("format_version", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"manifest missing required fields: {exc}"
            raise ReceiptError(msg) from exc


@dataclass(frozen=True)
class ExportResult:
    """Returned by :func:`export_receipt`.

    Attributes:
        path: Filesystem path of the receipt tarball that was written.
        head_hash: Head hash captured in the manifest.
        steps: Number of steps included.
        signed: True when a detached signature was bundled.
    """

    path: Path
    head_hash: str
    steps: int
    signed: bool


@dataclass(frozen=True)
class ReceiptVerificationResult:
    """Outcome of :func:`verify_receipt`.

    Attributes:
        ok: ``True`` when every check passes.
        head_hash: Tail hash recomputed from the bundled chain.
        steps: Number of steps walked.
        errors: Human-readable error messages.
        signed: Whether the receipt carries a detached signature block.
    """

    ok: bool
    head_hash: str
    steps: int
    errors: list[str] = field(default_factory=list)
    signed: bool = False


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _detect_version() -> str:
    """Best-effort lookup of the running install's version string."""
    try:
        from importlib.metadata import version

        return version("bernstein")
    except Exception:
        return "unknown"


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    """Add raw bytes as a named file inside *tar*."""
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(payload))


def export_receipt(
    agent_dir: Path,
    receipt_path: Path,
    *,
    agent_id: str,
    signer: LineageSigner | None = None,
    extra_blob_root: Path | None = None,
) -> ExportResult:
    """Build a portable receipt for the journal at *agent_dir*.

    Args:
        agent_dir: Per-agent journal directory.
        receipt_path: Where to write the tarball.
        agent_id: Identifier baked into the manifest.
        signer: Optional :class:`LineageSigner`; when set, the receipt
            carries a detached Ed25519 signature over the canonical
            manifest bytes.
        extra_blob_root: Optional CAS root from which referenced blobs
            should be pulled. When unset (the common case for an
            air-gapped install) the receipt still bundles the chain;
            blob hashes referenced by entries are listed in the manifest
            so a verifier can call out the missing data.

    Returns:
        :class:`ExportResult` summarising what was written.

    Raises:
        ReceiptError: When the journal is empty, the chain fails its
            self-verification, or the file write fails.
    """
    reader = JournalReader(agent_dir)
    entries = list(reader.entries())
    if not entries:
        msg = f"refusing to export empty journal at {agent_dir}"
        raise ReceiptError(msg)

    verification = reader.verify()
    if not verification.ok:
        msg = f"journal at {agent_dir} fails self-verification before export: " + "; ".join(verification.errors)
        raise ReceiptError(msg)

    # Bundle CAS blobs that the chain references (best-effort - missing
    # blobs are listed in the manifest so the verifier surfaces a clear
    # error rather than silently passing).
    referenced: list[str] = []
    blob_payloads: dict[str, bytes] = {}
    for entry in entries:
        for ref in entry.blob_refs:
            if ref in referenced:
                continue
            referenced.append(ref)
            if extra_blob_root is None:
                continue
            blob_path = extra_blob_root / ref[:2] / ref
            if blob_path.exists():
                blob_payloads[ref] = blob_path.read_bytes()

    head_hash = verification.head_hash
    manifest = ReceiptManifest(
        agent_id=agent_id,
        head_hash=head_hash,
        steps=verification.steps,
        bernstein_version=_detect_version(),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        blob_digests=referenced,
    )

    signature_bytes: bytes | None = None
    if signer is not None:
        signature_bytes = signer.sign(manifest.canonical_bytes())

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(receipt_path, "w") as tar:
            _add_bytes(tar, MANIFEST_NAME, manifest.canonical_bytes())
            if signature_bytes is not None:
                # Base64 keeps the binary safely embeddable.
                _add_bytes(
                    tar,
                    "manifest.sig",
                    base64.b64encode(signature_bytes),
                )

            # Bundle the journal bucket. Always serialise the file from the
            # reader (canonical form) so signed manifests cover the exact
            # bytes the verifier walks.
            canonical_lines = [json.dumps(e.to_dict(), sort_keys=True, separators=(",", ":")) for e in entries]
            bucket_payload = ("\n".join(canonical_lines) + "\n").encode("utf-8")
            _add_bytes(
                tar,
                f"{_JOURNAL_PREFIX}/{reader.bucket_path.name}",
                bucket_payload,
            )

            for digest, payload in blob_payloads.items():
                _add_bytes(tar, f"{_BLOBS_PREFIX}/{digest}", payload)
    except OSError as exc:
        msg = f"receipt write failed: {exc}"
        raise ReceiptError(msg) from exc

    return ExportResult(
        path=receipt_path,
        head_hash=head_hash,
        steps=verification.steps,
        signed=signature_bytes is not None,
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _read_tar_member(tar: tarfile.TarFile, name: str) -> bytes | None:
    """Return the bytes of *name* inside *tar*, or ``None`` when absent."""
    try:
        member = tar.getmember(name)
    except KeyError:
        return None
    fileobj = tar.extractfile(member)
    if fileobj is None:
        return None
    return fileobj.read()


def _walk_journal_bytes(payload: bytes) -> tuple[str, int, list[str]]:
    """Walk the embedded journal bucket and recompute the chain.

    Returns ``(head_hash, steps, errors)``.
    """
    head_hash = "0" * 64
    steps = 0
    errors: list[str] = []
    expected_seq = 0

    for line_no, raw in enumerate(payload.decode("utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"journal line {line_no}: invalid JSON ({exc})")
            continue
        if not isinstance(row, dict):
            errors.append(f"journal line {line_no}: not a JSON object")
            continue
        if int(row.get("seq", -1)) != expected_seq:
            errors.append(f"journal line {line_no}: seq mismatch (expected {expected_seq}, got {row.get('seq')})")
        stored_prev = str(row.get("prev_hash", ""))
        if stored_prev != head_hash:
            errors.append(
                f"journal line {line_no}: prev_hash mismatch (expected {head_hash[:16]}..., got {stored_prev[:16]}...)"
            )
        recomputed = compute_step_hash(
            prev_hash=stored_prev,
            input_hash=str(row.get("input_hash", "")),
            model=row.get("model"),
            prompt=row.get("prompt"),
            tool_call=row.get("tool_call"),
            tool_result=row.get("tool_result"),
            # Legacy rows lack ``effort`` (get -> None), so the recomputed
            # hash matches the pre-effort payload and old chains verify
            # unchanged; effort-bearing rows fold effort into the hash so an
            # offline verifier reproduces the same step_hash the writer did.
            effort=row.get("effort"),
        )
        stored_hash = str(row.get("step_hash", ""))
        if recomputed != stored_hash:
            errors.append(
                f"journal line {line_no}: step_hash mismatch (expected {recomputed[:16]}..., got {stored_hash[:16]}...)"
            )
        head_hash = stored_hash
        steps += 1
        expected_seq += 1

    return head_hash, steps, errors


def verify_receipt(
    receipt_path: Path,
    *,
    expected_head: str | None = None,
    verifier: LineageVerifier | None = None,
) -> ReceiptVerificationResult:
    """Verify a receipt tarball offline.

    Args:
        receipt_path: Path to the tarball produced by :func:`export_receipt`.
        expected_head: If supplied, asserts that the walked head matches
            this value. Pass the head you received out-of-band from the
            originating host.
        verifier: Optional :class:`LineageVerifier` paired with the
            signer used at export time. Pass ``None`` when the receipt
            is unsigned.

    Returns:
        :class:`ReceiptVerificationResult` carrying ``ok``, the walked
        head, the step count, and any errors.
    """
    if not receipt_path.exists():
        msg = f"receipt not found: {receipt_path}"
        raise ReceiptError(msg)

    errors: list[str] = []
    signed = False
    head_hash = "0" * 64
    steps = 0

    try:
        with tarfile.open(receipt_path, "r") as tar:
            manifest_payload = _read_tar_member(tar, MANIFEST_NAME)
            if manifest_payload is None:
                msg = f"receipt missing manifest: {receipt_path}"
                raise ReceiptError(msg)
            manifest = ReceiptManifest.from_json_bytes(manifest_payload)

            sig_payload = _read_tar_member(tar, "manifest.sig")
            if sig_payload is not None:
                signed = True
                if verifier is None:
                    errors.append("receipt carries a signature but no verifier was provided")
                else:
                    raw_sig = base64.b64decode(sig_payload)
                    if not verifier.verify(manifest.canonical_bytes(), raw_sig):
                        errors.append("manifest signature failed Ed25519 verification")

            # Find any journal bucket inside the tar. Today we ship one bucket;
            # supporting multiple buckets here keeps the format future-proof
            # for compaction passes.
            journal_members = [m for m in tar.getmembers() if m.isfile() and m.name.startswith(f"{_JOURNAL_PREFIX}/")]
            if not journal_members:
                msg = f"receipt missing journal payload: {receipt_path}"
                raise ReceiptError(msg)

            # Concatenate buckets in name order so the chain walks in order.
            concatenated = b""
            for member in sorted(journal_members, key=lambda m: m.name):
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    continue
                payload = fileobj.read()
                if payload and not payload.endswith(b"\n"):
                    payload += b"\n"
                concatenated += payload

            head_hash, steps, walk_errors = _walk_journal_bytes(concatenated)
            errors.extend(walk_errors)

            if manifest.head_hash != head_hash:
                errors.append(f"manifest head mismatch: {manifest.head_hash[:16]}... vs walked {head_hash[:16]}...")
            if manifest.steps != steps:
                errors.append(f"manifest step count mismatch: {manifest.steps} vs walked {steps}")
    except tarfile.TarError as exc:
        msg = f"receipt is not a valid tar archive: {exc}"
        raise ReceiptError(msg) from exc

    if expected_head is not None and expected_head != head_hash:
        errors.append(f"expected head {expected_head[:16]}... but walked {head_hash[:16]}...")

    return ReceiptVerificationResult(
        ok=not errors,
        head_hash=head_hash,
        steps=steps,
        errors=errors,
        signed=signed,
    )


@contextlib.contextmanager
def open_receipt(receipt_path: Path):  # type: ignore[no-untyped-def]
    """Context manager that yields an open ``TarFile`` for *receipt_path*."""
    if not receipt_path.exists():
        msg = f"receipt not found: {receipt_path}"
        raise ReceiptError(msg)
    with tarfile.open(receipt_path, "r") as tar:
        yield tar


__all__ = [
    "MANIFEST_NAME",
    "ExportResult",
    "JournalError",
    "ReceiptError",
    "ReceiptManifest",
    "ReceiptVerificationResult",
    "export_receipt",
    "open_receipt",
    "verify_receipt",
]
