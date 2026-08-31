from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bernstein.core.security.engagement_mandate import (
    EngagementMandate,
    MandateAction,
    MandateReceipt,
    check_mandate,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

TODAY = datetime(2026, 1, 15, tzinfo=UTC)
PAST = datetime(2026, 1, 1, tzinfo=UTC)
FUTURE = datetime(2026, 1, 31, tzinfo=UTC)


# ---------------------------------------------------------------------------
# MandateAction enum tests
# ---------------------------------------------------------------------------


class TestMandateAction:
    def test_enum_values_are_lowercase(self) -> None:
        assert MandateAction.RECON.value == "recon"
        assert MandateAction.ENUMERATE.value == "enumerate"
        assert MandateAction.SCAN.value == "scan"
        assert MandateAction.VERIFY.value == "verify"
        assert MandateAction.REPORT.value == "report"


# ---------------------------------------------------------------------------
# EngagementMandate tests
# ---------------------------------------------------------------------------


class TestEngagementMandate:
    def test_is_valid_at_true_when_within_window(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-1",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        assert mandate.is_valid_at(TODAY) is True

    def test_is_valid_at_true_at_boundaries(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-2",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        assert mandate.is_valid_at(PAST) is True
        assert mandate.is_valid_at(FUTURE) is True

    def test_is_valid_at_false_when_expired(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-3",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=TODAY,
        )
        assert mandate.is_valid_at(FUTURE) is False

    def test_is_valid_at_false_when_not_yet_valid(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-4",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=TODAY,
            valid_to=FUTURE,
        )
        assert mandate.is_valid_at(PAST) is False

    def test_is_valid_at_defaults_to_now_when_instant_is_none(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-5",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        result = mandate.is_valid_at()
        assert isinstance(result, bool)

    def test_permits_true_when_action_in_permitted_actions(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-6",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
            permitted_actions=(MandateAction.RECON, MandateAction.SCAN),
        )
        assert mandate.permits(MandateAction.RECON) is True
        assert mandate.permits(MandateAction.SCAN) is True

    def test_permits_false_when_action_not_in_permitted_actions(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-7",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
            permitted_actions=(MandateAction.RECON,),
        )
        assert mandate.permits(MandateAction.SCAN) is False

    def test_scope_contains_wildcard(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-8",
            scope="scope:*",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        assert mandate.scope_contains("scope:anything") is True
        assert mandate.scope_contains("scope:org/unit") is True

    def test_scope_contains_exact_match(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-9",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        assert mandate.scope_contains("scope:repo") is True

    def test_scope_contains_subscope(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-10",
            scope="scope:org",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        assert mandate.scope_contains("scope:org:unit-1") is True
        assert mandate.scope_contains("scope:org:unit-1:subunit") is True

    def test_scope_contains_negative(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-11",
            scope="scope:org",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
        )
        assert mandate.scope_contains("scope:different") is False
        assert mandate.scope_contains("scope:org2") is False

    def test_to_dict_serializes_correctly(self) -> None:
        mandate = EngagementMandate(
            mandate_id="test-12",
            scope="scope:repo",
            issued_by="test-resolver",
            valid_from=PAST,
            valid_to=FUTURE,
            permitted_actions=(MandateAction.RECON, MandateAction.SCAN),
        )
        result = mandate.to_dict()
        assert result == {
            "mandate_id": "test-12",
            "scope": "scope:repo",
            "issued_by": "test-resolver",
            "valid_from": PAST.isoformat(),
            "valid_to": FUTURE.isoformat(),
            "permitted_actions": ["recon", "scan"],
        }


# ---------------------------------------------------------------------------
# check_mandate tests
# ---------------------------------------------------------------------------


class TestCheckMandate:
    def test_expired_mandate_returns_admitted_false(self) -> None:
        mandate = EngagementMandate(
            mandate_id="expired-mandate",
            scope="scope:repo",
            issued_by="resolver",
            valid_from=PAST,
            valid_to=PAST,
        )
        receipt = check_mandate(mandate, "scope:repo", MandateAction.RECON)
        assert receipt.admitted is False
        assert receipt.refusal_reason == "Mandate not currently valid"

    def test_not_yet_valid_mandate_returns_admitted_false(self) -> None:
        mandate = EngagementMandate(
            mandate_id="future-mandate",
            scope="scope:repo",
            issued_by="resolver",
            valid_from=FUTURE,
            valid_to=FUTURE,
        )
        receipt = check_mandate(mandate, "scope:repo", MandateAction.RECON)
        assert receipt.admitted is False
        assert receipt.refusal_reason == "Mandate not currently valid"

    def test_scope_not_covered_returns_admitted_false(self) -> None:
        # Use a mandate that's valid NOW (current wall-clock time) so we can test scope check
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        valid_from = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=UTC)
        valid_to = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=UTC)

        mandate = EngagementMandate(
            mandate_id="scope-limited",
            scope="scope:org",
            issued_by="resolver",
            valid_from=valid_from,
            valid_to=valid_to,
        )
        receipt = check_mandate(mandate, "scope:other-repo", MandateAction.RECON)
        assert receipt.admitted is False
        assert "is not covered by mandate scope" in receipt.refusal_reason

    def test_action_not_permitted_returns_admitted_false(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        valid_from = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=UTC)
        valid_to = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=UTC)

        mandate = EngagementMandate(
            mandate_id="action-limited",
            scope="scope:repo",
            issued_by="resolver",
            valid_from=valid_from,
            valid_to=valid_to,
            permitted_actions=(MandateAction.RECON,),
        )
        receipt = check_mandate(mandate, "scope:repo", MandateAction.SCAN)
        assert receipt.admitted is False
        assert "is not permitted" in receipt.refusal_reason

    def test_all_checks_pass_returns_admitted_true(self) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        valid_from = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=UTC)
        valid_to = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=UTC)

        mandate = EngagementMandate(
            mandate_id="valid-mandate",
            scope="scope:repo",
            issued_by="resolver",
            valid_from=valid_from,
            valid_to=valid_to,
            permitted_actions=(MandateAction.RECON, MandateAction.SCAN),
        )
        receipt = check_mandate(mandate, "scope:repo", MandateAction.RECON)
        assert receipt.admitted is True
        assert receipt.refusal_reason == ""

    def test_at_instant_override_is_respected_by_is_valid_at(self) -> None:
        # Test that is_valid_at works with boundary instants
        mandate = EngagementMandate(
            mandate_id="valid-mandate",
            scope="scope:repo",
            issued_by="resolver",
            valid_from=PAST,
            valid_to=FUTURE,
            permitted_actions=(MandateAction.RECON,),
        )
        assert mandate.is_valid_at(TODAY) is True
        # PAST == valid_from, and boundary is inclusive, so True
        assert mandate.is_valid_at(PAST) is True
        # FUTURE == valid_to, and boundary is inclusive, so True
        assert mandate.is_valid_at(FUTURE) is True


# ---------------------------------------------------------------------------
# MandateReceipt tests
# ---------------------------------------------------------------------------


class TestMandateReceipt:
    def test_to_dict_serializes_correctly(self) -> None:
        receipt = MandateReceipt(
            mandate_id="test-receipt",
            requested_scope="scope:repo",
            requested_action=MandateAction.RECON,
            admitted=True,
            refusal_reason="",
            evaluated_at=TODAY,
        )
        result = receipt.to_dict()
        assert result == {
            "mandate_id": "test-receipt",
            "requested_scope": "scope:repo",
            "requested_action": "recon",
            "admitted": True,
            "refusal_reason": "",
            "evaluated_at": TODAY.isoformat(),
        }
