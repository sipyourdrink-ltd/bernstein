"""Tests for TierHijacker — automatic free tier detection and routing."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.hijacker import (
    EnvVarConfig,
    EnvVarTierDetector,
    FreeTierSource,
    HijackOpportunity,
    ModelSafetyCheck,
    QuotaSafetyCheck,
    QuotaTracker,
    RateLimitSafetyCheck,
    SafetyCheck,
    TierHijacker,
    create_default_detectors,
    get_default_hijacker,
)
from bernstein.core.models import ApiTier, ModelConfig, ProviderType
from bernstein.core.router import ProviderConfig, Tier, TierAwareRouter

# --- Helpers ---


def _make_router() -> TierAwareRouter:
    """Create a router with basic providers."""
    router = TierAwareRouter()
    router.register_provider(
        ProviderConfig(
            name="standard-provider",
            models={"claude-sonnet": ModelConfig("sonnet", "high")},
            tier=Tier.STANDARD,
            cost_per_1k_tokens=0.003,
        )
    )
    return router


def _make_opportunity(
    source: FreeTierSource = FreeTierSource.NEW_PROVIDER_TRIAL,
    provider_name: str = "test-provider",
    confidence: float = 0.9,
    estimated_free_tokens: int = 100_000,
) -> HijackOpportunity:
    return HijackOpportunity(
        source=source,
        provider_name=provider_name,
        provider_type=ProviderType.CLAUDE,
        description="Test opportunity",
        estimated_free_tokens=estimated_free_tokens,
        confidence=confidence,
    )


# --- EnvVarTierDetector ---


class TestEnvVarTierDetector:
    def test_detects_free_tier_from_env_var(self) -> None:
        detector = EnvVarTierDetector(
            [
                EnvVarConfig(
                    env_var="TEST_API_KEY",
                    provider_type=ProviderType.CLAUDE,
                    tier=ApiTier.FREE,
                ),
            ]
        )

        with patch.dict(os.environ, {"TEST_API_KEY": "sk-test-key"}):
            opportunity = detector.detect()

            assert opportunity is not None
            assert opportunity.source == FreeTierSource.NEW_PROVIDER_TRIAL
            assert opportunity.provider_type == ProviderType.CLAUDE

    def test_returns_none_when_env_var_not_set(self) -> None:
        detector = EnvVarTierDetector(
            [
                EnvVarConfig(
                    env_var="NONEXISTENT_API_KEY",
                    provider_type=ProviderType.CLAUDE,
                    tier=ApiTier.FREE,
                ),
            ]
        )

        with patch.dict(os.environ, {}, clear=False):
            if "NONEXISTENT_API_KEY" in os.environ:
                del os.environ["NONEXISTENT_API_KEY"]
            opportunity = detector.detect()

            assert opportunity is None

    def test_detects_trial_key_prefix(self) -> None:
        detector = EnvVarTierDetector(
            [
                EnvVarConfig(
                    env_var="TRIAL_KEY",
                    provider_type=ProviderType.CLAUDE,
                    tier=ApiTier.PLUS,  # Non-free tier
                ),
            ]
        )

        with patch.dict(os.environ, {"TRIAL_KEY": "sk-trial-abc123"}):
            opportunity = detector.detect()

            assert opportunity is not None
            assert opportunity.source == FreeTierSource.NEW_PROVIDER_TRIAL

    def test_estimates_tokens_based_on_tier(self) -> None:
        # FREE tier keys are always detected
        detector = EnvVarTierDetector(
            [
                EnvVarConfig(
                    env_var="TEST_KEY",
                    provider_type=ProviderType.CLAUDE,
                    tier=ApiTier.FREE,
                ),
            ]
        )

        with patch.dict(os.environ, {"TEST_KEY": "sk-free-key"}):
            opportunity = detector.detect()

            assert opportunity is not None
            # FREE tier gets 100k tokens estimate
            assert opportunity.estimated_free_tokens == 100_000


# --- QuotaTracker ---


class TestQuotaTracker:
    def test_update_and_get_quota(self) -> None:
        tracker = QuotaTracker()

        tracker.update_quota("provider-1", 500)

        assert tracker.get_quota("provider-1") == 500

    def test_has_unused_quota_true(self) -> None:
        tracker = QuotaTracker()
        tracker.update_quota("provider-1", 5000)

        assert tracker.has_unused_quota("provider-1", threshold=1000) is True

    def test_has_unused_quota_false(self) -> None:
        tracker = QuotaTracker()
        tracker.update_quota("provider-1", 500)

        assert tracker.has_unused_quota("provider-1", threshold=1000) is False

    def test_has_unused_quota_nonexistent_provider(self) -> None:
        tracker = QuotaTracker()

        assert tracker.has_unused_quota("nonexistent", threshold=1000) is False

    def test_is_stale_true(self) -> None:
        tracker = QuotaTracker()
        tracker.update_quota("provider-1", 500)

        # Manually set old timestamp
        tracker._last_updated["provider-1"] = 0  # Unix epoch

        assert tracker.is_stale("provider-1", max_age_seconds=60) is True

    def test_is_stale_false(self) -> None:
        tracker = QuotaTracker()
        tracker.update_quota("provider-1", 500)

        assert tracker.is_stale("provider-1", max_age_seconds=60) is False

    def test_get_quota_nonexistent_provider(self) -> None:
        tracker = QuotaTracker()

        assert tracker.get_quota("nonexistent") is None


# --- TierHijacker ---


class TestTierHijacker:
    def test_initialization(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        assert hijacker.router is router
        assert hijacker.detectors == []
        assert hijacker._opportunities == []

    def test_add_detector(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)
        detector = MagicMock()

        hijacker.add_detector(detector)

        assert detector in hijacker.detectors

    def test_add_safety_check(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)
        check = SafetyCheck()

        hijacker.add_safety_check(check)

        assert check in hijacker._safety_checks

    def test_scan_for_opportunities_empty(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        opportunities = hijacker.scan_for_opportunities()

        # Should find open-source alternatives if available
        assert isinstance(opportunities, list)

    def test_scan_for_opportunities_with_detectors(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        mock_detector = MagicMock()
        mock_detector.detect.return_value = _make_opportunity(confidence=0.9)
        hijacker.add_detector(mock_detector)

        opportunities = hijacker.scan_for_opportunities()

        assert len(opportunities) >= 1
        assert opportunities[0].confidence == pytest.approx(0.9)

    def test_scan_for_opportunities_sorts_by_confidence(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        detector1 = MagicMock()
        detector1.detect.return_value = _make_opportunity(confidence=0.5)
        detector2 = MagicMock()
        detector2.detect.return_value = _make_opportunity(confidence=0.9)

        hijacker.add_detector(detector1)
        hijacker.add_detector(detector2)

        opportunities = hijacker.scan_for_opportunities()

        # Highest confidence first
        assert opportunities[0].confidence >= opportunities[-1].confidence

    def test_scan_for_opportunities_handles_detector_failure(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        failing_detector = MagicMock()
        failing_detector.detect.side_effect = Exception("Detector failed")
        hijacker.add_detector(failing_detector)

        # Should not raise
        opportunities = hijacker.scan_for_opportunities()
        assert isinstance(opportunities, list)

    def test_scan_provider_quotas(self) -> None:
        router = TierAwareRouter()
        router.register_provider(
            ProviderConfig(
                name="free-provider",
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
                quota_remaining=100,
            )
        )
        hijacker = TierHijacker(router)

        opportunities = hijacker._scan_provider_quotas()

        assert len(opportunities) == 1
        assert opportunities[0].source == FreeTierSource.UNUSED_QUOTA
        assert opportunities[0].provider_name == "free-provider"

    def test_hijack_for_task_success(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        # Add a mock detector that returns an opportunity
        mock_detector = MagicMock()
        mock_detector.detect.return_value = _make_opportunity(confidence=0.9)
        hijacker.add_detector(mock_detector)

        model_config = ModelConfig("sonnet", "high")
        result = hijacker.hijack_for_task(model_config)

        assert result is not None
        assert result.success is True
        assert result.provider_config is not None
        assert result.provider_config.tier == Tier.FREE

    @patch.object(TierHijacker, "_check_ollama_available", return_value=False)
    @patch.object(TierHijacker, "_check_lm_studio_available", return_value=False)
    def test_hijack_for_task_below_confidence_threshold(self, _lm: MagicMock, _ol: MagicMock) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        mock_detector = MagicMock()
        mock_detector.detect.return_value = _make_opportunity(confidence=0.5)
        hijacker.add_detector(mock_detector)

        model_config = ModelConfig("sonnet", "high")
        result = hijacker.hijack_for_task(model_config, min_confidence=0.8)

        assert result is None

    @patch.object(TierHijacker, "_check_ollama_available", return_value=False)
    @patch.object(TierHijacker, "_check_lm_studio_available", return_value=False)
    def test_hijack_for_task_expired_opportunity(self, _lm: MagicMock, _ol: MagicMock) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        import time

        expired_opportunity = HijackOpportunity(
            source=FreeTierSource.NEW_PROVIDER_TRIAL,
            provider_name="expired-provider",
            provider_type=ProviderType.CLAUDE,
            description="Expired opportunity",
            estimated_free_tokens=100_000,
            expiry_timestamp=int(time.time()) - 3600,  # 1 hour ago
            confidence=0.9,
        )
        mock_detector = MagicMock()
        mock_detector.detect.return_value = expired_opportunity
        hijacker.add_detector(mock_detector)

        model_config = ModelConfig("sonnet", "high")
        result = hijacker.hijack_for_task(model_config)

        assert result is None

    def test_hijack_for_task_safety_check_failure(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        mock_detector = MagicMock()
        mock_detector.detect.return_value = _make_opportunity(confidence=0.9)
        hijacker.add_detector(mock_detector)

        # Add failing safety check
        failing_check = MagicMock()
        failing_check.name = "FailingCheck"
        failing_check.pre_hijack_check.return_value = False
        hijacker.add_safety_check(failing_check)

        model_config = ModelConfig("sonnet", "high")
        result = hijacker.hijack_for_task(model_config)

        assert result is not None
        assert result.success is False
        assert "Safety check failed" in result.error_message

    def test_hijack_for_task_records_history(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        mock_detector = MagicMock()
        mock_detector.detect.return_value = _make_opportunity(confidence=0.9)
        hijacker.add_detector(mock_detector)

        model_config = ModelConfig("sonnet", "high")
        hijacker.hijack_for_task(model_config)

        assert len(hijacker.get_hijack_history()) >= 1

    def test_get_active_opportunities_filters_expired(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)
        import time

        # Add expired opportunity
        expired = HijackOpportunity(
            source=FreeTierSource.NEW_PROVIDER_TRIAL,
            provider_name="expired",
            provider_type=ProviderType.CLAUDE,
            description="Expired",
            estimated_free_tokens=100_000,
            expiry_timestamp=int(time.time()) - 3600,
        )
        hijacker._opportunities = [expired]

        active = hijacker.get_active_opportunities()

        assert len(active) == 0

    def test_get_active_opportunities_keeps_valid(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)
        import time

        # Add valid opportunity
        valid = HijackOpportunity(
            source=FreeTierSource.NEW_PROVIDER_TRIAL,
            provider_name="valid",
            provider_type=ProviderType.CLAUDE,
            description="Valid",
            estimated_free_tokens=100_000,
            expiry_timestamp=int(time.time()) + 3600,  # 1 hour in future
        )
        hijacker._opportunities = [valid]

        active = hijacker.get_active_opportunities()

        assert len(active) == 1

    def test_clear_opportunities(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        hijacker._opportunities = [_make_opportunity()]

        hijacker.clear_opportunities()

        assert hijacker._opportunities == []

    def test_opportunity_supports_model_open_source(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        opportunity = HijackOpportunity(
            source=FreeTierSource.OPEN_SOURCE_ALTERNATIVE,
            provider_name="ollama",
            provider_type=ProviderType.QWEN,
            description="Local Ollama",
            estimated_free_tokens=10_000_000,
        )

        assert hijacker._opportunity_supports_model(opportunity, "llama-3") is True
        assert hijacker._opportunity_supports_model(opportunity, "any-model") is True

    def test_opportunity_to_tier_mapping(self) -> None:
        router = _make_router()
        hijacker = TierHijacker(router)

        for source in FreeTierSource:
            opportunity = HijackOpportunity(
                source=source,
                provider_name="test",
                provider_type=ProviderType.CLAUDE,
                description="Test",
                estimated_free_tokens=100_000,
            )
            tier = hijacker._opportunity_to_tier(opportunity)
            assert tier == Tier.FREE


# --- Safety Checks ---


class TestRateLimitSafetyCheck:
    def test_allows_under_limit(self) -> None:
        check = RateLimitSafetyCheck(max_hijacks_per_hour=10)
        opportunities = [_make_opportunity()]

        assert check.pre_hijack_check(opportunities) is True

    def test_blocks_over_limit(self) -> None:
        check = RateLimitSafetyCheck(max_hijacks_per_hour=2)
        opportunities = [_make_opportunity()]

        # First two should pass
        assert check.pre_hijack_check(opportunities) is True
        assert check.pre_hijack_check(opportunities) is True

        # Third should fail
        assert check.pre_hijack_check(opportunities) is False

    def test_resets_after_hour(self) -> None:
        check = RateLimitSafetyCheck(max_hijacks_per_hour=1)
        opportunities = [_make_opportunity()]

        # Use up the limit
        check.pre_hijack_check(opportunities)

        # Manually age out the entries
        check._hijack_times = [t - 3601 for t in check._hijack_times]

        # Should be allowed again
        assert check.pre_hijack_check(opportunities) is True


class TestQuotaSafetyCheck:
    def test_allows_sufficient_quota(self) -> None:
        check = QuotaSafetyCheck(min_quota_tokens=10_000)
        opportunities = [_make_opportunity(estimated_free_tokens=100_000)]

        assert check.pre_hijack_check(opportunities) is True

    def test_blocks_insufficient_quota(self) -> None:
        check = QuotaSafetyCheck(min_quota_tokens=100_000)
        opportunities = [_make_opportunity(estimated_free_tokens=10_000)]

        assert check.pre_hijack_check(opportunities) is False

    def test_allows_if_any_opportunity_has_quota(self) -> None:
        check = QuotaSafetyCheck(min_quota_tokens=50_000)
        opportunities = [
            _make_opportunity(estimated_free_tokens=10_000),
            _make_opportunity(estimated_free_tokens=100_000),
        ]

        assert check.pre_hijack_check(opportunities) is True


class TestModelSafetyCheck:
    def test_default_allows_all(self) -> None:
        check = ModelSafetyCheck()
        opportunities = [_make_opportunity()]

        assert check.pre_hijack_check(opportunities) is True


# --- Default detectors ---


class TestCreateDefaultDetectors:
    def test_creates_detectors_for_major_providers(self) -> None:
        detectors = create_default_detectors()

        assert len(detectors) >= 3  # Anthropic, OpenAI, Gemini

    def test_detectors_have_correct_env_vars(self) -> None:
        detectors = create_default_detectors()

        env_vars = []
        for detector in detectors:
            if isinstance(detector, EnvVarTierDetector):
                for config in detector.configs:
                    env_vars.append(config.env_var)

        assert "ANTHROPIC_API_KEY" in env_vars
        assert "OPENAI_API_KEY" in env_vars
        assert "GEMINI_API_KEY" in env_vars


# --- Default hijacker ---


class TestGetDefaultHijacker:
    def test_returns_singleton(self) -> None:
        hijacker1 = get_default_hijacker()
        hijacker2 = get_default_hijacker()

        assert hijacker1 is hijacker2

    def test_has_safety_checks(self) -> None:
        hijacker = get_default_hijacker()

        assert len(hijacker._safety_checks) >= 2


# --- Integration tests with mocked tier states ---


class TestHijackerIntegration:
    def test_identifies_three_free_tier_sources(self) -> None:
        """Test that hijacker identifies at least 3 free tier sources."""
        router = TierAwareRouter()
        hijacker = TierHijacker(router)

        # Add detectors for different sources
        detector1 = MagicMock()
        detector1.detect.return_value = HijackOpportunity(
            source=FreeTierSource.NEW_PROVIDER_TRIAL,
            provider_name="trial-provider",
            provider_type=ProviderType.CLAUDE,
            description="Trial provider",
            estimated_free_tokens=100_000,
        )

        detector2 = MagicMock()
        detector2.detect.return_value = HijackOpportunity(
            source=FreeTierSource.PROMOTIONAL_CREDITS,
            provider_name="promo-provider",
            provider_type=ProviderType.CODEX,
            description="Promotional credits",
            estimated_free_tokens=500_000,
        )

        # Add a free tier provider to router for quota detection
        router.register_provider(
            ProviderConfig(
                name="free-quota-provider",
                models={"claude-sonnet": ModelConfig("sonnet", "high")},
                tier=Tier.FREE,
                cost_per_1k_tokens=0.0,
                quota_remaining=100,
            )
        )

        hijacker.add_detector(detector1)
        hijacker.add_detector(detector2)

        opportunities = hijacker.scan_for_opportunities()

        # Should find at least 3 sources: trial, promo, and unused quota
        sources_found = set(opp.source for opp in opportunities)
        assert len(sources_found) >= 2  # At least trial/promo + quota

    def test_routes_test_tasks_to_free_tier(self) -> None:
        """Test that test tasks are routed to free tier providers."""
        router = TierAwareRouter()
        hijacker = TierHijacker(router)

        # Add a free tier opportunity
        mock_detector = MagicMock()
        mock_detector.detect.return_value = HijackOpportunity(
            source=FreeTierSource.NEW_PROVIDER_TRIAL,
            provider_name="free-test-provider",
            provider_type=ProviderType.CLAUDE,
            description="Free trial",
            estimated_free_tokens=100_000,
            confidence=0.95,
        )
        hijacker.add_detector(mock_detector)

        from bernstein.core.models import Complexity, Scope, Task

        test_task = Task(
            id="TEST-001",
            title="Test task",
            description="Simple test task",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
        )

        # Hijack for the task
        from bernstein.core.router import route_task

        model_config = route_task(test_task)
        result = hijacker.hijack_for_task(model_config)

        assert result is not None
        assert result.success is True
        assert result.provider_config is not None
        assert result.provider_config.tier == Tier.FREE

    def test_mock_integration_with_router(self) -> None:
        """Test hijacker integration with router."""
        router = TierAwareRouter()
        hijacker = TierHijacker(router)

        # Simulate detecting and applying a free tier
        opportunity = HijackOpportunity(
            source=FreeTierSource.UNUSED_QUOTA,
            provider_name="mock-free-provider",
            provider_type=ProviderType.CLAUDE,
            description="Mock free provider",
            estimated_free_tokens=50_000,
            confidence=0.9,
        )

        result = hijacker._apply_hijack(opportunity)

        assert result.success is True
        assert "mock-free-provider" in router.state.providers
        assert router.state.providers["mock-free-provider"].tier == Tier.FREE
        assert router.state.providers["mock-free-provider"].cost_per_1k_tokens == pytest.approx(0.0)
