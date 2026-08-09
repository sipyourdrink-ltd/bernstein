"""Sigstore/Rekor cryptographic attestation for task completions.

Every task completion can be attested with a keyless signature (Fulcio CA)
and recorded in the Rekor transparency log, providing non-repudiable proof
that a specific agent produced a specific result at a specific time.

This is complementary to the HMAC-chained audit log (audit.py) - the HMAC
chain is self-contained tamper detection, while Rekor provides third-party
immutable timestamping and signing that doesn't depend on the Bernstein
infrastructure itself.

When the ``sigstore`` Python package is not installed or the network is
unavailable, the module falls back to a local Ed25519 signature stored
alongside the attestation record.  The ``fallback_used`` flag on
``AttestationRecord`` signals which path was taken.

Usage::

    from bernstein.core.security.sigstore_attestation import attest_task_completion

    record = await attest_task_completion(
        task_id="abc123",
        agent_id="claude-backend",
        diff_sha256="e3b0c44...",
        event_hmac="deadbeef...",
    )
    print(record.rekor_log_id or "fallback used")

Local bundle contract
---------------------

A ``bernstein-local-attestation/v1`` bundle is untrusted input: its
``public_key_file`` field names the key that
:func:`verify_local_attestation` checks the signature against, so a bundle
that can steer that name picks its own verdict.  Producers must therefore
emit, and consumers may rely on, a narrow shape:

* ``public_key_file`` is a **single plain filename** resolved inside
  ``attestation_dir``.  Absolute paths, drive-qualified paths, anything
  containing ``/`` or ``\\``, ``.``, ``..``, and non-string values are all
  rejected with ``ValueError``.  Writers emit ``pub_path.name``, which
  always satisfies this.
* Containment is decided from that string alone -- no ``resolve``, no
  ``stat`` -- so it cannot be raced.  Subdirectories are refused for this
  reason, not because they are inherently unsafe.
* The key is read through a descriptor anchored to ``attestation_dir``.  A
  symlink at the name is refused, as is anything that is not a regular
  file, both with ``ValueError``.
* A missing or unreadable key still raises ``OSError``: that is a local
  fault rather than a hostile bundle, and callers distinguish the two.
* The symlink refusal is atomic only where ``os.open`` supports **both**
  ``dir_fd`` and ``O_NOFOLLOW``.  Missing either one -- Windows lacks both
  -- it degrades to a best-effort check, since no atomic equivalent exists
  there.  Name containment is unaffected and holds on every platform.

None of this depends on ``attestation_dir`` itself being adversary-proof.
Write access to that directory means control of the stored signing key, at
which point an attacker signs bundles outright rather than steering them.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.util
import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


_ISO_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttestationPayload:
    """The canonical payload that is signed and recorded.

    Attributes:
        task_id: Bernstein task identifier.
        agent_id: Agent that produced the result.
        diff_sha256: SHA-256 of the task result / diff.
        event_hmac: HMAC from the HMAC-chained audit log entry for this event.
        timestamp: ISO 8601 UTC timestamp.
    """

    task_id: str
    agent_id: str
    diff_sha256: str
    event_hmac: str
    timestamp: str

    def canonical_json(self) -> str:
        """Return a deterministic JSON string suitable for signing."""
        return json.dumps(
            {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "diff_sha256": self.diff_sha256,
                "event_hmac": self.event_hmac,
                "timestamp": self.timestamp,
            },
            sort_keys=True,
        )

    def digest(self) -> str:
        """SHA-256 hex digest of the canonical JSON payload."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


@dataclass(frozen=True)
class AttestationRecord:
    """Result of attesting a task completion.

    Attributes:
        payload: The attested payload.
        rekor_log_id: Rekor transparency log entry UUID (empty if fallback).
        rekor_log_index: Rekor log index (-1 if fallback).
        bundle_path: Path to the saved attestation bundle on disk.
        signed_at: ISO 8601 timestamp of the signing operation.
        fallback_used: True when sigstore was unavailable and Ed25519 local
            signing was used instead.
        error: Human-readable error message if attestation partially failed.
    """

    payload: AttestationPayload
    rekor_log_id: str = ""
    rekor_log_index: int = -1
    bundle_path: str = ""
    signed_at: str = ""
    fallback_used: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for persistence and API responses."""
        return {
            "payload": {
                "task_id": self.payload.task_id,
                "agent_id": self.payload.agent_id,
                "diff_sha256": self.payload.diff_sha256,
                "event_hmac": self.payload.event_hmac,
                "timestamp": self.payload.timestamp,
            },
            "rekor_log_id": self.rekor_log_id,
            "rekor_log_index": self.rekor_log_index,
            "bundle_path": self.bundle_path,
            "signed_at": self.signed_at,
            "fallback_used": self.fallback_used,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Sigstore attestation (requires optional ``sigstore`` package)
# ---------------------------------------------------------------------------


def _sigstore_available() -> bool:
    """Return True if the sigstore package is installed."""
    return importlib.util.find_spec("sigstore") is not None


def _attest_with_sigstore(
    payload: AttestationPayload,
    bundle_path: Path,
) -> tuple[str, int, str]:
    """Sign the payload with Sigstore and record it in Rekor.

    Args:
        payload: The payload to sign.
        bundle_path: Where to save the .sigstore bundle file.

    Returns:
        ``(log_id, log_index, signed_at)`` on success.

    Raises:
        Exception: If signing or Rekor recording fails.
    """
    from sigstore.sign import SigningContext  # type: ignore[import-untyped]

    payload_bytes = payload.canonical_json().encode()

    # Use ambient OIDC if available (GitHub Actions, GCP, etc.),
    # fall back to interactive browser flow for local development.
    ctx = SigningContext.production()
    with ctx.signer(identity_token=_get_identity_token()) as signer:
        result = signer.sign_artifact(input_=payload_bytes)

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(result.to_bundle().to_json())

    entry = result.transparency_log_entry
    log_id: str = getattr(entry, "uuid", "") or getattr(entry, "log_id", "")
    log_index: int = int(getattr(entry, "log_index", -1))
    signed_at = datetime.now(tz=UTC).strftime(_ISO_TIMESTAMP_FMT)

    logger.info(
        "Sigstore attestation recorded: task=%s rekor_index=%d",
        payload.task_id,
        log_index,
    )
    return log_id, log_index, signed_at


def _get_identity_token() -> str | None:
    """Retrieve OIDC identity token from the environment if available.

    Checks common CI/CD token sources (GitHub OIDC, Google OIDC).
    Returns None to trigger interactive browser flow.
    """
    # GitHub Actions OIDC
    if os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
        try:
            import urllib.request

            from bernstein.core.security.url_allowlist import ensure_http_url

            request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
            request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
            url = f"{request_url}&audience=sigstore"
            # ACTIONS_ID_TOKEN_REQUEST_URL is injected by GitHub Actions and
            # is always https in legitimate runs; rejecting other schemes
            # blocks tampered env vars from redirecting the OIDC fetch to a
            # local file or attacker-controlled scheme.
            ensure_http_url(url, source="sigstore.actions_oidc")
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"bearer {request_token}"},
            )
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return str(data.get("value", ""))
        except Exception as exc:
            # Only the exception (no token value) is logged here.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.debug("Failed to fetch GitHub OIDC token: %s", exc)

    return None  # sigstore will trigger interactive flow


# ---------------------------------------------------------------------------
# Fallback: local Ed25519 signing (cryptography package)
# ---------------------------------------------------------------------------


def _attest_with_ed25519_fallback(
    payload: AttestationPayload,
    bundle_path: Path,
    attestation_dir: Path,
) -> str:
    """Sign the payload locally with an Ed25519 key as a fallback.

    The signing key is stored in ``<attestation_dir>/ed25519-signing-key.pem``
    (auto-generated if absent).  The public key is written alongside it.

    Args:
        payload: The payload to sign.
        bundle_path: Where to save the attestation JSON.
        attestation_dir: Directory for the signing key.

    Returns:
        ISO 8601 timestamp of the signing operation.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    key_path = attestation_dir / "ed25519-signing-key.pem"
    pub_path = attestation_dir / "ed25519-public-key.pem"

    if key_path.exists():
        private_key = Ed25519PrivateKey.from_private_bytes(
            serialization.load_pem_private_key(
                key_path.read_bytes(),
                password=None,
            ).private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
    else:
        private_key = Ed25519PrivateKey.generate()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        key_path.write_bytes(pem)
        key_path.chmod(0o600)
        pub_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_path.write_bytes(pub_pem)
        logger.info("Generated Ed25519 attestation key: %s", key_path)

    payload_bytes = payload.canonical_json().encode()
    signature = private_key.sign(payload_bytes)
    signed_at = datetime.now(tz=UTC).strftime(_ISO_TIMESTAMP_FMT)

    bundle: dict[str, Any] = {
        "schema": "bernstein-local-attestation/v1",
        "payload_digest": payload.digest(),
        "payload": json.loads(payload.canonical_json()),
        "signature_hex": signature.hex(),
        "public_key_file": pub_path.name,
        "signed_at": signed_at,
        "note": (
            "Local Ed25519 fallback - sigstore package unavailable or "
            "network unreachable. Install sigstore for Rekor attestation."
        ),
    }
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps(bundle, indent=2))
    return signed_at


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class AttestationConfig:
    """Configuration for the attestation subsystem.

    Attributes:
        attestation_dir: Directory to store attestation bundles and keys.
        require_rekor: Raise an error if Rekor recording fails (default False
            - falls back to local signing).
    """

    attestation_dir: Path
    require_rekor: bool = False


def attest_task_completion(
    task_id: str,
    agent_id: str,
    diff_sha256: str,
    event_hmac: str,
    config: AttestationConfig | None = None,
    attestation_dir: Path | None = None,
) -> AttestationRecord:
    """Attest a task completion with a cryptographic signature.

    Attempts Sigstore keyless attestation (Fulcio + Rekor).  If sigstore is
    not installed or the network is unavailable, falls back to local Ed25519
    signing.

    Args:
        task_id: Bernstein task identifier.
        agent_id: Agent that produced the result.
        diff_sha256: SHA-256 of the task result / diff content.
        event_hmac: HMAC from the audit chain entry for this event.
        config: Attestation configuration.  Either ``config`` or
            ``attestation_dir`` must be provided.
        attestation_dir: Directory for attestation bundles (ignored when
            ``config`` is provided).

    Returns:
        AttestationRecord with signing details.

    Raises:
        ValueError: If neither ``config`` nor ``attestation_dir`` is given.
        RuntimeError: If ``config.require_rekor`` is True and Rekor fails.
    """
    if config is None:
        if attestation_dir is None:
            msg = "Provide either config or attestation_dir"
            raise ValueError(msg)
        from pathlib import Path as _Path  # avoid circular

        config = AttestationConfig(attestation_dir=_Path(attestation_dir))

    timestamp = datetime.now(tz=UTC).strftime(_ISO_TIMESTAMP_FMT)
    payload = AttestationPayload(
        task_id=task_id,
        agent_id=agent_id,
        diff_sha256=diff_sha256,
        event_hmac=event_hmac,
        timestamp=timestamp,
    )

    bundle_filename = f"attestation-{task_id}-{payload.digest()[:12]}.json"
    bundle_path = config.attestation_dir / bundle_filename

    # --- Try Sigstore first -----------------------------------------------
    if _sigstore_available():
        try:
            bundle_path_sigstore = bundle_path.with_suffix(".sigstore")
            log_id, log_index, signed_at = _attest_with_sigstore(payload, bundle_path_sigstore)
            record = AttestationRecord(
                payload=payload,
                rekor_log_id=log_id,
                rekor_log_index=log_index,
                bundle_path=str(bundle_path_sigstore),
                signed_at=signed_at,
                fallback_used=False,
            )
            _save_record_index(config.attestation_dir, record)
            return record
        except Exception as exc:
            logger.warning("Sigstore attestation failed, using local fallback: %s", exc)
            if config.require_rekor:
                msg = f"Rekor attestation required but failed: {exc}"
                raise RuntimeError(msg) from exc

    # --- Fallback: local Ed25519 ------------------------------------------
    signed_at = _attest_with_ed25519_fallback(payload, bundle_path, config.attestation_dir)
    record = AttestationRecord(
        payload=payload,
        rekor_log_id="",
        rekor_log_index=-1,
        bundle_path=str(bundle_path),
        signed_at=signed_at,
        fallback_used=True,
    )
    _save_record_index(config.attestation_dir, record)
    return record


def load_attestation_record(bundle_path: Path) -> dict[str, Any]:
    """Load an attestation bundle from disk.

    Args:
        bundle_path: Path to the attestation JSON file.

    Returns:
        Parsed attestation bundle dict.
    """
    return json.loads(bundle_path.read_text())  # type: ignore[return-value]


def list_attestations(attestation_dir: Path) -> list[dict[str, Any]]:
    """List all attestation records from the index file.

    Args:
        attestation_dir: Directory containing attestation bundles.

    Returns:
        List of attestation record dicts, newest first.
    """
    index_path = attestation_dir / "attestations.jsonl"
    if not index_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in index_path.read_text().splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return list(reversed(records))


def verify_local_attestation(bundle_path: Path, attestation_dir: Path) -> bool:
    """Verify an Ed25519 fallback attestation bundle.

    Args:
        bundle_path: Path to the attestation JSON file.
        attestation_dir: Directory containing the public key.

    Returns:
        True if the signature is valid.

    Raises:
        ValueError: If the bundle is not a local Ed25519 bundle.
    """
    from cryptography.hazmat.primitives import serialization

    bundle = json.loads(bundle_path.read_text())
    if bundle.get("schema") != "bernstein-local-attestation/v1":
        msg = "Not a local Ed25519 attestation bundle"
        raise ValueError(msg)

    # ``public_key_file`` comes from the untrusted bundle, so it decides which
    # key the signature is checked against: steer it outside the attestation
    # directory and any payload signed with an attacker-held key verifies.
    #
    # Containment is decided from the name alone, with no filesystem lookup.
    # Resolving a path to prove it is contained and then opening that path are
    # two separate lookups, and everything between them is a window; a name
    # that is a single plain component cannot leave the directory it is opened
    # relative to, so there is no window to begin with. The writer stores
    # ``pub_path.name``, so nothing legitimate is absolute, descends into a
    # subdirectory, or walks upwards.
    raw_key_name = bundle["public_key_file"]
    if _escapes_directory(raw_key_name):
        msg = f"Path traversal detected in public_key_file: {raw_key_name!r}"
        raise ValueError(msg)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    public_key = serialization.load_pem_public_key(_read_contained_key_bytes(attestation_dir, raw_key_name))
    if not isinstance(public_key, Ed25519PublicKey):
        # The schema says Ed25519, but the key file is named by the same
        # untrusted bundle, so the schema is not evidence about the key. Only
        # ``Ed25519PublicKey.verify`` takes ``(signature, data)``; every other
        # algorithm needs padding or a hash argument. Without this the mismatch
        # arrives as a ``TypeError`` swallowed by the ``except Exception``
        # below, which reaches the same ``False`` by accident rather than by
        # decision - and leaves nothing for a reader to check.
        return False

    payload_bytes = AttestationPayload(**bundle["payload"]).canonical_json().encode()
    signature = bytes.fromhex(bundle["signature_hex"])

    try:
        public_key.verify(signature, payload_bytes)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _escapes_directory(raw_key_name: object) -> bool:
    """Whether ``raw_key_name`` could name anything but a file in one directory.

    Decided from the string alone -- no ``resolve``, no ``stat``, nothing the
    filesystem can change underneath the answer.  Only a single plain
    component passes: anything absolute, drive-qualified, separated, or
    self/parent-referential is rejected, as is a non-string, which a crafted
    bundle can carry just as easily as a traversing name.

    Args:
        raw_key_name: The bundle-supplied ``public_key_file`` value.

    Returns:
        True when the value must be refused.
    """
    if not isinstance(raw_key_name, str) or not raw_key_name:
        return True
    drive, _ = os.path.splitdrive(raw_key_name)
    return bool(
        drive
        or os.path.isabs(raw_key_name)
        or raw_key_name in {os.curdir, os.pardir}
        or "/" in raw_key_name
        or "\\" in raw_key_name
    )


def _read_contained_key_bytes(attestation_dir: Path, raw_key_name: str) -> bytes:
    """Read the bundle-named public key from exactly one directory lookup.

    ``attestation_dir`` is resolved once, into a descriptor, and the key is
    opened relative to that descriptor.  Nothing re-derives the directory
    afterwards, so there is no interval in which swapping it could redirect
    the read; :func:`_escapes_directory` has already established that the name
    is a single component, which is what makes an anchored open sufficient.

    ``O_NOFOLLOW`` then refuses a symlink at that component, and ``fstat``
    requires a regular file, so a crafted bundle surfaces as the ``ValueError``
    callers already handle instead of a stray ``OSError``.

    Args:
        attestation_dir: Directory the key must be read from.
        raw_key_name: Bundle-supplied name, already checked by
            :func:`_escapes_directory`.

    Returns:
        The bytes of the key file.

    Raises:
        ValueError: If the name is a symlink or not a regular file.
    """
    # ``O_BINARY`` matters on Windows, where ``os.open`` would otherwise
    # translate line endings and hand ``load_pem_public_key`` altered bytes.
    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    dir_fd: int | None = None
    # Both capabilities are required, not just ``dir_fd``: the anchored open
    # refuses a symlink only because ``O_NOFOLLOW`` is among its flags, so
    # selecting that branch on ``dir_fd`` alone would, on a platform offering
    # one without the other, take the branch that has no best-effort check
    # while dropping the flag that would have made it unnecessary.
    can_anchor = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
    try:
        if can_anchor:
            dir_fd = os.open(attestation_dir, os.O_RDONLY)
            fd = os.open(raw_key_name, open_flags, dir_fd=dir_fd)
        else:
            # Windows reaches this branch, as does any platform missing
            # either capability. A single component still cannot escape the
            # directory it is joined to, so containment holds; what is
            # missing is an atomic refusal to follow a link at that
            # component. This check is therefore best effort by construction
            # -- it catches a symlink that is present, not one planted
            # between here and the open -- and it is the most such a platform
            # offers.
            key_path = attestation_dir / raw_key_name
            if key_path.is_symlink():
                msg = f"public_key_file is a symlink, refusing to follow it: {raw_key_name!r}"
                raise ValueError(msg)
            fd = os.open(key_path, open_flags)
    except OSError as exc:
        # O_NOFOLLOW reports a symlink at the final component as ELOOP.  Every
        # other OSError (a missing or unreadable key) is a genuine local fault
        # and keeps propagating as it did before.
        if exc.errno == errno.ELOOP:
            msg = f"public_key_file is a symlink, refusing to follow it: {raw_key_name!r}"
            raise ValueError(msg) from exc
        raise
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            msg = f"public_key_file is not a regular file: {raw_key_name!r}"
            raise ValueError(msg)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _save_record_index(attestation_dir: Path, record: AttestationRecord) -> None:
    """Append the attestation record to the JSONL index file."""
    attestation_dir.mkdir(parents=True, exist_ok=True)
    index_path = attestation_dir / "attestations.jsonl"
    with index_path.open("a") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
