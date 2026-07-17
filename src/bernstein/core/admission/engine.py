"""The admission arbiter: grants, leases, postures, quarantine (#2544).

:class:`AdmissionEngine` is the single cross-process arbiter over the admission
ledger. Every decision it makes is appended as a hash-chained row; the current
admission state is always re-derived by projecting the chain
(:class:`~bernstein.core.admission.projection.AdmissionState`), never held as a
mutable side table. The engine is therefore thin: it reads the projection,
decides, and appends. Two engines pointed at the same ledger see the same state
because they read the same rows.

Killer shape: a grant's identity is the ``entry_hash`` of the row that issued
it. The engine never mints a grant id out of band; it appends a grant row and
the row's hash *is* the grant. Strip the chain and the grant has no identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.admission.ledger import (
    DEFAULT_ADMISSION_ID,
    KIND_EXPIRE,
    KIND_GRANT,
    KIND_POOL_CHANGED,
    KIND_QUARANTINE,
    KIND_QUEUE_CHANGED,
    KIND_RATE_CHANGED,
    KIND_RATE_OBSERVED,
    KIND_RELEASE,
    KIND_RENEW,
    KIND_TAG_CHANGED,
    KIND_WAIVE,
    open_admission_ledger,
    open_admission_reader,
)
from bernstein.core.admission.models import Posture, canonical_name
from bernstein.core.admission.projection import AdmissionState
from bernstein.core.admission.receipts import (
    WaiverReceipt,
    assemble_waiver_receipt,
    sign_receipt,
)
from bernstein.core.admission.tags import tags_admissible

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.admission.models import Grant
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "AdmissionDecision",
    "AdmissionEngine",
    "ExpiryOutcome",
    "QuarantineResult",
]


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Outcome of :meth:`AdmissionEngine.request_grant`."""

    admitted: bool
    grant_id: str = ""
    reason: str = ""
    over_limit: bool = False
    waiver: WaiverReceipt | None = None


@dataclass(frozen=True, slots=True)
class ExpiryOutcome:
    """Consequence of one lease expiry: the receipt and the resume decision."""

    grant: Grant
    expire_hash: str
    receipt: Any  # supervisor_receipt.EscalationReceipt (signed)
    retry_decision: Any  # checkpoint_retry.RetryDecision


@dataclass(frozen=True, slots=True)
class QuarantineResult:
    """Outcome of a class freeze: the single chain entry and its manifest."""

    entry_hash: str
    manifest: dict[str, Any]


@dataclass
class AdmissionEngine:
    """Append-and-project arbiter over one admission ledger."""

    sdd_dir: Path
    ledger_id: str = DEFAULT_ADMISSION_ID
    private_key_pem: str = ""
    public_key_pem: str = ""
    #: When set, grants/waivers/quarantines are mirrored into the HMAC audit
    #: chain rooted here. ``None`` (default) keeps the engine hermetic.
    audit_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.private_key_pem or not self.public_key_pem:
            from bernstein.core.skills.catalog.signature import generate_signer_keypair

            priv, pub = generate_signer_keypair()
            self.private_key_pem = self.private_key_pem or priv
            self.public_key_pem = self.public_key_pem or pub

    @classmethod
    def for_workdir(cls, workdir: Path, *, ledger_id: str = DEFAULT_ADMISSION_ID) -> AdmissionEngine:
        """Return an engine rooted at ``<workdir>/.sdd`` with install signing.

        The signing keypair is the install agent-card keystore, so receipts
        across CLI invocations share one stable identity a verifier can pin.
        Mirrors admission events into ``<workdir>/.sdd/audit``.
        """
        sdd_dir = workdir / ".sdd"
        private_pem, public_pem = _install_keypair()
        return cls(
            sdd_dir=sdd_dir,
            ledger_id=ledger_id,
            private_key_pem=private_pem,
            public_key_pem=public_pem,
            audit_dir=sdd_dir / "audit",
        )

    def _mirror(self, record: Callable[[AuditChainStore], None]) -> None:
        """Fire an audit-chain mirror helper when an audit dir is configured."""
        if self.audit_dir is None:
            return
        from bernstein.core.security.audit_chain import AuditChainStore

        record(AuditChainStore(self.audit_dir))

    # -- state --------------------------------------------------------------

    def state(self) -> AdmissionState:
        """Return the current admission state, projected from the chain."""
        reader = open_admission_reader(self.sdd_dir, self.ledger_id)
        return AdmissionState.from_rows(reader.entries())

    def _append(self, *, kind: str, task_id: str = "", payload: dict[str, Any]) -> str:
        ledger = open_admission_ledger(self.sdd_dir, self.ledger_id)
        entry = ledger.append(kind=kind, task_id=task_id, payload=payload)
        ledger.close()
        return entry.entry_hash

    # -- declarations (pool / tag / rate / queue) ---------------------------

    def set_pool(self, name: str, slots: int, posture: Posture = Posture.ENFORCE) -> str:
        """Create or update a named slot pool. Returns the row entry_hash."""
        pool = canonical_name(name)
        return self._append(
            kind=KIND_POOL_CHANGED,
            payload={"name": pool, "posture": posture.value, "slots": slots},
        )

    def set_tag_limit(self, tag: str, limit: int, posture: Posture = Posture.ENFORCE) -> str:
        """Set a concurrency ceiling over a task tag."""
        name = canonical_name(tag)
        return self._append(
            kind=KIND_TAG_CHANGED,
            payload={"limit": limit, "posture": posture.value, "tag": name},
        )

    def set_rate_limit(self, name: str, base_limit: int, *, floor: int = 1, posture: Posture = Posture.ENFORCE) -> str:
        """Define a fleet-wide named rate limit with adaptive decay."""
        rate = canonical_name(name)
        return self._append(
            kind=KIND_RATE_CHANGED,
            payload={"base_limit": base_limit, "floor": floor, "name": rate, "posture": posture.value},
        )

    def observe_429(self, name: str, now: int) -> str:
        """Record a 429 observation for a named rate limit."""
        rate = canonical_name(name)
        return self._append(kind=KIND_RATE_OBSERVED, payload={"name": rate, "ts": now})

    def set_queue(self, name: str, *, priority: int = 0, paused: bool = False) -> str:
        """Create or update an operator-defined named queue."""
        queue = canonical_name(name)
        return self._append(
            kind=KIND_QUEUE_CHANGED,
            payload={"name": queue, "paused": paused, "priority": priority},
        )

    # -- grants -------------------------------------------------------------

    def request_grant(
        self,
        *,
        pool: str,
        task_id: str,
        worker_id: str,
        now: int,
        ttl_s: int = 0,
        tags: tuple[str, ...] = (),
    ) -> AdmissionDecision:
        """Request admission for a task; append a grant row iff admissible.

        The decision is a pure read of the projected state followed by an
        append. Under ENFORCE a full pool refuses the grant (the task waits);
        under ADVISE the grant is issued ``over_limit`` and paired with a
        signed waiver receipt; under OFF the gate is inert.
        """
        state = self.state()
        pool_name = canonical_name(pool) if pool else ""
        declared = tuple(canonical_name(t) for t in tags)

        over_limit, refusal = self._evaluate(state, pool_name, declared)
        if refusal is not None:
            return AdmissionDecision(admitted=False, reason=refusal)

        slot_freed_by = state.last_expiry_by_pool.get(pool_name, "") if pool_name else ""
        grant_hash = self._append(
            kind=KIND_GRANT,
            task_id=task_id,
            payload={
                "over_limit": over_limit,
                "pool": pool_name,
                "slot_freed_by": slot_freed_by,
                "tags": list(declared),
                "task_id": task_id,
                "ttl_s": ttl_s,
                "ts": now,
                "worker_id": worker_id,
            },
        )

        self._mirror(
            lambda chain: _mirror_grant(chain, grant_hash, pool_name, task_id, worker_id, self.head_hash(), over_limit)
        )

        waiver: WaiverReceipt | None = None
        if over_limit:
            waiver = self._record_waiver(
                gate=pool_name or (declared[0] if declared else "tag"),
                resource=pool_name or ",".join(declared),
                task_id=task_id,
                worker_id=worker_id,
                granted_ts=now,
                prev_chain_digest=grant_hash,
            )
        return AdmissionDecision(admitted=True, grant_id=grant_hash, over_limit=over_limit, waiver=waiver)

    def head_hash(self) -> str:
        """Return the current admission-ledger head hash."""
        return self.state().head_hash

    def _evaluate(self, state: AdmissionState, pool_name: str, declared: tuple[str, ...]) -> tuple[bool, str | None]:
        """Return ``(over_limit, refusal_reason)`` for a candidate grant.

        ``refusal_reason`` is ``None`` when the grant is admissible (possibly
        over-limit under ADVISE).
        """
        over_limit = False
        # Pool capacity gate.
        if pool_name:
            spec = state.pools.get(pool_name)
            if spec is not None and spec.posture is not Posture.OFF and state.pool_occupancy(pool_name) >= spec.slots:
                if spec.posture is Posture.ENFORCE:
                    return False, f"pool {pool_name!r} at capacity ({spec.slots})"
                over_limit = True
        # Tag concurrency gate (AND-of-all declared tags).
        if declared:
            occupancy = state.tag_occupancy()
            enforce_limits = {
                tag: spec.limit for tag, spec in state.tag_limits.items() if spec.posture is Posture.ENFORCE
            }
            if not tags_admissible(declared, occupancy, enforce_limits):
                return False, "declared tag(s) at capacity"
            advise_limits = {
                tag: spec.limit for tag, spec in state.tag_limits.items() if spec.posture is Posture.ADVISE
            }
            if not tags_admissible(declared, occupancy, advise_limits):
                over_limit = True
        return over_limit, None

    def renew(self, grant_id: str, now: int) -> str:
        """Renew a grant's lease (piggybacked on the heartbeat signal)."""
        return self._append(kind=KIND_RENEW, payload={"grant": grant_id, "ts": now})

    def release(self, grant_id: str, now: int) -> str:
        """Release a grant, freeing its slot."""
        return self._append(kind=KIND_RELEASE, payload={"grant": grant_id, "ts": now})

    def _record_waiver(
        self, *, gate: str, resource: str, task_id: str, worker_id: str, granted_ts: int, prev_chain_digest: str
    ) -> WaiverReceipt:
        """Assemble, sign, and chain a waiver receipt for an over-limit pass."""
        receipt = assemble_waiver_receipt(
            gate=gate,
            resource=resource,
            task_id=task_id,
            worker_id=worker_id,
            granted_ts=granted_ts,
            prev_chain_digest=prev_chain_digest,
        )
        signed = sign_receipt(receipt, private_key_pem=self.private_key_pem)
        self._append(
            kind=KIND_WAIVE,
            task_id=task_id,
            payload={
                "gate": gate,
                "grant": prev_chain_digest,
                "payload_digest": signed.payload_digest,
                "resource": resource,
                "signature": signed.signature,
                "ts": granted_ts,
            },
        )
        self._mirror(
            lambda chain: _mirror_waiver(
                chain, prev_chain_digest, resource, task_id, signed.payload_digest, self.head_hash()
            )
        )
        return signed

    # -- lease expiry -------------------------------------------------------

    def sweep_expired(self, now: int) -> list[ExpiryOutcome]:
        """Expire every lease past its TTL; return the receipted consequences.

        Deterministic: which grants expire is a pure function of the
        chain-recorded renewals plus *now*. Never a silent slot recycle -- each
        expiry appends an ``admission.expire`` row, assembles a signed
        escalation receipt in the supervisor-receipt shape, and computes a
        checkpointed-retry resume decision. The next grant for the freed slot
        references the expiry row via ``slot_freed_by``.
        """
        state = self.state()
        outcomes: list[ExpiryOutcome] = []
        for grant in state.expired_grants(now):
            expire_hash = self._append(
                kind=KIND_EXPIRE,
                task_id=grant.task_id,
                payload={"grant": grant.grant_id, "ts": now},
            )
            receipt = self._assemble_expiry_receipt(grant, state, prev_chain_digest=expire_hash)
            retry_decision = self._resume_decision(grant)
            outcomes.append(
                ExpiryOutcome(
                    grant=grant,
                    expire_hash=expire_hash,
                    receipt=receipt,
                    retry_decision=retry_decision,
                )
            )
        return outcomes

    def _assemble_expiry_receipt(self, grant: Grant, state: AdmissionState, *, prev_chain_digest: str) -> Any:
        """Build a signed escalation receipt in the supervisor-receipt shape."""
        from bernstein.core.orchestration.supervisor_receipt import (
            IdentityTokens,
            StallReason,
            assemble_receipt,
        )
        from bernstein.core.orchestration.supervisor_receipt import (
            sign_receipt as sign_escalation,
        )

        # The audit slice that caused expiry: the grant + its renewals as the
        # trailing admission facts. Passed as event dicts the receipt can bind.
        audit_entries: list[dict[str, Any]] = [
            {
                "event_type": "admission.lease_expired",
                "session_id": grant.worker_id,
                "worktree_id": grant.worker_id,
                "details": {
                    "grant_id": grant.grant_id,
                    "pool": grant.pool,
                    "effective_expiry": grant.effective_expiry,
                    "renewed_ts": grant.renewed_ts,
                },
            }
        ]
        identity = IdentityTokens(keyid=_keyid(self.public_key_pem), run_id=self.ledger_id)
        unsigned = assemble_receipt(
            worker_id=grant.worker_id,
            worktree_id=grant.worker_id,
            session_id=grant.worker_id,
            stall_reason=StallReason.HEARTBEAT_STALE,
            audit_entries=audit_entries,
            identity=identity,
            prev_chain_digest=prev_chain_digest,
            respawn_budget_remaining=0,
            details={"grant_id": grant.grant_id, "pool": grant.pool, "task_id": grant.task_id},
        )
        signing_key = _load_ed25519_private(self.private_key_pem)
        return sign_escalation(unsigned, signing_key=signing_key)

    def _resume_decision(self, grant: Grant) -> Any:
        """Compute the checkpointed-retry resume decision for an expired grant.

        A lease expires because its holder was hard-killed, not because it made
        progress; its worktree is therefore preserved at the last checkpoint.
        The recorded workspace hash is thus the intact hash, so a warm/fork
        resume is honoured when a checkpoint exists (never a silent cold wipe).
        """
        from bernstein.core.tasks.checkpoint_retry import (
            RetryMode,
            decide_retry,
            latest_checkpoint,
        )

        checkpoint = latest_checkpoint(self.sdd_dir, grant.task_id)
        actual_hash = checkpoint.workspace_hash if checkpoint is not None else ""
        return decide_retry(
            task_id=grant.task_id,
            requested_mode=RetryMode.WARM,
            checkpoint=checkpoint,
            actual_workspace_hash=actual_hash,
            template_id="crash",
            gate_name="lease_expiry",
            gate_output=f"lease on pool {grant.pool} expired",
        )

    # -- quarantine ---------------------------------------------------------

    def quarantine(
        self, *, target_kind: str, target: str, now: int, queued_tasks: tuple[str, ...] = ()
    ) -> QuarantineResult:
        """Freeze a class (pool or tag) in one operation with a manifest.

        Sets the class limit to ``0``, expires every in-flight matching grant
        (checkpointing its worker), and emits ONE chain entry carrying the
        complete affected-set manifest: the checkpointed workers plus the
        queued tasks. Replaying the chain reproduces the identical manifest,
        because the manifest is the hashed row payload.
        """
        name = canonical_name(target)
        state = self.state()

        if target_kind == "pool":
            self.set_pool(name, 0, posture=Posture.ENFORCE)
            affected = [g for g in state.active_grants.values() if g.pool == name]
        elif target_kind == "tag":
            self.set_tag_limit(name, 0, posture=Posture.ENFORCE)
            affected = [g for g in state.active_grants.values() if name in g.tags]
        else:
            msg = f"unknown quarantine target_kind {target_kind!r} (expected 'pool' or 'tag')"
            raise ValueError(msg)

        affected.sort(key=lambda g: (g.granted_ts, g.grant_id))
        checkpointed: list[dict[str, str]] = []
        for grant in affected:
            self._append(kind=KIND_EXPIRE, task_id=grant.task_id, payload={"grant": grant.grant_id, "ts": now})
            checkpointed.append({"grant_id": grant.grant_id, "task_id": grant.task_id, "worker_id": grant.worker_id})

        manifest = {
            "checkpointed": checkpointed,
            "queued_tasks": sorted(queued_tasks),
            "target": name,
            "target_kind": target_kind,
            "ts": now,
        }
        entry_hash = self._append(kind=KIND_QUARANTINE, payload=manifest)
        self._mirror(
            lambda chain: _mirror_quarantine(chain, entry_hash, target_kind, name, len(checkpointed), self.head_hash())
        )
        return QuarantineResult(entry_hash=entry_hash, manifest=manifest | {"entry_hash": entry_hash})

    # -- post-hoc tag conformance ------------------------------------------

    def seal_tag_conformance(
        self,
        *,
        task_id: str,
        worker_id: str,
        declared_tags: tuple[str, ...],
        changed_paths: tuple[str, ...],
    ) -> Any:
        """Check a completed task's diff against its declared tags and sign it.

        Returns a signed :class:`~bernstein.core.admission.receipts.TagConformanceReceipt`.
        A task declaring ``docs-only`` whose diff touched ``src/`` yields a
        receipt with ``conformant=False`` and the offending paths, visible to
        merge gates. The verdict is deterministic in the declared tags and the
        changed-path set, so the receipt replays byte-identically.
        """
        import hashlib

        from bernstein.core.admission.receipts import (
            assemble_tag_conformance_receipt,
        )
        from bernstein.core.admission.receipts import (
            sign_receipt as sign_conformance,
        )
        from bernstein.core.admission.tags import check_conformance

        declared = tuple(canonical_name(t) for t in declared_tags)
        violations = check_conformance(declared, changed_paths)
        diff_digest = hashlib.sha256("\n".join(sorted(changed_paths)).encode("utf-8")).hexdigest()
        receipt = assemble_tag_conformance_receipt(
            task_id=task_id,
            worker_id=worker_id,
            declared_tags=declared,
            violations=violations,
            diff_digest=diff_digest,
            prev_chain_digest=self.head_hash(),
        )
        signed = sign_conformance(receipt, private_key_pem=self.private_key_pem)
        self._mirror(
            lambda chain: _mirror_tag_conformance(
                chain, task_id, worker_id, signed.conformant, signed.payload_digest, len(violations)
            )
        )
        return signed

    # -- verification -------------------------------------------------------

    def verify(self) -> Any:
        """Recompute admission state from genesis and fail closed on drift."""
        from bernstein.core.admission.verify import verify_admission_ledger

        reader = open_admission_reader(self.sdd_dir, self.ledger_id)
        return verify_admission_ledger(reader)


def _install_keypair() -> tuple[str, str]:
    """Return the install agent-card keypair PEM, generating on first use."""
    try:
        from bernstein.core.security.agent_card_keystore import (
            DEFAULT_KEY_DIR,
            AgentCardKeystore,
        )

        private_pem, public_pem = AgentCardKeystore(DEFAULT_KEY_DIR).load_or_generate()
        return private_pem.decode("ascii"), public_pem.decode("ascii")
    except Exception:  # pragma: no cover - keystore unavailable
        from bernstein.core.skills.catalog.signature import generate_signer_keypair

        return generate_signer_keypair()


def _keyid(public_key_pem: str) -> str:
    """Return a stable short key id (sha256 of the PEM bytes)."""
    import hashlib

    return hashlib.sha256(public_key_pem.encode("utf-8")).hexdigest()[:16]


def _load_ed25519_private(private_key_pem: str) -> Any:
    """Load an Ed25519 private key object from PEM for supervisor-receipt signing."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):  # pragma: no cover - keystore invariant
        msg = "admission signing key must be Ed25519"
        raise TypeError(msg)
    return key


def _mirror_grant(
    chain: AuditChainStore, grant_id: str, pool: str, task_id: str, worker_id: str, ledger_head: str, over_limit: bool
) -> None:
    from bernstein.core.security.audit_chain import record_admission_grant

    record_admission_grant(
        chain=chain,
        grant_id=grant_id,
        pool=pool,
        task_id=task_id,
        worker_id=worker_id,
        ledger_head=ledger_head,
        over_limit=over_limit,
    )


def _mirror_waiver(
    chain: AuditChainStore, grant_id: str, resource: str, task_id: str, receipt_digest: str, ledger_head: str
) -> None:
    from bernstein.core.security.audit_chain import record_admission_waiver

    record_admission_waiver(
        chain=chain,
        grant_id=grant_id,
        resource=resource,
        task_id=task_id,
        receipt_digest=receipt_digest,
        ledger_head=ledger_head,
    )


def _mirror_quarantine(
    chain: AuditChainStore, entry_hash: str, target_kind: str, target: str, affected_count: int, ledger_head: str
) -> None:
    from bernstein.core.security.audit_chain import record_admission_quarantine

    record_admission_quarantine(
        chain=chain,
        entry_hash=entry_hash,
        target_kind=target_kind,
        target=target,
        affected_count=affected_count,
        ledger_head=ledger_head,
    )


def _mirror_tag_conformance(
    chain: AuditChainStore, task_id: str, worker_id: str, conformant: bool, receipt_digest: str, violation_count: int
) -> None:
    from bernstein.core.security.audit_chain import record_admission_tag_conformance

    record_admission_tag_conformance(
        chain=chain,
        task_id=task_id,
        worker_id=worker_id,
        conformant=conformant,
        receipt_digest=receipt_digest,
        violation_count=violation_count,
    )
