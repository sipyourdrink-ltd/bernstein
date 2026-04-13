"""Unit tests for graduated access control."""

from __future__ import annotations

from bernstein.core.security.graduated_access import (
    AccessPolicy,
    AgentTrustRecord,
    GraduatedAccessManager,
    TrustLevel,
)

# ---------------------------------------------------------------------------
# TrustLevel enum
# ---------------------------------------------------------------------------


class TestTrustLevel:
    def test_ordering(self) -> None:
        assert TrustLevel.UNTRUSTED < TrustLevel.PROBATIONARY
        assert TrustLevel.PROBATIONARY < TrustLevel.TRUSTED
        assert TrustLevel.TRUSTED < TrustLevel.ELEVATED

    def test_integer_values(self) -> None:
        assert TrustLevel.UNTRUSTED == 0
        assert TrustLevel.PROBATIONARY == 1
        assert TrustLevel.TRUSTED == 2
        assert TrustLevel.ELEVATED == 3

    def test_names(self) -> None:
        assert TrustLevel.UNTRUSTED.name == "UNTRUSTED"
        assert TrustLevel.ELEVATED.name == "ELEVATED"


# ---------------------------------------------------------------------------
# AgentTrustRecord dataclass
# ---------------------------------------------------------------------------


class TestAgentTrustRecord:
    def test_defaults(self) -> None:
        record = AgentTrustRecord(agent_id="a1")
        assert record.agent_id == "a1"
        assert record.trust_level == TrustLevel.UNTRUSTED
        assert record.successful_tasks == 0
        assert record.failed_tasks == 0
        assert record.security_violations == 0
        assert record.first_seen > 0
        assert record.last_seen > 0

    def test_custom_values(self) -> None:
        record = AgentTrustRecord(
            agent_id="a2",
            trust_level=TrustLevel.TRUSTED,
            successful_tasks=5,
            failed_tasks=1,
            security_violations=0,
            first_seen=1000.0,
            last_seen=2000.0,
        )
        assert record.trust_level == TrustLevel.TRUSTED
        assert record.successful_tasks == 5
        assert record.failed_tasks == 1
        assert record.first_seen == 1000.0
        assert record.last_seen == 2000.0


# ---------------------------------------------------------------------------
# AccessPolicy frozen dataclass
# ---------------------------------------------------------------------------


class TestAccessPolicy:
    def test_frozen(self) -> None:
        policy = AccessPolicy(
            trust_level=TrustLevel.UNTRUSTED,
            can_write_files=False,
            can_access_network=False,
            max_files_per_task=0,
            allowed_directories=("docs/",),
        )
        try:
            policy.can_write_files = True  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised, "AccessPolicy should be frozen"

    def test_fields(self) -> None:
        policy = AccessPolicy(
            trust_level=TrustLevel.ELEVATED,
            can_write_files=True,
            can_access_network=True,
            max_files_per_task=100,
            allowed_directories=(),
        )
        assert policy.trust_level == TrustLevel.ELEVATED
        assert policy.can_write_files is True
        assert policy.can_access_network is True
        assert policy.max_files_per_task == 100
        assert policy.allowed_directories == ()


# ---------------------------------------------------------------------------
# GraduatedAccessManager — basic operations
# ---------------------------------------------------------------------------


class TestManagerBasics:
    def test_new_agent_is_untrusted(self) -> None:
        mgr = GraduatedAccessManager()
        assert mgr.get_trust_level("new-agent") == TrustLevel.UNTRUSTED

    def test_get_policy_untrusted(self) -> None:
        mgr = GraduatedAccessManager()
        policy = mgr.get_policy("new-agent")
        assert policy.trust_level == TrustLevel.UNTRUSTED
        assert policy.can_write_files is False
        assert policy.can_access_network is False
        assert policy.max_files_per_task == 0

    def test_get_policy_elevated(self) -> None:
        mgr = GraduatedAccessManager()
        # Manually promote to ELEVATED
        mgr.promote("agent-x")
        mgr.promote("agent-x")
        mgr.promote("agent-x")
        policy = mgr.get_policy("agent-x")
        assert policy.trust_level == TrustLevel.ELEVATED
        assert policy.can_write_files is True
        assert policy.can_access_network is True
        assert policy.max_files_per_task == 100
        assert policy.allowed_directories == ()

    def test_idempotent_get_trust_level(self) -> None:
        mgr = GraduatedAccessManager()
        level1 = mgr.get_trust_level("agent-a")
        level2 = mgr.get_trust_level("agent-a")
        assert level1 == level2 == TrustLevel.UNTRUSTED


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


class TestPromotion:
    def test_auto_promote_untrusted_to_probationary(self) -> None:
        mgr = GraduatedAccessManager()
        level = mgr.record_outcome("a1", success=True)
        assert level == TrustLevel.PROBATIONARY

    def test_auto_promote_probationary_to_trusted(self) -> None:
        mgr = GraduatedAccessManager()
        # 1 success -> PROBATIONARY
        mgr.record_outcome("a1", success=True)
        # 2nd success (total 2) — not enough for TRUSTED
        mgr.record_outcome("a1", success=True)
        assert mgr.get_trust_level("a1") == TrustLevel.PROBATIONARY
        # 3rd success (total 3) -> TRUSTED
        level = mgr.record_outcome("a1", success=True)
        assert level == TrustLevel.TRUSTED

    def test_auto_promote_trusted_to_elevated(self) -> None:
        mgr = GraduatedAccessManager()
        for _ in range(10):
            mgr.record_outcome("a1", success=True)
        assert mgr.get_trust_level("a1") == TrustLevel.ELEVATED

    def test_no_promote_beyond_elevated(self) -> None:
        mgr = GraduatedAccessManager()
        for _ in range(20):
            mgr.record_outcome("a1", success=True)
        assert mgr.get_trust_level("a1") == TrustLevel.ELEVATED

    def test_manual_promote(self) -> None:
        mgr = GraduatedAccessManager()
        level = mgr.promote("a1")
        assert level == TrustLevel.PROBATIONARY

    def test_manual_promote_cap_at_elevated(self) -> None:
        mgr = GraduatedAccessManager()
        for _ in range(10):
            mgr.promote("a1")
        assert mgr.get_trust_level("a1") == TrustLevel.ELEVATED

    def test_should_promote_false_at_elevated(self) -> None:
        mgr = GraduatedAccessManager()
        record = AgentTrustRecord(
            agent_id="a1",
            trust_level=TrustLevel.ELEVATED,
            successful_tasks=100,
        )
        assert mgr.should_promote(record) is False

    def test_should_promote_blocked_by_violations(self) -> None:
        mgr = GraduatedAccessManager()
        record = AgentTrustRecord(
            agent_id="a1",
            trust_level=TrustLevel.PROBATIONARY,
            successful_tasks=10,
            security_violations=1,
        )
        assert mgr.should_promote(record) is False


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


class TestDemotion:
    def test_security_violation_demotes(self) -> None:
        mgr = GraduatedAccessManager()
        mgr.promote("a1")  # PROBATIONARY
        level = mgr.record_outcome("a1", success=False, security_violation=True)
        assert level == TrustLevel.UNTRUSTED

    def test_violation_at_untrusted_stays_untrusted(self) -> None:
        mgr = GraduatedAccessManager()
        level = mgr.record_outcome("a1", success=False, security_violation=True)
        assert level == TrustLevel.UNTRUSTED

    def test_manual_demote(self) -> None:
        mgr = GraduatedAccessManager()
        mgr.promote("a1")  # PROBATIONARY
        mgr.promote("a1")  # TRUSTED
        level = mgr.demote("a1")
        assert level == TrustLevel.PROBATIONARY

    def test_manual_demote_floor_at_untrusted(self) -> None:
        mgr = GraduatedAccessManager()
        level = mgr.demote("a1")
        assert level == TrustLevel.UNTRUSTED

    def test_violation_increments_counter(self) -> None:
        mgr = GraduatedAccessManager()
        mgr.promote("a1")
        mgr.record_outcome("a1", success=False, security_violation=True)
        mgr.record_outcome("a1", success=False, security_violation=True)
        record = mgr.get_record("a1")
        assert record.security_violations == 2

    def test_violation_prevents_future_auto_promotion(self) -> None:
        mgr = GraduatedAccessManager()
        # Get to PROBATIONARY, then violate
        mgr.record_outcome("a1", success=True)  # -> PROBATIONARY
        mgr.record_outcome("a1", success=False, security_violation=True)  # -> UNTRUSTED
        # Now have 1 success, 1 violation — should not auto-promote even
        # after more successes
        mgr.record_outcome("a1", success=True)  # 2 successes, 1 violation
        mgr.record_outcome("a1", success=True)  # 3 successes, 1 violation
        # Should stay UNTRUSTED because violations > 0 blocks promotion
        # (threshold for PROBATIONARY is 1 success + 0 violations)
        # Actually the code checks violations <= max_violations (0),
        # so 1 violation blocks all promotion.
        assert mgr.get_trust_level("a1") == TrustLevel.UNTRUSTED


# ---------------------------------------------------------------------------
# Failure without violation
# ---------------------------------------------------------------------------


class TestFailure:
    def test_failure_does_not_demote(self) -> None:
        mgr = GraduatedAccessManager()
        mgr.promote("a1")  # PROBATIONARY
        level = mgr.record_outcome("a1", success=False)
        assert level == TrustLevel.PROBATIONARY

    def test_failure_increments_counter(self) -> None:
        mgr = GraduatedAccessManager()
        mgr.record_outcome("a1", success=False)
        mgr.record_outcome("a1", success=False)
        record = mgr.get_record("a1")
        assert record.failed_tasks == 2
        assert record.successful_tasks == 0


# ---------------------------------------------------------------------------
# Custom policies
# ---------------------------------------------------------------------------


class TestCustomPolicies:
    def test_custom_policy_override(self) -> None:
        custom = {
            TrustLevel.UNTRUSTED: AccessPolicy(
                trust_level=TrustLevel.UNTRUSTED,
                can_write_files=True,
                can_access_network=True,
                max_files_per_task=999,
                allowed_directories=(),
            ),
            TrustLevel.PROBATIONARY: AccessPolicy(
                trust_level=TrustLevel.PROBATIONARY,
                can_write_files=True,
                can_access_network=True,
                max_files_per_task=999,
                allowed_directories=(),
            ),
            TrustLevel.TRUSTED: AccessPolicy(
                trust_level=TrustLevel.TRUSTED,
                can_write_files=True,
                can_access_network=True,
                max_files_per_task=999,
                allowed_directories=(),
            ),
            TrustLevel.ELEVATED: AccessPolicy(
                trust_level=TrustLevel.ELEVATED,
                can_write_files=True,
                can_access_network=True,
                max_files_per_task=999,
                allowed_directories=(),
            ),
        }
        mgr = GraduatedAccessManager(policies=custom)
        policy = mgr.get_policy("a1")
        assert policy.can_write_files is True
        assert policy.max_files_per_task == 999


# ---------------------------------------------------------------------------
# Policy per trust level
# ---------------------------------------------------------------------------


class TestPolicyProgression:
    def test_probationary_policy(self) -> None:
        mgr = GraduatedAccessManager()
        mgr.record_outcome("a1", success=True)  # -> PROBATIONARY
        policy = mgr.get_policy("a1")
        assert policy.trust_level == TrustLevel.PROBATIONARY
        assert policy.can_write_files is True
        assert policy.can_access_network is False
        assert policy.max_files_per_task == 5
        assert "src/" in policy.allowed_directories

    def test_trusted_policy(self) -> None:
        mgr = GraduatedAccessManager()
        for _ in range(3):
            mgr.record_outcome("a1", success=True)
        policy = mgr.get_policy("a1")
        assert policy.trust_level == TrustLevel.TRUSTED
        assert policy.can_write_files is True
        assert policy.can_access_network is True
        assert policy.max_files_per_task == 20
        assert "scripts/" in policy.allowed_directories

    def test_elevated_unrestricted_directories(self) -> None:
        mgr = GraduatedAccessManager()
        for _ in range(10):
            mgr.record_outcome("a1", success=True)
        policy = mgr.get_policy("a1")
        assert policy.allowed_directories == ()
