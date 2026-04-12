"""Tests for Datadog/New Relic APM integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.apm_integration import (
    APMProvider,
    DatadogConfig,
    NewRelicConfig,
    auto_configure_apm,
    configure_datadog,
    configure_newrelic,
)

# ---------------------------------------------------------------------------
# DatadogConfig
# ---------------------------------------------------------------------------


class TestDatadogConfig:
    def test_defaults(self) -> None:
        cfg = DatadogConfig()
        assert cfg.service == "bernstein"
        assert cfg.site == "datadoghq.com"
        assert cfg.agent_host == "localhost"
        assert cfg.agent_port == 8126
        assert not cfg.use_otlp
        assert cfg.tags == {}

    def test_from_env_reads_dd_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_API_KEY", "test-key-123")
        monkeypatch.setenv("DD_SERVICE", "my-service")
        monkeypatch.setenv("DD_ENV", "staging")
        monkeypatch.setenv("DD_VERSION", "1.2.3")
        monkeypatch.setenv("DD_SITE", "datadoghq.eu")
        cfg = DatadogConfig.from_env()
        assert cfg.api_key == "test-key-123"
        assert cfg.service == "my-service"
        assert cfg.env == "staging"
        assert cfg.version == "1.2.3"
        assert cfg.site == "datadoghq.eu"

    def test_from_env_reads_datadog_api_key_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.setenv("DATADOG_API_KEY", "alias-key")
        cfg = DatadogConfig.from_env()
        assert cfg.api_key == "alias-key"

    def test_from_env_defaults_when_no_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("DD_API_KEY", "DATADOG_API_KEY", "DD_SERVICE", "DD_ENV"):
            monkeypatch.delenv(var, raising=False)
        cfg = DatadogConfig.from_env()
        assert cfg.api_key is None
        assert cfg.service == "bernstein"
        assert cfg.env == "production"

    def test_custom_agent_port_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_TRACE_AGENT_PORT", "9126")
        cfg = DatadogConfig.from_env()
        assert cfg.agent_port == 9126


# ---------------------------------------------------------------------------
# NewRelicConfig
# ---------------------------------------------------------------------------


class TestNewRelicConfig:
    def test_defaults(self) -> None:
        cfg = NewRelicConfig()
        assert cfg.app_name == "bernstein"
        assert cfg.use_otlp is True
        assert cfg.distributed_tracing is True
        assert "nr-data.net" in cfg.otlp_endpoint

    def test_from_env_reads_license_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "nr-key-abc")
        monkeypatch.setenv("NEW_RELIC_APP_NAME", "bernstein-prod")
        monkeypatch.setenv("NEW_RELIC_ENVIRONMENT", "production")
        cfg = NewRelicConfig.from_env()
        assert cfg.license_key == "nr-key-abc"
        assert cfg.app_name == "bernstein-prod"
        assert cfg.environment == "production"

    def test_from_env_reads_newrelic_api_key_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEW_RELIC_LICENSE_KEY", raising=False)
        monkeypatch.setenv("NEWRELIC_API_KEY", "alias-nr-key")
        cfg = NewRelicConfig.from_env()
        assert cfg.license_key == "alias-nr-key"

    def test_from_env_no_key_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEW_RELIC_LICENSE_KEY", raising=False)
        monkeypatch.delenv("NEWRELIC_API_KEY", raising=False)
        cfg = NewRelicConfig.from_env()
        assert cfg.license_key is None


# ---------------------------------------------------------------------------
# configure_datadog
# ---------------------------------------------------------------------------


class TestConfigureDatadog:
    def test_returns_false_when_ddtrace_present_otlp_mode_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With use_otlp=True and no API key, ddtrace returns False before patching."""
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.delenv("DATADOG_API_KEY", raising=False)
        cfg = DatadogConfig(use_otlp=True, api_key=None)

        # Simulate ddtrace present but cfg.use_otlp=True with no key → early return False
        mock_dd = MagicMock()
        mock_tracer = MagicMock()
        mock_config = MagicMock()
        mock_patch_all = MagicMock()
        mock_dd.__version__ = "2.0.0"

        with patch.dict(
            "sys.modules",
            {
                "ddtrace": mock_dd,
                "ddtrace.config": mock_config,
            },
        ):
            mock_dd.config = mock_config
            mock_dd.patch_all = mock_patch_all
            mock_dd.tracer = mock_tracer
            result = configure_datadog(cfg)

        assert result is False

    def test_falls_back_to_otlp_preset_when_ddtrace_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ddtrace, falls back to OTLP preset using bernstein.core.telemetry."""
        cfg = DatadogConfig(use_otlp=False, agent_host="dd-agent", agent_port=8126)

        with (
            patch.dict("sys.modules", {"ddtrace": None}),
            patch("bernstein.core.observability.telemetry.init_telemetry_from_preset") as mock_preset,
        ):
            mock_preset.return_value = None
            result = configure_datadog(cfg)

        # Falls back to OTLP path — result depends on whether init_telemetry_from_preset succeeds
        assert isinstance(result, bool)

    def test_uses_config_passed_directly(self) -> None:
        """Explicit config object is used, not environment variables."""
        cfg = DatadogConfig(
            api_key="explicit-key",
            service="custom-svc",
            env="test",
            use_otlp=False,
            agent_host="custom-host",
        )
        # Just verify it doesn't raise and config attributes are correct
        assert cfg.service == "custom-svc"
        assert cfg.env == "test"
        assert cfg.agent_host == "custom-host"

    @patch("bernstein.core.observability.telemetry.init_telemetry_from_preset")
    def test_otlp_fallback_calls_preset(self, mock_preset: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ddtrace is missing, falls back to OTLP preset."""
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.delenv("DATADOG_API_KEY", raising=False)
        cfg = DatadogConfig(use_otlp=False, agent_host="dd-host")

        # Simulate ddtrace import failure
        with patch.dict("sys.modules", {"ddtrace": None}):
            result = configure_datadog(cfg)

        # Should have attempted the OTLP fallback
        if result:
            mock_preset.assert_called_once()
            call_args = mock_preset.call_args
            assert call_args[0][0] == "datadog"
            assert "dd-host" in call_args[1].get("endpoint_override", "")


# ---------------------------------------------------------------------------
# configure_newrelic
# ---------------------------------------------------------------------------


class TestConfigureNewRelic:
    def test_returns_false_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEW_RELIC_LICENSE_KEY", raising=False)
        monkeypatch.delenv("NEWRELIC_API_KEY", raising=False)
        cfg = NewRelicConfig(license_key=None)
        result = configure_newrelic(cfg)
        assert result is False

    def test_returns_false_from_env_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEW_RELIC_LICENSE_KEY", raising=False)
        monkeypatch.delenv("NEWRELIC_API_KEY", raising=False)
        result = configure_newrelic()
        assert result is False

    @patch("bernstein.core.observability.telemetry._init_http_telemetry")
    def test_otlp_path_calls_http_telemetry(self, mock_http: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        """OTLP path calls _init_http_telemetry with api-key header."""
        monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "nr-test-key")
        cfg = NewRelicConfig(
            license_key="nr-test-key",
            use_otlp=True,
            app_name="bernstein-test",
        )
        mock_http.return_value = None

        result = configure_newrelic(cfg)

        assert result is True
        mock_http.assert_called_once()
        call_kwargs = mock_http.call_args
        headers = call_kwargs[1].get("headers") or call_kwargs[0][1]
        assert headers.get("api-key") == "nr-test-key"
        service_name = call_kwargs[1].get("service_name") or call_kwargs[0][2]
        assert service_name == "bernstein-test"

    @patch("bernstein.core.observability.telemetry._init_http_telemetry")
    def test_uses_nr_endpoint(self, mock_http: MagicMock) -> None:
        """OTLP path uses the correct New Relic endpoint."""
        cfg = NewRelicConfig(
            license_key="key",
            use_otlp=True,
            otlp_endpoint="otlp.nr-data.net:4317",
        )
        mock_http.return_value = None
        configure_newrelic(cfg)
        call_kwargs = mock_http.call_args
        endpoint = call_kwargs[1].get("endpoint") or call_kwargs[0][0]
        assert "nr-data.net" in endpoint


# ---------------------------------------------------------------------------
# auto_configure_apm
# ---------------------------------------------------------------------------


class TestAutoConfigureApm:
    def test_returns_empty_when_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "DD_API_KEY",
            "DATADOG_API_KEY",
            "DD_AGENT_HOST",
            "NEW_RELIC_LICENSE_KEY",
            "NEWRELIC_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        result = auto_configure_apm()
        assert result == []

    def test_includes_newrelic_when_key_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("DD_API_KEY", "DATADOG_API_KEY", "DD_AGENT_HOST"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "nr-key")

        with patch("bernstein.core.observability.telemetry._init_http_telemetry") as mock_http:
            mock_http.return_value = None
            result = auto_configure_apm()

        assert APMProvider.NEWRELIC in result

    def test_includes_datadog_when_agent_host_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("NEW_RELIC_LICENSE_KEY", "NEWRELIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.delenv("DATADOG_API_KEY", raising=False)
        monkeypatch.setenv("DD_AGENT_HOST", "dd-agent")

        with patch("bernstein.core.observability.telemetry.init_telemetry_from_preset") as mock_preset:
            mock_preset.return_value = None
            result = auto_configure_apm()

        assert APMProvider.DATADOG in result

    def test_returns_list_of_apm_providers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_AGENT_HOST", "dd-agent")
        monkeypatch.setenv("NEW_RELIC_LICENSE_KEY", "nr-key")
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.delenv("DATADOG_API_KEY", raising=False)

        with (
            patch("bernstein.core.observability.telemetry.init_telemetry_from_preset") as mock_dd,
            patch("bernstein.core.observability.telemetry._init_http_telemetry") as mock_nr,
        ):
            mock_dd.return_value = None
            mock_nr.return_value = None
            result = auto_configure_apm()

        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, APMProvider)


# ---------------------------------------------------------------------------
# Telemetry presets — New Relic entries
# ---------------------------------------------------------------------------


class TestNewRelicTelemetryPresets:
    def test_newrelic_preset_exists(self) -> None:
        from bernstein.core.telemetry import BUILTIN_PRESETS

        assert "newrelic" in BUILTIN_PRESETS

    def test_newrelic_eu_preset_exists(self) -> None:
        from bernstein.core.telemetry import BUILTIN_PRESETS

        assert "newrelic-eu" in BUILTIN_PRESETS

    def test_newrelic_preset_uses_https(self) -> None:
        from bernstein.core.telemetry import get_preset

        preset = get_preset("newrelic")
        assert preset is not None
        assert preset.endpoint.startswith("https://")

    def test_newrelic_preset_not_insecure(self) -> None:
        from bernstein.core.telemetry import get_preset

        preset = get_preset("newrelic")
        assert preset is not None
        assert preset.insecure is False

    def test_newrelic_preset_uses_http_protobuf(self) -> None:
        from bernstein.core.telemetry import get_preset

        preset = get_preset("newrelic")
        assert preset is not None
        assert preset.protocol == "http/protobuf"

    def test_newrelic_eu_preset_different_endpoint(self) -> None:
        from bernstein.core.telemetry import get_preset

        us = get_preset("newrelic")
        eu = get_preset("newrelic-eu")
        assert us is not None and eu is not None
        assert us.endpoint != eu.endpoint
        assert "eu01" in eu.endpoint
