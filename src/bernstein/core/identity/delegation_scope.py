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
    "CHECK_BINDING",
    "CHECK_DUTIES",
    "CHECK_NARROWING",
    "DEFAULT_SEPARATED_DUTIES",
    "DUTY_APPROVE",
    "DUTY_MERGE",
    "DUTY_SPAWN",
    "GATED_DUTIES",
    "AuthorityReport",
    "DecisionBinding",
    "DelegationScope",
    "ScopeViolation",
    "check_binding",
    "check_duties",
    "check_narrowing",
    "duties_for_act",
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
