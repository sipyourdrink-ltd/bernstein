"""Automatic tier hijacking — detects and routes to free tier opportunities."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from bernstein.core.models import ApiTier, ModelConfig, ProviderType, RateLimit
from bernstein.core.router import ProviderConfig, Tier, TierAwareRouter

logger = logging.getLogger(__name__)


class FreeTierSource(Enum):
    """Sources of free tier access."""
    NEW_PROVIDER_TRIAL = "new_provider_trial"  # New provider with free trial credits
    UNUSED_QUOTA = "unused_quota"  # Existing provider with unused free quota
    PROMOTIONAL_CREDITS = "promotional_credits"  # Promotional/free credits
    OPEN_SOURCE_ALTERNATIVE = "open_source_alternative"  # Free open-source models
    COMMUNITY_TIER = "community_tier"  # Community-sponsored free access
    EDUCATIONAL_ACCESS = "educational_access"  # Educational/research free tier


@dataclass
class HijackOpportunity:
    """Represents a detected free tier opportunity."""
    source: FreeTierSource
    provider_name: str
    provider_type: ProviderType
    description: str
    estimated_free_tokens: int
    expiry_timestamp: int | None = None  # Unix timestamp, None = no expiry
    constraints: list[str] = field(default_factory=list)  # Usage constraints
    confidence: float = 1.0  # 0.0-1.0 confidence in detection


@dataclass
class HijackResult:
    """Result of a hijack attempt."""
    success: bool
    opportunity: HijackOpportunity
    provider_config: ProviderConfig | None = None
    error_message: str = ""


class TierDetector(Protocol):
    """Protocol for tier detection strategies."""

    def detect(self) -> HijackOpportunity | None:
        """Detect a free tier opportunity. Returns None if not found."""
        ...


@dataclass
class EnvVarConfig:
    """Configuration for environment variable based detection."""
    env_var: str
    provider_type: ProviderType
    tier: ApiTier
    rate_limit: RateLimit | None = None
    description: str = ""


class EnvVarTierDetector:
    """Detects free tiers from environment variables."""

    def __init__(self, configs: list[EnvVarConfig]) -> None:
        self.configs = configs

    def detect(self) -> HijackOpportunity | None:
        for config in self.configs:
            value = os.environ.get(config.env_var, "")
            if value and self._is_free_tier_key(value, config):
                return HijackOpportunity(
                    source=FreeTierSource.NEW_PROVIDER_TRIAL,
                    provider_name=f"{config.provider_type.value}-{config.tier.value}",
                    provider_type=config.provider_type,
                    description=config.description or f"Detected {config.tier.value} tier from {config.env_var}",
                    estimated_free_tokens=self._estimate_tokens(config.tier),
                    confidence=0.9,
                )
        return None

    def _is_free_tier_key(self, value: str, config: EnvVarConfig) -> bool:
        """Check if the API key indicates a free tier."""
        if config.tier == ApiTier.FREE:
            return True
        # Check for trial-specific key prefixes
        return value.startswith("sk-trial-") or value.startswith("trial-")

    def _estimate_tokens(self, tier: ApiTier) -> int:
        """Estimate free token allocation based on tier."""
        estimates = {
            ApiTier.FREE: 100_000,
            ApiTier.PLUS: 500_000,
            ApiTier.PRO: 1_000_000,
            ApiTier.ENTERPRISE: 10_000_000,
            ApiTier.UNLIMITED: 100_000_000,
        }
        return estimates.get(tier, 100_000)


class QuotaTracker:
    """Tracks remaining quota for providers."""

    def __init__(self) -> None:
        self._quotas: dict[str, int] = {}
        self._last_updated: dict[str, float] = {}

    def update_quota(self, provider_name: str, remaining: int) -> None:
        """Update the remaining quota for a provider."""
        self._quotas[provider_name] = remaining
        self._last_updated[provider_name] = time.time()

    def get_quota(self, provider_name: str) -> int | None:
        """Get the remaining quota for a provider."""
        return self._quotas.get(provider_name)

    def has_unused_quota(self, provider_name: str, threshold: int = 1000) -> bool:
        """Check if provider has unused quota above threshold."""
        remaining = self._quotas.get(provider_name, 0)
        return remaining > threshold

    def is_stale(self, provider_name: str, max_age_seconds: int = 3600) -> bool:
        """Check if quota information is stale."""
        last_updated = self._last_updated.get(provider_name, 0)
        return (time.time() - last_updated) > max_age_seconds


class TierHijacker:
    """
    Automatically detects free tier opportunities and routes tasks to them.

    Free tier sources detected:
    1. New provider trials (API keys with trial prefixes)
    2. Unused quotas (providers with remaining free allocation)
    3. Promotional credits (special promotional API keys)
    4. Open-source alternatives (local models, Ollama, etc.)
    5. Community tiers (community-sponsored access)
    6. Educational access (research/education free tiers)
    """

    def __init__(
        self,
        router: TierAwareRouter,
        detectors: list[TierDetector] | None = None,
    ) -> None:
        self.router = router
        self.detectors = detectors or []
        self.quota_tracker = QuotaTracker()
        self._opportunities: list[HijackOpportunity] = []
        self._hijack_history: list[HijackResult] = []
        self._safety_checks: list[SafetyCheck] = []

    def add_detector(self, detector: TierDetector) -> None:
        """Add a tier detection strategy."""
        self.detectors.append(detector)

    def add_safety_check(self, check: SafetyCheck) -> None:
        """Add a safety check to prevent abuse."""
        self._safety_checks.append(check)

    def scan_for_opportunities(self) -> list[HijackOpportunity]:
        """
        Scan all detectors for free tier opportunities.

        Returns:
            List of detected opportunities, sorted by confidence.
        """
        opportunities = []

        # Run all detectors
        for detector in self.detectors:
            try:
                opportunity = detector.detect()
                if opportunity:
                    opportunities.append(opportunity)
            except Exception as exc:
                # Don't let detector failures break scanning
                logger.warning("Detector %s failed: %s", type(detector).__name__, exc)

        # Check for unused quotas in registered providers
        quota_opportunities = self._scan_provider_quotas()
        opportunities.extend(quota_opportunities)

        # Check for open-source alternatives
        oss_opportunities = self._scan_open_source_alternatives()
        opportunities.extend(oss_opportunities)

        # Sort by confidence (highest first)
        opportunities.sort(key=lambda o: o.confidence, reverse=True)
        self._opportunities = opportunities
        return opportunities

    def _scan_provider_quotas(self) -> list[HijackOpportunity]:
        """Scan registered providers for unused quotas."""
        opportunities = []

        for provider_name, provider in self.router.state.providers.items():
            if provider.tier == Tier.FREE and provider.quota_remaining and provider.quota_remaining > 0:
                # Check if this quota was already tracked
                old_quota = self.quota_tracker.get_quota(provider_name)
                if old_quota is None or provider.quota_remaining > old_quota:
                    opportunities.append(HijackOpportunity(
                        source=FreeTierSource.UNUSED_QUOTA,
                        provider_name=provider_name,
                        provider_type=ProviderType.CLAUDE,  # Default, detectors should specify
                        description=f"Provider {provider_name} has {provider.quota_remaining} free requests remaining",
                        estimated_free_tokens=provider.quota_remaining * 1000,  # Rough estimate
                        confidence=0.95,
                    ))

        return opportunities

    def _scan_open_source_alternatives(self) -> list[HijackOpportunity]:
        """Scan for open-source model alternatives."""
        opportunities = []

        # Check for Ollama
        if os.environ.get("OLLAMA_HOST") or self._check_ollama_available():
            opportunities.append(HijackOpportunity(
                source=FreeTierSource.OPEN_SOURCE_ALTERNATIVE,
                provider_name="ollama-local",
                provider_type=ProviderType.QWEN,  # Generic
                description="Local Ollama instance available for free inference",
                estimated_free_tokens=10_000_000,  # Effectively unlimited
                confidence=0.85,
                constraints=["Local models may be slower", "Limited model selection"],
            ))

        # Check for LM Studio
        if os.environ.get("LM_STUDIO_HOST") or self._check_lm_studio_available():
            opportunities.append(HijackOpportunity(
                source=FreeTierSource.OPEN_SOURCE_ALTERNATIVE,
                provider_name="lm-studio-local",
                provider_type=ProviderType.QWEN,
                description="Local LM Studio instance available for free inference",
                estimated_free_tokens=10_000_000,
                confidence=0.85,
                constraints=["Requires local model downloads"],
            ))

        return opportunities

    def _check_ollama_available(self) -> bool:
        """Check if Ollama is running locally."""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("localhost", 11434))
            sock.close()
            return result == 0
        except Exception as exc:
            logger.warning("Ollama availability check failed: %s", exc)
            return False

    def _check_lm_studio_available(self) -> bool:
        """Check if LM Studio is running locally."""
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("localhost", 1234))
            sock.close()
            return result == 0
        except Exception as exc:
            logger.warning("LM Studio availability check failed: %s", exc)
            return False

    def hijack_for_task(
        self,
        model_config: ModelConfig,
        min_confidence: float = 0.7,
    ) -> HijackResult | None:
        """
        Find and apply the best free tier opportunity for a task.

        Args:
            model_config: The model configuration needed for the task.
            min_confidence: Minimum confidence threshold for hijacking.

        Returns:
            HijackResult if a suitable opportunity is found and applied, None otherwise.
        """
        # Scan for opportunities if not already scanned
        if not self._opportunities:
            self.scan_for_opportunities()

        # Run safety checks
        for check in self._safety_checks:
            if not check.pre_hijack_check(self._opportunities):
                return HijackResult(
                    success=False,
                    opportunity=HijackOpportunity(
                        source=FreeTierSource.COMMUNITY_TIER,
                        provider_name="unknown",
                        provider_type=ProviderType.CLAUDE,
                        description="Safety check failed",
                        estimated_free_tokens=0,
                    ),
                    error_message=f"Safety check failed: {check.name}",
                )

        # Find best matching opportunity
        for opportunity in self._opportunities:
            if opportunity.confidence < min_confidence:
                continue

            # Check if opportunity supports the required model
            if not self._opportunity_supports_model(opportunity, model_config.model):
                continue

            # Check expiry
            if opportunity.expiry_timestamp and opportunity.expiry_timestamp < time.time():
                continue

            # Try to hijack
            result = self._apply_hijack(opportunity)
            self._hijack_history.append(result)

            if result.success:
                return result

        return None

    def _opportunity_supports_model(
        self,
        opportunity: HijackOpportunity,
        model: str,
    ) -> bool:
        """Check if an opportunity supports the required model."""
        # Open source alternatives can run most models via conversion
        if opportunity.source == FreeTierSource.OPEN_SOURCE_ALTERNATIVE:
            return True

        # Trial/promotional credits usually support standard models
        if opportunity.source in [
            FreeTierSource.NEW_PROVIDER_TRIAL,
            FreeTierSource.PROMOTIONAL_CREDITS,
        ]:
            supported_models = ["sonnet", "opus", "gpt-4", "gpt-3.5"]
            return any(m in model.lower() for m in supported_models)

        return True

    def _apply_hijack(self, opportunity: HijackOpportunity) -> HijackResult:
        """Apply a hijack opportunity by registering/updating a provider."""
        try:
            # Create provider config from opportunity
            tier = self._opportunity_to_tier(opportunity)
            provider_config = ProviderConfig(
                name=opportunity.provider_name,
                models=self._get_models_for_opportunity(opportunity),
                tier=tier,
                cost_per_1k_tokens=0.0,  # Free tier
                available=True,
                quota_remaining=opportunity.estimated_free_tokens // 1000,
            )

            # Register or update provider in router
            self.router.register_provider(provider_config)

            # Update quota tracker
            self.quota_tracker.update_quota(
                opportunity.provider_name,
                provider_config.quota_remaining or 0,
            )

            return HijackResult(
                success=True,
                opportunity=opportunity,
                provider_config=provider_config,
            )
        except Exception as e:
            return HijackResult(
                success=False,
                opportunity=opportunity,
                error_message=str(e),
            )

    def _opportunity_to_tier(self, opportunity: HijackOpportunity) -> Tier:
        """Convert HijackOpportunity source to Tier."""
        mapping = {
            FreeTierSource.NEW_PROVIDER_TRIAL: Tier.FREE,
            FreeTierSource.UNUSED_QUOTA: Tier.FREE,
            FreeTierSource.PROMOTIONAL_CREDITS: Tier.FREE,
            FreeTierSource.OPEN_SOURCE_ALTERNATIVE: Tier.FREE,
            FreeTierSource.COMMUNITY_TIER: Tier.FREE,
            FreeTierSource.EDUCATIONAL_ACCESS: Tier.FREE,
        }
        return mapping.get(opportunity.source, Tier.FREE)

    def _get_models_for_opportunity(
        self,
        opportunity: HijackOpportunity,
    ) -> dict[str, ModelConfig]:
        """Get available models for an opportunity."""
        if opportunity.source == FreeTierSource.OPEN_SOURCE_ALTERNATIVE:
            # Open source can run various models
            return {
                "llama-3": ModelConfig("llama-3", "high"),
                "mistral": ModelConfig("mistral", "high"),
                "qwen": ModelConfig("qwen", "high"),
            }
        else:
            # Default to standard models
            return {
                "claude-sonnet": ModelConfig("sonnet", "high"),
                "claude-opus": ModelConfig("opus", "max"),
            }

    def get_hijack_history(self) -> list[HijackResult]:
        """Get history of hijack attempts."""
        return self._hijack_history.copy()

    def get_active_opportunities(self) -> list[HijackOpportunity]:
        """Get currently active opportunities."""
        # Filter out expired opportunities
        now = time.time()
        return [
            opp for opp in self._opportunities
            if opp.expiry_timestamp is None or opp.expiry_timestamp > now
        ]

    def clear_opportunities(self) -> None:
        """Clear all detected opportunities."""
        self._opportunities = []


class SafetyCheck:
    """Base class for safety checks to prevent abuse."""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def pre_hijack_check(self, opportunities: list[HijackOpportunity]) -> bool:
        """
        Check if hijacking is safe.

        Args:
            opportunities: List of detected opportunities.

        Returns:
            True if hijacking is safe, False otherwise.
        """
        return True


class RateLimitSafetyCheck(SafetyCheck):
    """Prevents hijacking if rate limits would be exceeded."""

    def __init__(self, max_hijacks_per_hour: int = 10) -> None:
        self.max_hijacks_per_hour = max_hijacks_per_hour
        self._hijack_times: list[float] = []

    def pre_hijack_check(self, opportunities: list[HijackOpportunity]) -> bool:
        now = time.time()
        hour_ago = now - 3600

        # Remove old entries
        self._hijack_times = [t for t in self._hijack_times if t > hour_ago]

        if len(self._hijack_times) >= self.max_hijacks_per_hour:
            return False

        self._hijack_times.append(now)
        return True


class ModelSafetyCheck(SafetyCheck):
    """Prevents hijacking for sensitive tasks."""

    def __init__(self, blocked_models: list[str] | None = None) -> None:
        self.blocked_models = blocked_models or []

    def pre_hijack_check(self, opportunities: list[HijackOpportunity]) -> bool:
        # This check would need context about the task's model requirements
        # For now, always pass - actual implementation would check task model
        return True


class QuotaSafetyCheck(SafetyCheck):
    """Prevents hijacking if quota is too low."""

    def __init__(self, min_quota_tokens: int = 10_000) -> None:
        self.min_quota_tokens = min_quota_tokens

    def pre_hijack_check(self, opportunities: list[HijackOpportunity]) -> bool:
        # Check if any opportunity has sufficient quota
        return any(opp.estimated_free_tokens >= self.min_quota_tokens for opp in opportunities)


# Default detector configurations

def create_default_detectors() -> list[TierDetector]:
    """Create default tier detectors for common providers."""
    detectors = []

    # Anthropic environment detection
    detectors.append(EnvVarTierDetector([
        EnvVarConfig(
            env_var="ANTHROPIC_API_KEY",
            provider_type=ProviderType.CLAUDE,
            tier=ApiTier.FREE,
            description="Anthropic API key detected",
        ),
    ]))

    # OpenAI environment detection
    detectors.append(EnvVarTierDetector([
        EnvVarConfig(
            env_var="OPENAI_API_KEY",
            provider_type=ProviderType.CODEX,
            tier=ApiTier.FREE,
            description="OpenAI API key detected",
        ),
    ]))

    # Gemini environment detection
    detectors.append(EnvVarTierDetector([
        EnvVarConfig(
            env_var="GEMINI_API_KEY",
            provider_type=ProviderType.GEMINI,
            tier=ApiTier.FREE,
            description="Gemini API key detected",
        ),
    ]))

    return detectors


# Default hijacker instance
_default_hijacker: TierHijacker | None = None


def get_default_hijacker(router: TierAwareRouter | None = None) -> TierHijacker:
    """Get or create the default hijacker instance."""
    global _default_hijacker
    if _default_hijacker is None:
        from bernstein.core.router import get_default_router

        _default_hijacker = TierHijacker(
            router=router or get_default_router(),
            detectors=create_default_detectors(),
        )
        # Add default safety checks
        _default_hijacker.add_safety_check(RateLimitSafetyCheck())
        _default_hijacker.add_safety_check(QuotaSafetyCheck())
    return _default_hijacker
