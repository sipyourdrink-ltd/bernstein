"""Tests for quota probing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.models import ApiTier, ApiTierInfo, ProviderType, RateLimit
from bernstein.core.quota_probe import probe_provider_quota


def test_probe_provider_quota_returns_snapshot() -> None:
    adapter = MagicMock()
    adapter.detect_tier.return_value = ApiTierInfo(
        provider=ProviderType.CODEX,
        tier=ApiTier.PRO,
        rate_limit=RateLimit(requests_per_minute=120, tokens_per_minute=10_000),
        is_active=True,
    )

    with patch("bernstein.core.quota_probe.get_adapter", return_value=adapter):
        snapshot = probe_provider_quota("codex", "gpt-5.4-mini")

    assert snapshot is not None
    assert snapshot.provider == "codex"
    assert snapshot.requests_per_minute == 120
    assert snapshot.tokens_per_minute == 10_000


def test_probe_provider_quota_returns_none_when_adapter_has_no_data() -> None:
    adapter = MagicMock()
    adapter.detect_tier.return_value = None

    with patch("bernstein.core.quota_probe.get_adapter", return_value=adapter):
        snapshot = probe_provider_quota("codex", "gpt-5.4-mini")

    assert snapshot is None
