"""An empty allowlist authorizes nothing (issue #2952, step 1).

The neighbouring convention is the opposite: ``AgentIdentityCard.in_scope``
treats an empty scope as *unrestricted*. An engagement scope grant must
fail closed, so these tests pin the empty-allowlist rule first and then
walk every remaining deny path of :func:`check_scope`.
"""

from __future__ import annotations

import pytest

from bernstein.core.security.engagement.mandate import (
    EngagementMandate,
    ScopeDenyReason,
    check_scope,
)

KEY = b"engagement-hmac-key"
OTHER_KEY = b"a-different-hmac-key"

NOT_BEFORE = 1_000
NOT_AFTER = 2_000
NOW = 1_500


def _mandate(
    *,
    targets: tuple[str, ...] = ("repo/app", "10.0.0.1"),
    categories: tuple[str, ...] = ("sast", "sca"),
    not_before: int = NOT_BEFORE,
    not_after: int = NOT_AFTER,
    rate_per_min: int = 60,
) -> EngagementMandate:
    return EngagementMandate(
        engagement_id="eng-1",
        targets=targets,
        categories=categories,
        not_before=not_before,
        not_after=not_after,
        rate_per_min=rate_per_min,
    ).sign(KEY)


def test_empty_target_allowlist_authorizes_nothing() -> None:
    """A signed mandate with no targets denies every target."""
    mandate = _mandate(targets=())

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.TARGET_NOT_IN_SCOPE


def test_empty_category_allowlist_authorizes_nothing() -> None:
    """A signed mandate with no categories denies every category."""
    mandate = _mandate(categories=())

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.CATEGORY_NOT_IN_SCOPE


def test_unsigned_mandate_denies() -> None:
    """An unsigned mandate authorizes nothing."""
    mandate = EngagementMandate(
        engagement_id="eng-1",
        targets=("repo/app",),
        categories=("sast",),
        not_before=NOT_BEFORE,
        not_after=NOT_AFTER,
        rate_per_min=60,
    )

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.BAD_SIGNATURE


def test_signature_from_another_key_denies() -> None:
    """A mandate signed under a different key authorizes nothing."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=OTHER_KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.BAD_SIGNATURE


def test_tampered_target_list_denies() -> None:
    """Widening the target list after signing invalidates the signature."""
    signed = _mandate(targets=("repo/app",))
    widened = EngagementMandate(
        engagement_id=signed.engagement_id,
        targets=("repo/app", "repo/other"),
        categories=signed.categories,
        not_before=signed.not_before,
        not_after=signed.not_after,
        rate_per_min=signed.rate_per_min,
        signature=signed.signature,
    )

    decision = check_scope(widened, target="repo/other", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.BAD_SIGNATURE


def test_action_before_window_open_denies() -> None:
    """``now`` earlier than ``not_before`` authorizes nothing."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOT_BEFORE - 1)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.NOT_YET_VALID


def test_action_after_window_close_denies() -> None:
    """``now`` later than ``not_after`` authorizes nothing."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOT_AFTER + 1)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.EXPIRED


def test_window_bounds_are_inclusive() -> None:
    """Both window edges are inside the grant."""
    mandate = _mandate()

    for now in (NOT_BEFORE, NOT_AFTER):
        decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=now)
        assert decision.allowed is True, now


def test_target_outside_allowlist_denies() -> None:
    """A target absent from the allowlist authorizes nothing."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/unlisted", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.TARGET_NOT_IN_SCOPE


def test_target_match_is_exact_not_prefix() -> None:
    """Membership is exact: a longer path is not covered by a listed prefix."""
    mandate = _mandate(targets=("repo/app",))

    decision = check_scope(mandate, target="repo/app-staging", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.TARGET_NOT_IN_SCOPE


def test_category_outside_allowlist_denies() -> None:
    """A category absent from the allowlist authorizes nothing."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/app", category="dast", hmac_key=KEY, now=NOW)

    assert decision.allowed is False
    assert decision.reason is ScopeDenyReason.CATEGORY_NOT_IN_SCOPE


def test_in_scope_action_is_allowed_and_carries_mandate_hash() -> None:
    """An in-scope action allows and reports the hash callers bind into lineage."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOW)

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.mandate_hash == mandate.mandate_hash()
    assert decision.target == "repo/app"
    assert decision.category == "sast"


def test_denied_decision_still_carries_mandate_hash() -> None:
    """A deny names the grant it was evaluated against."""
    mandate = _mandate()

    decision = check_scope(mandate, target="repo/unlisted", category="sast", hmac_key=KEY, now=NOW)

    assert decision.mandate_hash == mandate.mandate_hash()


def test_check_scope_never_raises_on_any_deny_path() -> None:
    """Every failure is a returned deny, never an exception."""
    unsigned = EngagementMandate(
        engagement_id="eng-1",
        targets=(),
        categories=(),
        not_before=NOT_AFTER,
        not_after=NOT_BEFORE,
        rate_per_min=0,
    )

    decision = check_scope(unsigned, target="", category="", hmac_key=b"", now=0)

    assert decision.allowed is False
    assert decision.reason in set(ScopeDenyReason)


def test_identical_field_values_produce_identical_mandate_hash() -> None:
    """Two independent constructions of the same grant are the same content."""
    first = EngagementMandate(
        engagement_id="eng-1",
        targets=("10.0.0.1", "repo/app"),
        categories=("sast", "sca"),
        not_before=NOT_BEFORE,
        not_after=NOT_AFTER,
        rate_per_min=60,
    ).sign(KEY)
    second = EngagementMandate(
        engagement_id="eng-1",
        targets=("repo/app", "10.0.0.1", "repo/app"),
        categories=("sca", "sast"),
        not_before=NOT_BEFORE,
        not_after=NOT_AFTER,
        rate_per_min=60,
    ).sign(KEY)

    assert first.mandate_hash() == second.mandate_hash()
    assert first.mandate_hash().startswith("sha256:")


def test_rate_per_min_is_signed_but_not_enforced() -> None:
    """``rate_per_min`` is bound into the grant yet gates nothing here."""
    slow = _mandate(rate_per_min=1)
    fast = _mandate(rate_per_min=600)

    assert slow.mandate_hash() != fast.mandate_hash()
    for mandate in (slow, fast):
        assert check_scope(mandate, target="repo/app", category="sast", hmac_key=KEY, now=NOW).allowed is True


def test_round_trip_through_dict_preserves_the_hash() -> None:
    """A serialized grant rebuilds to the identical content address."""
    mandate = _mandate()

    restored = EngagementMandate.from_dict(mandate.to_dict())

    assert restored.to_dict() == mandate.to_dict()
    assert restored.mandate_hash() == mandate.mandate_hash()
    assert restored.verify_signature(KEY) is True


@pytest.mark.parametrize("reason", list(ScopeDenyReason))
def test_deny_reasons_are_a_closed_string_enum(reason: ScopeDenyReason) -> None:
    """Reasons serialize as plain strings for receipts and lineage rows."""
    assert isinstance(reason.value, str)
    assert ScopeDenyReason(reason.value) is reason
