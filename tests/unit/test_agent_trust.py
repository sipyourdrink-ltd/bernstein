"""Tests for graduated agent access control (agent_trust.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.agent_trust import (
    AgentTrustScore,
    AgentTrustStore,
    TrustEvaluator,
    TrustLevel,
    TrustPolicy,
    get_permissions_for_trust_level,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


@pytest.fixture()
def store(sdd_dir: Path) -> AgentTrustStore:
    return AgentTrustStore(sdd_dir)


@pytest.fixture()
def evaluator() -> TrustEvaluator:
    return TrustEvaluator()


# ---------------------------------------------------------------------------
# TrustLevel ordering
# ---------------------------------------------------------------------------


class TestTrustLevelOrdering:
    def test_next_level_untrusted(self, evaluator: TrustEvaluator) -> None:
        assert evaluator.next_level(TrustLevel.UNTRUSTED) == TrustLevel.RESTRICTED

    def test_next_level_restricted(self, evaluator: TrustEvaluator) -> None:
        assert evaluator.next_level(TrustLevel.RESTRICTED) == TrustLevel.TRUSTED

    def test_next_level_trusted(self, evaluator: TrustEvaluator) -> None:
        assert evaluator.next_level(TrustLevel.TRUSTED) == TrustLevel.ELEVATED

    def test_next_level_elevated_raises(self, evaluator: TrustEvaluator) -> None:
        with pytest.raises(ValueError, match="no level after"):
            evaluator.next_level(TrustLevel.ELEVATED)


# ---------------------------------------------------------------------------
# Permission profiles
# ---------------------------------------------------------------------------


class TestPermissionProfiles:
    def test_untrusted_denies_all_writes(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.UNTRUSTED)
        # denied_paths=("*",) means no file writes are allowed
        assert "*" in perms.denied_paths

    def test_untrusted_allows_read_commands(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.UNTRUSTED)
        assert any("git log" in cmd for cmd in perms.allowed_commands)

    def test_untrusted_blocks_network_commands(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.UNTRUSTED)
        assert any("curl" in cmd for cmd in perms.denied_commands)
        assert any("wget" in cmd for cmd in perms.denied_commands)

    def test_restricted_allows_src_writes(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.RESTRICTED)
        assert "src/*" in perms.allowed_paths
        assert "tests/*" in perms.allowed_paths

    def test_restricted_denies_sdd_writes(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.RESTRICTED)
        assert ".sdd/*" in perms.denied_paths

    def test_trusted_has_broader_paths(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.TRUSTED)
        assert "docs/*" in perms.allowed_paths

    def test_elevated_allows_github_workflows(self) -> None:
        perms = get_permissions_for_trust_level(TrustLevel.ELEVATED)
        assert ".github/*" in perms.allowed_paths

    def test_each_level_has_permissions(self) -> None:
        for level in TrustLevel:
            perms = get_permissions_for_trust_level(level)
            # Should return an AgentPermissions instance (not None)
            assert perms is not None


# ---------------------------------------------------------------------------
# AgentTrustScore
# ---------------------------------------------------------------------------


class TestAgentTrustScore:
    def test_defaults_to_untrusted(self) -> None:
        score = AgentTrustScore(agent_id="test-agent")
        assert score.trust_level == TrustLevel.UNTRUSTED

    def test_tasks_total(self) -> None:
        score = AgentTrustScore(agent_id="a", tasks_completed=7, tasks_failed=3)
        assert score.tasks_total == 10

    def test_success_rate_no_tasks(self) -> None:
        score = AgentTrustScore(agent_id="a")
        assert score.success_rate == pytest.approx(0.0)

    def test_success_rate_all_success(self) -> None:
        score = AgentTrustScore(agent_id="a", tasks_completed=5, tasks_failed=0)
        assert score.success_rate == pytest.approx(1.0)

    def test_success_rate_partial(self) -> None:
        score = AgentTrustScore(agent_id="a", tasks_completed=9, tasks_failed=1)
        assert score.success_rate == pytest.approx(0.9)

    def test_permissions_property(self) -> None:
        score = AgentTrustScore(agent_id="a", trust_level=TrustLevel.RESTRICTED)
        assert score.permissions == get_permissions_for_trust_level(TrustLevel.RESTRICTED)

    def test_roundtrip_serialisation(self) -> None:
        score = AgentTrustScore(
            agent_id="agent-xyz",
            trust_level=TrustLevel.TRUSTED,
            tasks_completed=12,
            tasks_failed=1,
            security_violations=0,
            consecutive_successes=6,
        )
        restored = AgentTrustScore.from_dict(score.to_dict())
        assert restored.agent_id == score.agent_id
        assert restored.trust_level == score.trust_level
        assert restored.tasks_completed == score.tasks_completed
        assert restored.success_rate == pytest.approx(score.success_rate)


# ---------------------------------------------------------------------------
# TrustEvaluator — can_promote
# ---------------------------------------------------------------------------


class TestCanPromote:
    def test_elevated_is_terminal(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(agent_id="a", trust_level=TrustLevel.ELEVATED)
        ok, reason = evaluator.can_promote(score)
        assert not ok
        assert "terminal" in reason

    def test_insufficient_tasks_blocks_promotion(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(
            agent_id="a",
            trust_level=TrustLevel.UNTRUSTED,
            tasks_completed=1,  # needs 3
            consecutive_successes=1,
        )
        ok, reason = evaluator.can_promote(score)
        assert not ok
        assert "3" in reason

    def test_security_violation_blocks_promotion(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(
            agent_id="a",
            trust_level=TrustLevel.UNTRUSTED,
            tasks_completed=3,
            security_violations=1,  # max is 0 for UNTRUSTED
            consecutive_successes=3,
        )
        ok, reason = evaluator.can_promote(score)
        assert not ok
        assert "violation" in reason.lower()

    def test_insufficient_consecutive_successes_blocks(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(
            agent_id="a",
            trust_level=TrustLevel.UNTRUSTED,
            tasks_completed=3,
            security_violations=0,
            consecutive_successes=2,  # needs 3
        )
        ok, reason = evaluator.can_promote(score)
        assert not ok
        assert "consecutive" in reason.lower()

    def test_meets_all_criteria(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(
            agent_id="a",
            trust_level=TrustLevel.UNTRUSTED,
            tasks_completed=3,
            security_violations=0,
            consecutive_successes=3,
        )
        ok, reason = evaluator.can_promote(score)
        assert ok
        assert "restricted" in reason.lower()

    def test_success_rate_too_low_for_restricted(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(
            agent_id="a",
            trust_level=TrustLevel.RESTRICTED,
            tasks_completed=10,
            tasks_failed=5,  # 66% success, need 90%
            security_violations=0,
            consecutive_successes=5,
        )
        ok, reason = evaluator.can_promote(score)
        assert not ok
        assert "%" in reason


# ---------------------------------------------------------------------------
# TrustEvaluator — promote
# ---------------------------------------------------------------------------


class TestPromote:
    def test_promote_advances_level(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(agent_id="a", trust_level=TrustLevel.UNTRUSTED)
        evaluator.promote(score)
        assert score.trust_level == TrustLevel.RESTRICTED

    def test_promote_records_log_entry(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(agent_id="a", trust_level=TrustLevel.UNTRUSTED)
        evaluator.promote(score, reason="test-run", promoted_by="ci")
        assert len(score.promotion_log) == 1
        entry = score.promotion_log[0]
        assert entry["from_level"] == "untrusted"
        assert entry["to_level"] == "restricted"
        assert entry["reason"] == "test-run"
        assert entry["promoted_by"] == "ci"

    def test_promote_elevated_raises(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(agent_id="a", trust_level=TrustLevel.ELEVATED)
        with pytest.raises(ValueError):
            evaluator.promote(score)

    def test_promote_sequential_levels(self, evaluator: TrustEvaluator) -> None:
        score = AgentTrustScore(agent_id="a", trust_level=TrustLevel.UNTRUSTED)
        evaluator.promote(score)
        evaluator.promote(score)
        evaluator.promote(score)
        assert score.trust_level == TrustLevel.ELEVATED
        assert len(score.promotion_log) == 3


# ---------------------------------------------------------------------------
# AgentTrustStore — persistence
# ---------------------------------------------------------------------------


class TestAgentTrustStore:
    def test_get_or_create_new_agent(self, store: AgentTrustStore) -> None:
        score = store.get_or_create("new-agent")
        assert score.trust_level == TrustLevel.UNTRUSTED
        assert score.agent_id == "new-agent"

    def test_get_or_create_persists(self, store: AgentTrustStore) -> None:
        score1 = store.get_or_create("agent-a")
        score1.tasks_completed = 5
        store.save(score1)

        score2 = store.get_or_create("agent-a")
        assert score2.tasks_completed == 5

    def test_load_returns_none_for_unknown(self, store: AgentTrustStore) -> None:
        assert store.load("nonexistent") is None

    def test_save_and_load_roundtrip(self, store: AgentTrustStore) -> None:
        score = AgentTrustScore(
            agent_id="roundtrip-agent",
            trust_level=TrustLevel.TRUSTED,
            tasks_completed=15,
            tasks_failed=2,
        )
        store.save(score)
        loaded = store.load("roundtrip-agent")
        assert loaded is not None
        assert loaded.trust_level == TrustLevel.TRUSTED
        assert loaded.tasks_completed == 15

    def test_list_all_empty(self, store: AgentTrustStore) -> None:
        assert store.list_all() == []

    def test_list_all_returns_all_agents(self, store: AgentTrustStore) -> None:
        store.get_or_create("agent-1")
        store.get_or_create("agent-2")
        scores = store.list_all()
        ids = {s.agent_id for s in scores}
        assert ids == {"agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# AgentTrustStore — record_task_outcome
# ---------------------------------------------------------------------------


class TestRecordTaskOutcome:
    def test_success_increments_completed(self, store: AgentTrustStore) -> None:
        score = store.record_task_outcome("agent-a", success=True)
        assert score.tasks_completed == 1
        assert score.tasks_failed == 0

    def test_failure_increments_failed(self, store: AgentTrustStore) -> None:
        score = store.record_task_outcome("agent-a", success=False)
        assert score.tasks_failed == 1
        assert score.tasks_completed == 0

    def test_failure_resets_consecutive_successes(self, store: AgentTrustStore) -> None:
        store.record_task_outcome("agent-a", success=True)
        store.record_task_outcome("agent-a", success=True)
        score = store.record_task_outcome("agent-a", success=False)
        assert score.consecutive_successes == 0

    def test_security_violation_increments_counter(self, store: AgentTrustStore) -> None:
        score = store.record_task_outcome("agent-a", success=True, security_violation=True)
        assert score.security_violations == 1

    def test_security_violation_resets_consecutive_successes(self, store: AgentTrustStore) -> None:
        store.record_task_outcome("agent-a", success=True)
        store.record_task_outcome("agent-a", success=True)
        score = store.record_task_outcome("agent-a", success=True, security_violation=True)
        assert score.consecutive_successes == 0

    def test_auto_promote_on_threshold(self, store: AgentTrustStore) -> None:
        # 3 consecutive clean successes promotes UNTRUSTED → RESTRICTED
        for _ in range(3):
            store.record_task_outcome("promo-agent", success=True, auto_promote=True)
        score = store.load("promo-agent")
        assert score is not None
        assert score.trust_level == TrustLevel.RESTRICTED

    def test_no_auto_promote_when_disabled(self, store: AgentTrustStore) -> None:
        for _ in range(3):
            store.record_task_outcome("no-promo-agent", success=True, auto_promote=False)
        score = store.load("no-promo-agent")
        assert score is not None
        assert score.trust_level == TrustLevel.UNTRUSTED

    def test_violation_blocks_auto_promotion(self, store: AgentTrustStore) -> None:
        # 2 clean successes, then a violation, then more successes
        store.record_task_outcome("viol-agent", success=True)
        store.record_task_outcome("viol-agent", success=True)
        store.record_task_outcome("viol-agent", success=True, security_violation=True)
        # Even after the violation, the agent should still be UNTRUSTED
        score = store.load("viol-agent")
        assert score is not None
        assert score.trust_level == TrustLevel.UNTRUSTED

    def test_persistence_across_store_instances(self, sdd_dir: Path) -> None:
        store1 = AgentTrustStore(sdd_dir)
        store1.record_task_outcome("persist-agent", success=True)

        store2 = AgentTrustStore(sdd_dir)
        score = store2.load("persist-agent")
        assert score is not None
        assert score.tasks_completed == 1

    def test_custom_policy_respected(self, store: AgentTrustStore) -> None:
        # Override: require only 1 task for UNTRUSTED → RESTRICTED
        custom_policy = TrustPolicy(
            level=TrustLevel.UNTRUSTED,
            min_tasks=1,
            min_success_rate=0.0,
            max_security_violations=0,
            min_consecutive_successes=1,
        )
        evaluator = TrustEvaluator(policies={TrustLevel.UNTRUSTED.value: custom_policy})
        score = store.record_task_outcome("custom-agent", success=True, evaluator=evaluator)
        assert score.trust_level == TrustLevel.RESTRICTED
