"""Chain-anchored agent-principal provisioning and deprovisioning (issue #4972).

Scoped per-task grants (:mod:`bernstein.core.identity.grants`) bound what a
single task can do, but they never removed the *principal* the grants were
issued to. An install that has run for months therefore accumulates agent
principals nobody can account for, each still able to back a fresh grant.

This module gives a principal the same two-anchor lifecycle a grant already
has. Provisioning and deprovisioning are records in an append-only,
HMAC-chained, Ed25519-signed journal:

* the **Ed25519 signature** by the manager identity proves who drew the line;
* the **HMAC** over ``prev_hmac`` plus the canonical record body, keyed by the
  install audit key, makes deletion, mutation, and reordering visible.

The journal *is* the registry: :meth:`PrincipalChainResult.registry` replays it
into the current set of principals and the capability ceiling each one carries.
There is no separate mutable table that could disagree with the chain.

Finality
--------
Deprovisioning is consulted by the grant-validity path itself
(:func:`bernstein.core.identity.grants.find_active_grant`), not by an
out-of-band flag: after a ``principal_deprovisioned`` record at time ``T``, no
check performed at or after ``T`` returns a grant bound to that principal, and
a grant whose principal has no ``principal_provisioned`` record at all is
refused rather than waved through. Deprovisioning is *not* retroactive: a check
against a moment inside the window the grant covered still returns it, and the
grant's own signature and linkage stay valid forever.

Record shape
------------
One JSONL line per record under ``<root>/principals/<scope>.jsonl``::

    {
      "scope": "install",
      "record_index": 0,
      "kind": "principal_provisioned",   # provisioned | deprovisioned
      "principal_id": "agent:nightly-refactor",
      "external_id": "dir-9911",         # the directory's own id, if any
      "display_name": "Nightly refactor agent",
      "capability_ceiling": ["list", "read"],
      "issuer": "manager:...",
      "issuer_pubkey": "-----BEGIN PUBLIC KEY----- ...",
      "reason": "",                      # set on deprovision
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from bernstein.core.identity.grants import (
    GENESIS_HMAC,
    GrantSigner,
    verify_grant_signature,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_SCOPE",
    "GENESIS_HMAC",
    "PRINCIPAL_DEPROVISIONED",
    "PRINCIPAL_PROVISIONED",
    "PrincipalChainResult",
    "PrincipalError",
    "PrincipalLedger",
    "PrincipalReceipt",
    "PrincipalState",
    "default_principal_ledger",
    "verify_principal_chain",
]

#: Record kinds. Both are chain events; there is no in-place mutation.
PRINCIPAL_PROVISIONED: Final[str] = "principal_provisioned"
PRINCIPAL_DEPROVISIONED: Final[str] = "principal_deprovisioned"

_KINDS: Final[frozenset[str]] = frozenset({PRINCIPAL_PROVISIONED, PRINCIPAL_DEPROVISIONED})

_SUBDIR: Final[str] = "principals"

#: Principals outlive runs, so the journal is scoped to the install rather than
#: to a run id the way the grant journal is.
DEFAULT_SCOPE: Final[str] = "install"


class PrincipalError(Exception):
    """Raised when a principal record cannot be signed or recorded."""


def _compute_hmac(key: bytes, prev_hmac: str, body: dict[str, Any]) -> str:
    """HMAC-SHA256 over ``prev_hmac`` concatenated with the canonical record body.

    Identical construction to
    :func:`bernstein.core.identity.grants._compute_hmac`, so a verifier holding
    the install audit key walks the principal chain exactly as it walks the
    grant chain.
    """
    payload = prev_hmac + json.dumps(body, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _safe_scope(scope: str) -> str:
    return scope.replace("/", "_").replace("\\", "_")


# ---------------------------------------------------------------------------
# Principal receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrincipalReceipt:
    """A single chain-anchored, Ed25519-signed principal-lifecycle record."""

    scope: str
    record_index: int
    kind: str
    principal_id: str
    external_id: str
    display_name: str
    capability_ceiling: tuple[str, ...]
    issuer: str
    issuer_pubkey: str
    created: int
    reason: str = ""
    prev_hmac: str = GENESIS_HMAC
    signature: str = ""
    hmac: str = ""

    def signed_body(self) -> dict[str, Any]:
        """Return the canonical body the manager signs (chain-state-free)."""
        return {
            "scope": self.scope,
            "record_index": self.record_index,
            "kind": self.kind,
            "principal_id": self.principal_id,
            "external_id": self.external_id,
            "display_name": self.display_name,
            "capability_ceiling": list(self.capability_ceiling),
            "issuer": self.issuer,
            "issuer_pubkey": self.issuer_pubkey,
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
    def from_entry(cls, obj: dict[str, Any]) -> PrincipalReceipt:
        """Rebuild a :class:`PrincipalReceipt` from its on-disk entry."""
        return cls(
            scope=str(obj["scope"]),
            record_index=int(obj["record_index"]),
            kind=str(obj["kind"]),
            principal_id=str(obj["principal_id"]),
            external_id=str(obj.get("external_id", "")),
            display_name=str(obj.get("display_name", "")),
            capability_ceiling=tuple(str(c) for c in obj.get("capability_ceiling", [])),
            issuer=str(obj["issuer"]),
            issuer_pubkey=str(obj["issuer_pubkey"]),
            created=int(obj["created"]),
            reason=str(obj.get("reason", "")),
            prev_hmac=str(obj.get("prev_hmac", GENESIS_HMAC)),
            signature=str(obj.get("signature", "")),
            hmac=str(obj.get("hmac", "")),
        )


# ---------------------------------------------------------------------------
# Projected state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrincipalState:
    """The current lifecycle state of one principal, replayed from the chain."""

    principal_id: str
    display_name: str
    external_id: str
    capability_ceiling: tuple[str, ...]
    provisioned_at: int
    deprovisioned_at: int = 0
    reason: str = ""

    def active_at(self, now: float) -> bool:
        """Return whether the principal existed and was not deprovisioned at ``now``.

        A deprovision recorded at ``T`` takes effect at ``T``: checks at or
        after ``T`` refuse the principal, checks before ``T`` still see it.
        """
        if now < self.provisioned_at:
            return False
        return not (self.deprovisioned_at and now >= self.deprovisioned_at)


# ---------------------------------------------------------------------------
# Principal ledger (append-only, one JSONL file per scope)
# ---------------------------------------------------------------------------


class PrincipalLedger:
    """Append-only, HMAC-chained + Ed25519-signed principal-lifecycle store."""

    def __init__(self, root: Path, key: bytes, signer: GrantSigner, *, scope: str = DEFAULT_SCOPE) -> None:
        """Bind the ledger to a root directory, an HMAC key, and a manager signer.

        Args:
            root: Base directory; records land under ``root/principals/``.
            key: HMAC key -- use the install audit key so the chain is anchored
                to the install identity, exactly as the grant chain is.
            signer: The Ed25519 manager identity that authorizes the lifecycle.
            scope: Journal scope; principals outlive runs, so this defaults to
                the install rather than to a run id.
        """
        self.root = Path(root)
        self._key = key
        self._signer = signer
        self._scope = scope
        self._dir = self.root / _SUBDIR
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def issuer(self) -> str:
        return self._signer.issuer

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def hmac_key(self) -> bytes:
        """Return the install-scoped HMAC key the chain is anchored on."""
        return self._key

    def receipt_path(self) -> Path:
        """Return the JSONL path backing this ledger's scope."""
        return self._dir / f"{_safe_scope(self._scope)}.jsonl"

    def _tail(self) -> tuple[str, int]:
        """Return ``(prev_hmac, next_record_index)`` for the scope."""
        path = self.receipt_path()
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
        kind: str,
        principal_id: str,
        external_id: str,
        display_name: str,
        capability_ceiling: Sequence[str],
        reason: str,
        created: int | None,
    ) -> PrincipalReceipt:
        if kind not in _KINDS:  # pragma: no cover - defensive
            raise PrincipalError(f"unknown principal record kind {kind!r}")
        if not principal_id:
            raise PrincipalError("principal_id must not be empty")
        prev_hmac, record_index = self._tail()
        ts = int(created if created is not None else time.time())
        signed = {
            "scope": self._scope,
            "record_index": record_index,
            "kind": kind,
            "principal_id": principal_id,
            "external_id": external_id,
            "display_name": display_name,
            "capability_ceiling": sorted(set(capability_ceiling)),
            "issuer": self._signer.issuer,
            "issuer_pubkey": self._signer.public_key_pem,
            "reason": reason,
            "created": ts,
        }
        signature = self._signer.sign(signed)
        chain_body = signed.copy()
        chain_body["prev_hmac"] = prev_hmac
        chain_body["signature"] = signature
        entry = chain_body.copy()
        entry["hmac"] = _compute_hmac(self._key, prev_hmac, chain_body)
        with self.receipt_path().open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return PrincipalReceipt.from_entry(entry)

    def provision(
        self,
        *,
        principal_id: str,
        display_name: str = "",
        capability_ceiling: Sequence[str] = (),
        external_id: str = "",
        created: int | None = None,
    ) -> PrincipalReceipt:
        """Append a signed ``principal_provisioned`` record.

        Args:
            principal_id: Stable id grants bind to (``principal`` on a grant).
            display_name: Human-readable label carried for the registry.
            capability_ceiling: Symbolic capability names the principal caps.
            external_id: The upstream directory's own id for the resource.
            created: Optional unix timestamp (exposed for deterministic tests).

        Returns:
            The freshly-appended :class:`PrincipalReceipt`.
        """
        return self._append(
            kind=PRINCIPAL_PROVISIONED,
            principal_id=principal_id,
            external_id=external_id,
            display_name=display_name,
            capability_ceiling=capability_ceiling,
            reason="",
            created=created,
        )

    def deprovision(
        self,
        *,
        principal_id: str,
        reason: str = "deprovisioned",
        created: int | None = None,
    ) -> PrincipalReceipt:
        """Append a signed ``principal_deprovisioned`` record drawing the line."""
        return self._append(
            kind=PRINCIPAL_DEPROVISIONED,
            principal_id=principal_id,
            external_id="",
            display_name="",
            capability_ceiling=(),
            reason=reason,
            created=created,
        )


# ---------------------------------------------------------------------------
# Offline verification + registry projection
# ---------------------------------------------------------------------------


@dataclass
class PrincipalChainResult:
    """Outcome of reconstructing a scope's principal chain offline."""

    valid: bool
    records: list[PrincipalReceipt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def registry(self) -> dict[str, PrincipalState]:
        """Replay the records into the current principal registry.

        Later records win, so a re-provision after a deprovision restores the
        principal and a deprovision closes it. The projection is deterministic:
        two verifiers replaying the same chain slice produce the same registry.
        """
        states: dict[str, PrincipalState] = {}
        for r in self.records:
            if not r.principal_id:
                continue
            current = states.get(r.principal_id)
            if r.kind == PRINCIPAL_PROVISIONED:
                states[r.principal_id] = PrincipalState(
                    principal_id=r.principal_id,
                    display_name=r.display_name,
                    external_id=r.external_id,
                    capability_ceiling=r.capability_ceiling,
                    provisioned_at=r.created,
                    deprovisioned_at=0,
                    reason="",
                )
            elif r.kind == PRINCIPAL_DEPROVISIONED and current is not None:
                states[r.principal_id] = PrincipalState(
                    principal_id=current.principal_id,
                    display_name=current.display_name,
                    external_id=current.external_id,
                    capability_ceiling=current.capability_ceiling,
                    provisioned_at=current.provisioned_at,
                    deprovisioned_at=r.created,
                    reason=r.reason,
                )
        return states


def verify_principal_chain(*, root: Path, key: bytes, scope: str = DEFAULT_SCOPE) -> PrincipalChainResult:
    """Reconstruct and verify a scope's principal chain offline.

    Walks ``<root>/principals/<scope>.jsonl`` from genesis, recomputing each
    record's HMAC, checking linkage, and verifying the manager Ed25519
    signature against the embedded issuer public key. A mutated field, a
    deleted record, or reordered records surface as an error naming the
    offending record; verification stops at the first break.

    Args:
        root: Base directory the ledger was rooted at.
        key: The HMAC key (install audit key) the records were written with.
        scope: Journal scope to verify.

    Returns:
        A :class:`PrincipalChainResult`. ``valid`` is True only when at least
        one record exists and the whole chain verifies from genesis to tail.
    """
    path = Path(root) / _SUBDIR / f"{_safe_scope(scope)}.jsonl"
    if not path.is_file():
        return PrincipalChainResult(valid=False, records=[], errors=["no principal records for scope"])

    records: list[PrincipalReceipt] = []
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
            errors.append(f"record {idx}: Ed25519 signature invalid (record not authorized by the issuer key)")
            break
        records.append(PrincipalReceipt.from_entry(obj))
        prev_hmac = stored_hmac

    return PrincipalChainResult(valid=not errors and len(records) > 0, records=records, errors=errors)


def default_principal_ledger(
    root: Path | None = None,
    *,
    signer: GrantSigner | None = None,
    issuer: str = "manager",
    scope: str = DEFAULT_SCOPE,
) -> PrincipalLedger:
    """Return a ledger rooted at the audit tree, keyed by the install audit key.

    The principal chain shares the grant chain's root and key so a verifier
    holding the install audit key can reconstruct both from one slice.
    """
    from bernstein.core.identity.grants import DEFAULT_ROOT, install_grant_signer
    from bernstein.core.security.audit import load_or_create_audit_key

    active_signer = signer if signer is not None else install_grant_signer(issuer=issuer)
    return PrincipalLedger(
        root=root or DEFAULT_ROOT,
        key=load_or_create_audit_key(),
        signer=active_signer,
        scope=scope,
    )
