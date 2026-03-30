"""Tests for quota polling and persistence."""

from __future__ import annotations

import json
from unittest.mock import patch

from bernstein.core.models import ModelConfig
from bernstein.core.quota_poller import QuotaPoller
from bernstein.core.quota_probe import QuotaSnapshot
from bernstein.core.router import ProviderConfig, Tier, TierAwareRouter


def test_poll_once_updates_provider_snapshots_and_writes_status(tmp_path) -> None:
    router = TierAwareRouter()
    router.register_provider(
        ProviderConfig(
            name="codex",
            models={"gpt-5.4-mini": ModelConfig("gpt-5.4-mini", "high")},
            tier=Tier.FREE,
            cost_per_1k_tokens=0.0,
        )
    )
    poller = QuotaPoller(router=router, workdir=tmp_path, interval_seconds=300.0)

    with patch(
        "bernstein.core.quota_poller.probe_provider_quota",
        return_value=QuotaSnapshot(
            provider="codex",
            model="gpt-5.4-mini",
            source="test",
            observed_at=123.0,
            available=True,
            requests_per_minute=120,
        ),
    ):
        payload = poller.poll_once()

    assert payload["providers"]["codex"]["quota_snapshot"]["requests_per_minute"] == 120
    assert router.state.providers["codex"].quota_snapshot is not None
    status_path = tmp_path / ".sdd" / "runtime" / "provider_status.json"
    assert json.loads(status_path.read_text())["providers"]["codex"]["health"] == "healthy"


def test_maybe_poll_respects_interval(tmp_path) -> None:
    router = TierAwareRouter()
    poller = QuotaPoller(router=router, workdir=tmp_path, interval_seconds=300.0)

    with patch.object(poller, "poll_once") as poll_once:
        assert poller.maybe_poll() is True
        assert poller.maybe_poll() is False

    assert poll_once.call_count == 1
