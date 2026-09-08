"""Declared derivation rules, and the facts they produce (issue #5121).

Site, owning team, contact and per-target endpoints are facts nothing derived.
This module makes them data: a rule file states ``network prefix -> site``,
``owner -> contact`` and ``site -> endpoints``, and every fact a rule produces
records which rule produced it.

Three properties, and each exists because of a specific way this goes wrong.

**Rules are validated at load, not at use.** A malformed rule silently producing
wrong ownership is worse than one that fails to load: the first is discovered
three weeks later by whoever was paged instead of the real owner. So an unknown
key is rejected rather than dropped, and an empty field is rejected rather than
carried as ``""``. Same posture :class:`~bernstein.core.govern.playbook_models.Playbook`
takes to its clauses, applied here at load time as well.

**No rule derives ownership from a NAME PATTERN.** It is the one derivation
shortcut that looks convenient and is wrong the first time a host is named after
the wrong team -- and it is wrong silently, because a plausible answer is
indistinguishable from a correct one. A rule that tries is refused at load with
the reason, not accepted and then trusted.

**A target no rule matches is ``unknown``, not absent.** A missing fact and a
fact nobody could derive read alike if the row simply is not there, and only one
of them is a gap somebody should close. ``unknown`` is a value, so it shows up in
a gap query.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: The value a fact takes when no declared rule matched the target. A value, not
#: an absence: a gap query can only find what is written down.
UNKNOWN = "unknown"


class RuleKind(StrEnum):
    """The derivations this module knows how to declare.

    A closed set. A rule naming anything else is a typo, not a looser schema --
    and a typo that loads is a derivation that silently never fires.
    """

    #: A network prefix (CIDR) to the site the addresses in it belong to.
    PREFIX_SITE = "prefix_site"
    #: An owning team to the contact for it.
    OWNER_CONTACT = "owner_contact"
    #: A site to the endpoints reachable for targets at it.
    SITE_ENDPOINTS = "site_endpoints"


#: Keys a rule dict may carry. Declared once, so `from_dict` can tell a key the
#: schema does not know from one it merely left unset.
_RULE_KEYS = frozenset({"kind", "match", "value", "rule_id"})

#: Substrings that betray a name-pattern ownership rule. Matched on the MATCH
#: side of an owner rule, which is the only place the shortcut can hide: a
#: prefix rule matching on an address is exact by construction.
_GLOB_MARKERS = ("*", "?", "[", "regex:", "glob:")


class DerivationRuleError(ValueError):
    """Raised when a rule file cannot be loaded as declared rules.

    A distinct type so a caller can tell "your rules are wrong" from every other
    ``ValueError`` a load might raise, and report it at startup as the
    configuration error it is.
    """


@dataclass(frozen=True, slots=True)
class DerivationRule:
    """One declared derivation.

    Attributes:
        kind: Which derivation this rule performs.
        match: The left-hand side -- a CIDR for ``prefix_site``, an owning team
            for ``owner_contact``, a site for ``site_endpoints``.
        value: The fact the rule produces.
        rule_id: Stable identifier, carried onto every fact this rule derives so
            an explain names its source. Defaults to ``<kind>:<match>``, which is
            unique because two rules of one kind cannot share a match.
    """

    kind: RuleKind
    match: str
    value: str
    rule_id: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "kind": self.kind.value,
            "match": self.match,
            "value": self.value,
            "rule_id": self.rule_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DerivationRule:
        """Rebuild a rule from a serialized dict, or refuse it.

        Raises:
            DerivationRuleError: The record carries an unknown key, omits or
                empties a required field, names a kind outside
                :class:`RuleKind`, declares a network prefix that is not a valid
                CIDR, or tries to match an owner by name pattern.
        """
        unknown = set(raw) - _RULE_KEYS
        if unknown:
            raise DerivationRuleError(f"derivation rule has unknown key(s): {sorted(unknown)}")
        for name in ("kind", "match", "value"):
            if not str(raw.get(name, "")).strip():
                raise DerivationRuleError(f"derivation rule is missing required field {name!r}")

        raw_kind = str(raw["kind"])
        try:
            kind = RuleKind(raw_kind)
        except ValueError as exc:
            known = ", ".join(sorted(member.value for member in RuleKind))
            raise DerivationRuleError(f"derivation rule has unknown kind {raw_kind!r}; known: {known}") from exc

        match = str(raw["match"]).strip()
        _reject_name_pattern_ownership(kind, match)
        if RuleKind.PREFIX_SITE is kind:
            _require_cidr(match)

        rule_id = str(raw.get("rule_id", "")).strip() or f"{kind.value}:{match}"
        return cls(kind=kind, match=match, value=str(raw["value"]).strip(), rule_id=rule_id)


def _reject_name_pattern_ownership(kind: RuleKind, match: str) -> None:
    """Refuse an ownership rule that matches on a name pattern.

    Wrong the first time a host is named after the wrong team, and wrong
    SILENTLY -- a plausible owner is indistinguishable from a correct one to
    everybody except the person who gets paged.
    """
    if RuleKind.OWNER_CONTACT is not kind:
        return
    for marker in _GLOB_MARKERS:
        if marker in match:
            raise DerivationRuleError(
                f"ownership rule matches on a name pattern ({match!r}); ownership must be declared "
                "per owner, not inferred from a name -- a host named after the wrong team would be "
                "attributed to it silently"
            )


def _require_cidr(match: str) -> None:
    """Refuse a prefix rule whose match is not a network.

    A prefix that does not parse can never match anything, so accepting it
    declares a derivation that is guaranteed never to fire -- the failure this
    validation exists to make loud.
    """
    try:
        ipaddress.ip_network(match, strict=False)
    except ValueError as exc:
        raise DerivationRuleError(f"prefix rule match {match!r} is not a valid network: {exc}") from exc


@dataclass(frozen=True, slots=True)
class DerivedFact:
    """One fact, and the rule that produced it.

    Attributes:
        value: The derived value, or :data:`UNKNOWN`.
        rule_id: The rule that fired, or ``""`` when none did. Reading one field
            answers "why is this the answer", instead of re-deriving by hand to
            guess which rule matched.
    """

    value: str
    rule_id: str = ""

    @property
    def is_known(self) -> bool:
        """Whether a rule actually produced this value."""
        return self.value != UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {"value": self.value, "rule_id": self.rule_id}


#: The fact a target gets when nothing matched. One shared instance: it carries
#: no per-target state, and an identical object makes "nothing derived this"
#: comparable by value.
NO_FACT = DerivedFact(value=UNKNOWN, rule_id="")


@dataclass(frozen=True, slots=True)
class DerivationRules:
    """A validated, ordered set of declared rules.

    Attributes:
        rules: The rules, in declared order. Order is the tie-break for prefix
            matching only -- see :meth:`site_for_address`.
    """

    rules: tuple[DerivationRule, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DerivationRules:
        """Load and validate rules from a serialized document.

        Raises:
            DerivationRuleError: Any rule is malformed, or two rules of the same
                kind declare the same match -- which is not a merge, it is two
                answers to one question with nothing to choose between them.
        """
        unknown = set(raw) - {"rules"}
        if unknown:
            raise DerivationRuleError(f"derivation rules document has unknown key(s): {sorted(unknown)}")
        entries = raw.get("rules", [])
        if not isinstance(entries, list):
            raise DerivationRuleError("derivation rules document's 'rules' must be a list")
        rules = tuple(DerivationRule.from_dict(entry) for entry in entries)

        seen: set[tuple[str, str]] = set()
        for rule in rules:
            key = (rule.kind.value, rule.match)
            if key in seen:
                raise DerivationRuleError(
                    f"two {rule.kind.value} rules declare the same match {rule.match!r}; "
                    "one question cannot have two declared answers"
                )
            seen.add(key)
        return cls(rules=rules)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {"rules": [rule.to_dict() for rule in self.rules]}

    def _of_kind(self, kind: RuleKind) -> tuple[DerivationRule, ...]:
        return tuple(rule for rule in self.rules if rule.kind is kind)

    def site_for_address(self, address: str) -> DerivedFact:
        """The site an address belongs to, by declared prefix.

        The MOST SPECIFIC prefix wins, not the first declared. A site declared
        for ``10.0.0.0/8`` and a different one for ``10.1.2.0/24`` are not in
        conflict -- the narrower one is the more precise statement, and picking
        by declaration order would make the answer depend on file layout.

        An address that does not parse derives :data:`UNKNOWN` rather than
        raising: this is a query about a target, and one unreadable target must
        not take the report down.
        """
        try:
            parsed = ipaddress.ip_address(address.strip())
        except ValueError:
            return NO_FACT
        best: DerivationRule | None = None
        best_bits = -1
        for rule in self._of_kind(RuleKind.PREFIX_SITE):
            network = ipaddress.ip_network(rule.match, strict=False)
            if parsed.version != network.version or parsed not in network:
                continue
            if network.prefixlen > best_bits:
                best, best_bits = rule, network.prefixlen
        return NO_FACT if best is None else DerivedFact(value=best.value, rule_id=best.rule_id)

    def contact_for_owner(self, owner: str) -> DerivedFact:
        """The contact declared for an owning team. Exact match, by construction."""
        return self._exact(RuleKind.OWNER_CONTACT, owner)

    def endpoints_for_site(self, site: str) -> DerivedFact:
        """The endpoints declared for a site. Exact match."""
        return self._exact(RuleKind.SITE_ENDPOINTS, site)

    def _exact(self, kind: RuleKind, match: str) -> DerivedFact:
        wanted = match.strip()
        for rule in self._of_kind(kind):
            if rule.match == wanted:
                return DerivedFact(value=rule.value, rule_id=rule.rule_id)
        return NO_FACT


__all__ = [
    "NO_FACT",
    "UNKNOWN",
    "DerivationRule",
    "DerivationRuleError",
    "DerivationRules",
    "DerivedFact",
    "RuleKind",
]
