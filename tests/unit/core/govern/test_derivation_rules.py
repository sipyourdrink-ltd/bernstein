"""Issue #5121: derived classification facts are declared, validated, and attributed.

Site, owning team, contact and per-target endpoints were facts nothing derived.
The nearest precedent, ``Playbook.from_dict``, loads typed rules from data but
validates nothing beyond `str()` casts -- and a malformed derivation rule
silently producing wrong ownership is worse than one that fails to load: the
first is discovered three weeks later by whoever was paged instead of the real
owner.
"""

from __future__ import annotations

import pytest

from bernstein.core.govern.derivation import (
    UNKNOWN,
    DerivationRuleError,
    DerivationRules,
    RuleKind,
)

RULES = {
    "rules": [
        {"kind": "prefix_site", "match": "10.0.0.0/8", "value": "dc-main"},
        {"kind": "prefix_site", "match": "10.1.2.0/24", "value": "dc-lab"},
        {"kind": "owner_contact", "match": "platform", "value": "platform@example.test"},
        {"kind": "site_endpoints", "match": "dc-lab", "value": "https://lab.example.test"},
    ]
}


def _rules() -> DerivationRules:
    return DerivationRules.from_dict(RULES)


# ---------------------------------------------------------------------------
# Load-time validation
# ---------------------------------------------------------------------------


def test_a_well_formed_document_loads() -> None:
    assert len(_rules().rules) == 4


def test_an_unknown_key_is_rejected_not_dropped() -> None:
    """Silently dropping it would load a rule that is not the one written."""
    with pytest.raises(DerivationRuleError, match="unknown key"):
        DerivationRules.from_dict(
            {"rules": [{"kind": "owner_contact", "match": "a", "value": "b", "contct": "typo"}]}
        )


def test_an_unknown_key_on_the_document_is_rejected() -> None:
    with pytest.raises(DerivationRuleError, match="unknown key"):
        DerivationRules.from_dict({"rules": [], "rulez": []})


@pytest.mark.parametrize("missing", ["kind", "match", "value"])
def test_a_missing_or_empty_field_is_rejected(missing: str) -> None:
    rule = {"kind": "owner_contact", "match": "a", "value": "b"}
    rule[missing] = "   "
    with pytest.raises(DerivationRuleError, match=missing):
        DerivationRules.from_dict({"rules": [rule]})


def test_an_unknown_kind_is_rejected_and_names_the_known_ones() -> None:
    """A kind that loads is a derivation that silently never fires."""
    with pytest.raises(DerivationRuleError, match="unknown kind") as excinfo:
        DerivationRules.from_dict({"rules": [{"kind": "hostname_owner", "match": "a", "value": "b"}]})
    for kind in RuleKind:
        assert kind.value in str(excinfo.value)


def test_a_prefix_that_is_not_a_network_is_rejected() -> None:
    """It could never match, so accepting it declares a rule guaranteed not to fire."""
    with pytest.raises(DerivationRuleError, match="not a valid network"):
        DerivationRules.from_dict({"rules": [{"kind": "prefix_site", "match": "10.0.0.*", "value": "x"}]})


def test_two_rules_of_one_kind_cannot_claim_the_same_match() -> None:
    """Not a merge -- two answers to one question with nothing to choose between."""
    with pytest.raises(DerivationRuleError, match="same match"):
        DerivationRules.from_dict(
            {
                "rules": [
                    {"kind": "owner_contact", "match": "platform", "value": "a@x"},
                    {"kind": "owner_contact", "match": "platform", "value": "b@x"},
                ]
            }
        )


def test_the_same_match_under_different_kinds_is_fine() -> None:
    """They are different questions; a site and an owner may share a name."""
    rules = DerivationRules.from_dict(
        {
            "rules": [
                {"kind": "owner_contact", "match": "lab", "value": "lab@x"},
                {"kind": "site_endpoints", "match": "lab", "value": "https://lab"},
            ]
        }
    )
    assert len(rules.rules) == 2


def test_a_rules_list_that_is_not_a_list_is_rejected() -> None:
    with pytest.raises(DerivationRuleError, match="must be a list"):
        DerivationRules.from_dict({"rules": {"kind": "owner_contact"}})


# ---------------------------------------------------------------------------
# No ownership from a name pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["team-*", "web-?", "host[0-9]", "regex:^svc-", "glob:svc-*"])
def test_ownership_by_name_pattern_is_refused(pattern: str) -> None:
    """Wrong the first time a host is named after the wrong team, and wrong SILENTLY."""
    with pytest.raises(DerivationRuleError, match="name pattern"):
        DerivationRules.from_dict({"rules": [{"kind": "owner_contact", "match": pattern, "value": "x@y"}]})


def test_the_refusal_says_why() -> None:
    with pytest.raises(DerivationRuleError) as excinfo:
        DerivationRules.from_dict({"rules": [{"kind": "owner_contact", "match": "svc-*", "value": "x@y"}]})
    assert "declared per owner" in str(excinfo.value)


def test_a_literal_owner_name_is_still_accepted() -> None:
    """The control: the refusal must not swallow ordinary team names."""
    rules = DerivationRules.from_dict(
        {"rules": [{"kind": "owner_contact", "match": "platform-infra", "value": "x@y"}]}
    )
    assert rules.contact_for_owner("platform-infra").value == "x@y"


# ---------------------------------------------------------------------------
# Derivation, and provenance
# ---------------------------------------------------------------------------


def test_the_most_specific_prefix_wins_not_the_first_declared() -> None:
    """Picking by declaration order would make the answer depend on file layout."""
    rules = _rules()
    assert rules.site_for_address("10.1.2.7").value == "dc-lab"
    assert rules.site_for_address("10.9.9.9").value == "dc-main"


def test_every_derived_fact_names_the_rule_that_produced_it() -> None:
    """Reading one field answers "why is this the answer"."""
    rules = _rules()
    assert rules.site_for_address("10.1.2.7").rule_id == "prefix_site:10.1.2.0/24"
    assert rules.contact_for_owner("platform").rule_id == "owner_contact:platform"
    assert rules.endpoints_for_site("dc-lab").rule_id == "site_endpoints:dc-lab"


def test_a_declared_rule_id_is_carried_instead_of_the_derived_one() -> None:
    rules = DerivationRules.from_dict(
        {"rules": [{"kind": "owner_contact", "match": "a", "value": "b", "rule_id": "OWN-7"}]}
    )
    assert rules.contact_for_owner("a").rule_id == "OWN-7"


# ---------------------------------------------------------------------------
# Unknown is a value
# ---------------------------------------------------------------------------


def test_an_unmatched_target_is_unknown_rather_than_absent() -> None:
    """A missing fact and one nobody could derive read alike if the row is not there."""
    rules = _rules()
    fact = rules.site_for_address("192.0.2.1")
    assert fact.value == UNKNOWN
    assert fact.rule_id == ""
    assert fact.is_known is False


def test_an_unparseable_address_is_unknown_rather_than_an_exception() -> None:
    """One unreadable target must not take the whole report down."""
    assert _rules().site_for_address("not-an-address").value == UNKNOWN


def test_an_ipv6_address_does_not_match_an_ipv4_prefix() -> None:
    """Comparing across versions raises inside ipaddress; it must read as no match."""
    assert _rules().site_for_address("2001:db8::1").value == UNKNOWN


def test_a_known_fact_reports_itself_as_known() -> None:
    assert _rules().contact_for_owner("platform").is_known is True


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_rules_round_trip() -> None:
    rules = _rules()
    assert DerivationRules.from_dict(rules.to_dict()) == rules


def test_a_round_trip_preserves_the_derived_rule_ids() -> None:
    """The provenance has to survive a save, or an explain loses its source."""
    reloaded = DerivationRules.from_dict(_rules().to_dict())
    assert reloaded.site_for_address("10.1.2.7").rule_id == "prefix_site:10.1.2.0/24"
