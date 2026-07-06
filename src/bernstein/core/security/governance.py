"""RBAC, budgets, and seat attribution as verifiable governance projections.

Issue #2309. Operators running Bernstein for teams need role-based access
control, budget enforcement, and per-seat attribution. Those are usually
mutable database state that is *logged* but not itself *verifiable*: the row
that recorded a denial can be edited after the fact, and the counter a budget
check reads can drift from the calls that produced it.

This module expresses each governance decision as a deterministic projection
over the signed lineage spine instead. Every access decision, budget check,
and seat attribution recomputes from the same inputs a verifier holds, and
each decision is anchored in the spine so it is independently provable rather
than merely recorded:

    decision = {subject, action, verdict, inputs_hash, journal_entry_hash}

The record's canonical bytes are what the spine hashes, so its
``journal_entry_hash`` is the spine entry hash over exactly those bytes -- the
decision's chain-verifiable identity. Strip the spine and the record is just a
file; anchored, it is a chain-verifiable attestation that recomputes offline.

Access control (AC1, AC2)
-------------------------
:func:`decide_access` resolves an IDP group set to a role via a signed
:class:`RoleBindings`, projects the role's permissions onto the requested
action, and writes a signed, anchored :class:`GovernanceDecision` -- ``allow``
when the role grants the action, ``deny`` otherwise. A denied action is still a
signed record. :func:`verify_governance` re-resolves and re-projects every
recorded decision from the presented bindings and confirms the recomputed
verdict matches the recorded one; a tampered verdict or a widened permission
fails the check.

Budgets (AC3)
-------------
:func:`check_budget_decision` recomputes the subject's cumulative spend from
the cost ledger (never a stored counter), and refuses -- raising
:class:`BudgetRefused` and writing a signed ``refuse`` record -- when the
subject's spend plus the next call would breach the cap. The cost ledger is the
single enforcement point.

Seat attribution (AC4)
----------------------
:func:`seat_spend` projects per-subject spend as a pure function of the ledger
rows on disk (``load_entries``), so two operators holding the same ledger
compute the byte-identical total without trusting a mutable in-process counter.

Determinism (AC5)
-----------------
Every decision row is canonical JSON (sorted keys, minimal separators, UTF-8)
and every field is either caller-supplied or a pure function of caller input,
so two replays of a governance-gated run against the same fixtures produce
byte-identical records and anchors -- no LLM in the decision loop.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.spend_ledger import SpendLedger
from bernstein.core.lineage.spine import LineageSpine, content_hash_of

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Actor recorded on every governance spine entry.
GOVERNANCE_ACTOR = "bernstein.governance"

#: Model string recorded on governance spine entries (no model runs at
#: decision time; the field is part of the spine schema).
_GOVERNANCE_MODEL = "none"

#: Version stamped into every decision / bindings preimage. Bump only on a
#: wire-format change.
GOVERNANCE_SCHEMA_VERSION = 1

#: Sub-path (relative to a run's spine dir) the persisted decision records land
#: in, colocated with the run's spine so the record and its anchor share one
#: root.
_DECISION_SUBPATH = ("governance_decisions",)

#: Role privilege order, highest first. When a subject's IDP groups map to more
#: than one role, the highest-privilege role wins.
_ROLE_PRIORITY: tuple[str, ...] = ("admin", "operator", "viewer")

#: The action string recorded on a budget decision.
_BUDGET_ACTION = "budget"


# ---------------------------------------------------------------------------
# Canonical hashing / signing helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _sign(key: bytes, payload: dict[str, Any]) -> str:
    """Return the HMAC-SHA256 signature over ``payload``'s canonical bytes."""
    return _hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()


def _safe_name(run_id: str) -> str:
    """Return a filesystem-safe artefact name component for *run_id*."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in run_id) or "run"


# ---------------------------------------------------------------------------
# RoleBindings -- signed IDP-group-to-role + role-to-permission mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleBindings:
    """Signed mapping of IDP groups to roles and roles to permissions.

    The bindings are the policy a verifier presents to recompute a recorded
    access verdict. Signing them binds the policy to the audit-chain key so a
    forged binding cannot silently rewrite history; :func:`verify_governance`
    checks the signature before re-projecting.

    Attributes:
        group_to_role: IDP group name -> role name. A subject in several groups
            resolves to the highest-privilege role among them.
        role_permissions: Role name -> the permission strings it grants. A role
            grants an action when the action is in its permission tuple.
        signature: HMAC signature over the mapping body; populated by
            :meth:`sign`.
    """

    group_to_role: dict[str, str]
    role_permissions: dict[str, tuple[str, ...]]
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "v": GOVERNANCE_SCHEMA_VERSION,
            "kind": "role_bindings",
            "group_to_role": dict(sorted(self.group_to_role.items())),
            "role_permissions": {role: sorted(set(perms)) for role, perms in sorted(self.role_permissions.items())},
        }

    def sign(self, key: bytes) -> RoleBindings:
        """Return a copy carrying the HMAC signature over the body."""
        return RoleBindings(
            group_to_role=self.group_to_role,
            role_permissions=self.role_permissions,
            signature=_sign(key, self._body()),
        )

    def verify_signature(self, key: bytes) -> bool:
        """Return True when ``signature`` matches the body under ``key``."""
        if not self.signature:
            return False
        return _hmac.compare_digest(self.signature, _sign(key, self._body()))

    def bindings_hash(self) -> str:
        """Return the content hash of the signed bindings (policy identity)."""
        return _sha256(self._body() | {"signature": self.signature})

    def to_dict(self) -> dict[str, Any]:
        return self._body() | {"signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> RoleBindings:
        raw_perms = row.get("role_permissions", {})
        role_permissions = {str(role): tuple(str(p) for p in perms) for role, perms in raw_perms.items()}
        return cls(
            group_to_role={str(g): str(r) for g, r in row.get("group_to_role", {}).items()},
            role_permissions=role_permissions,
            signature=str(row.get("signature", "")),
        )


def resolve_role(idp_groups: tuple[str, ...], bindings: RoleBindings) -> str:
    """Return the highest-privilege role the *idp_groups* map to.

    A pure projection: the subject's groups are mapped to roles via the
    bindings, and the highest-privilege role among them (per
    :data:`_ROLE_PRIORITY`) is returned. An unmapped group set returns the empty
    string (no role -> no permissions), so an unknown subject is denied.
    """
    roles = {bindings.group_to_role[g] for g in idp_groups if g in bindings.group_to_role}
    for role in _ROLE_PRIORITY:
        if role in roles:
            return role
    # A mapped-but-unranked role (operator-defined) still counts; pick the
    # lexicographically-first for determinism.
    return sorted(roles)[0] if roles else ""


def _role_grants(role: str, action: str, bindings: RoleBindings) -> bool:
    """Return True when *role* grants *action* under *bindings*."""
    if not role:
        return False
    return action in set(bindings.role_permissions.get(role, ()))


# ---------------------------------------------------------------------------
# GovernanceDecision -- the journal-anchored primary artefact (AC1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernanceDecision:
    """A signed, anchored record of one access or budget decision.

    ``journal_entry_hash`` anchors the record in the lineage spine over the
    record's canonical bytes -- its chain-verifiable identity. The record is the
    primary artefact: it is meaningless without the spine anchor.

    Attributes:
        run_id: The run whose spine the record anchors to.
        subject: The subject the decision is about (a seat / actor / user id).
        action: The requested action (a permission string, or ``budget``).
        verdict: One of ``allow``, ``deny`` (access), or ``refuse`` (budget).
        inputs_hash: Content hash of the decision inputs -- for access, the
            resolved ``(role, action, bindings_hash)``; for budget, the
            ``(subject, cap, prior_spend, next_cost)`` projection inputs.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
        context: Decision-kind-specific policy inputs carried in the binding so
            a verifier can recompute the verdict. For budget rows this holds the
            operator ``cap_usd`` and ``next_cost_usd`` (policy, never secrets);
            the recomputed part -- prior spend -- is projected from the ledger at
            verify time and never trusted from a stored counter. Empty for
            access rows.
        journal_entry_hash: The lineage-spine entry hash anchoring the record.
            Empty until the emitting function records it.
    """

    run_id: str
    subject: str
    action: str
    verdict: str
    inputs_hash: str
    timestamp: int
    context: dict[str, Any] = field(default_factory=dict)
    journal_entry_hash: str = ""

    def _binding(self) -> dict[str, Any]:
        """Return the anchored binding (everything except the anchor itself)."""
        return {
            "v": GOVERNANCE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "subject": self.subject,
            "action": self.action,
            "verdict": self.verdict,
            "inputs_hash": self.inputs_hash,
            "timestamp": self.timestamp,
            "context": dict(sorted(self.context.items())),
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialise the binding to canonical JSON bytes (spine-hashed)."""
        return _canonical_bytes(self._binding())

    def to_dict(self) -> dict[str, Any]:
        return self._binding() | {"journal_entry_hash": self.journal_entry_hash}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> GovernanceDecision:
        raw_context = row.get("context") or {}
        context = dict(raw_context) if isinstance(raw_context, dict) else {}
        return cls(
            run_id=str(row["run_id"]),
            subject=str(row["subject"]),
            action=str(row["action"]),
            verdict=str(row["verdict"]),
            inputs_hash=str(row["inputs_hash"]),
            timestamp=int(row["timestamp"]),
            context=context,
            journal_entry_hash=str(row.get("journal_entry_hash", "")),
        )


# ---------------------------------------------------------------------------
# Persistence (colocated with the run spine)
# ---------------------------------------------------------------------------


def decisions_dir(lineage_root: Path, run_id: str) -> Path:
    """Return the directory holding persisted decision records for *run_id*.

    Colocated with the run's spine dir so the record and its anchor share one
    root; the spine validates *run_id* against traversal at record time.
    """
    return lineage_root / run_id / _DECISION_SUBPATH[0]


def _record_filename(decision: GovernanceDecision, seq: int) -> str:
    """Return a stable, ordered artefact filename for *decision*.

    ``seq`` is a zero-padded monotonic index so records sort in emit order, and
    the inputs-hash fragment keeps names distinct within a timestamp.
    """
    frag = decision.inputs_hash[7:23] if decision.inputs_hash.startswith("sha256:") else decision.inputs_hash[:16]
    return f"{seq:06d}-{_safe_name(decision.subject)}-{frag}.json"


def _next_seq(out_dir: Path) -> int:
    """Return the next zero-based emit index for *out_dir* (append order)."""
    if not out_dir.is_dir():
        return 0
    return sum(1 for _ in out_dir.glob("*.json"))


def read_decisions(lineage_root: Path, run_id: str) -> list[GovernanceDecision]:
    """Load every persisted decision record for *run_id* (append order)."""
    out_dir = decisions_dir(lineage_root, run_id)
    if not out_dir.is_dir():
        return []
    records: list[GovernanceDecision] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            records.append(GovernanceDecision.from_dict(json.loads(path.read_bytes())))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("governance: malformed decision record at %s", path)
            continue
    return records


def _anchor_decision(
    *,
    lineage_root: Path,
    hmac_key: bytes,
    decision: GovernanceDecision,
) -> GovernanceDecision:
    """Anchor *decision* in the run spine and persist it. Returns the anchored copy.

    The decision's canonical bytes are what the spine hashes, so the returned
    record's ``journal_entry_hash`` is the spine entry hash over exactly those
    bytes (AC1).
    """
    out_dir = decisions_dir(lineage_root, decision.run_id)
    seq = _next_seq(out_dir)
    filename = _record_filename(decision, seq)
    artifact_path = "/".join((*_DECISION_SUBPATH, filename))

    spine = LineageSpine(lineage_root, run_id=decision.run_id, hmac_key=hmac_key)
    anchor = spine.record(
        artifact_path=artifact_path,
        content=decision.to_canonical_bytes(),
        actor=GOVERNANCE_ACTOR,
        step_id=decision.inputs_hash,
        model=_GOVERNANCE_MODEL,
        timestamp=decision.timestamp,
    )
    anchored = GovernanceDecision(
        run_id=decision.run_id,
        subject=decision.subject,
        action=decision.action,
        verdict=decision.verdict,
        inputs_hash=decision.inputs_hash,
        timestamp=decision.timestamp,
        context=decision.context,
        journal_entry_hash=anchor,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return anchored


# ---------------------------------------------------------------------------
# Access-decision inputs hash (recomputable projection)
# ---------------------------------------------------------------------------


def _access_inputs_hash(*, role: str, action: str, bindings: RoleBindings) -> str:
    """Return the content hash of the access-decision projection inputs.

    A verifier recomputes this from the same ``(role, action, bindings)`` and
    finds it must match the recorded ``inputs_hash`` -- so a widened binding is
    detected (the hash changes).
    """
    return _sha256(
        {
            "kind": "access",
            "role": role,
            "action": action,
            "bindings_hash": bindings.bindings_hash(),
        }
    )


def decide_access(
    *,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    subject: str,
    idp_groups: tuple[str, ...],
    action: str,
    bindings: RoleBindings,
    now: int,
) -> GovernanceDecision:
    """Project an access request onto a signed, anchored decision (AC1).

    The verdict is a pure projection: resolve the subject's IDP groups to a
    role, then check whether the role grants the action. ``allow`` when it does,
    ``deny`` otherwise -- a denied action is still a signed, anchored record.

    Args:
        run_id: The run whose spine the decision anchors to.
        lineage_root: Spine root (``.sdd/lineage``); the per-run dir lives
            beneath it.
        hmac_key: Audit-chain HMAC key that tags spine entries.
        subject: The subject the decision is about.
        idp_groups: The subject's IDP group memberships.
        action: The requested permission string.
        bindings: The signed role bindings the decision projects over.
        now: Integer timestamp; the decision timestamp and spine timestamp.

    Returns:
        The anchored :class:`GovernanceDecision`.
    """
    role = resolve_role(idp_groups, bindings)
    verdict = "allow" if _role_grants(role, action, bindings) else "deny"
    inputs_hash = _access_inputs_hash(role=role, action=action, bindings=bindings)
    decision = GovernanceDecision(
        run_id=run_id,
        subject=subject,
        action=action,
        verdict=verdict,
        inputs_hash=inputs_hash,
        timestamp=now,
    )
    return _anchor_decision(lineage_root=lineage_root, hmac_key=hmac_key, decision=decision)


# ---------------------------------------------------------------------------
# Seat / cost attribution (AC4)
# ---------------------------------------------------------------------------


def seat_spend(ledger_path: Path, subject: str, *, dimension: str = "agent") -> float:
    """Return *subject*'s cumulative spend recomputed from the ledger rows.

    A pure projection over the ledger file on disk: every row is re-read via
    :meth:`SpendLedger.load_entries` and the matching subject's ``cost_usd``
    summed. No mutable in-process counter is trusted, so two operators holding
    the same ledger compute the byte-identical total (AC4).

    Args:
        ledger_path: Path to the append-only spend ledger JSONL.
        subject: The seat / actor id to attribute spend to.
        dimension: Which attribution dimension the subject is matched against
            (``agent`` / ``task`` / ``role`` / ``feature_label``). Defaults to
            ``agent`` (the per-seat dimension).

    Returns:
        The subject's total spend in USD, or ``0.0`` when the ledger is missing
        or the subject has no rows.
    """
    total = 0.0
    for entry in SpendLedger.load_entries(ledger_path):
        if _entry_subject(entry, dimension) == subject:
            total += max(0.0, float(entry.cost_usd))
    return total


def _entry_subject(entry: Any, dimension: str) -> str:
    """Return a ledger entry's value on *dimension*."""
    return {
        "agent": entry.agent_id,
        "task": entry.task_id,
        "role": entry.role,
        "feature_label": entry.feature_label,
    }.get(dimension, entry.agent_id)


# ---------------------------------------------------------------------------
# Budget decision (AC3)
# ---------------------------------------------------------------------------


class BudgetRefused(RuntimeError):
    """Raised when a budget check refuses an action for a cap breach."""


def _budget_inputs_hash(
    *,
    subject: str,
    cap_usd: float,
    prior_spend_usd: float,
    next_cost_usd: float,
) -> str:
    """Return the content hash of the budget-decision projection inputs.

    A verifier recomputes ``prior_spend_usd`` from the ledger and this hash, and
    finds it must match the recorded ``inputs_hash``.
    """
    return _sha256(
        {
            "kind": "budget",
            "subject": subject,
            "cap_usd": round(cap_usd, 6),
            "prior_spend_usd": round(prior_spend_usd, 6),
            "next_cost_usd": round(next_cost_usd, 6),
        }
    )


def check_budget_decision(
    *,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    subject: str,
    cap_usd: float,
    next_cost_usd: float,
    ledger_path: Path,
    now: int,
    dimension: str = "agent",
) -> GovernanceDecision:
    """Project a per-subject budget check onto a signed, anchored decision (AC3).

    The subject's prior spend is recomputed from the ledger (never a stored
    counter). When prior spend plus the next call would breach the cap, a signed
    ``refuse`` record is anchored and :class:`BudgetRefused` is raised so the
    action is blocked. Otherwise a signed ``allow`` record is anchored and
    returned.

    Args:
        run_id: The run whose spine the decision anchors to.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: Audit-chain HMAC key that tags spine entries.
        subject: The seat / actor the budget is scoped to.
        cap_usd: Per-subject USD ceiling. A negative cap is treated as ``0``.
        next_cost_usd: The cost of the action being gated.
        ledger_path: Path to the append-only spend ledger.
        now: Integer timestamp.
        dimension: Attribution dimension the subject is matched against.

    Returns:
        The anchored ``allow`` :class:`GovernanceDecision`.

    Raises:
        BudgetRefused: When the projected spend would breach the cap.
    """
    cap = max(0.0, cap_usd)
    next_cost = max(0.0, next_cost_usd)
    prior = seat_spend(ledger_path, subject, dimension=dimension)
    projected = prior + next_cost
    verdict = "refuse" if projected > cap else "allow"
    inputs_hash = _budget_inputs_hash(
        subject=subject,
        cap_usd=cap,
        prior_spend_usd=prior,
        next_cost_usd=next_cost,
    )
    decision = GovernanceDecision(
        run_id=run_id,
        subject=subject,
        action=_BUDGET_ACTION,
        verdict=verdict,
        inputs_hash=inputs_hash,
        timestamp=now,
        context={
            "cap_usd": round(cap, 6),
            "next_cost_usd": round(next_cost, 6),
            "dimension": dimension,
        },
    )
    anchored = _anchor_decision(lineage_root=lineage_root, hmac_key=hmac_key, decision=decision)
    if verdict == "refuse":
        raise BudgetRefused(
            f"budget cap breach for {subject!r}: spend ${prior:.4f} + ${next_cost:.4f} exceeds cap ${cap:.4f}"
        )
    return anchored


# ---------------------------------------------------------------------------
# Verify (AC2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernanceVerifyResult:
    """Outcome of :func:`verify_governance`."""

    ok: bool
    checked: int = 0
    reason: str = ""
    mismatches: tuple[str, ...] = field(default_factory=tuple)


def verify_governance(
    *,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    bindings: RoleBindings,
    ledger_path: Path | None = None,
) -> GovernanceVerifyResult:
    """Recompute every recorded decision from the chain and match verdicts (AC2).

    For each persisted decision the verifier recomputes, from the presented
    bindings (and ledger, for budget rows) alone:

    * the bindings signature is valid under ``hmac_key``;
    * the record is still anchored in a spine that itself verifies, and the
      recorded ``journal_entry_hash`` matches the spine anchor over the record's
      canonical bytes;
    * the recorded ``inputs_hash`` recomputes from the projection inputs -- so a
      widened binding is detected (the hash changes);
    * a fresh projection of the same inputs reproduces the recorded verdict.

    A single-byte edit to any record, the bindings, or the spine fails the
    check. ``ok`` is True only when every recomputation matches. An empty run is
    not ``ok`` (a run with no decisions must not trivially pass).

    Args:
        run_id: The run whose decisions are verified.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: Audit-chain HMAC key that tags spine entries.
        bindings: The signed role bindings the access decisions project over.
        ledger_path: Optional spend ledger for recomputing budget rows. Required
            when the run carries budget decisions.

    Returns:
        A :class:`GovernanceVerifyResult`.
    """
    if not bindings.verify_signature(hmac_key):
        return GovernanceVerifyResult(ok=False, reason="role bindings signature invalid")

    records = read_decisions(lineage_root, run_id)
    if not records:
        return GovernanceVerifyResult(ok=False, checked=0, reason="no governance decisions recorded for this run")

    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return GovernanceVerifyResult(
            ok=False,
            checked=len(records),
            reason=f"governance spine failed verification ({spine_result.status.value})",
        )

    # Index spine entries by content hash so each record recomputes its anchor.
    anchor_by_content: dict[str, str] = {}
    for entry in spine.iter_entries():
        anchor_by_content.setdefault(entry.content_hash, entry.entry_hash)

    mismatches: list[str] = []
    for record in records:
        problem = _verify_one(
            record=record,
            bindings=bindings,
            ledger_path=ledger_path,
            anchor_by_content=anchor_by_content,
        )
        if problem:
            mismatches.append(problem)

    if mismatches:
        return GovernanceVerifyResult(
            ok=False,
            checked=len(records),
            reason=mismatches[0],
            mismatches=tuple(mismatches),
        )
    return GovernanceVerifyResult(ok=True, checked=len(records))


def _verify_one(
    *,
    record: GovernanceDecision,
    bindings: RoleBindings,
    ledger_path: Path | None,
    anchor_by_content: dict[str, str],
) -> str:
    """Return an empty string when *record* verifies, else the failure reason."""
    want = content_hash_of(record.to_canonical_bytes())
    anchor = anchor_by_content.get(want)
    if anchor is None:
        return f"{record.subject}/{record.action}: record is not anchored in the governance spine"
    if anchor != record.journal_entry_hash:
        return f"{record.subject}/{record.action}: recorded journal_entry_hash does not match the spine anchor"

    if record.action == _BUDGET_ACTION:
        return _verify_budget_row(record, ledger_path)
    return _verify_access_row(record, bindings)


def _verify_access_row(record: GovernanceDecision, bindings: RoleBindings) -> str:
    """Recompute an access verdict from the presented bindings and match it."""
    # The recorded inputs_hash pins the resolved role. Re-derive the verdict for
    # every role the bindings could resolve to, and confirm the recorded
    # inputs_hash + verdict are internally consistent with the presented policy.
    for role in _candidate_roles(bindings):
        if _access_inputs_hash(role=role, action=record.action, bindings=bindings) == record.inputs_hash:
            expected = "allow" if _role_grants(role, record.action, bindings) else "deny"
            if expected != record.verdict:
                return (
                    f"{record.subject}/{record.action}: recomputed verdict {expected!r} "
                    f"differs from recorded {record.verdict!r}"
                )
            return ""
    return (
        f"{record.subject}/{record.action}: recorded inputs_hash does not match any role "
        "under the presented bindings (policy differs)"
    )


def _candidate_roles(bindings: RoleBindings) -> tuple[str, ...]:
    """Return every role the bindings could resolve to, plus the empty role."""
    roles = set(bindings.group_to_role.values()) | set(bindings.role_permissions)
    return ("", *sorted(roles))


def _verify_budget_row(record: GovernanceDecision, ledger_path: Path | None) -> str:
    """Recompute a budget verdict from the ledger and match it.

    The record's ``context`` carries the operator policy inputs (``cap_usd``,
    ``next_cost_usd``, ``dimension``) -- policy, never secrets. The recomputed
    part, prior spend, is projected from the ledger on disk (never a stored
    counter). The verifier:

    * recomputes prior spend from the ledger;
    * confirms the recorded ``inputs_hash`` equals the hash over
      ``(subject, cap, recomputed prior, next_cost)`` -- so a ledger that no
      longer produces the pinned prior spend is detected;
    * re-derives the verdict (``refuse`` iff prior + next > cap) and confirms it
      matches the recorded verdict.
    """
    if ledger_path is None:
        return f"{record.subject}/budget: ledger required to verify budget decisions but none presented"
    try:
        cap = float(record.context["cap_usd"])
        next_cost = float(record.context["next_cost_usd"])
    except (KeyError, TypeError, ValueError):
        return f"{record.subject}/budget: record is missing cap_usd / next_cost_usd policy context"
    dimension = str(record.context.get("dimension", "agent"))

    prior = seat_spend(ledger_path, record.subject, dimension=dimension)
    recomputed_inputs = _budget_inputs_hash(
        subject=record.subject,
        cap_usd=cap,
        prior_spend_usd=prior,
        next_cost_usd=next_cost,
    )
    if recomputed_inputs != record.inputs_hash:
        return (
            f"{record.subject}/budget: recomputed inputs_hash does not match the recorded one "
            "(ledger spend differs from the decision inputs)"
        )
    expected = "refuse" if prior + max(0.0, next_cost) > max(0.0, cap) else "allow"
    if expected != record.verdict:
        return f"{record.subject}/budget: recomputed verdict {expected!r} differs from recorded {record.verdict!r}"
    return ""


__all__ = [
    "GOVERNANCE_ACTOR",
    "GOVERNANCE_SCHEMA_VERSION",
    "BudgetRefused",
    "GovernanceDecision",
    "GovernanceVerifyResult",
    "RoleBindings",
    "check_budget_decision",
    "decide_access",
    "decisions_dir",
    "read_decisions",
    "resolve_role",
    "seat_spend",
    "verify_governance",
]
