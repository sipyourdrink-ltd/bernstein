"""Recomputable authority narrowing for per-hop delegation receipts (#2554).

A delegation receipt chain proves *sequence and integrity*: hop N follows hop
N-1 and no field was flipped. On its own it cannot prove *narrowing*, because
the authority granted at each hop was never recorded - so "did this sub-agent
end up holding more than the worker that spawned it?" could only be answered by
trusting that route checks had rejected every invalid action in the first
place. That is an argument, not a receipt.

This module supplies the missing record and the recomputation over it:

* :class:`DelegationScope` - the effective authority granted at one hop,
  content-addressed by :meth:`DelegationScope.scope_ref` so a receipt may carry
  either the scope inline or just its reference.
* :class:`DecisionBinding` - the charter hash and tenant-certificate version in
  force *at the moment the hop was recorded*, so a later membership change
  cannot reinterpret a historical decision.
* Three deliberately **distinct** checks, each answering a different question:

  ============================  ===================================================
  :func:`check_narrowing`       Did a child hop gain authority its parent lacked?
  :func:`check_duties`          Did one principal exercise incompatible powers?
  :func:`check_binding`         Was the decision pinned to a charter version?
  ============================  ===================================================

  Blurring them loses information: a chain can narrow perfectly and still let a
  single worker both spawn a task and approve its own gate, and a chain can
  separate duties cleanly while silently widening scope at one hop.

Subset semantics are not reinvented here. The allowlist, POSIX path-prefix, and
upper-bound primitives are imported from
:mod:`bernstein.core.security.capability_tokens`, which already defines them for
signed capability tokens, so a scope axis means the same thing on both surfaces.

Backward compatibility
----------------------
Every field this module adds to a receipt is optional. A chain recorded before
these fields existed carries no scope, produces no violations, and verifies
exactly as it did before. A chain that records scope must record it at *every*
hop up to its root: a scoped hop whose parent has no recorded scope is reported
as ``unscoped_parent``, because an unrecorded ceiling makes narrowing
unprovable rather than proven.

:func:`grade_chain` adds a second, additive reading of the same receipts: a
three-valued pass / fail / unproven verdict with one row per hop. It exists
because ``ChainResult.valid`` cannot separate a chain whose narrowing was
checked and held from a chain that recorded no scope to check. It records no
:class:`ScopeViolation` and leaves :class:`AuthorityReport` alone, so ``valid``
keeps the meaning it already had.

One reason from the design sketch is absent because this schema cannot reach
it: there is no scope version field for ``scope_version_unsupported`` to fire
on. ``parent_ref_missing`` is absent for a different reason. The chain is
HMAC-linked, so a hop that names no parent still has a known predecessor and
is graded against it rather than excused.

``parent_ref`` is part of the signed body, written by the same party that
writes the scope, so the grader never lets it decide whether a hop is examined.
Root status is positional: only a hop with nothing before it in the supplied
set is a root. A hop that names the genesis anchor mid-chain is reported
``root_claimed_mid_chain`` and is unproven, never a pass.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs
from bernstein.core.security.capability_tokens import (
    allowlist_narrows,
    bound_narrows,
    prefixes_narrow,
    uses_narrows,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from bernstein.core.identity.delegation import DelegationReceipt

__all__ = [
    "CHAIN_REASONS",
    "CHECK_BINDING",
    "CHECK_DUTIES",
    "CHECK_NARROWING",
    "DEFAULT_SEPARATED_DUTIES",
    "DIAGNOSTIC_SCOPE_REF_ONLY_RESOLVED",
    "DIAGNOSTIC_SCOPE_REF_UNRESOLVED",
    "DUTY_APPROVE",
    "DUTY_MERGE",
    "DUTY_SPAWN",
    "FAIL_REASONS",
    "GATED_DUTIES",
    "PASS_REASONS",
    "REASON_AXIS_WIDENED",
    "REASON_AXIS_WIDENED_VS_ANCESTOR",
    "REASON_CHAIN_INVALID",
    "REASON_COMPARISON_AXIS_UNSUPPORTED",
    "REASON_NO_SCOPE_RECORDED",
    "REASON_PARENT_RECEIPT_UNAVAILABLE",
    "REASON_PARENT_SCOPE_UNAVAILABLE",
    "REASON_ROOT_CLAIMED_MID_CHAIN",
    "REASON_ROOT_STRUCTURAL_ONLY",
    "REASON_SCOPE_MISSING",
    "REASON_SCOPE_REF_CONFLICT",
    "REASON_SCOPE_REF_ONLY_UNRESOLVED",
    "SCOPE_BODY_KEYS",
    "UNPROVEN_REASONS",
    "VERDICT_DIAGNOSTICS",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_REASONS",
    "VERDICT_UNPROVEN",
    "AuthorityReport",
    "ChainVerdict",
    "DecisionBinding",
    "DelegationScope",
    "HopVerdict",
    "ScopeViolation",
    "check_binding",
    "check_duties",
    "check_narrowing",
    "duties_for_act",
    "grade_chain",
    "narrowing_violations",
    "verify_authority",
]

#: Duty vocabulary for separation of duties. Kept deliberately small: these are
#: the three powers the delegation surface can separate today.
DUTY_SPAWN: str = "spawn"
DUTY_APPROVE: str = "approve"
DUTY_MERGE: str = "merge"

#: Duty pairs that may not be held by the same principal within one run. The
#: maker-checker rule in both directions: whoever asked for the work does not
#: get to bless it, and whoever blessed it does not get to land it.
DEFAULT_SEPARATED_DUTIES: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({DUTY_SPAWN, DUTY_APPROVE}),
        frozenset({DUTY_SPAWN, DUTY_MERGE}),
        frozenset({DUTY_APPROVE, DUTY_MERGE}),
    }
)

#: Duties whose exercise is a *decision* rather than an execution step. A
#: decision must cite the charter version it was taken under, otherwise a later
#: membership change silently reinterprets it.
GATED_DUTIES: frozenset[str] = frozenset({DUTY_APPROVE, DUTY_MERGE})

#: Check identifiers carried on every :class:`ScopeViolation`.
CHECK_NARROWING: str = "narrowing"
CHECK_DUTIES: str = "separation_of_duties"
CHECK_BINDING: str = "binding"

_KNOWN_DUTIES: frozenset[str] = frozenset({DUTY_SPAWN, DUTY_APPROVE, DUTY_MERGE})


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _frozen(value: Any) -> frozenset[str] | None:
    """Return ``value`` as a frozenset, preserving ``None`` (unconstrained)."""
    if value is None:
        return None
    return frozenset(value)


@dataclass(frozen=True)
class DelegationScope:
    """The effective authority granted at one delegation hop.

    Every axis follows the capability-token convention that ``None`` is the
    *widest* value (unconstrained / unbounded), so an axis a parent left open
    cannot be tightened away by omission on the child - a child that drops a
    bound its parent imposed widens on that axis and is reported.

    ``permissions`` and ``duties`` are plain sets: an empty set is the
    narrowest possible grant, never the widest.
    """

    permissions: frozenset[str] = frozenset()
    duties: frozenset[str] = frozenset()
    task_ids: frozenset[str] | None = None
    path_prefixes: frozenset[str] | None = None
    not_after: float | None = None
    max_uses: int | None = None
    max_depth: int | None = None

    def to_body(self) -> dict[str, Any]:
        """Return the JCS-ready dict (sets rendered as sorted lists)."""
        return {
            "duties": sorted(self.duties),
            "max_depth": self.max_depth,
            "max_uses": self.max_uses,
            "not_after": self.not_after,
            "path_prefixes": sorted(self.path_prefixes) if self.path_prefixes is not None else None,
            "permissions": sorted(self.permissions),
            "task_ids": sorted(self.task_ids) if self.task_ids is not None else None,
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> DelegationScope:
        """Inverse of :meth:`to_body` (tolerates absent keys as widest/empty)."""
        return cls(
            permissions=frozenset(body.get("permissions") or ()),
            duties=frozenset(body.get("duties") or ()),
            task_ids=_frozen(body.get("task_ids")),
            path_prefixes=_frozen(body.get("path_prefixes")),
            not_after=None if body.get("not_after") is None else float(body["not_after"]),
            max_uses=None if body.get("max_uses") is None else int(body["max_uses"]),
            max_depth=None if body.get("max_depth") is None else int(body["max_depth"]),
        )

    def scope_ref(self) -> str:
        """Return the content address of this scope (``sha256:<hex>``).

        The digest is taken over the RFC 8785 canonical bytes of
        :meth:`to_body`, so two operators describing the same authority mint
        the same reference and a receipt can carry the reference alone.
        """
        return "sha256:" + hashlib.sha256(canonicalize_jcs(self.to_body())).hexdigest()

    def narrowing_violations(self, parent: DelegationScope) -> tuple[str, ...]:
        """Return the axes on which ``self`` widens ``parent`` (empty = narrows)."""
        return narrowing_violations(self, parent)

    def is_narrowing_of(self, parent: DelegationScope) -> bool:
        """Return True iff this scope is a subset of ``parent`` on every axis."""
        return not narrowing_violations(self, parent)


def narrowing_violations(child: DelegationScope, parent: DelegationScope) -> tuple[str, ...]:
    """Return the scope axes on which ``child`` widens ``parent``.

    An empty tuple means the child is a strict subset of the parent's grant.
    Axis names are stable identifiers so a verifier can report *which* axis was
    widened instead of a bare pass/fail.
    """
    axes: list[str] = []
    if not child.permissions <= parent.permissions:
        axes.append("permissions")
    if not child.duties <= parent.duties:
        axes.append("duties")
    if not allowlist_narrows(child.task_ids, parent.task_ids):
        axes.append("task_ids")
    if not prefixes_narrow(child.path_prefixes, parent.path_prefixes):
        axes.append("path_prefixes")
    if not bound_narrows(child.not_after, parent.not_after):
        axes.append("not_after")
    if not uses_narrows(child.max_uses, parent.max_uses):
        axes.append("max_uses")
    if not bound_narrows(child.max_depth, parent.max_depth):
        axes.append("max_depth")
    return tuple(axes)


# ---------------------------------------------------------------------------
# Decision-time version binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionBinding:
    """The charter/certificate version in force when a hop was recorded.

    Carried inside the receipt body, so it is covered by the receipt HMAC: the
    version a decision was taken under is fixed at decision time and a later
    membership change cannot reinterpret it.
    """

    charter_hash: str
    certificate_hash: str | None = None
    certificate_version: str | None = None

    def to_body(self) -> dict[str, Any]:
        """Return the JSON body, omitting absent optional fields."""
        body: dict[str, Any] = {"charter_hash": self.charter_hash}
        if self.certificate_hash is not None:
            body["certificate_hash"] = self.certificate_hash
        if self.certificate_version is not None:
            body["certificate_version"] = self.certificate_version
        return body

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> DecisionBinding:
        """Inverse of :meth:`to_body`."""
        return cls(
            charter_hash=str(body.get("charter_hash", "")),
            certificate_hash=body.get("certificate_hash"),
            certificate_version=body.get("certificate_version"),
        )


# ---------------------------------------------------------------------------
# Violations and report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeViolation:
    """One recomputed authority failure, with the offending axis named."""

    check: str
    hop_index: int
    axis: str
    detail: str
    parent_hop_index: int | None = None
    principal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for CLI / machine consumption."""
        return {
            "axis": self.axis,
            "check": self.check,
            "detail": self.detail,
            "hop_index": self.hop_index,
            "parent_hop_index": self.parent_hop_index,
            "principal": self.principal,
        }

    def __str__(self) -> str:
        where = f"hop {self.hop_index}"
        if self.parent_hop_index is not None:
            where += f" (parent hop {self.parent_hop_index})"
        return f"{where}: {self.check}/{self.axis}: {self.detail}"


@dataclass
class AuthorityReport:
    """Outcome of the three authority checks over one run's receipts."""

    narrowing_ok: bool = True
    duties_ok: bool = True
    binding_ok: bool = True
    #: ``"none"`` (no hop records a scope), ``"partial"``, or ``"full"``.
    scope_coverage: str = "none"
    violations: list[ScopeViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff every check passed."""
        return self.narrowing_ok and self.duties_ok and self.binding_ok

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "binding_ok": self.binding_ok,
            "duties_ok": self.duties_ok,
            "narrowing_ok": self.narrowing_ok,
            "ok": self.ok,
            "scope_coverage": self.scope_coverage,
            "violations": [v.to_dict() for v in self.violations],
        }


# ---------------------------------------------------------------------------
# Receipt-side helpers
# ---------------------------------------------------------------------------


def duties_for_act(act: str) -> frozenset[str]:
    """Infer the duties an ``act`` string exercises.

    Only used when a receipt records no explicit scope duties, so chains
    written before this module gain separation-of-duties coverage from the act
    names they already carry. Matching is on whole dot/underscore/dash-separated
    segments (``task.spawn`` -> ``spawn``), never on substrings, so an act like
    ``spawner.status`` does not accidentally claim the spawn duty.
    """
    return frozenset(set(_split_act(act)) & _KNOWN_DUTIES)


def _split_act(act: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for ch in act.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _resolve_scope(
    receipt: DelegationReceipt,
    resolver: Callable[[str], DelegationScope | None] | None,
) -> DelegationScope | None:
    """Return the scope a receipt grants, inline or via its content address."""
    if receipt.scope is not None:
        return DelegationScope.from_body(receipt.scope)
    if receipt.scope_ref and resolver is not None:
        return resolver(receipt.scope_ref)
    return None


def _binding_of(receipt: DelegationReceipt) -> DecisionBinding | None:
    if receipt.binding is None:
        return None
    return DecisionBinding.from_body(receipt.binding)


def _exercised_duties(
    receipt: DelegationReceipt,
    scope: DelegationScope | None,
) -> frozenset[str]:
    """Return the duties this hop exercises (explicit scope wins over the act)."""
    if scope is not None and scope.duties:
        return scope.duties
    return duties_for_act(receipt.act)


def _parent_index(
    receipts: Sequence[DelegationReceipt],
    index: int,
    by_hmac: dict[str, int],
    genesis: str,
) -> int | None | str:
    """Resolve a hop's parent index.

    Returns the parent's index, ``None`` when the hop is a chain root, or an
    error string when ``parent_ref`` names no earlier hop.
    """
    receipt = receipts[index]
    ref = receipt.parent_ref
    if ref is None:
        return index - 1 if index > 0 else None
    if ref == genesis:
        return None
    parent = by_hmac.get(ref)
    if parent is None or parent >= index:
        return f"parent_ref {ref[:16]}... names no preceding hop"
    return parent


# ---------------------------------------------------------------------------
# Check 1: narrowing (did a child gain authority its parent lacked?)
# ---------------------------------------------------------------------------


def check_narrowing(
    receipts: Sequence[DelegationReceipt],
    *,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
    genesis: str = "0" * 64,
) -> tuple[list[ScopeViolation], str]:
    """Recompute child-scope is-a-subset-of parent-scope for every hop.

    Returns ``(violations, coverage)`` where ``coverage`` is ``"none"``,
    ``"partial"``, or ``"full"``. Violations name the offending axis.

    Failure modes, each reported with its own axis name:

    * ``scope_ref_mismatch`` - the inline scope does not hash to the recorded
      content address, so the reference and the body disagree.
    * ``unresolved_parent`` - ``parent_ref`` names no preceding hop.
    * ``unscoped_parent`` - the hop records a scope but its parent does not, so
      the ceiling it narrows against is unrecorded and narrowing is unprovable.
    * one entry per widened axis (``permissions``, ``duties``, ``task_ids``,
      ``path_prefixes``, ``not_after``, ``max_uses``, ``max_depth``).
    """
    violations: list[ScopeViolation] = []
    by_hmac = {r.hmac: i for i, r in enumerate(receipts) if r.hmac}
    scopes: list[DelegationScope | None] = []

    for receipt in receipts:
        scope = _resolve_scope(receipt, scope_resolver)
        scopes.append(scope)

    scoped = sum(1 for s in scopes if s is not None)
    if scoped == 0:
        coverage = "none"
    elif scoped == len(receipts):
        coverage = "full"
    else:
        coverage = "partial"

    for index, receipt in enumerate(receipts):
        scope = scopes[index]

        if receipt.scope is not None and receipt.scope_ref:
            expected = DelegationScope.from_body(receipt.scope).scope_ref()
            if expected != receipt.scope_ref:
                violations.append(
                    ScopeViolation(
                        check=CHECK_NARROWING,
                        hop_index=receipt.hop_index,
                        axis="scope_ref_mismatch",
                        detail=(
                            f"recorded scope_ref {receipt.scope_ref} does not match the "
                            f"content address of the inline scope ({expected})"
                        ),
                        principal=receipt.subject,
                    )
                )

        if receipt.scope_ref and scope is None:
            violations.append(
                ScopeViolation(
                    check=CHECK_NARROWING,
                    hop_index=receipt.hop_index,
                    axis="unresolved_scope",
                    detail=(f"scope_ref {receipt.scope_ref} could not be resolved to a scope body"),
                    principal=receipt.subject,
                )
            )
            continue

        parent = _parent_index(receipts, index, by_hmac, genesis)
        if isinstance(parent, str):
            violations.append(
                ScopeViolation(
                    check=CHECK_NARROWING,
                    hop_index=receipt.hop_index,
                    axis="unresolved_parent",
                    detail=parent,
                    principal=receipt.subject,
                )
            )
            continue

        if scope is None or parent is None:
            # No recorded grant to check, or a chain root whose scope *is* the
            # ceiling. Either way there is nothing above it to narrow against.
            continue

        parent_scope = scopes[parent]
        parent_hop = receipts[parent].hop_index
        if parent_scope is None:
            violations.append(
                ScopeViolation(
                    check=CHECK_NARROWING,
                    hop_index=receipt.hop_index,
                    axis="unscoped_parent",
                    detail=(
                        "hop records an effective scope but its parent records none, "
                        "so the ceiling it narrows against is unrecorded"
                    ),
                    parent_hop_index=parent_hop,
                    principal=receipt.subject,
                )
            )
            continue

        for axis in narrowing_violations(scope, parent_scope):
            violations.append(
                ScopeViolation(
                    check=CHECK_NARROWING,
                    hop_index=receipt.hop_index,
                    axis=axis,
                    detail=(
                        f"child scope widens the parent on {axis}: "
                        f"{_axis_repr(scope, axis)} is not within {_axis_repr(parent_scope, axis)}"
                    ),
                    parent_hop_index=parent_hop,
                    principal=receipt.subject,
                )
            )

    return violations, coverage


def _axis_repr(scope: DelegationScope, axis: str) -> str:
    """Render one scope axis for a violation message."""
    value = getattr(scope, axis, None)
    if value is None:
        return "unconstrained"
    if isinstance(value, frozenset):
        return "{" + ", ".join(sorted(value)) + "}"
    return repr(value)


# ---------------------------------------------------------------------------
# Check 2: separation of duties (did one principal hold incompatible powers?)
# ---------------------------------------------------------------------------


def check_duties(
    receipts: Sequence[DelegationReceipt],
    *,
    separated: Iterable[Iterable[str]] = DEFAULT_SEPARATED_DUTIES,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
) -> list[ScopeViolation]:
    """Report principals that exercised incompatible duties within one run.

    This is *not* the narrowing check. Narrowing asks whether a child gained
    authority its parent lacked; a chain can narrow perfectly at every hop and
    still route both ``spawn`` and ``approve`` to the same principal, which is
    exactly the separation-of-duties failure an auditor asks about.

    The acting principal is the receipt's ``subject`` (the party being
    authorized to act). Duties come from the hop's recorded scope when it
    declares any, and are otherwise inferred from the ``act`` name so chains
    recorded before scopes existed are still covered.
    """
    pairs = [frozenset(p) for p in separated]
    seen: dict[str, dict[str, DelegationReceipt]] = {}
    violations: list[ScopeViolation] = []

    for receipt in receipts:
        scope = _resolve_scope(receipt, scope_resolver)
        principal = receipt.subject
        held = seen.setdefault(principal, {})
        for duty in sorted(_exercised_duties(receipt, scope)):
            for pair in pairs:
                if duty not in pair:
                    continue
                other = next(iter(pair - {duty}), None)
                if other is None or other not in held:
                    continue
                first = held[other]
                violations.append(
                    ScopeViolation(
                        check=CHECK_DUTIES,
                        hop_index=receipt.hop_index,
                        axis="|".join(sorted(pair)),
                        detail=(
                            f"principal {principal!r} exercised {other!r} at hop "
                            f"{first.hop_index} ({first.act}) and {duty!r} at hop "
                            f"{receipt.hop_index} ({receipt.act}); these duties are separated"
                        ),
                        parent_hop_index=first.hop_index,
                        principal=principal,
                    )
                )
            held.setdefault(duty, receipt)

    return violations


# ---------------------------------------------------------------------------
# Check 3: decision-time version binding
# ---------------------------------------------------------------------------


def check_binding(
    receipts: Sequence[DelegationReceipt],
    *,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
    genesis: str = "0" * 64,
) -> list[ScopeViolation]:
    """Report hops whose charter/certificate version binding is absent or drifts.

    Two failures, both of which let a later membership change reinterpret a
    historical decision:

    * ``binding_missing`` - the chain binds a charter version somewhere, but a
      hop exercising a gated duty (``approve`` / ``merge``) records none, so
      that decision floats free of the version it was taken under.
    * ``binding_drift`` - a hop cites a different charter hash or certificate
      version than the parent it descends from. One authority chain is
      evaluated under one version; a changed charter needs a new certificate,
      not a re-interpreted hop.
    """
    violations: list[ScopeViolation] = []
    bindings = [_binding_of(r) for r in receipts]
    if not any(b is not None for b in bindings):
        return violations

    by_hmac = {r.hmac: i for i, r in enumerate(receipts) if r.hmac}

    for index, receipt in enumerate(receipts):
        binding = bindings[index]
        duties = _exercised_duties(receipt, _resolve_scope(receipt, scope_resolver))

        if binding is None:
            if duties & GATED_DUTIES:
                violations.append(
                    ScopeViolation(
                        check=CHECK_BINDING,
                        hop_index=receipt.hop_index,
                        axis="binding_missing",
                        detail=(
                            f"hop exercises {sorted(duties & GATED_DUTIES)} but records no "
                            "charter/certificate binding, so a later membership change would "
                            "reinterpret this decision"
                        ),
                        principal=receipt.subject,
                    )
                )
            continue

        parent = _parent_index(receipts, index, by_hmac, genesis)
        if isinstance(parent, str) or parent is None:
            continue
        parent_binding = bindings[parent]
        if parent_binding is None:
            continue
        for axis in ("charter_hash", "certificate_hash", "certificate_version"):
            mine = getattr(binding, axis)
            theirs = getattr(parent_binding, axis)
            if mine != theirs:
                violations.append(
                    ScopeViolation(
                        check=CHECK_BINDING,
                        hop_index=receipt.hop_index,
                        axis="binding_drift",
                        detail=(
                            f"{axis} {mine!r} differs from the parent hop's {theirs!r}; "
                            "one authority chain is evaluated under one charter version"
                        ),
                        parent_hop_index=receipts[parent].hop_index,
                        principal=receipt.subject,
                    )
                )

    return violations


# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------


def verify_authority(
    receipts: Sequence[DelegationReceipt],
    *,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
    separated: Iterable[Iterable[str]] = DEFAULT_SEPARATED_DUTIES,
    genesis: str = "0" * 64,
) -> AuthorityReport:
    """Run the three authority checks and return their combined verdict.

    The sub-verdicts stay separate on the report: a caller can tell a widening
    chain from one that merely concentrated duties, which a single blended
    boolean would hide.
    """
    narrowing, coverage = check_narrowing(receipts, scope_resolver=scope_resolver, genesis=genesis)
    duties = check_duties(receipts, separated=separated, scope_resolver=scope_resolver)
    binding = check_binding(receipts, scope_resolver=scope_resolver, genesis=genesis)
    return AuthorityReport(
        narrowing_ok=not narrowing,
        duties_ok=not duties,
        binding_ok=not binding,
        scope_coverage=coverage,
        violations=[*narrowing, *duties, *binding],
    )


# ---------------------------------------------------------------------------
# Graded chain verdict (issue #2554)
# ---------------------------------------------------------------------------

#: A hop whose narrowing was evaluated and held.
VERDICT_PASS: str = "pass"
#: A hop that contradicts itself or gained authority its ceiling lacked.
VERDICT_FAIL: str = "fail"
#: A hop that is well formed but carries too little signed material to decide.
VERDICT_UNPROVEN: str = "unproven"

#: Reason strings. The set is closed: consumers are expected to fail closed on
#: exact matches, so adding one is a versioned change to this contract. Every
#: reason below is reachable from a code path in this module.
REASON_ROOT_STRUCTURAL_ONLY: str = "root_structural_only"
REASON_SCOPE_REF_CONFLICT: str = "scope_ref_conflict"
REASON_AXIS_WIDENED: str = "axis_widened"
REASON_AXIS_WIDENED_VS_ANCESTOR: str = "axis_widened_vs_ancestor"
REASON_CHAIN_INVALID: str = "chain_invalid"
REASON_SCOPE_MISSING: str = "scope_missing"
REASON_SCOPE_REF_ONLY_UNRESOLVED: str = "scope_ref_only_unresolved"
REASON_PARENT_RECEIPT_UNAVAILABLE: str = "parent_receipt_unavailable"
REASON_PARENT_SCOPE_UNAVAILABLE: str = "parent_scope_unavailable"
REASON_COMPARISON_AXIS_UNSUPPORTED: str = "comparison_axis_unsupported"
REASON_ROOT_CLAIMED_MID_CHAIN: str = "root_claimed_mid_chain"
REASON_NO_SCOPE_RECORDED: str = "no_scope_recorded"

#: Reasons that make a hop fail.
FAIL_REASONS: frozenset[str] = frozenset(
    {
        REASON_AXIS_WIDENED,
        REASON_AXIS_WIDENED_VS_ANCESTOR,
        REASON_SCOPE_REF_CONFLICT,
    }
)

#: Reasons that make a hop unproven when no fail reason is also present.
UNPROVEN_REASONS: frozenset[str] = frozenset(
    {
        REASON_COMPARISON_AXIS_UNSUPPORTED,
        REASON_PARENT_RECEIPT_UNAVAILABLE,
        REASON_PARENT_SCOPE_UNAVAILABLE,
        REASON_ROOT_CLAIMED_MID_CHAIN,
        REASON_SCOPE_MISSING,
        REASON_SCOPE_REF_ONLY_UNRESOLVED,
    }
)

#: Reasons a row may carry without disturbing a pass verdict.
PASS_REASONS: frozenset[str] = frozenset({REASON_ROOT_STRUCTURAL_ONLY})

#: Reasons that appear on the chain rather than on any single row.
CHAIN_REASONS: frozenset[str] = frozenset({REASON_CHAIN_INVALID, REASON_NO_SCOPE_RECORDED})

#: Every reason string this module can emit.
VERDICT_REASONS: frozenset[str] = FAIL_REASONS | UNPROVEN_REASONS | PASS_REASONS | CHAIN_REASONS

#: Observations recorded alongside a verdict without changing it.
DIAGNOSTIC_SCOPE_REF_UNRESOLVED: str = "scope_ref_unresolved_inline_governs"
DIAGNOSTIC_SCOPE_REF_ONLY_RESOLVED: str = "scope_ref_only_resolved"
VERDICT_DIAGNOSTICS: frozenset[str] = frozenset({DIAGNOSTIC_SCOPE_REF_ONLY_RESOLVED, DIAGNOSTIC_SCOPE_REF_UNRESOLVED})

#: Keys :meth:`DelegationScope.from_body` interprets. A body carrying anything
#: else is not something the comparator can reason about: the unreadable key
#: records ``comparison_axis_unsupported``. Readable axes are still compared,
#: and a widening found on one of them fails the hop, fail dominating unproven.
SCOPE_BODY_KEYS: frozenset[str] = frozenset(
    {
        "duties",
        "max_depth",
        "max_uses",
        "not_after",
        "path_prefixes",
        "permissions",
        "task_ids",
    }
)


@dataclass(frozen=True)
class HopVerdict:
    """One graded row: what this hop established, and what it did not.

    ``is_root`` marks the hop with no parent to narrow against. A root is
    verified structurally and reported as root, never as a narrowing pass.
    ``axes`` names the scope axes the row is about, whether they were widened
    or could not be interpreted.
    """

    hop_index: int
    verdict: str
    is_root: bool = False
    reasons: tuple[str, ...] = ()
    axes: tuple[str, ...] = ()
    parent_hop_index: int | None = None
    ancestor_hop_index: int | None = None
    diagnostics: tuple[str, ...] = ()
    principal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for CLI / machine consumption."""
        return {
            "ancestor_hop_index": self.ancestor_hop_index,
            "axes": list(self.axes),
            "diagnostics": list(self.diagnostics),
            "hop_index": self.hop_index,
            "is_root": self.is_root,
            "parent_hop_index": self.parent_hop_index,
            "principal": self.principal,
            "reasons": list(self.reasons),
            "verdict": self.verdict,
        }

    def __str__(self) -> str:
        parts = [f"hop {self.hop_index}: {self.verdict}"]
        if self.is_root:
            parts.append("(root)")
        if self.reasons:
            parts.append(" ".join(self.reasons))
        if self.axes:
            parts.append("axes: " + ", ".join(self.axes))
        if self.diagnostics:
            parts.append("diagnostics: " + ", ".join(self.diagnostics))
        return " ".join(parts)


@dataclass(frozen=True)
class ChainVerdict:
    """Graded outcome over one run's receipts, one row per hop.

    ``verdict`` composes the rows: fail dominates, otherwise any unproven row
    makes the chain unproven, otherwise pass. ``unproven_hops`` is carried at
    the top level so a ten-hop chain with nine unproven hops cannot present as
    green, and the summary never replaces the rows.

    What a pass does not establish: that runtime enforcement matched the
    recorded scope, including consumption state such as remaining uses; that
    any grant was appropriate policy; that the supplied receipt set is
    complete, or that no alternate delegation path exists; that an unresolved
    reference would have matched; anything about execution outcomes. unproven
    is not valid, and pass is the only positive claim.
    """

    verdict: str = VERDICT_UNPROVEN
    hops: tuple[HopVerdict, ...] = ()
    unproven_hops: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True only for a full pass. Unproven is not valid."""
        return self.verdict == VERDICT_PASS

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "hops": [h.to_dict() for h in self.hops],
            "ok": self.ok,
            "reasons": list(self.reasons),
            "unproven_hops": self.unproven_hops,
            "verdict": self.verdict,
        }


def grade_chain(
    receipts: Sequence[DelegationReceipt],
    *,
    scope_resolver: Callable[[str], DelegationScope | None] | None = None,
    genesis: str = "0" * 64,
    chain_ok: bool = True,
) -> ChainVerdict:
    """Grade every hop pass / fail / unproven and compose one chain verdict.

    This is an additive surface. It records no :class:`ScopeViolation` and does
    not feed :class:`AuthorityReport`, so ``ChainResult.valid`` keeps the
    structural meaning it already had. What it adds is the distinction ``valid``
    cannot draw: a chain whose narrowing was checked and held reads differently
    from a chain that recorded no scope to check.

    Every hop is evaluated and nothing short-circuits, so a widening late in
    the chain is still found when an earlier hop is unproven.

    What a pass does not establish: that runtime enforcement matched the
    recorded scope, including consumption state such as remaining uses; that
    any grant was appropriate policy; that the supplied receipt set is
    complete, or that no alternate delegation path exists; that an unresolved
    reference would have matched; anything about execution outcomes. unproven
    is not valid, and pass is the only positive claim.

    Args:
        receipts: The run's receipts in ledger order.
        scope_resolver: Optional lookup from ``scope_ref`` to a scope body.
        genesis: Linkage anchor marking a chain root.
        chain_ok: The tamper-evidence verdict. False makes the chain fail,
            since a chain that does not verify establishes nothing about
            narrowing either.

    Returns:
        A :class:`ChainVerdict` carrying one :class:`HopVerdict` per receipt.
    """
    by_hmac = {r.hmac: i for i, r in enumerate(receipts) if r.hmac}
    scopes = [_resolve_scope(r, scope_resolver) for r in receipts]
    rows = tuple(
        _grade_hop(
            receipts,
            index,
            scopes,
            by_hmac,
            scope_resolver=scope_resolver,
            genesis=genesis,
        )
        for index in range(len(receipts))
    )
    return _compose_chain(
        rows,
        any_scope=any(s is not None for s in scopes),
        chain_ok=chain_ok,
    )


def _grade_hop(
    receipts: Sequence[DelegationReceipt],
    index: int,
    scopes: list[DelegationScope | None],
    by_hmac: dict[str, int],
    *,
    scope_resolver: Callable[[str], DelegationScope | None] | None,
    genesis: str,
) -> HopVerdict:
    """Grade one hop without consulting any other hop's verdict."""
    receipt = receipts[index]
    scope = scopes[index]
    reasons: list[str] = []
    axes: set[str] = set()
    ancestor_hop_index: int | None = None

    resolved = _parent_index(receipts, index, by_hmac, genesis)
    parent = resolved if isinstance(resolved, int) else None
    if isinstance(resolved, str):
        reasons.append(REASON_PARENT_RECEIPT_UNAVAILABLE)

    # Root status is positional, never self-declared. ``_parent_index`` reports
    # "no parent" for any hop whose ``parent_ref`` is the genesis anchor, and
    # ``parent_ref`` is written by the same party that writes the scope. Reading
    # that as root would let a hop opt out of the comparison by naming genesis:
    # the ceiling it widened would go unexamined and the row would read pass.
    # Only a hop with nothing before it in the supplied set is a root. A hop
    # that claims otherwise mid-chain may be a second tree's root or may be
    # evading its ceiling, and the receipts cannot tell those apart, so it is
    # unproven.
    is_root = resolved is None and index == 0
    if resolved is None and index > 0:
        reasons.append(REASON_ROOT_CLAIMED_MID_CHAIN)
    parent_hop_index = None if parent is None else receipts[parent].hop_index

    ref_reasons, diagnostics = _cross_check_scope_ref(receipt, scope_resolver)
    reasons.extend(ref_reasons)

    if scope is None:
        reasons.append(REASON_SCOPE_REF_ONLY_UNRESOLVED if receipt.scope_ref else REASON_SCOPE_MISSING)
    elif receipt.scope is not None:
        axes.update(set(receipt.scope) - SCOPE_BODY_KEYS)
        if axes:
            reasons.append(REASON_COMPARISON_AXIS_UNSUPPORTED)

    if is_root:
        if scope is not None:
            reasons.append(REASON_ROOT_STRUCTURAL_ONLY)
    elif scope is not None and parent is not None:
        ceiling = _nearest_scoped_ancestor(receipts, scopes, parent, by_hmac, genesis)
        ceiling_scope = None if ceiling is None else scopes[ceiling]
        if ceiling is None or ceiling_scope is None:
            reasons.append(REASON_PARENT_SCOPE_UNAVAILABLE)
        else:
            widened = narrowing_violations(scope, ceiling_scope)
            if widened:
                across_gap = ceiling != parent
                reasons.append(REASON_AXIS_WIDENED_VS_ANCESTOR if across_gap else REASON_AXIS_WIDENED)
                axes.update(widened)
                if across_gap:
                    ancestor_hop_index = receipts[ceiling].hop_index

    return HopVerdict(
        hop_index=receipt.hop_index,
        verdict=_hop_verdict(reasons),
        is_root=is_root,
        reasons=tuple(sorted(set(reasons))),
        axes=tuple(sorted(axes)),
        parent_hop_index=parent_hop_index,
        ancestor_hop_index=ancestor_hop_index,
        diagnostics=tuple(sorted(set(diagnostics))),
        principal=receipt.subject,
    )


def _cross_check_scope_ref(
    receipt: DelegationReceipt,
    scope_resolver: Callable[[str], DelegationScope | None] | None,
) -> tuple[list[str], list[str]]:
    """Adjudicate ``scope`` against ``scope_ref``; return (reasons, diagnostics).

    A recorded reference that is not the content address of the inline scope is
    a contradiction inside one signed body, and so is a reference that resolves
    to a different scope than the body carries. The signer held the key in both
    cases, which makes this an integrity defect rather than missing evidence.
    An unresolved reference alongside an inline scope is only a diagnostic: the
    inline value governs the comparison.
    """
    if not receipt.scope_ref:
        return [], []
    if receipt.scope is None:
        if scope_resolver is not None and scope_resolver(receipt.scope_ref) is not None:
            return [], [DIAGNOSTIC_SCOPE_REF_ONLY_RESOLVED]
        return [], []

    inline = DelegationScope.from_body(receipt.scope)
    if inline.scope_ref() != receipt.scope_ref:
        return [REASON_SCOPE_REF_CONFLICT], []
    if scope_resolver is None:
        return [], [DIAGNOSTIC_SCOPE_REF_UNRESOLVED]
    referenced = scope_resolver(receipt.scope_ref)
    if referenced is None:
        return [], [DIAGNOSTIC_SCOPE_REF_UNRESOLVED]
    if referenced != inline:
        return [REASON_SCOPE_REF_CONFLICT], []
    return [], []


def _nearest_scoped_ancestor(
    receipts: Sequence[DelegationReceipt],
    scopes: list[DelegationScope | None],
    start: int,
    by_hmac: dict[str, int],
    genesis: str,
) -> int | None:
    """Walk parent links from ``start`` upward to the first hop with a scope.

    Subset-of-ancestor is a necessary condition under transitive narrowing, so
    a widening found across an unscoped gap is evidence about the far side of
    the gap and not merely about the missing link.
    """
    seen: set[int] = set()
    cursor: int | None = start
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        if scopes[cursor] is not None:
            return cursor
        step = _parent_index(receipts, cursor, by_hmac, genesis)
        cursor = step if isinstance(step, int) else None
    return None


def _hop_verdict(reasons: list[str]) -> str:
    """Reduce one hop's reasons to a verdict. Fail dominates unproven."""
    if any(r in FAIL_REASONS for r in reasons):
        return VERDICT_FAIL
    if any(r in UNPROVEN_REASONS for r in reasons):
        return VERDICT_UNPROVEN
    return VERDICT_PASS


def _compose_chain(
    rows: tuple[HopVerdict, ...],
    *,
    any_scope: bool,
    chain_ok: bool,
) -> ChainVerdict:
    """Compose rows into one chain verdict without discarding any row."""
    reasons: list[str] = []
    unproven = sum(1 for row in rows if row.verdict == VERDICT_UNPROVEN)

    if not chain_ok:
        reasons.append(REASON_CHAIN_INVALID)
        verdict = VERDICT_FAIL
    elif any(row.verdict == VERDICT_FAIL for row in rows):
        verdict = VERDICT_FAIL
    elif unproven or not rows:
        verdict = VERDICT_UNPROVEN
    else:
        verdict = VERDICT_PASS

    if not any_scope:
        reasons.append(REASON_NO_SCOPE_RECORDED)

    return ChainVerdict(
        verdict=verdict,
        hops=rows,
        unproven_hops=unproven,
        reasons=tuple(sorted(set(reasons))),
    )
