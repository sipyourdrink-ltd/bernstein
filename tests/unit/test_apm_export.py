"""Tests for Datadog / New Relic APM payload generation.

Covers APMProvider, APMConfig, APMEvent, APMExporter, default config
factories, and the integration guide renderer.
"""

from __future__ import annotations

import os
import time

import pytest

from bernstein.core.observability.apm_export import (
    APMConfig,
    APMEvent,
    APMExporter,
    APMProvider,
    get_datadog_config,
    get_newrelic_config,
    render_integration_guide,
)

# ---------------------------------------------------------------------------
# APMProvider
# ---------------------------------------------------------------------------


class TestAPMProvider:
    """APMProvider StrEnum values and membership."""

    def test_datadog_value(self) -> None:
        assert APMProvider.DATADOG == "datadog"

    def test_newrelic_value(self) -> None:
        assert APMProvider.NEW_RELIC == "new_relic"

    def test_generic_value(self) -> None:
        assert APMProvider.GENERIC == "generic"

    def test_is_str_subclass(self) -> None:
        assert isinstance(APMProvider.DATADOG, str)


# ---------------------------------------------------------------------------
# APMConfig
# ---------------------------------------------------------------------------


class TestAPMConfig:
    """APMConfig frozen dataclass behaviour."""

    def test_frozen(self) -> None:
        cfg = APMConfig(provider=APMProvider.DATADOG, api_key_env_var="DD_API_KEY")
        with pytest.raises(AttributeError):
            cfg.provider = APMProvider.GENERIC  # type: ignore[misc]

    def test_defaults(self) -> None:
        cfg = APMConfig(provider=APMProvider.GENERIC, api_key_env_var="KEY")
        assert cfg.endpoint is None
        assert cfg.service_name == "bernstein"
        assert cfg.tags == {}

    def test_custom_tags(self) -> None:
        cfg = APMConfig(
            provider=APMProvider.DATADOG,
            api_key_env_var="DD_API_KEY",
            tags={"env": "staging"},
        )
        assert cfg.tags == {"env": "staging"}


# ---------------------------------------------------------------------------
# APMEvent
# ---------------------------------------------------------------------------


class TestAPMEvent:
    """APMEvent frozen dataclass behaviour."""

    def test_frozen(self) -> None:
        ev = APMEvent(name="x", timestamp=1.0, duration_ms=0.0)
        with pytest.raises(AttributeError):
            ev.name = "y"  # type: ignore[misc]

    def test_defaults(self) -> None:
        ev = APMEvent(name="ping", timestamp=100.0, duration_ms=5.0)
        assert ev.attributes == {}
        assert ev.provider == APMProvider.GENERIC

    def test_with_provider(self) -> None:
        ev = APMEvent(
            name="span",
            timestamp=1.0,
            duration_ms=10.0,
            provider=APMProvider.DATADOG,
        )
        assert ev.provider == APMProvider.DATADOG


# ---------------------------------------------------------------------------
# Default config factories
# ---------------------------------------------------------------------------


class TestDefaultConfigs:
    """get_datadog_config and get_newrelic_config factories."""

    def test_datadog_defaults(self) -> None:
        cfg = get_datadog_config()
        assert cfg.provider == APMProvider.DATADOG
        assert cfg.api_key_env_var == "DD_API_KEY"
        assert cfg.service_name == "bernstein"
        assert cfg.endpoint is not None
        assert "datadoghq.com" in cfg.endpoint

    def test_newrelic_defaults(self) -> None:
        cfg = get_newrelic_config()
        assert cfg.provider == APMProvider.NEW_RELIC
        assert cfg.api_key_env_var == "NEW_RELIC_LICENSE_KEY"
        assert cfg.service_name == "bernstein"
        assert cfg.endpoint is not None
        assert "newrelic.com" in cfg.endpoint

    def test_datadog_custom(self) -> None:
        cfg = get_datadog_config(
            service_name="my-svc",
            endpoint="https://custom.dd",
            tags={"team": "core"},
        )
        assert cfg.service_name == "my-svc"
        assert cfg.endpoint == "https://custom.dd"
        assert cfg.tags == {"team": "core"}

    def test_newrelic_custom(self) -> None:
        cfg = get_newrelic_config(
            service_name="my-svc",
            endpoint="https://custom.nr",
            tags={"team": "ops"},
        )
        assert cfg.service_name == "my-svc"
        assert cfg.endpoint == "https://custom.nr"
        assert cfg.tags == {"team": "ops"}


# ---------------------------------------------------------------------------
# APMExporter — span / metric / log creation
# ---------------------------------------------------------------------------


class TestAPMExporterEventCreation:
    """Tests for export_span, export_metric, export_log."""

    def _dd_exporter(self) -> APMExporter:
        return APMExporter(get_datadog_config())

    def _nr_exporter(self) -> APMExporter:
        return APMExporter(get_newrelic_config())

    # -- span ----------------------------------------------------------------

    def test_export_span_sets_type(self) -> None:
        ev = self._dd_exporter().export_span("op", duration_ms=42.0)
        assert ev.attributes["type"] == "span"

    def test_export_span_sets_service(self) -> None:
        ev = self._dd_exporter().export_span("op", duration_ms=1.0)
        assert ev.attributes["service"] == "bernstein"

    def test_export_span_merges_attributes(self) -> None:
        ev = self._dd_exporter().export_span("op", duration_ms=1.0, attributes={"task_id": "t-1"})
        assert ev.attributes["task_id"] == "t-1"

    def test_export_span_duration(self) -> None:
        ev = self._dd_exporter().export_span("op", duration_ms=99.9)
        assert ev.duration_ms == pytest.approx(99.9)

    def test_export_span_provider(self) -> None:
        ev = self._dd_exporter().export_span("op", duration_ms=1.0)
        assert ev.provider == APMProvider.DATADOG

    # -- metric --------------------------------------------------------------

    def test_export_metric_sets_type(self) -> None:
        ev = self._dd_exporter().export_metric("cpu", 0.75)
        assert ev.attributes["type"] == "metric"

    def test_export_metric_value(self) -> None:
        ev = self._dd_exporter().export_metric("cpu", 0.75)
        assert ev.attributes["metric_value"] == pytest.approx(0.75)

    def test_export_metric_merges_tags(self) -> None:
        ev = self._dd_exporter().export_metric("cpu", 0.5, tags={"host": "web-1"})
        assert ev.attributes["host"] == "web-1"

    def test_export_metric_zero_duration(self) -> None:
        ev = self._dd_exporter().export_metric("cpu", 0.5)
        assert ev.duration_ms == pytest.approx(0.0)

    # -- log -----------------------------------------------------------------

    def test_export_log_sets_type(self) -> None:
        ev = self._dd_exporter().export_log("error", "boom")
        assert ev.attributes["type"] == "log"

    def test_export_log_level(self) -> None:
        ev = self._dd_exporter().export_log("warn", "hmm")
        assert ev.attributes["level"] == "warn"

    def test_export_log_message_is_name(self) -> None:
        ev = self._dd_exporter().export_log("info", "all good")
        assert ev.name == "all good"


# ---------------------------------------------------------------------------
# APMExporter — build_payload (Datadog)
# ---------------------------------------------------------------------------


class TestBuildPayloadDatadog:
    """Datadog-specific payload structure."""

    def _exporter(self) -> APMExporter:
        return APMExporter(get_datadog_config())

    def test_empty_payload(self) -> None:
        p = self._exporter().build_payload([])
        assert p == {"series": [], "traces": [], "logs": []}

    def test_span_in_traces(self) -> None:
        ex = self._exporter()
        span = ex.export_span("task.run", duration_ms=100.0)
        p = ex.build_payload([span])
        assert len(p["traces"]) == 1
        assert p["traces"][0]["name"] == "task.run"

    def test_span_duration_nanoseconds(self) -> None:
        ex = self._exporter()
        span = ex.export_span("op", duration_ms=1.0)
        p = ex.build_payload([span])
        assert p["traces"][0]["duration"] == 1_000_000

    def test_span_start_nanoseconds(self) -> None:
        ex = self._exporter()
        span = ex.export_span("op", duration_ms=1.0)
        p = ex.build_payload([span])
        # start should be within a few seconds of now, in nanoseconds
        now_ns = int(time.time() * 1_000_000_000)
        assert abs(p["traces"][0]["start"] - now_ns) < 5_000_000_000

    def test_metric_in_series(self) -> None:
        ex = self._exporter()
        m = ex.export_metric("cpu", 0.85)
        p = ex.build_payload([m])
        assert len(p["series"]) == 1
        assert p["series"][0]["metric"] == "cpu"

    def test_metric_points_format(self) -> None:
        ex = self._exporter()
        m = ex.export_metric("mem", 1024.0)
        p = ex.build_payload([m])
        points = p["series"][0]["points"]
        assert len(points) == 1
        assert len(points[0]) == 2  # [timestamp, value]
        assert points[0][1] == pytest.approx(1024.0)

    def test_log_in_logs(self) -> None:
        ex = self._exporter()
        log = ex.export_log("error", "disk full")
        p = ex.build_payload([log])
        assert len(p["logs"]) == 1
        assert p["logs"][0]["message"] == "disk full"
        assert p["logs"][0]["status"] == "error"
        assert p["logs"][0]["ddsource"] == "bernstein"

    def test_mixed_events(self) -> None:
        ex = self._exporter()
        span = ex.export_span("op", duration_ms=10.0)
        metric = ex.export_metric("cpu", 0.5)
        log = ex.export_log("info", "ok")
        p = ex.build_payload([span, metric, log])
        assert len(p["traces"]) == 1
        assert len(p["series"]) == 1
        assert len(p["logs"]) == 1


# ---------------------------------------------------------------------------
# APMExporter — build_payload (New Relic)
# ---------------------------------------------------------------------------


class TestBuildPayloadNewRelic:
    """New Relic-specific payload structure."""

    def _exporter(self) -> APMExporter:
        return APMExporter(get_newrelic_config())

    def test_empty_payload(self) -> None:
        p = self._exporter().build_payload([])
        assert p == {"metrics": [], "spans": [], "logs": []}

    def test_span_in_spans(self) -> None:
        ex = self._exporter()
        span = ex.export_span("task.run", duration_ms=50.0)
        p = ex.build_payload([span])
        assert len(p["spans"]) == 1
        assert p["spans"][0]["attributes"]["name"] == "task.run"

    def test_span_duration_ms(self) -> None:
        ex = self._exporter()
        span = ex.export_span("op", duration_ms=77.0)
        p = ex.build_payload([span])
        assert p["spans"][0]["attributes"]["duration.ms"] == pytest.approx(77.0)

    def test_metric_wrapped_in_list(self) -> None:
        ex = self._exporter()
        m = ex.export_metric("cpu", 0.9)
        p = ex.build_payload([m])
        assert len(p["metrics"]) == 1
        inner = p["metrics"][0]["metrics"]
        assert len(inner) == 1
        assert inner[0]["name"] == "cpu"
        assert inner[0]["value"] == pytest.approx(0.9)

    def test_log_wrapped_in_list(self) -> None:
        ex = self._exporter()
        log = ex.export_log("info", "started")
        p = ex.build_payload([log])
        assert len(p["logs"]) == 1
        inner = p["logs"][0]["logs"]
        assert len(inner) == 1
        assert inner[0]["message"] == "started"

    def test_mixed_events(self) -> None:
        ex = self._exporter()
        span = ex.export_span("op", duration_ms=10.0)
        metric = ex.export_metric("mem", 512.0)
        log = ex.export_log("warn", "low disk")
        p = ex.build_payload([span, metric, log])
        assert len(p["spans"]) == 1
        assert len(p["metrics"]) == 1
        assert len(p["logs"]) == 1


# ---------------------------------------------------------------------------
# APMExporter — build_payload (Generic)
# ---------------------------------------------------------------------------


class TestBuildPayloadGeneric:
    """Generic provider payload structure."""

    def test_generic_payload_structure(self) -> None:
        cfg = APMConfig(provider=APMProvider.GENERIC, api_key_env_var="KEY")
        ex = APMExporter(cfg)
        ev = APMEvent(name="ping", timestamp=1.0, duration_ms=5.0)
        p = ex.build_payload([ev])
        assert p["service"] == "bernstein"
        assert len(p["events"]) == 1
        assert p["events"][0]["name"] == "ping"


# ---------------------------------------------------------------------------
# APMExporter — get_headers
# ---------------------------------------------------------------------------


class TestGetHeaders:
    """Provider-specific auth headers."""

    def test_datadog_header_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_API_KEY", "dd-secret")
        ex = APMExporter(get_datadog_config())
        headers = ex.get_headers()
        assert headers["DD-API-KEY"] == "dd-secret"
        assert headers["Content-Type"] == "application/json"

    def test_newrelic_header_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "nr-secret")
        ex = APMExporter(get_newrelic_config())
        headers = ex.get_headers()
        assert headers["Api-Key"] == "nr-secret"
        assert headers["Content-Type"] == "application/json"

    def test_generic_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APM_API_KEY", "tok-123")
        cfg = APMConfig(provider=APMProvider.GENERIC, api_key_env_var="APM_API_KEY")
        ex = APMExporter(cfg)
        headers = ex.get_headers()
        assert headers["Authorization"] == "Bearer tok-123"

    def test_missing_env_var_returns_empty(self) -> None:
        # Ensure env var is not set
        env_var = "_TEST_MISSING_KEY_12345"
        os.environ.pop(env_var, None)
        cfg = APMConfig(provider=APMProvider.GENERIC, api_key_env_var=env_var)
        ex = APMExporter(cfg)
        headers = ex.get_headers()
        assert headers["Authorization"] == "Bearer "


# ---------------------------------------------------------------------------
# render_integration_guide
# ---------------------------------------------------------------------------


class TestRenderIntegrationGuide:
    """Integration guide rendering."""

    def test_datadog_guide_contains_dd_api_key(self) -> None:
        guide = render_integration_guide(APMProvider.DATADOG)
        assert "DD_API_KEY" in guide

    def test_newrelic_guide_contains_license_key(self) -> None:
        guide = render_integration_guide(APMProvider.NEW_RELIC)
        assert "NEW_RELIC_LICENSE_KEY" in guide

    def test_generic_guide_mentions_generic(self) -> None:
        guide = render_integration_guide(APMProvider.GENERIC)
        assert "generic" in guide.lower()

    def test_guides_are_markdown(self) -> None:
        for provider in APMProvider:
            guide = render_integration_guide(provider)
            assert guide.startswith("#")


# ---------------------------------------------------------------------------
# Config tags propagation
# ---------------------------------------------------------------------------


class TestTagPropagation:
    """Verify that config-level tags propagate into events and payloads."""

    def test_config_tags_in_span(self) -> None:
        cfg = get_datadog_config(tags={"env": "prod"})
        ex = APMExporter(cfg)
        ev = ex.export_span("op", duration_ms=1.0)
        assert ev.attributes["env"] == "prod"

    def test_config_tags_in_metric(self) -> None:
        cfg = get_newrelic_config(tags={"region": "us-east-1"})
        ex = APMExporter(cfg)
        ev = ex.export_metric("latency", 42.0)
        assert ev.attributes["region"] == "us-east-1"

    def test_config_tags_in_log(self) -> None:
        cfg = get_datadog_config(tags={"team": "infra"})
        ex = APMExporter(cfg)
        ev = ex.export_log("info", "booted")
        assert ev.attributes["team"] == "infra"
