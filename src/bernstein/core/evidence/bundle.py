"""Content-addressed verification evidence bundles (issue #2362).

"Done" today is a status plus logs. The artefacts that *prove* a task passed --
test-runner output, coverage reports, lint results, screenshots for UI work --
scatter across worker output and die with the worktree. This module makes the
artefact an operator consumes at review time *be* the proof:

* A task declares **evidence producers** (test command, coverage, lint, an
  optional screenshot / recording for web-facing tasks). Producers are marked
  ``required`` (they gate completion) or advisory (they only attach). At gate
  time the producers run and their outputs are captured.
* Each output is stored **content-addressed** in an :class:`EvidenceStore`
  under a per-blob size cap, with a ``gc`` that removes blobs no live bundle
  references.
* The outputs are bound into an :class:`EvidenceBundle` -- a signed record whose
  canonical bytes are anchored in the evidence lineage spine
  (:class:`bernstein.core.lineage.spine.LineageSpine`). The spine entry hash is
  the bundle's identity; the audit chain mirrors it via
  ``record_evidence_bundle`` (see
  :mod:`bernstein.core.security.audit_chain`).
* Media evidence (screenshot / recording) additionally flows through the
  existing content-credentials support
  (:mod:`bernstein.core.lineage.c2pa`): a signed C2PA manifest binds the media
  bytes so a tampered screenshot fails the hard-binding check.

Determinism (AC4)
-----------------
The bundle binding is canonical JSON (sorted keys, minimal separators, UTF-8)
over hashes that are pure functions of the producer outputs plus caller-supplied
metadata and timestamp. Ed25519 is deterministic (RFC 8032) and the spine tags
are HMAC over canonical bytes, so two replays of the same deterministic
producers against the same fixtures anchor byte-identical bundle hashes,
signatures, and spine anchors.

Verifiability (AC1, AC2)
------------------------
:func:`verify_evidence_bundle` recomputes, from the stored bundle and blobs
alone: the Ed25519 signature over the canonical binding; the spine anchor over
the same bytes (and the whole evidence spine); and the content hash of every
stored blob against the manifest. A single-byte edit to any evidence file, the
bundle, or the spine fails the check, naming the offending item. ``bernstein
audit verify`` runs the same check across every bundle, so a tampered evidence
report is detected exactly like a tampered chain entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.evidence.output_diff import OutputDiff
from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.lineage.spine import (
    SPINE_ENTRY_VERSION,
    LineageSpine,
    SpineEntry,
    compute_entry_hash,
    content_hash_of,
)
from bernstein.core.skills.catalog.signature import sign_payload, verify_payload

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_BLOB_BYTES",
    "EVIDENCE_RUN_ID",
    "EVIDENCE_SCHEMA_VERSION",
    "EvidenceBundle",
    "EvidenceItem",
    "EvidenceProducer",
    "EvidenceStore",
    "EvidenceVerifyResult",
    "OutputDiff",
    "ProducerOutcome",
    "StoredBlob",
    "build_evidence_bundle",
    "bundle_path",
    "load_or_create_evidence_identity",
    "parse_producers",
    "read_evidence_bundle",
    "run_evidence_gate",
    "run_producers",
    "verify_all_evidence_bundles",
    "verify_evidence_bundle",
]

#: Lineage run id under which every evidence bundle is anchored. Kept in one
#: dedicated run so evidence lineage never interleaves with per-task journals.
EVIDENCE_RUN_ID = "evidence"

#: Version stamped into every bundle binding preimage. Bump only on a
#: wire-format change.
EVIDENCE_SCHEMA_VERSION = 1

#: Per-blob storage cap (bytes). Producer output longer than this is truncated
#: at capture time so the store cannot grow without bound; the stored (capped)
#: bytes are what the content hash and the bundle bind, so replay stays
#: byte-identical. See the ``Risks`` note in the issue: caps from day one.
DEFAULT_MAX_BLOB_BYTES = 1 << 20  # 1 MiB

_EVIDENCE_ACTOR = "bernstein.evidence_bundle"
_EVIDENCE_MODEL = "none"

_BUNDLE_SUBPATH = (".sdd", "evidence", "bundles")
_BLOB_SUBPATH = (".sdd", "evidence", "blobs")
_IDENTITY_PRIVATE_NAME = "evidence-identity-key.pem"
_IDENTITY_PUBLIC_NAME = "evidence-identity-public.pem"

#: Producer kinds. Media kinds additionally flow through content-credentials.
_MEDIA_KINDS = frozenset({"screenshot", "recording"})
_VALID_KINDS = frozenset({"test", "coverage", "lint", "generic"}) | _MEDIA_KINDS

_STATUS_PASS = "pass"
_STATUS_FAIL = "fail"


# ---------------------------------------------------------------------------
# Canonical hashing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _hex_of(content_hash: str) -> str:
    """Return the bare hex of a ``sha256:``-prefixed digest."""
    return content_hash.split(":", 1)[-1]


def _safe_task_name(task_id: str) -> str:
    """Return a filesystem-safe basename for a task id.

    The id is content-hashed so the name is portable and cannot introduce a
    path separator regardless of the id's shape.
    """
    if not task_id:
        raise ValueError("empty task_id")
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Producer contract (declared in the task spec)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceProducer:
    """One declared evidence producer.

    Attributes:
        name: Stable producer name (e.g. ``"tests"``, ``"coverage"``).
        kind: One of :data:`_VALID_KINDS`. ``screenshot`` / ``recording`` are
            media kinds and additionally flow through content-credentials.
        command: The argv executed at gate time to produce the evidence.
        required: When True the producer gates completion (a non-zero exit
            blocks the gate). Advisory producers (``required=False``) never
            block; a failure only attaches a failure record (AC5).
    """

    name: str
    kind: str
    command: tuple[str, ...]
    required: bool = True

    def is_media(self) -> bool:
        """Return True for media producers (screenshot / recording)."""
        return self.kind in _MEDIA_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "command": list(self.command),
            "required": self.required,
        }

    @classmethod
    def from_spec(cls, raw: dict[str, Any]) -> EvidenceProducer:
        """Build a producer from a task-spec mapping, validating the kind."""
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("evidence producer requires a non-empty 'name'")
        kind = str(raw.get("kind", "generic"))
        if kind not in _VALID_KINDS:
            raise ValueError(f"unknown evidence producer kind {kind!r}; expected one of {sorted(_VALID_KINDS)}")
        command_raw = raw.get("command", [])
        if isinstance(command_raw, str | bytes):
            raise TypeError("evidence producer 'command' must be a list of argv parts, not a string")
        if not isinstance(command_raw, list | tuple):
            raise TypeError(f"evidence producer 'command' must be a list, got {type(command_raw).__name__}")
        command = tuple(str(part) for part in command_raw)
        return cls(name=name, kind=kind, command=command, required=bool(raw.get("required", True)))


def parse_producers(raw: Sequence[dict[str, Any]]) -> tuple[EvidenceProducer, ...]:
    """Parse the task-spec ``evidence_producers`` list into typed producers."""
    return tuple(EvidenceProducer.from_spec(spec) for spec in raw)


# ---------------------------------------------------------------------------
# Producer execution (at gate time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProducerOutcome:
    """The raw result of running one producer at gate time.

    Attributes:
        producer: The declared producer that ran.
        exit_code: The producer's process exit code (0 == pass).
        output: The captured stdout+stderr bytes (uncapped; the store applies
            the size cap when the blob is written).
    """

    producer: EvidenceProducer
    exit_code: int
    output: bytes

    @property
    def passed(self) -> bool:
        """Return True when the producer exited 0."""
        return self.exit_code == 0


def _default_runner(cwd: Path, *, timeout: int) -> Callable[[EvidenceProducer], tuple[int, bytes]]:
    """Return a subprocess-backed runner rooted at ``cwd``.

    Each producer's argv is executed with no shell; stdout and stderr are
    merged so the captured evidence is a single stream. A timeout or launch
    failure is surfaced as a non-zero exit with the diagnostic in the output.
    """

    def run(producer: EvidenceProducer) -> tuple[int, bytes]:
        if not producer.command:
            return 1, b"evidence producer has no command\n"
        try:
            completed = subprocess.run(  # nosec B603 - argv fully supplied by the caller's task spec
                list(producer.command),
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # sanitize_log is unnecessary: exc text is not re-emitted to a log
            # line, only captured into the (content-addressed) evidence blob.
            return 1, f"evidence producer failed to run: {exc}\n".encode()
        return completed.returncode, completed.stdout or b""

    return run


def run_producers(
    producers: Sequence[EvidenceProducer],
    *,
    runner: Callable[[EvidenceProducer], tuple[int, bytes]],
) -> tuple[ProducerOutcome, ...]:
    """Run every producer via ``runner`` and return the outcomes in order.

    The runner is injected so the caller controls execution (a real subprocess
    runner from :func:`_default_runner`, or a deterministic test runner). Every
    producer runs; advisory failures are not raised here -- the gate verdict is
    computed when the bundle is built.
    """
    outcomes: list[ProducerOutcome] = []
    for producer in producers:
        exit_code, output = runner(producer)
        outcomes.append(ProducerOutcome(producer=producer, exit_code=exit_code, output=output))
    return tuple(outcomes)


# ---------------------------------------------------------------------------
# Content-addressed blob store (size caps + gc)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredBlob:
    """The result of storing one evidence blob.

    Attributes:
        content_hash: ``sha256:``-prefixed hash of the stored (capped) bytes.
        size: Number of bytes actually stored (<= the per-blob cap).
        original_size: The pre-cap length of the producer output.
        truncated: True when the output exceeded the cap and was truncated.
    """

    content_hash: str
    size: int
    original_size: int
    truncated: bool


class EvidenceStore:
    """A content-addressed blob store with a per-blob size cap and gc.

    Blobs live under ``<root>/blobs/<hex[:2]>/<hex>`` where ``hex`` is the
    SHA-256 of the stored (capped) bytes. Because the address is the content
    hash, tampering with a stored file is detected by rehashing it at verify
    time.
    """

    def __init__(self, root: Path, *, max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES) -> None:
        # ``root`` is the ``.sdd/evidence`` directory.
        self._root = Path(root)
        self._max_blob_bytes = max(1, max_blob_bytes)

    @property
    def blobs_dir(self) -> Path:
        return self._root / "blobs"

    def blob_path(self, content_hash: str) -> Path:
        """Return the on-disk path for ``content_hash`` (need not exist)."""
        hexpart = _hex_of(content_hash)
        return self.blobs_dir / hexpart[:2] / hexpart

    def put(self, content: bytes) -> StoredBlob:
        """Store ``content`` (capped), returning its :class:`StoredBlob`.

        Storage is idempotent: an identical blob is written once.
        """
        original = len(content)
        stored = content[: self._max_blob_bytes] if original > self._max_blob_bytes else content
        c_hash = content_hash_of(stored)
        path = self.blob_path(c_hash)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(stored)
            tmp.replace(path)
        return StoredBlob(
            content_hash=c_hash,
            size=len(stored),
            original_size=original,
            truncated=original > self._max_blob_bytes,
        )

    def get(self, content_hash: str) -> bytes | None:
        """Return the stored bytes for ``content_hash`` or ``None``."""
        path = self.blob_path(content_hash)
        if not path.is_file():
            return None
        return path.read_bytes()

    def has(self, content_hash: str) -> bool:
        """Return True when a blob for ``content_hash`` is present."""
        return self.blob_path(content_hash).is_file()

    def total_size(self) -> int:
        """Return the total bytes stored across all blobs."""
        if not self.blobs_dir.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.blobs_dir.rglob("*") if p.is_file())

    def gc(self, live_hashes: set[str]) -> int:
        """Remove blobs whose content hash is not in ``live_hashes``.

        Returns the number of blobs removed. ``live_hashes`` holds the
        ``sha256:``-prefixed hashes referenced by live bundles.
        """
        if not self.blobs_dir.is_dir():
            return 0
        live_hex = {_hex_of(h) for h in live_hashes}
        removed = 0
        for path in self.blobs_dir.rglob("*"):
            if path.is_file() and path.name not in live_hex:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    logger.debug("evidence gc: could not remove %s", path)
        return removed


# ---------------------------------------------------------------------------
# Bundle item + bundle (the signed, spine-anchored receipt)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceItem:
    """One producer's captured evidence, content-addressed into the store.

    Attributes:
        name: The producer name.
        kind: The producer kind.
        required: Whether the producer gates completion.
        status: ``"pass"`` (exit 0) or ``"fail"`` (non-zero).
        exit_code: The producer exit code.
        content_hash: ``sha256:`` hash of the stored (capped) output blob.
        size: Bytes stored for the output.
        truncated: True when the output was truncated by the size cap.
        content_credential_hash: For media items, the ``sha256:`` hash of the
            stored signed C2PA manifest; empty for non-media items.
    """

    name: str
    kind: str
    required: bool
    status: str
    exit_code: int
    content_hash: str
    size: int
    truncated: bool
    content_credential_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "required": self.required,
            "status": self.status,
            "exit_code": self.exit_code,
            "content_hash": self.content_hash,
            "size": self.size,
            "truncated": self.truncated,
            "content_credential_hash": self.content_credential_hash,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EvidenceItem:
        return cls(
            name=str(row["name"]),
            kind=str(row["kind"]),
            required=bool(row["required"]),
            status=str(row["status"]),
            exit_code=int(row["exit_code"]),
            content_hash=str(row["content_hash"]),
            size=int(row["size"]),
            truncated=bool(row["truncated"]),
            content_credential_hash=str(row.get("content_credential_hash", "")),
        )


@dataclass(frozen=True)
class EvidenceBundle:
    """The signed, spine-anchored proof-of-done bundle for a task.

    The binding (``schema_version``, ``task_id``, ``items``, ``gate_passed``,
    ``timestamp``) is what gets signed and anchored; ``signature`` and
    ``journal_entry_hash`` are the bundle's chain-verifiable identity.
    """

    task_id: str
    items: tuple[EvidenceItem, ...]
    gate_passed: bool
    timestamp: int
    schema_version: int = EVIDENCE_SCHEMA_VERSION
    signer_public_key_pem: str = ""
    signature: str = ""
    journal_entry_hash: str = ""
    # Issue #2559: the declared-vs-produced output diff, inside the *binding*
    # so the undeclared-write finding is covered by the signature and the spine
    # anchor rather than being advisory metadata a tamperer could strip.
    output_diff: OutputDiff | None = None

    def _binding(self) -> dict[str, Any]:
        binding: dict[str, Any] = {
            "v": self.schema_version,
            "task_id": self.task_id,
            "items": [item.to_dict() for item in self.items],
            "gate_passed": self.gate_passed,
            "timestamp": self.timestamp,
        }
        # Dropped when absent or empty, exactly like ``trust_class`` on a
        # lineage entry: a bundle for a task that declares no outputs
        # canonicalises byte-for-byte identically to a pre-#2559 bundle, so
        # every sealed signature and spine anchor already on disk stays valid.
        if self.output_diff is not None and not self.output_diff.is_empty:
            binding["output_diff"] = self.output_diff.to_dict()
        return binding

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical bytes (signed + spine-hashed)."""
        return _canonical_bytes(self._binding())

    def bundle_hash(self) -> str:
        """Return the ``sha256:`` hash of the canonical binding bytes."""
        return _sha256_bytes(self.to_canonical_bytes())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {
            "signer_public_key_pem": self.signer_public_key_pem,
            "signature": self.signature,
            "journal_entry_hash": self.journal_entry_hash,
        }

    @classmethod
    def from_bytes(cls, raw: bytes) -> EvidenceBundle:
        row = json.loads(raw)
        raw_diff = row.get("output_diff")
        return cls(
            task_id=str(row["task_id"]),
            items=tuple(EvidenceItem.from_dict(i) for i in row.get("items", [])),
            gate_passed=bool(row["gate_passed"]),
            timestamp=int(row["timestamp"]),
            schema_version=int(row.get("v", EVIDENCE_SCHEMA_VERSION)),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            signature=str(row.get("signature", "")),
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
            output_diff=OutputDiff.from_dict(raw_diff) if isinstance(raw_diff, dict) else None,
        )

    @property
    def passed_count(self) -> int:
        return sum(1 for i in self.items if i.status == _STATUS_PASS)

    @property
    def failed_count(self) -> int:
        return sum(1 for i in self.items if i.status != _STATUS_PASS)


# ---------------------------------------------------------------------------
# Install identity (Ed25519), persisted so verify is offline
# ---------------------------------------------------------------------------


def load_or_create_evidence_identity(identity_dir: Path) -> tuple[str, str]:
    """Load (or on first use create) the install's evidence Ed25519 identity.

    The keypair is persisted under ``identity_dir`` so the same install signs
    every bundle and a verifier can check the signature offline against the
    embedded public key. The private key file is written ``0600``.

    Returns:
        ``(private_key_pem, public_key_pem)``.
    """
    private_path = identity_dir / _IDENTITY_PRIVATE_NAME
    public_path = identity_dir / _IDENTITY_PUBLIC_NAME
    if private_path.is_file() and public_path.is_file():
        # Read the raw PEM verbatim -- never strip: the signer key bytes must be
        # byte-identical to what was written or the signature will not verify.
        return (
            private_path.read_text(encoding="ascii"),
            public_path.read_text(encoding="ascii"),
        )
    identity_dir.mkdir(parents=True, exist_ok=True)
    private_pem, public_pem = generate_keypair()
    tmp_priv = private_path.with_suffix(".pem.tmp")
    tmp_priv.write_text(private_pem, encoding="ascii")
    tmp_priv.chmod(0o600)
    tmp_priv.replace(private_path)
    public_path.write_text(public_pem, encoding="ascii")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Media content-credentials (C2PA) -- media evidence flows through the spine
# ---------------------------------------------------------------------------


def _keyid_from_public_pem(public_key_pem: str) -> str:
    """Return a stable short key id derived from the public key PEM."""
    return hashlib.sha256(public_key_pem.encode("ascii")).hexdigest()[:16]


def _seal_media_credential(
    *,
    store: EvidenceStore,
    blob: StoredBlob,
    task_id: str,
    timestamp: int,
    private_key_pem: str,
    public_key_pem: str,
    install_rev: str,
) -> str:
    """Project + sign a C2PA manifest for a media blob and store it.

    The media blob is treated as a lineage artifact: a spine-entry shape carries
    its content hash, and :func:`bernstein.core.lineage.c2pa.project_manifest`
    turns it into a hard-binding-plus-actions manifest whose ``c2pa.hash.data``
    assertion pins the stored blob's content hash. The manifest is signed with
    the evidence identity's Ed25519 key -- the same key that signs the bundle --
    so one attestation root covers both "who ran this" and "what was produced".
    Returns the ``sha256:`` hash of the stored signed manifest (empty on
    failure).
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from bernstein.core.lineage.c2pa import (
            ManifestIdentity,
            manifest_to_dict,
            project_manifest,
            sign_manifest,
        )

        hexpart = _hex_of(blob.content_hash)
        artifact_path = "/".join((*_BLOB_SUBPATH, hexpart[:2], hexpart))
        entry = SpineEntry(
            v=SPINE_ENTRY_VERSION,
            prev_hash="",
            artifact_path=artifact_path,
            content_hash=blob.content_hash,
            actor=_EVIDENCE_ACTOR,
            step_id=task_id,
            model=_EVIDENCE_MODEL,
            timestamp=timestamp,
            entry_hash=compute_entry_hash(
                prev_hash="",
                artifact_path=artifact_path,
                content_hash=blob.content_hash,
                actor=_EVIDENCE_ACTOR,
                step_id=task_id,
                model=_EVIDENCE_MODEL,
                timestamp=timestamp,
            ),
            hmac="",
        )
        identity = ManifestIdentity(
            install_rev=install_rev,
            keyid=_keyid_from_public_pem(public_key_pem),
            run_id=EVIDENCE_RUN_ID,
        )
        manifest = project_manifest(artifact_path=artifact_path, entries=[entry], identity=identity)
        priv = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
        if not isinstance(priv, Ed25519PrivateKey):
            raise TypeError("evidence identity is not an Ed25519 key")
        signed = sign_manifest(manifest, signing_key=priv)
        credential = store.put(_canonical_bytes(manifest_to_dict(signed)))
    except (ValueError, TypeError, KeyError, OSError) as exc:  # pragma: no cover - defensive
        # Log only the exception type: this path handles a private key, so
        # even sanitized exception text stays out of the log stream.
        logger.debug("evidence: signed media projection skipped, exception type %s", type(exc).__name__)
        return ""
    else:
        return credential.content_hash


def _verify_media_credential(
    *,
    store: EvidenceStore,
    item: EvidenceItem,
    media_bytes: bytes,
    public_key_pem: str,
) -> bool:
    """Re-verify a media item's C2PA manifest against the stored media bytes."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        from bernstein.core.lineage.c2pa import manifest_from_dict, verify_manifest

        raw = store.get(item.content_credential_hash)
        if raw is None:
            return False
        manifest = manifest_from_dict(json.loads(raw))
        pub = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        return verify_manifest(manifest, media_bytes, pub).ok
    except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
        return False


# ---------------------------------------------------------------------------
# Build + seal (AC1)
# ---------------------------------------------------------------------------


def bundle_path(workdir: Path, task_id: str) -> Path:
    """Return the on-disk bundle path for ``task_id``."""
    return workdir.joinpath(*_BUNDLE_SUBPATH, f"{_safe_task_name(task_id)}.json")


def read_evidence_bundle(workdir: Path, task_id: str) -> EvidenceBundle | None:
    """Return the sealed bundle for ``task_id`` or ``None`` if absent."""
    path = bundle_path(workdir, task_id)
    if not path.is_file():
        return None
    try:
        return EvidenceBundle.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("evidence: malformed bundle at %s", path)
        return None


def build_evidence_bundle(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    task_id: str,
    outcomes: Sequence[ProducerOutcome],
    timestamp: int,
    store: EvidenceStore | None = None,
    chain: AuditChainStore | None = None,
    install_rev: str = "",
    output_diff: OutputDiff | None = None,
) -> EvidenceBundle:
    """Store producer outputs, bind + sign + anchor them into a bundle (AC1).

    Each outcome's output is stored content-addressed (capped). Media outputs
    additionally get a signed C2PA manifest. The items are bound into canonical
    bytes, signed with the evidence identity, and anchored in the evidence
    lineage spine; the spine entry hash is the bundle's identity. When ``chain``
    is supplied the bundle is mirrored into the HMAC audit chain via
    ``record_evidence_bundle``. The signed bundle is persisted for offline
    verification.

    Returns:
        The signed, anchored :class:`EvidenceBundle`.
    """
    store = store or EvidenceStore(workdir / ".sdd" / "evidence")

    items: list[EvidenceItem] = []
    for outcome in outcomes:
        blob = store.put(outcome.output)
        credential_hash = ""
        if outcome.producer.is_media():
            credential_hash = _seal_media_credential(
                store=store,
                blob=blob,
                task_id=task_id,
                timestamp=timestamp,
                private_key_pem=private_key_pem,
                public_key_pem=public_key_pem,
                install_rev=install_rev,
            )
        items.append(
            EvidenceItem(
                name=outcome.producer.name,
                kind=outcome.producer.kind,
                required=outcome.producer.required,
                status=_STATUS_PASS if outcome.passed else _STATUS_FAIL,
                exit_code=outcome.exit_code,
                content_hash=blob.content_hash,
                size=blob.size,
                truncated=blob.truncated,
                content_credential_hash=credential_hash,
            )
        )

    # The gate passes iff every *required* producer passed. Advisory failures
    # attach a failure record but never block (AC5).
    gate_passed = all(item.status == _STATUS_PASS for item in items if item.required)

    unsigned = EvidenceBundle(
        task_id=task_id,
        items=tuple(items),
        gate_passed=gate_passed,
        timestamp=timestamp,
        output_diff=output_diff,
    )
    payload = unsigned.to_canonical_bytes()
    signature = sign_payload(payload, private_key_pem)

    spine = LineageSpine(lineage_root, run_id=EVIDENCE_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_BUNDLE_SUBPATH, f"{_safe_task_name(task_id)}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_EVIDENCE_ACTOR,
        step_id=unsigned.bundle_hash(),
        model=_EVIDENCE_MODEL,
        timestamp=timestamp,
    )

    sealed = EvidenceBundle(
        task_id=unsigned.task_id,
        items=unsigned.items,
        gate_passed=unsigned.gate_passed,
        timestamp=unsigned.timestamp,
        signer_public_key_pem=public_key_pem,
        signature=signature,
        journal_entry_hash=anchor,
        output_diff=unsigned.output_diff,
    )
    path = bundle_path(workdir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sealed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_evidence_bundle

        record_evidence_bundle(
            chain=chain,
            task_id=task_id,
            bundle_hash=sealed.bundle_hash(),
            item_count=len(sealed.items),
            gate_passed=sealed.gate_passed,
            journal_entry_hash=anchor,
        )

    return sealed


def run_evidence_gate(
    *,
    workdir: Path,
    task_id: str,
    producers: Sequence[EvidenceProducer],
    runner: Callable[[EvidenceProducer], tuple[int, bytes]] | None = None,
    timestamp: int,
    hmac_key: bytes | None = None,
    install_rev: str = "",
    producer_timeout_s: int = 600,
    output_diff: OutputDiff | None = None,
) -> tuple[EvidenceBundle, bool]:
    """Run declared producers at gate time and seal a bundle (AC1, AC5).

    Resolves the evidence identity, HMAC key, and audit chain under
    ``workdir/.sdd``, runs every producer (via ``runner`` or a subprocess
    runner rooted at ``workdir``), builds + signs + anchors the bundle, and
    mirrors it into the audit chain. Returns ``(bundle, gate_passed)`` where
    ``gate_passed`` blocks only on required-producer failures.
    """
    workdir = Path(workdir)
    if hmac_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        hmac_key = load_or_create_audit_key()
    private_pem, public_pem = load_or_create_evidence_identity(workdir / ".sdd" / "identity")

    active_runner = runner or _default_runner(workdir, timeout=producer_timeout_s)
    outcomes = run_producers(producers, runner=active_runner)

    from bernstein.core.security.audit_chain import AuditChainStore

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
    bundle = build_evidence_bundle(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        task_id=task_id,
        outcomes=outcomes,
        timestamp=timestamp,
        chain=chain,
        install_rev=install_rev,
        output_diff=output_diff,
    )
    return bundle, bundle.gate_passed


# ---------------------------------------------------------------------------
# Verify (AC1, AC2, AC4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceVerifyResult:
    """Outcome of :func:`verify_evidence_bundle`.

    Attributes:
        ok: True only when every recomputation matches.
        reason: Human-readable failure reason (empty on success).
        bundle: The bundle under verification (``None`` when absent).
        tampered_items: Names of items whose stored blob diverged from the
            manifest (empty unless a blob was tampered).
    """

    ok: bool
    reason: str
    bundle: EvidenceBundle | None = None
    tampered_items: tuple[str, ...] = field(default_factory=tuple)


def _recompute_anchor(spine: LineageSpine, canonical: bytes) -> str | None:
    """Return the spine entry hash whose content matches ``canonical`` bytes."""
    want = content_hash_of(canonical)
    for entry in spine.iter_entries():
        if entry.content_hash == want:
            return entry.entry_hash
    return None


def verify_evidence_bundle(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    task_id: str,
    store: EvidenceStore | None = None,
) -> EvidenceVerifyResult:
    """Prove offline that ``task_id``'s bundle and every evidence file are intact.

    Recomputes, from the recorded bundle and stored blobs alone:

    * the Ed25519 signature over the canonical binding (no operator override);
    * the whole evidence spine, and the bundle's ``journal_entry_hash`` as the
      spine entry hash over the binding bytes;
    * the content hash of every stored blob against the manifest -- a tampered
      evidence file diverges and is named (AC2);
    * for media items, the signed C2PA manifest against the stored media bytes.

    ``ok`` is True only when every check passes.
    """
    store = store or EvidenceStore(workdir / ".sdd" / "evidence")

    bundle = read_evidence_bundle(workdir, task_id)
    if bundle is None:
        return EvidenceVerifyResult(ok=False, reason="no evidence bundle found")

    if not bundle.signature or not bundle.signer_public_key_pem:
        return EvidenceVerifyResult(ok=False, reason="bundle is unsigned", bundle=bundle)
    outcome = verify_payload(
        bundle.to_canonical_bytes(),
        bundle.signature,
        bundle.signer_public_key_pem,
        allow_unverified=True,
    )
    if not outcome.verified:
        return EvidenceVerifyResult(
            ok=False,
            reason=f"signature does not verify ({outcome.reason})",
            bundle=bundle,
        )

    spine = LineageSpine(lineage_root, run_id=EVIDENCE_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return EvidenceVerifyResult(
            ok=False,
            reason=f"evidence spine failed verification ({spine_result.status.value})",
            bundle=bundle,
        )
    recomputed = _recompute_anchor(spine, bundle.to_canonical_bytes())
    if recomputed is None:
        return EvidenceVerifyResult(ok=False, reason="bundle is not anchored in the evidence spine", bundle=bundle)
    if recomputed != bundle.journal_entry_hash:
        return EvidenceVerifyResult(
            ok=False,
            reason="recorded journal_entry_hash does not match the spine anchor over the bundle bytes",
            bundle=bundle,
        )

    tampered: list[str] = []
    for item in bundle.items:
        blob = store.get(item.content_hash)
        if blob is None:
            tampered.append(item.name)
            continue
        if content_hash_of(blob) != item.content_hash:
            tampered.append(item.name)
            continue
        if item.content_credential_hash and not _verify_media_credential(
            store=store,
            item=item,
            media_bytes=blob,
            public_key_pem=bundle.signer_public_key_pem,
        ):
            tampered.append(item.name)

    if tampered:
        names = ", ".join(tampered)
        return EvidenceVerifyResult(
            ok=False,
            reason=f"evidence file(s) diverge from the sealed bundle: {names}",
            bundle=bundle,
            tampered_items=tuple(tampered),
        )

    return EvidenceVerifyResult(ok=True, reason="", bundle=bundle)


def verify_all_evidence_bundles(workdir: Path, *, hmac_key: bytes) -> list[EvidenceVerifyResult]:
    """Verify every sealed bundle under ``workdir/.sdd/evidence/bundles``.

    Used by ``bernstein audit verify`` so a tampered evidence report is detected
    exactly like a tampered chain entry. Returns one result per bundle (empty
    list when no bundles exist).
    """
    lineage_root = workdir / ".sdd" / "lineage"
    bundles_dir = workdir.joinpath(*_BUNDLE_SUBPATH)
    if not bundles_dir.is_dir():
        return []
    results: list[EvidenceVerifyResult] = []
    for path in sorted(bundles_dir.glob("*.json")):
        try:
            bundle = EvidenceBundle.from_bytes(path.read_bytes())
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            results.append(EvidenceVerifyResult(ok=False, reason=f"malformed bundle at {path.name}"))
            continue
        results.append(
            verify_evidence_bundle(
                workdir=workdir,
                lineage_root=lineage_root,
                hmac_key=hmac_key,
                task_id=bundle.task_id,
            )
        )
    return results
