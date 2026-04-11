"""Tests for ProviderLatencyTracker (ROAD-155)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.provider_latency import (
    DegradationAlert,
    ProviderLatencyTracker,
    _make_key,
    _split_key,
)


class TestKeyHelpers:
    """Test key construction helpers."""

    def test_make_key(self) -> None:
        assert _make_key("anthropic", "claude-sonnet") == "anthropic:claude-sonnet"

    def test_split_key(self) -> None:
        provider, model = _split_key("anthropic:claude-sonnet")
        assert provider == "anthropic"
        assert model == "claude-sonnet"

    def test_split_key_no_colon(self) -> None:
        provider, model = _split_key("anthropic")
        assert provider == "anthropic"
        assert model == ""

    def test_split_key_model_with_colon(self) -> None:
        # Only splits on first colon
        provider, model = _split_key("openai:gpt-4:preview")
        assert provider == "openai"
        assert model == "gpt-4:preview"


class TestProviderLatencyTracker:
    """Tests for ProviderLatencyTracker."""

    def test_record_and_retrieve_percentiles(self, tmp_path: Path) -> None:
        """Recording samples and reading back p50/p95/p99."""
        tracker = ProviderLatencyTracker(tmp_path)

        for ms in range(1, 101):
            tracker.record("anthropic", "sonnet", float(ms))

        percs = tracker.get_percentiles("anthropic", "sonnet")
        assert percs.sample_count == 100
        assert 49.0 <= percs.p50_ms <= 51.0
        assert 94.0 <= percs.p95_ms <= 96.0
        assert 98.0 <= percs.p99_ms <= 100.0
        assert percs.provider == "anthropic"
        assert percs.model == "sonnet"

    def test_unknown_provider_returns_zeros(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        percs = tracker.get_percentiles("unknown", "model")
        assert percs.p50_ms == 0.0
        assert percs.p95_ms == 0.0
        assert percs.p99_ms == 0.0
        assert percs.sample_count == 0

    def test_multiple_providers_tracked_independently(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        # Anthropic: low latency
        for _ in range(20):
            tracker.record("anthropic", "sonnet", 100.0)
        # OpenAI: high latency
        for _ in range(20):
            tracker.record("openai", "gpt-4", 1000.0)

        a = tracker.get_percentiles("anthropic", "sonnet")
        b = tracker.get_percentiles("openai", "gpt-4")
        assert a.p50_ms == pytest.approx(100.0)
        assert b.p50_ms == pytest.approx(1000.0)

    def test_all_percentiles_returns_all_keys(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        tracker.record("anthropic", "sonnet", 100.0)
        tracker.record("openai", "gpt-4", 200.0)
        tracker.record("anthropic", "haiku", 50.0)

        all_p = tracker.all_percentiles()
        keys = {(p.provider, p.model) for p in all_p}
        assert ("anthropic", "sonnet") in keys
        assert ("openai", "gpt-4") in keys
        assert ("anthropic", "haiku") in keys

    def test_negative_latency_ignored(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        result = tracker.record("anthropic", "sonnet", -1.0)
        assert result is None
        percs = tracker.get_percentiles("anthropic", "sonnet")
        assert percs.sample_count == 0

    def test_persists_samples_to_jsonl(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        tracker.record("anthropic", "sonnet", 150.0)

        jsonl_files = list(tmp_path.glob("provider_latency_*.jsonl"))
        assert len(jsonl_files) == 1

        lines = jsonl_files[0].read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["provider"] == "anthropic"
        assert record["model"] == "sonnet"
        assert record["latency_ms"] == pytest.approx(150.0)

    def test_get_history_filters_by_provider(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        tracker.record("anthropic", "sonnet", 100.0)
        tracker.record("openai", "gpt-4", 200.0)

        history = tracker.get_history(provider="anthropic")
        assert all(r["provider"] == "anthropic" for r in history)
        assert len(history) >= 1

    def test_get_history_filters_by_model(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        tracker.record("anthropic", "sonnet", 100.0)
        tracker.record("anthropic", "haiku", 50.0)

        history = tracker.get_history(model="haiku")
        assert all(r["model"] == "haiku" for r in history)
        assert len(history) >= 1

    def test_degradation_alert_triggered(self, tmp_path: Path) -> None:
        """Alert when p99 exceeds 2× baseline."""
        # We need to build a baseline first (≥10 samples)
        tracker = ProviderLatencyTracker(tmp_path, degradation_threshold=2.0)

        # Build baseline: 10 samples at 100ms
        for _ in range(20):
            tracker.record("anthropic", "sonnet", 100.0)

        # Force baseline to be set with a known value
        from bernstein.core.provider_latency import _make_key
        key = _make_key("anthropic", "sonnet")
        tracker._baseline_p99[key] = 100.0

        # Now inject high-latency samples to spike p99 > 200ms
        alert: DegradationAlert | None = None
        for _ in range(30):
            alert = tracker.record("anthropic", "sonnet", 400.0)

        # After filling the window with 400ms samples, p99 >> 2× baseline
        assert alert is not None
        assert alert.provider == "anthropic"
        assert alert.model == "sonnet"
        assert alert.ratio >= 2.0
        assert "degraded" in alert.message

    def test_no_alert_before_min_samples(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        # Only 5 samples — below the 10-sample minimum
        for _ in range(5):
            result = tracker.record("anthropic", "sonnet", 9999.0)
        # No alert should fire without sufficient baseline
        assert result is None

    def test_latency_percentiles_to_dict(self, tmp_path: Path) -> None:
        tracker = ProviderLatencyTracker(tmp_path)
        for ms in range(1, 21):
            tracker.record("anthropic", "sonnet", float(ms * 10))

        percs = tracker.get_percentiles("anthropic", "sonnet")
        d = percs.to_dict()
        assert d["provider"] == "anthropic"
        assert d["model"] == "sonnet"
        assert "p50_ms" in d
        assert "p95_ms" in d
        assert "p99_ms" in d
        assert "sample_count" in d
        assert "baseline_p99_ms" in d

    def test_baseline_loaded_from_persisted_data(self, tmp_path: Path) -> None:
        """Baseline is reconstructed from existing JSONL files on init."""
        from datetime import UTC, datetime

        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        jsonl = tmp_path / f"provider_latency_{date_str}.jsonl"
        ts = time.time() - 3600  # 1 hour ago

        # Write 15 samples at 200ms
        with jsonl.open("w") as fh:
            for _ in range(15):
                fh.write(
                    json.dumps({"timestamp": ts, "provider": "openai", "model": "gpt-4", "latency_ms": 200.0}) + "\n"
                )

        tracker = ProviderLatencyTracker(tmp_path)
        key = _make_key("openai", "gpt-4")
        assert key in tracker._baseline_p99
        assert tracker._baseline_p99[key] == pytest.approx(200.0, abs=1.0)
