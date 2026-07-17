"""Runtime context capsule: a chain-anchored projection of what a worker got.

Issue #2545. A spawned worker has no unified, verifiable answer to "what was I
given". Identity and context arrive as scattered env vars (``BERNSTEIN_RUN_ID``,
``BERNSTEIN_TASK_ID``) plus whatever the prompt happens to say. Budget envelope
remaining, dependency state, and the audit chain head at spawn are not exposed
to the worker at all, and worker-side code that depends on this context cannot
be tested without a live server.

This module builds one canonical, content-addressed :class:`ContextCapsule` of
what the worker was given: task id, run id, params hash, worktree path, role,
budget envelope remaining, dependency state, and the audit chain head at spawn,
plus the intent capsule hash when one exists (#2514). The capsule is Ed25519
signed and its hash is recorded in the spawn record and the run journal, so it
is not a convenience getter: it is the attested record of the inputs and
context the worker acted on, recomputable offline from the journal at that chain
position.

Killer shape: the capsule IS a chain-position projection. ``bernstein context
verify`` recomputes the capsule from the on-disk bytes and checks its hash
against the ``context.capsule`` audit-chain entry and the ``context.capsule_recorded``
journal event; a context divergence (different params, budget, or chain head
than asserted) is detected as a hash mismatch, not by reading prose. Strip the
audit chain and the run journal and the feature collapses to a getter with a
log.

Sanctioned mock layer: :func:`seal_mock_capsule` signs over a mock domain that
is byte-distinct from the real domain, so a fixture capsule injected for an
offline test can never pass :func:`verify_context_capsule` -- it fails with an
explicit mock-domain diagnostic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.identity import AgentCard, sign_detached, verify_detached

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "CAPSULE_RECORDED_EVENT",
    "CONTEXT_CAPSULE_DOMAIN_MOCK",
    "CONTEXT_CAPSULE_DOMAIN_REAL",
    "CONTEXT_CAPSULE_KID",
    "CONTEXT_CAPSULE_VERSION",
    "ContextCapsule",
    "ContextVerifyResult",
    "SignedContextCapsule",
    "anchor_capsule",
    "build_context_capsule",
    "capsule_spawn_binding",
    "project_capsule",
    "read_capsule_record",
    "record_capsule_in_journal",
    "seal_and_bind",
    "seal_context_capsule",
    "seal_mock_capsule",
    "verify_context_capsule",
    "verify_signature",
    "write_capsule_record",
]

#: Wire-format version stamped into every capsule preimage.
CONTEXT_CAPSULE_VERSION = 1

#: Journal event recorded when a capsule is sealed for a run. The event carries
#: the capsule hash, so replay at that chain position re-derives the capsule.
CAPSULE_RECORDED_EVENT = "context.capsule_recorded"

#: Key id carried in the detached JWS protected header.
CONTEXT_CAPSULE_KID = "context-capsule"

#: Domain-separation tags. A real capsule is signed over ``DOMAIN_REAL ||
#: canonical``; a mock capsule over ``DOMAIN_MOCK || canonical``. The two
#: preimages never collide, so a mock signature can never verify as real even if
#: an attacker flips the on-disk ``is_mock`` flag.
CONTEXT_CAPSULE_DOMAIN_REAL = b"bernstein.context-capsule.v1\x00"
CONTEXT_CAPSULE_DOMAIN_MOCK = b"bernstein.context-capsule.MOCK.v1\x00"


@dataclass(frozen=True, slots=True)
class ContextCapsule:
    """Canonical, content-addressed record of what a worker was given.

    Frozen + slots so the byte form is canonical. All numeric budget fields are
    integers (USD is carried in micro-dollars) so the canonical bytes never
    depend on a host-dependent float repr.

    Attributes:
        v: Wire-format version.
        task_id: The task the worker was spawned for.
        run_id: The run whose journal the capsule hash is bound into.
        params_hash: ``sha256:`` hash of the validated parameter map -- the same
            value the spawn record and the fire projection carry.
        worktree_path: The isolated worktree path handed to the worker.
        role: The worker role (e.g. ``backend``, ``manager``).
        budget_remaining_tokens: Token envelope remaining at spawn.
        budget_remaining_usd_micros: USD envelope remaining, in micro-dollars.
        dependency_state: Sorted ``(task_id, state)`` pairs of the worker's
            declared dependencies at spawn.
        audit_chain_head: The HMAC audit-chain head pinned at spawn.
        intent_capsule_hash: The #2514 intent capsule hash when one exists.
        spawned_at: Integer Unix timestamp the capsule was built at.
    """

    v: int
    task_id: str
    run_id: str
    params_hash: str
    worktree_path: str
    role: str
    budget_remaining_tokens: int
    budget_remaining_usd_micros: int
    dependency_state: tuple[tuple[str, str], ...]
    audit_chain_head: str
    intent_capsule_hash: str
    spawned_at: int

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the JCS-canonical mapping (sorted, lists not tuples)."""
        return {
            "v": self.v,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "params_hash": self.params_hash,
            "worktree_path": self.worktree_path,
            "role": self.role,
            "budget_remaining_tokens": self.budget_remaining_tokens,
            "budget_remaining_usd_micros": self.budget_remaining_usd_micros,
            "dependency_state": [list(pair) for pair in self.dependency_state],
            "audit_chain_head": self.audit_chain_head,
            "intent_capsule_hash": self.intent_capsule_hash,
            "spawned_at": self.spawned_at,
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for on-disk storage."""
        return self.to_canonical_dict()

    def canonical_bytes(self) -> bytes:
        """RFC 8785-style canonical bytes of the capsule."""
        return json.dumps(
            self.to_canonical_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    def capsule_hash(self) -> str:
        """``sha256:`` content hash of the canonical capsule bytes."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ContextCapsule:
        deps = row.get("dependency_state", [])
        dependency_state = tuple((str(p[0]), str(p[1])) for p in deps if isinstance(p, (list, tuple)) and len(p) == 2)
        return cls(
            v=int(row.get("v", CONTEXT_CAPSULE_VERSION)),
            task_id=str(row["task_id"]),
            run_id=str(row.get("run_id", "")),
            params_hash=str(row.get("params_hash", "")),
            worktree_path=str(row.get("worktree_path", "")),
            role=str(row.get("role", "")),
            budget_remaining_tokens=int(row.get("budget_remaining_tokens", 0)),
            budget_remaining_usd_micros=int(row.get("budget_remaining_usd_micros", 0)),
            dependency_state=dependency_state,
            audit_chain_head=str(row.get("audit_chain_head", "")),
            intent_capsule_hash=str(row.get("intent_capsule_hash", "")),
            spawned_at=int(row.get("spawned_at", 0)),
        )


def build_context_capsule(
    *,
    task_id: str,
    run_id: str,
    params_hash: str = "",
    worktree_path: str = "",
    role: str = "",
    budget_remaining_tokens: int = 0,
    budget_remaining_usd_micros: int = 0,
    dependency_state: dict[str, str] | None = None,
    audit_chain_head: str = "",
    intent_capsule_hash: str = "",
    spawned_at: int = 0,
) -> ContextCapsule:
    """Build a canonical capsule from spawn inputs. Dependency state is sorted."""
    deps = tuple(sorted((str(k), str(val)) for k, val in (dependency_state or {}).items()))
    return ContextCapsule(
        v=CONTEXT_CAPSULE_VERSION,
        task_id=task_id,
        run_id=run_id,
        params_hash=params_hash,
        worktree_path=worktree_path,
        role=role,
        budget_remaining_tokens=int(budget_remaining_tokens),
        budget_remaining_usd_micros=int(budget_remaining_usd_micros),
        dependency_state=deps,
        audit_chain_head=audit_chain_head,
        intent_capsule_hash=intent_capsule_hash,
        spawned_at=int(spawned_at),
    )


@dataclass(frozen=True, slots=True)
class SignedContextCapsule:
    """A capsule plus its detached Ed25519 signature and provenance.

    ``is_mock`` is the on-disk marker used only for a friendly diagnostic; the
    cryptographic guarantee that a mock never verifies as real comes from the
    domain-separated signing preimage, not this flag.
    """

    capsule: ContextCapsule
    signature: str
    signer_public_key_pem: str
    is_mock: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "capsule": self.capsule.to_dict(),
            "capsule_hash": self.capsule.capsule_hash(),
            "signature": self.signature,
            "signer_public_key_pem": self.signer_public_key_pem,
            "is_mock": self.is_mock,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> SignedContextCapsule:
        return cls(
            capsule=ContextCapsule.from_dict(row["capsule"]),
            signature=str(row.get("signature", "")),
            signer_public_key_pem=str(row.get("signer_public_key_pem", "")),
            is_mock=bool(row.get("is_mock", False)),
        )


def _sign(capsule: ContextCapsule, private_key_pem: str, *, domain: bytes) -> str:
    return sign_detached(domain + capsule.canonical_bytes(), private_key_pem, kid=CONTEXT_CAPSULE_KID)


def seal_context_capsule(capsule: ContextCapsule, private_key_pem: str, public_key_pem: str) -> SignedContextCapsule:
    """Sign a real capsule over the real domain."""
    signature = _sign(capsule, private_key_pem, domain=CONTEXT_CAPSULE_DOMAIN_REAL)
    return SignedContextCapsule(capsule=capsule, signature=signature, signer_public_key_pem=public_key_pem)


def seal_mock_capsule(capsule: ContextCapsule, private_key_pem: str, public_key_pem: str) -> SignedContextCapsule:
    """Sign a fixture capsule over the MOCK domain (never verifies as real).

    The sanctioned mock layer for offline tests: worker-side code that consumes
    a capsule can be exercised against a fixture without a live server, and the
    fixture provably cannot masquerade as a real, chain-anchored capsule.
    """
    signature = _sign(capsule, private_key_pem, domain=CONTEXT_CAPSULE_DOMAIN_MOCK)
    return SignedContextCapsule(
        capsule=capsule,
        signature=signature,
        signer_public_key_pem=public_key_pem,
        is_mock=True,
    )


def verify_signature(signed: SignedContextCapsule) -> tuple[bool, bool]:
    """Return ``(real_ok, mock_ok)`` for a signed capsule's detached signature.

    A genuine capsule verifies under the real domain only; a mock capsule under
    the mock domain only. A tampered capsule verifies under neither.
    """
    if not signed.signature or not signed.signer_public_key_pem:
        return False, False
    card = AgentCard(agent_id="install", kid=CONTEXT_CAPSULE_KID, public_key_pem=signed.signer_public_key_pem)
    real_ok = verify_detached(CONTEXT_CAPSULE_DOMAIN_REAL + signed.capsule.canonical_bytes(), signed.signature, card)
    mock_ok = verify_detached(CONTEXT_CAPSULE_DOMAIN_MOCK + signed.capsule.canonical_bytes(), signed.signature, card)
    return real_ok, mock_ok


# ---------------------------------------------------------------------------
# On-disk record + journal binding + chain anchor
# ---------------------------------------------------------------------------


def _safe_task_id(task_id: str) -> str:
    if not task_id or "/" in task_id or "\\" in task_id or "\x00" in task_id or task_id in {".", ".."}:
        raise ValueError(f"unsafe task_id for capsule path: {task_id!r}")
    return task_id


def capsule_path(sdd_dir: Path, task_id: str) -> Path:
    """Return the on-disk capsule record path for ``task_id``."""
    return sdd_dir / "context" / "capsules" / f"{_safe_task_id(task_id)}.json"


def write_capsule_record(sdd_dir: Path, signed: SignedContextCapsule) -> Path:
    """Persist a signed capsule record to disk."""
    path = capsule_path(sdd_dir, signed.capsule.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(signed.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def read_capsule_record(sdd_dir: Path, task_id: str) -> SignedContextCapsule | None:
    """Load a signed capsule record; ``None`` on missing / malformed file."""
    path = capsule_path(sdd_dir, task_id)
    if not path.is_file():
        return None
    try:
        return SignedContextCapsule.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def record_capsule_in_journal(journal: EventJournal, *, task_id: str, capsule_hash: str) -> None:
    """Record the capsule hash as a Merkle-chained journal event.

    Replay at this chain position re-derives the capsule: the recorded hash must
    match the recomputed hash of the on-disk capsule bytes (a reordered journal
    breaks the chain; a divergent capsule diverges on the hash).
    """
    journal.record(CAPSULE_RECORDED_EVENT, task_id=task_id, capsule_hash=capsule_hash)


def capsule_spawn_binding(*, task_id: str, params_hash: str, capsule_hash: str) -> dict[str, str]:
    """Return the spawn-record fragment binding a worker to its capsule.

    Carries ``params_hash`` alongside ``capsule_hash`` so the spawn record, the
    journal, and the worker-visible capsule all carry the same ``params_hash``
    (AC3). Merged into the worker's spawn record.
    """
    return {
        "context_task_id": task_id,
        "context_params_hash": params_hash,
        "context_capsule_hash": capsule_hash,
    }


def anchor_capsule(chain: AuditChainStore, signed: SignedContextCapsule) -> AuditEvent:
    """Mirror a sealed capsule's identity into the HMAC audit chain."""
    from bernstein.core.security.audit_chain import record_context_capsule

    capsule = signed.capsule
    return record_context_capsule(
        chain=chain,
        task_id=capsule.task_id,
        run_id=capsule.run_id,
        params_hash=capsule.params_hash,
        capsule_hash=capsule.capsule_hash(),
        audit_chain_head=capsule.audit_chain_head,
        intent_capsule_hash=capsule.intent_capsule_hash,
    )


def seal_and_bind(
    *,
    chain: AuditChainStore,
    sdd_dir: Path,
    journal: EventJournal | None,
    capsule: ContextCapsule,
    private_key_pem: str,
    public_key_pem: str,
) -> tuple[SignedContextCapsule, dict[str, str]]:
    """Sign, persist, chain-anchor, and journal a real capsule in one call.

    Returns the sealed capsule and the spawn-record binding fragment. The three
    surfaces (spawn record, journal, chain) all carry the same ``capsule_hash``
    -- and therefore the same ``params_hash``.
    """
    signed = seal_context_capsule(capsule, private_key_pem, public_key_pem)
    write_capsule_record(sdd_dir, signed)
    anchor_capsule(chain, signed)
    if journal is not None:
        record_capsule_in_journal(journal, task_id=capsule.task_id, capsule_hash=capsule.capsule_hash())
    binding = capsule_spawn_binding(
        task_id=capsule.task_id,
        params_hash=capsule.params_hash,
        capsule_hash=capsule.capsule_hash(),
    )
    return signed, binding


# ---------------------------------------------------------------------------
# Offline verification (Phase 5 / AC3 + AC4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextVerifyResult:
    """Outcome of :func:`verify_context_capsule`."""

    ok: bool
    reason: str
    is_mock: bool = False
    signature_ok: bool = False
    chain_ok: bool = False
    journal_ok: bool = False
    capsule: ContextCapsule | None = None
    matched_fields: tuple[str, ...] = field(default_factory=tuple)


def verify_context_capsule(
    *,
    sdd_dir: Path,
    chain: AuditChainStore,
    task_id: str,
) -> ContextVerifyResult:
    """Recompute a capsule offline and check it against chain + journal (AC3/AC4).

    Verifies, in order:

    * the capsule record loads and is not a mock (a mock capsule fails with an
      explicit mock-domain diagnostic; its signature is over the mock domain and
      never verifies as real);
    * the real-domain detached signature verifies against the embedded key;
    * the HMAC audit chain verifies, and a ``context.capsule`` entry carries the
      recomputed capsule hash (a tampered capsule diverges here);
    * the run journal's Merkle chain verifies and the ``context.capsule_recorded``
      event at the recorded chain position carries the same capsule hash (a
      context divergence -- different params, budget, or chain head -- is caught
      as a hash mismatch).

    ``ok`` is True only when every check holds. A verifier holding only the
    journal, the chain, and the capsule record can run this end to end.
    """
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.audit_chain import EVENT_CONTEXT_CAPSULE

    signed = read_capsule_record(sdd_dir, task_id)
    if signed is None:
        return ContextVerifyResult(ok=False, reason="no context capsule for task")

    real_ok, mock_ok = verify_signature(signed)
    if signed.is_mock or (mock_ok and not real_ok):
        return ContextVerifyResult(
            ok=False,
            reason="capsule is a mock-layer fixture (mock-domain signature); mock capsules never verify as real",
            is_mock=True,
            signature_ok=False,
            capsule=signed.capsule,
        )
    if not real_ok:
        return ContextVerifyResult(
            ok=False,
            reason="capsule signature does not verify (tampered or wrong key)",
            capsule=signed.capsule,
        )

    capsule = signed.capsule
    recomputed = capsule.capsule_hash()

    chain_ok, chain_errors = chain.verify()
    if not chain_ok:
        detail = chain_errors[0] if chain_errors else "chain break"
        return ContextVerifyResult(
            ok=False,
            reason=f"audit chain fails verification ({detail})",
            signature_ok=True,
            capsule=capsule,
        )
    chain_matches = [
        e
        for e in chain.query(event_type=EVENT_CONTEXT_CAPSULE)
        if str(e.details.get("capsule_hash", "")) == recomputed and str(e.details.get("task_id", "")) == task_id
    ]
    if not chain_matches:
        return ContextVerifyResult(
            ok=False,
            reason="capsule hash is not anchored in the audit chain (tampered or never recorded)",
            signature_ok=True,
            chain_ok=True,
            capsule=capsule,
        )

    journal_path = sdd_dir / "runs" / capsule.run_id / "journal.jsonl"
    if not journal_path.exists():
        return ContextVerifyResult(
            ok=False,
            reason=f"run journal for {capsule.run_id!r} is missing; cannot re-derive at chain position",
            signature_ok=True,
            chain_ok=True,
            capsule=capsule,
        )
    jres = verify_journal(journal_path)
    if not jres.ok:
        detail = jres.errors[0] if jres.errors else "chain break"
        return ContextVerifyResult(
            ok=False,
            reason=f"run journal chain diverges ({detail}); steps were reordered or tampered",
            signature_ok=True,
            chain_ok=True,
            capsule=capsule,
        )
    journal_matches = [
        e
        for e in load_events(journal_path)
        if e.get("event") == CAPSULE_RECORDED_EVENT
        and str(e.get("task_id", "")) == task_id
        and str(e.get("capsule_hash", "")) == recomputed
    ]
    if not journal_matches:
        return ContextVerifyResult(
            ok=False,
            reason="capsule hash not recorded at any chain position in the run journal (context divergence)",
            signature_ok=True,
            chain_ok=True,
            journal_ok=True,
            capsule=capsule,
        )

    return ContextVerifyResult(
        ok=True,
        reason="",
        signature_ok=True,
        chain_ok=True,
        journal_ok=True,
        capsule=capsule,
        matched_fields=("params_hash", "capsule_hash", "audit_chain_head"),
    )


def project_capsule(signed: SignedContextCapsule) -> dict[str, Any]:
    """Return a compact operator-facing projection of a signed capsule."""
    capsule = signed.capsule
    return {
        "task_id": capsule.task_id,
        "run_id": capsule.run_id,
        "capsule_hash": capsule.capsule_hash(),
        "params_hash": capsule.params_hash,
        "role": capsule.role,
        "worktree_path": capsule.worktree_path,
        "budget_remaining_tokens": capsule.budget_remaining_tokens,
        "budget_remaining_usd_micros": capsule.budget_remaining_usd_micros,
        "dependency_state": [list(p) for p in capsule.dependency_state],
        "audit_chain_head": capsule.audit_chain_head,
        "intent_capsule_hash": capsule.intent_capsule_hash,
        "is_mock": signed.is_mock,
    }
