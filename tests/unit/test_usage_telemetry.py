"""Tests for opt-in usage telemetry consent and event recording."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.usage_telemetry import (
    TelemetryConfig,
    TelemetryConsent,
    load_consent,
    record_usage_event,
    save_consent,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Consent persistence
# ---------------------------------------------------------------------------


def test_load_consent_missing_file(tmp_path: Path) -> None:
    """Missing telemetry.json should return UNDECIDED."""
    assert load_consent(tmp_path) is TelemetryConsent.UNDECIDED


def test_save_and_load_opt_in(tmp_path: Path) -> None:
    save_consent(tmp_path, TelemetryConsent.OPT_IN)
    assert load_consent(tmp_path) is TelemetryConsent.OPT_IN


def test_save_and_load_opt_out(tmp_path: Path) -> None:
    save_consent(tmp_path, TelemetryConsent.OPT_OUT)
    assert load_consent(tmp_path) is TelemetryConsent.OPT_OUT


def test_save_creates_directory(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "config"
    save_consent(nested, TelemetryConsent.OPT_IN)
    assert (nested / "telemetry.json").exists()
    assert load_consent(nested) is TelemetryConsent.OPT_IN


def test_load_consent_corrupt_file(tmp_path: Path) -> None:
    (tmp_path / "telemetry.json").write_text("not json!!!", encoding="utf-8")
    assert load_consent(tmp_path) is TelemetryConsent.UNDECIDED


def test_load_consent_invalid_value(tmp_path: Path) -> None:
    (tmp_path / "telemetry.json").write_text(
        json.dumps({"consent": "banana"}),
        encoding="utf-8",
    )
    assert load_consent(tmp_path) is TelemetryConsent.UNDECIDED


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


def test_record_event_opt_in(tmp_path: Path) -> None:
    config = TelemetryConfig(consent=TelemetryConsent.OPT_IN, anonymous_id="test-id-123")
    record_usage_event(config, "run.start", {"plan": "my-plan.yaml"}, log_dir=tmp_path)

    log_path = tmp_path / "usage_events.jsonl"
    assert log_path.exists()

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["event"] == "run.start"
    assert record["anonymous_id"] == "test-id-123"
    assert record["properties"]["plan"] == "my-plan.yaml"
    assert "timestamp" in record


def test_record_event_opt_out_is_noop(tmp_path: Path) -> None:
    config = TelemetryConfig(consent=TelemetryConsent.OPT_OUT)
    record_usage_event(config, "run.start", {"x": 1}, log_dir=tmp_path)

    log_path = tmp_path / "usage_events.jsonl"
    assert not log_path.exists()


def test_record_event_undecided_is_noop(tmp_path: Path) -> None:
    config = TelemetryConfig(consent=TelemetryConsent.UNDECIDED)
    record_usage_event(config, "run.start", {"x": 1}, log_dir=tmp_path)

    log_path = tmp_path / "usage_events.jsonl"
    assert not log_path.exists()


def test_record_multiple_events(tmp_path: Path) -> None:
    config = TelemetryConfig(consent=TelemetryConsent.OPT_IN, anonymous_id="multi")
    record_usage_event(config, "run.start", {}, log_dir=tmp_path)
    record_usage_event(config, "task.complete", {"task_id": "t1"}, log_dir=tmp_path)

    log_path = tmp_path / "usage_events.jsonl"
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    events = [json.loads(line)["event"] for line in lines]
    assert events == ["run.start", "task.complete"]


# ---------------------------------------------------------------------------
# TelemetryConfig defaults
# ---------------------------------------------------------------------------


def test_config_generates_anonymous_id() -> None:
    config = TelemetryConfig(consent=TelemetryConsent.OPT_IN)
    assert len(config.anonymous_id) == 32  # uuid4 hex
    assert config.events_endpoint == ""
