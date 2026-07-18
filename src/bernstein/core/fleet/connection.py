"""Named connection documents for the fleet config plane (#2550).

A connection document is a typed, named record - ``prod-github``,
``team-slack`` - that task specs, routines, and triggers reference by name.
It carries **no secret material**: it holds a *reference* to a
broker-managed secret (``broker_ref`` - an environment variable name, a Vault
path, an AWS secret id), a scope, and connector defaults, and it is signed
with the local Ed25519 install identity. The reference shape is enforced
where the document is written, so a pasted credential cannot be signed and
persisted in its place, and the value behind the reference is read only
inside :meth:`SecretsBroker.mint`. Documents written before that check
existed still load, with a warning naming the command that rotates them.
The naming and reuse layer sits *above* the secrets broker's mint / resolve
/ revoke lifecycle and changes nothing that lifecycle owns.

Three substrate-coupled properties make it more than a config file:

* **Install-bound signature (isolation).** The document is signed by the
  local install identity and :func:`resolve_document` verifies against the
  *local* identity, not the embedded key. A document copied to another
  install therefore refuses to resolve, and the refusal is a recorded
  ``fleet.conn_refuse`` chain event - not a silent denial.

* **Broker-only resolution + lineage receipt (verifiability).** Resolution
  runs only through :meth:`SecretsBroker.mint`, so the raw secret is minted
  into a short-lived token and registered for redaction; it never reaches an
  agent environment or persisted artifact. Each resolution emits a
  ``fleet.conn_resolve`` receipt binding ``(name, document hash, task id,
  token id)``, so :func:`audit_resolutions` reconstructs every resolving task
  offline from the chain alone.

* **Rotation as a signed supersede.** Rotating one document re-points every
  consumer at the next mint with zero task-spec edits (consumers reference by
  name), and the rotation is a signed ``fleet.conn_rotate`` chain event.

This module never imports the CLI or a running server.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import (
    AgentCard,
    load_or_create_signing_identity,
    sign_detached,
    verify_detached,
)
from bernstein.core.security.audit_chain import (
    EVENT_FLEET_CONN_RESOLVE,
    record_fleet_conn_create,
    record_fleet_conn_refuse,
    record_fleet_conn_resolve,
    record_fleet_conn_rotate,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.security.secrets_broker import MintedToken, SecretsBroker

__all__ = [
    "ConnectionDocument",
    "ConnectionDocumentStore",
    "ConnectionReferenceError",
    "ConnectionRefused",
    "ResolutionReceipt",
    "audit_resolutions",
    "create_document",
    "resolve_document",
    "rotate_document",
    "verify_document_local",
]

logger = logging.getLogger(__name__)

#: Domain-separation tag folded into every signing preimage so a connection
#: document signature can never be replayed as some other install artifact.
_SIGN_DOMAIN = b"bernstein.fleet.conn.v1\x00"

#: Fixed key id for the install identity that signs connection documents.
FLEET_CONN_KID = "bernstein/fleet-conn/v1"

_IDENTITY_PRIVATE_NAME = "fleet_conn_signing.pem"
_IDENTITY_PUBLIC_NAME = "fleet_conn_signing.pub"


class ConnectionRefused(Exception):
    """Raised when a connection document refuses to resolve."""


class ConnectionReferenceError(ValueError):
    """Raised when a broker reference does not look like a lookup reference.

    Subclasses :class:`ValueError` so existing callers keep working, while
    giving a caller something narrow enough to report as operator input error
    without also swallowing an unrelated failure from the same call.
    """


@dataclass(frozen=True)
class ConnectionDocument:
    """A signed, named connection document. Carries no secret material.

    :attr:`broker_ref` is a *lookup reference* into the secrets broker - an
    environment variable name, a Vault path, an AWS secret id - and never the
    value behind it. Only :meth:`SecretsBroker.mint` ever holds the value, and
    only for the lifetime of a mint. The field is named for what it holds so
    the on-disk document is not mistaken for a credential store; the wire key
    stays ``secret_name`` because it is inside the signed preimage.

    Construction does **not** enforce the reference shape. The shape is
    enforced where the bytes are written (:meth:`ConnectionDocumentStore.put`),
    not where they are read: refusing to parse a document that is already on
    disk cannot un-write it, and would strand a document an earlier release
    accepted. See :func:`_validate_broker_ref`.
    """

    name: str
    broker_ref: str
    scope: str
    connector_defaults: dict[str, Any] = field(default_factory=dict)
    signer_public_key_pem: str = ""
    signature: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        # Own a deep copy of the connector defaults so a caller mutating the
        # dict it passed in cannot alter a document after it was signed, which
        # would otherwise desync the persisted bytes from the recorded
        # document hash.
        object.__setattr__(self, "connector_defaults", copy.deepcopy(self.connector_defaults))

    def _payload(self, *, include_signature: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            # Wire key is load-bearing: it is inside the signed preimage and
            # the document hash, so it is fixed for every document ever signed.
            "secret_name": self.broker_ref,
            "scope": self.scope,
            "connector_defaults": self.connector_defaults,
            "signer_public_key_pem": self.signer_public_key_pem,
            "version": self.version,
        }
        if include_signature:
            payload["signature"] = self.signature
        return payload

    def unsigned_canonical_bytes(self) -> bytes:
        """Return the canonical bytes that are signed (excludes the signature)."""
        return json.dumps(
            self._payload(include_signature=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    def document_hash(self) -> str:
        """Return the ``sha256:`` content hash of the unsigned canonical bytes."""
        return "sha256:" + hashlib.sha256(self.unsigned_canonical_bytes()).hexdigest()

    def to_json(self) -> str:
        """Serialise the full signed document to canonical JSON."""
        return json.dumps(
            self._payload(include_signature=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> ConnectionDocument:
        """Parse a document previously produced by :meth:`to_json`."""
        data = json.loads(raw)
        return cls(
            name=data["name"],
            broker_ref=data["secret_name"],
            scope=data.get("scope", ""),
            connector_defaults=data.get("connector_defaults", {}),
            signer_public_key_pem=data.get("signer_public_key_pem", ""),
            signature=data.get("signature", ""),
            version=int(data.get("version", 1)),
        )


@dataclass(frozen=True)
class ResolutionReceipt:
    """A projected ``fleet.conn_resolve`` receipt (reconstructed from chain)."""

    name: str
    document_hash: str
    task_id: str
    token_id: str


class ConnectionDocumentStore:
    """Filesystem-backed store of connection documents, keyed by name.

    Conventionally rooted at ``<sdd>/fleet/connections``. Documents are stored
    one JSON file per name. What lands on disk is the signed document: a name,
    a broker *reference*, a scope, and connector defaults. The value behind the
    reference is never read on this path, so it can never be written here.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, name: str) -> Path:
        _validate_name(name)
        return self._root / f"{name}.json"

    def put(self, doc: ConnectionDocument) -> None:
        """Persist *doc* under its name (atomic write).

        This is the write sink, so it is where the reference shape is
        enforced: nothing that fails :func:`_validate_broker_ref` reaches
        disk, and the check runs before the file is created.

        Raises:
            ConnectionReferenceError: If the document's broker reference does
                not have reference shape.
        """
        _validate_broker_ref(doc.broker_ref)
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(doc.name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(doc.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, name: str) -> ConnectionDocument:
        """Load the document named *name*.

        The document's embedded ``name`` must match the requested name, so a
        file renamed or swapped on disk cannot resolve under a name it was not
        signed for.

        A document written before the reference shape was enforced still
        loads. It is reported once per read at warning level with the exact
        command that remediates it, because refusing to parse it would strand
        an operator on an upgrade with no way back: rotation is the only fix
        and rotation has to load the old document first.

        Raises:
            KeyError: If no document is stored under *name*.
            ValueError: If the stored document's embedded name differs from
                the requested name.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)
        doc = ConnectionDocument.from_json(path.read_text(encoding="utf-8"))
        if doc.name != name:
            raise ValueError(f"connection document name mismatch: requested {name!r}, embedded {doc.name!r}")
        if not _is_reference_shaped(doc.broker_ref):
            # %r so a name or reference carrying CR/LF cannot forge a record.
            logger.warning(
                "connection document %r holds a broker reference that is not reference-shaped (%r). It was "
                "written before the shape was enforced and still resolves, but it should name a secret, not "
                "carry one. Remediate with: bernstein conn rotate %r --secret <name>",
                doc.name,
                _describe_ref(doc.broker_ref),
                doc.name,
            )
        return doc

    def exists(self, name: str) -> bool:
        """Return ``True`` if a document is stored under *name*."""
        return self._path(name).exists()

    def list_names(self) -> list[str]:
        """Return the names of all stored documents, sorted."""
        if not self._root.exists():
            return []
        return sorted(p.stem for p in self._root.glob("*.json"))


def _validate_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
        raise ValueError(f"invalid connection document name: {name!r}")


#: Upper bound on a broker lookup reference. Real references are short - an
#: environment variable name, a Vault path, an AWS secret id. The cap is
#: generous for those and well under the size of a key blob.
_MAX_BROKER_REF_LEN = 256


def _is_reference_shaped(broker_ref: str) -> bool:
    """Return True when *broker_ref* has the shape of a lookup reference.

    A reference is a single line of printable characters with no whitespace
    and a bounded length. This is the predicate; :func:`_validate_broker_ref`
    is the enforcing form that raises with a specific message.
    """
    if not broker_ref or len(broker_ref) > _MAX_BROKER_REF_LEN:
        return False
    return not any(ch.isspace() or not ch.isprintable() for ch in broker_ref)


def _describe_ref(broker_ref: str) -> str:
    """Return a shape summary of *broker_ref* that never reveals its value.

    A reference that fails the shape check may be a pasted credential, so the
    operator warning has to describe it without reproducing it - logging the
    value would be the exact leak the check exists to prevent.
    """
    if not broker_ref:
        return "<empty>"
    traits: list[str] = []
    if len(broker_ref) > _MAX_BROKER_REF_LEN:
        traits.append("over length cap")
    if any(ch == "\n" or ch == "\r" for ch in broker_ref):
        traits.append("multi-line")
    elif any(ch.isspace() for ch in broker_ref):
        traits.append("contains whitespace")
    if any(not ch.isprintable() and not ch.isspace() for ch in broker_ref):
        traits.append("contains control characters")
    return f"<{len(broker_ref)} chars, {', '.join(traits) or 'unrecognised shape'}>"


def _validate_broker_ref(broker_ref: str) -> None:
    """Reject a broker reference that does not look like a lookup reference.

    The document is signed and persisted in the clear, so the invariant that
    it names a secret rather than carrying one is enforced where the bytes are
    written (:meth:`ConnectionDocumentStore.put`). Pasted key material - a PEM
    block, a JSON service-account blob, a wrapped token - carries newlines,
    spaces, or length and is refused before it can reach disk.

    Deliberately *not* enforced on the read path. A document written by an
    earlier release may hold a reference this check would reject; refusing to
    parse it cannot un-write it, and would leave the operator unable to load,
    list, or rotate it. :meth:`ConnectionDocumentStore.get` accepts such a
    document and warns instead.

    This is a shape check, not a proof of non-secrecy: a short opaque token
    is indistinguishable from a short opaque reference. It removes the
    accidents (a pasted multi-line credential) and pins the documented
    contract to an assertion the type actually enforces.

    Raises:
        ConnectionReferenceError: If *broker_ref* is empty, over-long, or not
            a single whitespace-free printable line.
    """
    if not broker_ref:
        raise ConnectionReferenceError("connection document broker reference must not be empty")
    if len(broker_ref) > _MAX_BROKER_REF_LEN:
        raise ConnectionReferenceError(
            f"connection document broker reference is {len(broker_ref)} chars, "
            f"over the {_MAX_BROKER_REF_LEN}-char cap; it must name a secret, not carry one"
        )
    if any(ch.isspace() or not ch.isprintable() for ch in broker_ref):
        raise ConnectionReferenceError(
            "connection document broker reference must be a single printable line "
            "with no whitespace; it must name a secret, not carry one"
        )


def _digest_broker_ref(broker_ref: str) -> str:
    """Return an install-keyed digest of *broker_ref* for chain records.

    The raw reference never lands in the audit chain; only a keyed digest. A
    plain hash would let a chain reader enumerate likely references by hashing
    candidates, so the digest is an HMAC under the local install audit key:
    reproducible on this install (so ``conn audit`` can correlate) but not
    precomputable by a reader who lacks the key.
    """
    from bernstein.core.security.audit import load_or_create_audit_key

    key = load_or_create_audit_key()
    return "hmac-sha256:" + hmac.new(key, broker_ref.encode("utf-8"), hashlib.sha256).hexdigest()


def _local_identity(identity_dir: Path) -> tuple[str, str]:
    return load_or_create_signing_identity(
        Path(identity_dir),
        private_name=_IDENTITY_PRIVATE_NAME,
        public_name=_IDENTITY_PUBLIC_NAME,
    )


def _sign(doc: ConnectionDocument, private_key_pem: str) -> ConnectionDocument:
    signature = sign_detached(
        _SIGN_DOMAIN + doc.unsigned_canonical_bytes(),
        private_key_pem,
        kid=FLEET_CONN_KID,
    )
    return replace(doc, signature=signature)


def verify_document_local(doc: ConnectionDocument, *, identity_dir: Path) -> bool:
    """Return ``True`` iff *doc* was signed by the *local* install identity.

    Verification uses the local install public key, not the key embedded in
    the document, so a document copied from another install fails here even
    though it is internally self-consistent.
    """
    _, local_pub = _local_identity(identity_dir)
    card = AgentCard(agent_id="install", kid=FLEET_CONN_KID, public_key_pem=local_pub)
    return verify_detached(_SIGN_DOMAIN + doc.unsigned_canonical_bytes(), doc.signature, card)


def create_document(
    *,
    name: str,
    broker_ref: str,
    scope: str,
    connector_defaults: dict[str, Any] | None,
    identity_dir: Path,
    chain: AuditChainStore,
    store: ConnectionDocumentStore,
) -> ConnectionDocument:
    """Create, sign, record, and persist a new connection document.

    Refuses to overwrite an existing document (use :func:`rotate_document`
    to change one). The audit record is written before the document is
    persisted, so a document can never exist on disk without its create
    receipt on the chain.

    ``broker_ref`` names a broker-managed secret; the value behind it is never
    read, signed, or persisted here.

    Raises:
        FileExistsError: If a document already exists under *name*.
        ValueError: If *name* is malformed.
        ConnectionReferenceError: If *broker_ref* is malformed.
    """
    _validate_name(name)
    # Validate before the chain record is written. `store.put` enforces this
    # too, but the create receipt is recorded first, so refusing only at the
    # write sink would leave a receipt on the chain for a document that never
    # reached disk.
    _validate_broker_ref(broker_ref)
    if store.exists(name):
        raise FileExistsError(f"connection document {name!r} already exists; use rotate to change it")
    private_key_pem, public_key_pem = _local_identity(identity_dir)
    unsigned = ConnectionDocument(
        name=name,
        broker_ref=broker_ref,
        scope=scope,
        connector_defaults=dict(connector_defaults or {}),
        signer_public_key_pem=public_key_pem,
        version=1,
    )
    doc = _sign(unsigned, private_key_pem)
    record_fleet_conn_create(
        chain=chain,
        name=name,
        document_hash=doc.document_hash(),
        secret_name_digest=_digest_broker_ref(broker_ref),
    )
    store.put(doc)
    return doc


def rotate_document(
    name: str,
    *,
    identity_dir: Path,
    chain: AuditChainStore,
    store: ConnectionDocumentStore,
    new_broker_ref: str | None = None,
    new_scope: str | None = None,
    new_connector_defaults: dict[str, Any] | None = None,
) -> ConnectionDocument:
    """Rotate the document named *name* and record a signed rotation event.

    Consumers reference the document by name, so rotation re-points all of
    them at the next mint with zero task-spec edits. The current document's
    signature is verified against the local install identity before rotation,
    so a tampered or foreign document on disk cannot be laundered into a
    freshly locally-signed one. The rotation receipt is recorded before the
    new document is persisted.

    Raises:
        ConnectionRefused: If the current document is not signed by the local
            install identity.
    """
    current = store.get(name)
    # The reference the rotation will persist: the new one when given, else
    # the current one carried forward. Validated before the rotate receipt is
    # recorded, for the same reason as create.
    if new_broker_ref is not None:
        _validate_broker_ref(new_broker_ref)
    elif not _is_reference_shaped(current.broker_ref):
        # Rotating a legacy document without supplying a new reference would
        # re-persist the unshaped one. Name the remedy instead of reporting
        # this against a --secret argument the operator never passed.
        raise ConnectionReferenceError(
            f"connection document {name!r} holds a broker reference written before the shape was "
            f"enforced ({_describe_ref(current.broker_ref)}); rotating it forward would persist it "
            f"again. Pass a new reference: bernstein conn rotate {name!r} --secret <name>"
        )
    if not verify_document_local(current, identity_dir=identity_dir):
        record_fleet_conn_refuse(
            chain=chain,
            name=name,
            document_hash=current.document_hash(),
            reason="signature_verification_failed",
        )
        raise ConnectionRefused(f"connection document {name!r} is not signed by the local install identity")
    private_key_pem, public_key_pem = _local_identity(identity_dir)
    unsigned = ConnectionDocument(
        name=name,
        broker_ref=new_broker_ref if new_broker_ref is not None else current.broker_ref,
        scope=new_scope if new_scope is not None else current.scope,
        connector_defaults=(
            dict(new_connector_defaults) if new_connector_defaults is not None else current.connector_defaults
        ),
        signer_public_key_pem=public_key_pem,
        version=current.version + 1,
    )
    rotated = _sign(unsigned, private_key_pem)
    record_fleet_conn_rotate(
        chain=chain,
        name=name,
        old_document_hash=current.document_hash(),
        new_document_hash=rotated.document_hash(),
        secret_name_digest=_digest_broker_ref(rotated.broker_ref),
    )
    store.put(rotated)
    return rotated


def resolve_document(
    name: str,
    *,
    identity_dir: Path,
    task_id: str,
    broker: SecretsBroker,
    chain: AuditChainStore,
    store: ConnectionDocumentStore,
    ttl_seconds: int | None = None,
) -> MintedToken:
    """Resolve the document *name* to a short-lived broker token.

    The document's signature is verified against the local install identity
    first; on failure a ``fleet.conn_refuse`` event is recorded and
    :class:`ConnectionRefused` is raised. On success the broker mints a
    short-lived token (registering the raw secret for redaction), and a
    ``fleet.conn_resolve`` lineage receipt is recorded.
    """
    doc = store.get(name)
    if not verify_document_local(doc, identity_dir=identity_dir):
        record_fleet_conn_refuse(
            chain=chain,
            name=name,
            document_hash=doc.document_hash(),
            reason="signature_verification_failed",
        )
        raise ConnectionRefused(f"connection document {name!r} is not signed by the local install identity")

    token = broker.mint(secret_name=doc.broker_ref, task_id=task_id, ttl_seconds=ttl_seconds)
    record_fleet_conn_resolve(
        chain=chain,
        name=name,
        document_hash=doc.document_hash(),
        task_id=task_id,
        token_id=token.token_id,
    )
    return token


def audit_resolutions(chain: AuditChainStore, *, name: str | None = None) -> list[ResolutionReceipt]:
    """Reconstruct every document resolution from the chain, oldest first.

    With ``name`` given, only that document's resolutions are returned. This
    resolves offline from the chain alone - no server, no live store.
    """
    receipts: list[ResolutionReceipt] = []
    for event in chain.query(event_type=EVENT_FLEET_CONN_RESOLVE):
        details = event.details
        if name is not None and details.get("name") != name:
            continue
        receipts.append(
            ResolutionReceipt(
                name=str(details.get("name", "")),
                document_hash=str(details.get("document_hash", "")),
                task_id=str(details.get("task_id", "")),
                token_id=str(details.get("token_id", "")),
            )
        )
    return receipts
