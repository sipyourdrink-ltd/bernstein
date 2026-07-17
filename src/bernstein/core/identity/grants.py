"""Chain-anchored, Ed25519-signed per-task credential grants (issue #2516).

A worker fleet should never hand each task the install-wide static credential.
Instead the orchestrator writes a *scoped grant* before a worker spawn: the
task id, the backing secret name, the audience the token may be presented to,
an expiry, and a capability ceiling. The grant is the authorization artifact --
the secrets broker refuses to mint a downstream token without a grant that
verifies -- and it is recorded as a record in the same HMAC-chained
construction as the per-hop delegation receipts
(:mod:`bernstein.core.identity.delegation`), anchored to the install-scoped
audit key.

Killer shape
------------
The grant is not "a secret plus a log line". The grant record *is* a signed
chain event, and the broker exchange references it. Strip the audit chain and
the grant loses its meaning, not just its observability: an ordinary token
vault cannot prove to a third party which task held which credential power over
which time window, and who authorized it. Here that proof is the chain itself.

Two anchors per record
----------------------
Each record carries two independent tamper anchors:

* an **Ed25519 signature** by the manager identity over the grant's canonical
  body -- proves *who authorized* the grant; and
* an **HMAC** over ``prev_hmac`` concatenated with the canonical record body,
  keyed by the install audit key (``load_or_create_audit_key``) -- the same
  construction as :mod:`delegation`, so linkage, deletion, and reordering
  surface as a chain break.

A verifier holding only the chain slice and the install audit key reconstructs
the full ``grant_issued -> grant_exchanged -> grant_revoked`` lifecycle offline
and rejects any mutated, deleted, or reordered record, naming the offender. The
issuer public key travels inside each record, so the signature layer is
self-describing; an attacker who swaps it cannot re-chain the HMAC without the
install key.

Record shape
------------
One JSONL line per record under ``<root>/grants/<run_id>.jsonl``::

    {
      "run_id": "...",
      "record_index": 0,
      "kind": "grant_issued",           # issued | exchanged | revoked | refused
      "grant_id": "...",
      "task_id": "t-42",
      "secret_name": "sha256:<hex>",    # digest only -- the raw backend
                                         # secret/key name is never persisted
      "audience": "api.anthropic.com",
      "expiry": 1730000900,             # epoch seconds; 0 == no explicit expiry
      "capability_ceiling": ["read"],   # sorted, canonical
      "issuer": "manager:...",          # or a spiffe:// id under SPIFFE mode
      "issuer_pubkey": "-----BEGIN PUBLIC KEY----- ...",
      "token_id": "",                   # set on grant_exchanged
      "reason": "",                     # set on revoked / refused
      "created": 1730000000,
      "prev_hmac": "<hex>",
      "signature": "<hex ed25519>",
      "hmac": "<hex hmac>"
    }
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_ROOT",
    "GENESIS_HMAC",
    "GRANT_EXCHANGED",
    "GRANT_ISSUED",
    "GRANT_REFUSED",
    "GRANT_REVOKED",
    "GrantChainResult",
    "GrantError",
    "GrantLedger",
    "GrantReceipt",
    "GrantSigner",
    "default_ledger",
    "digest_secret_name",
    "find_active_grant",
    "install_grant_signer",
    "render_report",
    "verify_grant_chain",
    "verify_grant_signature",
]

#: Genesis linkage value for the first record of a run (matches the audit-chain
#: convention of a fixed 64-hex-zero anchor, identical to :mod:`delegation`).
GENESIS_HMAC: Final[str] = "0" * 64

_SUBDIR: Final[str] = "grants"

#: Default root -- the same tree as the HMAC-chained audit log, so
#: ``.sdd/audit/grants/`` sits beside ``.sdd/audit/delegation/``.
DEFAULT_ROOT: Final[Path] = Path(".sdd/audit")

#: Record kinds. ``grant_issued`` and ``grant_revoked`` are the audit events the
#: issue calls out; ``grant_exchanged`` records the broker token exchange and
#: ``grant_refused`` records a mint/resolve refusal so the refusal itself is a
#: chain-anchored event, not only an in-process callback.
GRANT_ISSUED: Final[str] = "grant_issued"
GRANT_EXCHANGED: Final[str] = "grant_exchanged"
GRANT_REVOKED: Final[str] = "grant_revoked"
GRANT_REFUSED: Final[str] = "grant_refused"

_KINDS: Final[frozenset[str]] = frozenset({GRANT_ISSUED, GRANT_EXCHANGED, GRANT_REVOKED, GRANT_REFUSED})

#: Fields the manager signs (the grant's semantic identity). Excludes chain
#: state (``prev_hmac``, ``hmac``) and the signature itself.
_SIGNED_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "record_index",
    "kind",
    "grant_id",
    "task_id",
    "secret_name",
    "audience",
    "expiry",
    "capability_ceiling",
    "issuer",
    "issuer_pubkey",
    "token_id",
    "reason",
    "created",
)


class GrantError(Exception):
    """Raised when a grant cannot be signed, recorded, or verified."""


# ---------------------------------------------------------------------------
# Canonical encoding + HMAC (identical construction to delegation / audit)
# ---------------------------------------------------------------------------


def _canonical(body: dict[str, Any]) -> str:
    """Return the canonical JSON encoding used for signing (compact, sorted)."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _compute_hmac(key: bytes, prev_hmac: str, body: dict[str, Any]) -> str:
    """HMAC-SHA256 over ``prev_hmac`` concatenated with the canonical record body.

    Identical construction to
    :func:`bernstein.core.identity.delegation._compute_hmac` and
    :func:`bernstein.core.security.audit._compute_hmac`, so the grant chain
    shares tamper-evidence semantics with the delegation and audit chains.
    """
    payload = prev_hmac + json.dumps(body, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def digest_secret_name(secret_name: str) -> str:
    """Return a ``sha256:<hex>`` reference for ``secret_name``, safe to persist.

    The backing-store secret name (e.g. an env var or Vault path) is never
    written to a chain record, receipt, report, or log in clear text -- only
    this deterministic digest is. The digest is stable, so matching a grant
    by ``(task_id, secret_name)`` still works by comparing digests; the raw
    name itself is only ever held in memory for the duration of the call that
    computes this digest. An empty name digests to an empty string so an
    absent/optional ``secret_name`` stays absent rather than becoming the
    digest of the empty string.
    """
    if not secret_name:
        return ""
    return "sha256:" + hashlib.sha256(secret_name.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ed25519 manager signer
# ---------------------------------------------------------------------------


class GrantSigner:
    """Ed25519 manager identity that signs scoped grants.

    Reuses the ``cryptography`` package already pulled in by the agent-card
    signer. The public key is embedded in every record so verification is
    self-describing; the HMAC chain anchored on the install audit key is what
    prevents an attacker from swapping the key and re-chaining.
    """

    def __init__(self, private_key_pem: bytes, public_key_pem: bytes, *, issuer: str) -> None:
        self._private_pem = private_key_pem
        self._public_pem = public_key_pem
        self._issuer = issuer

    @classmethod
    def generate(cls, *, issuer: str) -> GrantSigner:
        """Generate a fresh Ed25519 manager signer (test / ephemeral use)."""
        from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

        private_pem, public_pem = generate_ed25519_keypair()
        return cls(private_pem, public_pem, issuer=issuer)

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def public_key_pem(self) -> str:
        return self._public_pem.decode() if isinstance(self._public_pem, bytes) else str(self._public_pem)

    def with_issuer(self, issuer: str) -> GrantSigner:
        """Return a signer with the same key but a different issuer label.

        Used when a SPIFFE identity mode relabels the issuer to the workload's
        SPIFFE ID while the manager key that proves authorship is unchanged.
        """
        return GrantSigner(self._private_pem, self._public_pem, issuer=issuer)

    def sign(self, body: dict[str, Any]) -> str:
        """Return the hex Ed25519 signature over the canonical ``body``."""
        from cryptography.hazmat.primitives import serialization

        return (
            serialization.load_pem_private_key(self._private_pem, password=None)
            .sign(_canonical(body).encode())  # type: ignore[union-attr]
            .hex()
        )


def verify_grant_signature(public_key_pem: bytes | str, body: dict[str, Any], signature_hex: str) -> bool:
    """Return True when ``signature_hex`` is a valid Ed25519 signature over ``body``.

    ``body`` must be the signed body (the :data:`_SIGNED_FIELDS`), and
    ``public_key_pem`` the SPKI PEM embedded in the record.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    pem = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
    try:
        key = serialization.load_pem_public_key(pem)
        key.verify(bytes.fromhex(signature_hex), _canonical(body).encode())  # type: ignore[union-attr]
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Grant receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantReceipt:
    """A single chain-anchored, Ed25519-signed grant-lifecycle record."""

    run_id: str
    record_index: int
    kind: str
    grant_id: str
    task_id: str
    secret_name: str
    audience: str
    expiry: int
    capability_ceiling: tuple[str, ...]
    issuer: str
    issuer_pubkey: str
    created: int
    token_id: str = ""
    reason: str = ""
    prev_hmac: str = GENESIS_HMAC
    signature: str = ""
    hmac: str = ""

    def signed_body(self) -> dict[str, Any]:
        """Return the canonical body the manager signs (chain-state-free)."""
        return {
            "run_id": self.run_id,
            "record_index": self.record_index,
            "kind": self.kind,
            "grant_id": self.grant_id,
            "task_id": self.task_id,
            "secret_name": self.secret_name,
            "audience": self.audience,
            "expiry": self.expiry,
            "capability_ceiling": list(self.capability_ceiling),
            "issuer": self.issuer,
            "issuer_pubkey": self.issuer_pubkey,
            "token_id": self.token_id,
            "reason": self.reason,
            "created": self.created,
        }

    def chain_body(self) -> dict[str, Any]:
        """Return the full record body the HMAC covers (everything but ``hmac``)."""
        body = self.signed_body()
        body["prev_hmac"] = self.prev_hmac
        body["signature"] = self.signature
        return body

    def to_entry(self) -> dict[str, Any]:
        """Return the on-disk JSONL entry (chain body plus ``hmac``)."""
        entry = self.chain_body()
        entry["hmac"] = self.hmac
        return entry

    @classmethod
    def from_entry(cls, obj: dict[str, Any]) -> GrantReceipt:
        """Rebuild a :class:`GrantReceipt` from its on-disk entry."""
        return cls(
            run_id=str(obj["run_id"]),
            record_index=int(obj["record_index"]),
            kind=str(obj["kind"]),
            grant_id=str(obj["grant_id"]),
            task_id=str(obj["task_id"]),
            secret_name=str(obj["secret_name"]),
            audience=str(obj["audience"]),
            expiry=int(obj["expiry"]),
            capability_ceiling=tuple(str(c) for c in obj.get("capability_ceiling", [])),
            issuer=str(obj["issuer"]),
            issuer_pubkey=str(obj["issuer_pubkey"]),
            created=int(obj["created"]),
            token_id=str(obj.get("token_id", "")),
            reason=str(obj.get("reason", "")),
            prev_hmac=str(obj.get("prev_hmac", GENESIS_HMAC)),
            signature=str(obj.get("signature", "")),
            hmac=str(obj.get("hmac", "")),
        )


# ---------------------------------------------------------------------------
# Grant ledger (append-only, one JSONL file per run)
# ---------------------------------------------------------------------------


class GrantLedger:
    """Append-only, HMAC-chained + Ed25519-signed grant-record store.

    One JSONL file per run under ``<root>/grants/``. The running ``prev_hmac``
    per run is read from the tail of the file, so records appended across
    process restarts still chain continuously.
    """

    def __init__(self, root: Path, key: bytes, signer: GrantSigner) -> None:
        """Bind the ledger to a root directory, an HMAC key, and a manager signer.

        Args:
            root: Base directory; records land under ``root/grants/``.
            key: HMAC key -- use the install audit key so the chain is anchored
                to the install identity.
            signer: The Ed25519 manager identity that authorizes grants.
        """
        self.root = Path(root)
        self._key = key
        self._signer = signer
        self._dir = self.root / _SUBDIR
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def issuer(self) -> str:
        return self._signer.issuer

    @property
    def hmac_key(self) -> bytes:
        """Return the install-scoped HMAC key the chain is anchored on."""
        return self._key

    def receipt_path(self, run_id: str) -> Path:
        """Return the JSONL path backing ``run_id``'s records."""
        safe = run_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.jsonl"

    def _tail(self, run_id: str) -> tuple[str, int]:
        """Return ``(prev_hmac, next_record_index)`` for the run."""
        path = self.receipt_path(run_id)
        if not path.is_file():
            return GENESIS_HMAC, 0
        prev = GENESIS_HMAC
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prev = obj.get("hmac", prev)
            count += 1
        return prev, count

    def _append(
        self,
        *,
        run_id: str,
        kind: str,
        grant_id: str,
        task_id: str,
        secret_name: str,
        audience: str,
        expiry: int,
        capability_ceiling: Sequence[str],
        token_id: str,
        reason: str,
        created: int | None,
    ) -> GrantReceipt:
        if kind not in _KINDS:  # pragma: no cover - defensive
            raise GrantError(f"unknown grant record kind {kind!r}")
        prev_hmac, record_index = self._tail(run_id)
        ts = int(created if created is not None else time.time())
        # The raw secret_name is used only transiently, right here, to derive
        # the digest that actually gets signed and persisted; it is never
        # itself written into `signed` (and therefore never into the chain
        # record, the receipt, or a rendered report).
        signed = {
            "run_id": run_id,
            "record_index": record_index,
            "kind": kind,
            "grant_id": grant_id,
            "task_id": task_id,
            "secret_name": digest_secret_name(secret_name),
            "audience": audience,
            "expiry": expiry,
            "capability_ceiling": sorted(capability_ceiling),
            "issuer": self._signer.issuer,
            "issuer_pubkey": self._signer.public_key_pem,
            "token_id": token_id,
            "reason": reason,
            "created": ts,
        }
        signature = self._signer.sign(signed)
        chain_body = signed.copy()
        chain_body["prev_hmac"] = prev_hmac
        chain_body["signature"] = signature
        computed = _compute_hmac(self._key, prev_hmac, chain_body)
        entry = chain_body.copy()
        entry["hmac"] = computed
        path = self.receipt_path(run_id)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return GrantReceipt.from_entry(entry)

    def issue_grant(
        self,
        *,
        run_id: str,
        task_id: str,
        secret_name: str,
        audience: str,
        expiry: int = 0,
        capability_ceiling: Sequence[str] = (),
        grant_id: str | None = None,
        created: int | None = None,
    ) -> GrantReceipt:
        """Issue a scoped grant and append it as a ``grant_issued`` record.

        Args:
            run_id: Run the grant belongs to.
            task_id: Task the grant scopes the credential to.
            secret_name: Backing secret name in the broker backend.
            audience: The downstream target the minted token may be presented to.
            expiry: Epoch-second expiry; ``0`` means no explicit expiry.
            capability_ceiling: Symbolic capability names the grant caps.
            grant_id: Optional explicit id (defaults to a fresh uuid4 hex).
            created: Optional unix timestamp (exposed for deterministic tests).

        Returns:
            The freshly-appended :class:`GrantReceipt`.
        """
        return self._append(
            run_id=run_id,
            kind=GRANT_ISSUED,
            grant_id=grant_id or uuid.uuid4().hex,
            task_id=task_id,
            secret_name=secret_name,
            audience=audience,
            expiry=expiry,
            capability_ceiling=capability_ceiling,
            token_id="",
            reason="",
            created=created,
        )

    def record_exchange(
        self,
        *,
        run_id: str,
        grant_id: str,
        token_id: str,
        task_id: str = "",
        secret_name: str = "",
        audience: str = "",
        created: int | None = None,
    ) -> GrantReceipt:
        """Append a ``grant_exchanged`` record binding a minted ``token_id`` to a grant."""
        return self._append(
            run_id=run_id,
            kind=GRANT_EXCHANGED,
            grant_id=grant_id,
            task_id=task_id,
            secret_name=secret_name,
            audience=audience,
            expiry=0,
            capability_ceiling=(),
            token_id=token_id,
            reason="",
            created=created,
        )

    def revoke_grant(
        self,
        *,
        run_id: str,
        grant_id: str,
        reason: str = "revoked",
        task_id: str = "",
        secret_name: str = "",
        created: int | None = None,
    ) -> GrantReceipt:
        """Append a signed ``grant_revoked`` record referencing ``grant_id``."""
        return self._append(
            run_id=run_id,
            kind=GRANT_REVOKED,
            grant_id=grant_id,
            task_id=task_id,
            secret_name=secret_name,
            audience="",
            expiry=0,
            capability_ceiling=(),
            token_id="",
            reason=reason,
            created=created,
        )

    def record_refusal(
        self,
        *,
        run_id: str,
        task_id: str,
        secret_name: str,
        reason: str,
        grant_id: str = "",
        audience: str = "",
        token_id: str = "",
        created: int | None = None,
    ) -> GrantReceipt:
        """Append a ``grant_refused`` record so a refusal is itself chain-anchored."""
        return self._append(
            run_id=run_id,
            kind=GRANT_REFUSED,
            grant_id=grant_id,
            task_id=task_id,
            secret_name=secret_name,
            audience=audience,
            expiry=0,
            capability_ceiling=(),
            token_id=token_id,
            reason=reason,
            created=created,
        )


# ---------------------------------------------------------------------------
# Offline verification
# ---------------------------------------------------------------------------


@dataclass
class GrantChainResult:
    """Outcome of reconstructing a run's grant chain offline."""

    valid: bool
    records: list[GrantReceipt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def lifecycles(self) -> dict[str, dict[str, Any]]:
        """Reconstruct each grant's issue/exchange/revoke state from the records."""
        life: dict[str, dict[str, Any]] = {}
        for r in self.records:
            if not r.grant_id:
                continue
            state = life.setdefault(
                r.grant_id,
                {
                    "task_id": "",
                    "secret_name": "",
                    "audience": "",
                    "expiry": 0,
                    "issued": False,
                    "revoked": False,
                    "token_ids": [],
                    "refusals": [],
                },
            )
            if r.kind == GRANT_ISSUED:
                state["issued"] = True
                state["task_id"] = r.task_id
                state["secret_name"] = r.secret_name
                state["audience"] = r.audience
                state["expiry"] = r.expiry
            elif r.kind == GRANT_EXCHANGED and r.token_id:
                state["token_ids"].append(r.token_id)
            elif r.kind == GRANT_REVOKED:
                state["revoked"] = True
            elif r.kind == GRANT_REFUSED:
                state["refusals"].append(r.reason)
        return life


def verify_grant_chain(*, root: Path, run_id: str, key: bytes) -> GrantChainResult:
    """Reconstruct and verify a run's grant chain offline.

    Walks ``<root>/grants/<run_id>.jsonl`` from genesis, recomputing each
    record's HMAC, checking linkage, and verifying the manager Ed25519
    signature against the embedded issuer public key. A mutated field, a
    deleted record, or reordered records surface as an error naming the
    offending record; verification stops at the first break.

    Args:
        root: Base directory the ledger was rooted at.
        run_id: Run to verify.
        key: The HMAC key (install audit key) the records were written with.

    Returns:
        A :class:`GrantChainResult`. ``valid`` is True only when at least one
        record exists and the whole chain verifies from genesis to tail.
    """
    ledger_dir = Path(root) / _SUBDIR
    safe = run_id.replace("/", "_").replace("\\", "_")
    path = ledger_dir / f"{safe}.jsonl"
    if not path.is_file():
        return GrantChainResult(valid=False, records=[], errors=["no grant records for run"])

    records: list[GrantReceipt] = []
    errors: list[str] = []
    prev_hmac = GENESIS_HMAC
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"record {lineno}: malformed JSON: {exc}")
            break
        stored_hmac = obj.get("hmac", "")
        idx = obj.get("record_index", lineno)
        chain_body = {k: v for k, v in obj.items() if k != "hmac"}
        if chain_body.get("prev_hmac") != prev_hmac:
            errors.append(f"record {idx}: broken linkage (prev_hmac does not match preceding record)")
            break
        expected = _compute_hmac(key, prev_hmac, chain_body)
        if not _hmac.compare_digest(expected, stored_hmac):
            errors.append(f"record {idx}: HMAC mismatch (record tampered or wrong key)")
            break
        signed = {k: v for k, v in chain_body.items() if k not in ("prev_hmac", "signature")}
        if not verify_grant_signature(str(obj.get("issuer_pubkey", "")), signed, str(obj.get("signature", ""))):
            errors.append(f"record {idx}: Ed25519 signature invalid (grant not authorized by the issuer key)")
            break
        records.append(GrantReceipt.from_entry(obj))
        prev_hmac = stored_hmac

    valid = not errors and len(records) > 0
    return GrantChainResult(valid=valid, records=records, errors=errors)


def render_report(result: GrantChainResult, *, run_id: str) -> str:
    """Render a deterministic, byte-identical verification report as canonical JSON.

    Two independent verifiers given the same chain slice produce byte-identical
    output, so the report can be diffed across environments or handed to a
    third party.
    """
    payload = {
        "run": run_id,
        "valid": result.valid,
        "record_count": len(result.records),
        "errors": result.errors.copy(),
        "records": [
            {
                "record_index": r.record_index,
                "kind": r.kind,
                "grant_id": r.grant_id,
                "task_id": r.task_id,
                "secret_name": r.secret_name,
                "audience": r.audience,
                "expiry": r.expiry,
                "capability_ceiling": list(r.capability_ceiling),
                "issuer": r.issuer,
                "token_id": r.token_id,
                "reason": r.reason,
            }
            for r in result.records
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def find_active_grant(
    result: GrantChainResult,
    *,
    task_id: str,
    secret_name: str,
    now: float | None = None,
) -> GrantReceipt | None:
    """Return the newest verified grant for ``(task_id, secret_name)`` still active.

    A grant is active when it was issued, is not revoked in the chain, and has
    either no expiry or an expiry in the future. Returns ``None`` if the chain
    did not verify or no matching active grant exists.
    """
    if not result.valid:
        return None
    current = now if now is not None else time.time()
    life = result.lifecycles()
    # Index the issued records so we can return the receipt itself.
    issued: dict[str, GrantReceipt] = {r.grant_id: r for r in result.records if r.kind == GRANT_ISSUED}
    best: GrantReceipt | None = None
    # Records only ever carry the digest (see digest_secret_name), so the
    # lookup key is digested here too rather than the raw secret name.
    wanted_secret = digest_secret_name(secret_name)
    for grant_id, state in life.items():
        if not state["issued"] or state["revoked"]:
            continue
        if state["task_id"] != task_id or state["secret_name"] != wanted_secret:
            continue
        expiry = int(state["expiry"])
        if expiry and current >= expiry:
            continue
        candidate = issued.get(grant_id)
        if candidate is None:
            continue
        if best is None or candidate.record_index > best.record_index:
            best = candidate
    return best


# ---------------------------------------------------------------------------
# Install-anchored convenience helpers
# ---------------------------------------------------------------------------


def _audit_key() -> bytes:
    """Return the install-scoped audit HMAC key (the grant chain anchor)."""
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def install_grant_signer(*, runtime_dir: Path | None = None, issuer: str = "manager") -> GrantSigner:
    """Return a manager signer backed by the install agent-card keystore.

    The keystore persists one Ed25519 keypair per install, so grant records
    across runs share a stable manager identity that a verifier can pin.
    """
    import os

    from bernstein.core.identity.http_signing import ENV_KEY_DIR
    from bernstein.core.security.agent_card_keystore import (
        DEFAULT_KEY_DIR,
        AgentCardKeystore,
    )

    if runtime_dir is not None:
        key_dir: Path = runtime_dir
    else:
        override = os.environ.get(ENV_KEY_DIR, "").strip()
        key_dir = Path(override) if override else DEFAULT_KEY_DIR
    private_pem, public_pem = AgentCardKeystore(key_dir).load_or_generate()
    return GrantSigner(private_pem, public_pem, issuer=issuer)


def default_ledger(
    root: Path | None = None,
    *,
    signer: GrantSigner | None = None,
    issuer: str = "manager",
) -> GrantLedger:
    """Return a ledger rooted at the audit tree, keyed by the install audit key.

    Args:
        root: Optional root override; defaults to :data:`DEFAULT_ROOT`.
        signer: Optional signer override; defaults to the install manager signer.
        issuer: Issuer label when the default signer is built.

    Returns:
        A :class:`GrantLedger` chained to the install identity's audit key.
    """
    active_signer = signer if signer is not None else install_grant_signer(issuer=issuer)
    return GrantLedger(root=root or DEFAULT_ROOT, key=_audit_key(), signer=active_signer)
