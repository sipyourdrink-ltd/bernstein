"""Signed, content-addressed engagement scope grants (issue #2952, step 1).

An outbound payment cannot execute until it is bound to a signed
:class:`~bernstein.core.protocols.payments.mandates.IntentMandate`.
Security-tool actions have no equivalent: a scanner can be pointed at any
target with nothing in the record establishing that the run was authorized
and in-scope, and a finding discovered outside an agreed scope is a
liability rather than a result.

:class:`EngagementMandate` closes that gap with the same substrate the
payment mandates use. The grant is canonical JSON (sorted keys, minimal
separators, UTF-8) signed with HMAC-SHA256 under the audit-chain key, and
its identity is the ``sha256:`` content hash over body **plus** signature.
Two operators who construct the same grant compute the byte-identical
hash, so the hash a caller binds into a lineage entry recomputes offline
with no registry and no network.

Fail-closed allowlists
----------------------
``targets`` and ``categories`` are allowlists that authorize *only* what
they list. An empty allowlist authorizes **nothing**. This is deliberately
the opposite of the neighbouring
:meth:`~bernstein.core.identity.agent_card.AgentIdentityCard.in_scope`
convention, where an empty scope means *unrestricted*: a scope grant whose
fields silently widen when a field is omitted cannot be the foundation of a
compliance-sensitive deliverable.

Scope of this module
--------------------
:func:`check_scope` is a pure projection of ``(grant, target, category,
key, time)`` onto an allow/deny decision. It reads no clock, touches no
filesystem, and never raises: every failure is a returned
:class:`ScopeDecision` carrying a closed :class:`ScopeDenyReason`, so a
caller can seal the deny into a receipt instead of catching an exception.

``rate_per_min`` is signed into the grant so a later rate gate is bound by
the same signature, but nothing here enforces it:
:func:`~bernstein.core.admission.rate_limit.effective_rate_limit` needs
recorded 429 observation timestamps that no caller supplies yet.

Not to be confused with the projection stub
------------------------------------------
``bernstein.core.security.engagement_mandate.EngagementMandate`` is a
different, unrelated type: an unsigned, wall-clock, prefix-matching stub
that tags phase nodes during engagement projection. It carries no
signature and no content address, so it cannot bind an action into the
audit chain. This module is the signed substrate; the two are kept
separate rather than merged, because collapsing them would either strip
the projection stub's prefix matching or widen this grant with a
subsumption rule that has no monotonicity check behind it yet.

Revocation is not part of this projection's failure set. Consulting a
revocation log requires a location that a content-addressed body cannot
carry, so no unreachable ``revoked`` outcome ships here; the gate lands
with the append-only revocation log.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "ENGAGEMENT_SCHEMA_VERSION",
    "EngagementMandate",
    "ScopeDecision",
    "ScopeDenyReason",
    "check_scope",
]

#: Version stamped into every engagement-grant preimage. Bump only on a
#: wire-format change; it is covered by the signature and the content hash.
ENGAGEMENT_SCHEMA_VERSION = 1

#: Discriminator in the signed body, so an engagement grant can never be
#: mistaken for a payment mandate signed under the same key.
_ENGAGEMENT_KIND = "engagement"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _sign(key: bytes, payload: dict[str, Any]) -> str:
    """Return the HMAC-SHA256 signature over ``payload``'s canonical bytes."""
    return _hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()


class ScopeDenyReason(StrEnum):
    """Closed set of reasons :func:`check_scope` refuses an action.

    A string enum so a reason drops straight into a canonical-JSON refusal
    row without a conversion step.
    """

    #: The grant carries no signature, or one that does not verify.
    BAD_SIGNATURE = "bad_signature"
    #: ``now`` is earlier than the grant's ``not_before``.
    NOT_YET_VALID = "not_yet_valid"
    #: ``now`` is later than the grant's ``not_after``.
    EXPIRED = "expired"
    #: The target is absent from the ``targets`` allowlist (empty included).
    TARGET_NOT_IN_SCOPE = "target_not_in_scope"
    #: The category is absent from the ``categories`` allowlist (empty included).
    CATEGORY_NOT_IN_SCOPE = "category_not_in_scope"


@dataclass(frozen=True)
class ScopeDecision:
    """The outcome of evaluating one action against one grant.

    Attributes:
        allowed: True only when every gate passed.
        reason: ``None`` on an allow; the closed-enum refusal reason
            otherwise.
        mandate_hash: The content hash of the grant the action was
            evaluated against. Present on both outcomes: an allow binds it
            into the action's lineage entry, a deny names the grant the
            refusal was measured against.
        target: The target the caller asked about, echoed back.
        category: The tool category the caller asked about, echoed back.
    """

    allowed: bool
    reason: ScopeDenyReason | None
    mandate_hash: str
    target: str
    category: str


@dataclass(frozen=True)
class EngagementMandate:
    """A signed, content-addressed rules-of-engagement scope grant.

    Attributes:
        engagement_id: The engagement the grant belongs to.
        targets: Allowlist of targets (hosts, CIDR strings, domains, repo
            paths) the grant authorizes. Membership is **exact string
            equality**; an empty tuple authorizes nothing.
        categories: Allowlist of tool categories the grant authorizes
            (``sast``, ``sca``, ``secret``, ``iac``, ``recon``, ``dast``).
            An empty tuple authorizes nothing.
        not_before: Inclusive lower bound of the grant window.
        not_after: Inclusive upper bound of the grant window. There is no
            "unbounded" sentinel: a grant with no usable window authorizes
            nothing rather than everything.
        rate_per_min: Max requests per minute per target. Signed so a later
            rate gate is bound by this signature; not enforced here.
        signature: HMAC signature over the body; populated by :meth:`sign`.
    """

    engagement_id: str
    targets: tuple[str, ...]
    categories: tuple[str, ...]
    not_before: int
    not_after: int
    rate_per_min: int
    signature: str = ""

    def _body(self) -> dict[str, Any]:
        return {
            "v": ENGAGEMENT_SCHEMA_VERSION,
            "kind": _ENGAGEMENT_KIND,
            "engagement_id": self.engagement_id,
            "targets": sorted(set(self.targets)),
            "categories": sorted(set(self.categories)),
            "not_before": self.not_before,
            "not_after": self.not_after,
            "rate_per_min": self.rate_per_min,
        }

    def sign(self, key: bytes) -> EngagementMandate:
        """Return a copy carrying the HMAC signature over the body."""
        return EngagementMandate(
            engagement_id=self.engagement_id,
            targets=self.targets,
            categories=self.categories,
            not_before=self.not_before,
            not_after=self.not_after,
            rate_per_min=self.rate_per_min,
            signature=_sign(key, self._body()),
        )

    def verify_signature(self, key: bytes) -> bool:
        """Return True when ``signature`` matches the body under ``key``."""
        if not self.signature:
            return False
        return _hmac.compare_digest(self.signature, _sign(key, self._body()))

    def mandate_hash(self) -> str:
        """Return the content hash of the signed grant.

        The allowlists are sorted and de-duplicated in the body, so the
        hash is a function of the *sets* granted, not their listing order.
        """
        return _sha256(self._body() | {"signature": self.signature})

    def to_dict(self) -> dict[str, Any]:
        return self._body() | {"signature": self.signature}

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EngagementMandate:
        return cls(
            engagement_id=str(row["engagement_id"]),
            targets=tuple(str(t) for t in row.get("targets", ())),
            categories=tuple(str(c) for c in row.get("categories", ())),
            not_before=int(row["not_before"]),
            not_after=int(row["not_after"]),
            rate_per_min=int(row.get("rate_per_min", 0)),
            signature=str(row.get("signature", "")),
        )


def check_scope(
    mandate: EngagementMandate,
    *,
    target: str,
    category: str,
    hmac_key: bytes,
    now: int,
) -> ScopeDecision:
    """Project ``(grant, target, category, key, time)`` onto an allow/deny.

    The action is allowed only when all four gates pass: the signature
    verifies under ``hmac_key``, ``now`` lies inside the inclusive window
    ``[not_before, not_after]``, ``target`` is listed in ``targets``, and
    ``category`` is listed in ``categories``. Membership is exact string
    equality -- no prefix, CIDR, or domain subsumption -- because any
    subsuming matcher would widen a grant without a monotonicity rule to
    check the widening against.

    An **empty** ``targets`` or ``categories`` authorizes nothing. This is
    the opposite of :meth:`AgentIdentityCard.in_scope
    <bernstein.core.identity.agent_card.AgentIdentityCard.in_scope>`,
    where an empty scope means unrestricted.

    Pure and total: it reads no clock and no filesystem, and every failure
    is a returned :class:`ScopeDecision` with a
    :class:`ScopeDenyReason` -- it never raises. Two operators with the
    same arguments compute the identical decision.

    Args:
        mandate: The scope grant to evaluate against.
        target: The target the action would touch.
        category: The tool category the action belongs to.
        hmac_key: The audit-chain key the grant was signed under.
        now: Logical time to evaluate at; supplied by the caller so the
            projection stays deterministic.
    """
    mandate_hash = mandate.mandate_hash()

    def deny(reason: ScopeDenyReason) -> ScopeDecision:
        return ScopeDecision(
            allowed=False,
            reason=reason,
            mandate_hash=mandate_hash,
            target=target,
            category=category,
        )

    if not mandate.verify_signature(hmac_key):
        return deny(ScopeDenyReason.BAD_SIGNATURE)
    if now < mandate.not_before:
        return deny(ScopeDenyReason.NOT_YET_VALID)
    if now > mandate.not_after:
        return deny(ScopeDenyReason.EXPIRED)
    if target not in set(mandate.targets):
        return deny(ScopeDenyReason.TARGET_NOT_IN_SCOPE)
    if category not in set(mandate.categories):
        return deny(ScopeDenyReason.CATEGORY_NOT_IN_SCOPE)
    return ScopeDecision(
        allowed=True,
        reason=None,
        mandate_hash=mandate_hash,
        target=target,
        category=category,
    )
