"""Native APM integration for Datadog and New Relic.

Provides vendor-specific APM setup beyond the generic OTLP export already
available in :mod:`bernstein.core.telemetry`.  Use this module when you need:

* Datadog APM — span tags, service maps, infrastructure correlation via
  ``ddtrace``.
* New Relic APM — distributed tracing, error inbox, and logs-in-context via
  the ``newrelic`` agent SDK.
* Forwarding OTLP signals to Datadog/New Relic *without* a local agent (direct
  API key path — useful for containers / serverless deployments).

Quick start::

    from bernstein.core.apm_integration import configure_datadog, configure_newrelic

    # Using ddtrace (requires DD_API_KEY env var and ddtrace package)
    configure_datadog()

    # Using New Relic agent SDK (requires NEW_RELIC_LICENSE_KEY env var)
    configure_newrelic()

    # Or let auto-configure pick up whatever is available
    from bernstein.core.apm_integration import auto_configure_apm
    auto_configure_apm()

Environment variables consumed:

Datadog
    ``DD_API_KEY``            — Datadog API key (required for direct ingest)
    ``DD_SITE``               — Datadog intake site (default: datadoghq.com)
    ``DD_SERVICE``            — service name (default: bernstein)
    ``DD_ENV``                — deployment environment (default: production)
    ``DD_VERSION``            — service version tag
    ``DD_AGENT_HOST``         — Datadog Agent host for ddtrace (default: localhost)
    ``DD_TRACE_AGENT_PORT``   — Datadog Agent trace port (default: 8126)
    ``DATADOG_API_KEY``       — alias for ``DD_API_KEY`` (both accepted)

New Relic
    ``NEW_RELIC_LICENSE_KEY`` — New Relic ingest licence / API key (required)
    ``NEW_RELIC_APP_NAME``    — app name in New Relic UI (default: bernstein)
    ``NEW_RELIC_ENVIRONMENT`` — deployment environment label
    ``NEWRELIC_API_KEY``      — alias for ``NEW_RELIC_LICENSE_KEY`` (both accepted)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVICE_NAME = "bernstein"
_DD_DEFAULT_SITE = "datadoghq.com"
_DD_DEFAULT_AGENT_HOST = "localhost"
_DD_DEFAULT_AGENT_PORT = 8126

# New Relic OTLP endpoint — used when forwarding via OTLP instead of the SDK
_NR_OTLP_ENDPOINT = "otlp.nr-data.net:4317"
_NR_OTLP_ENDPOINT_EU = "otlp.eu01.nr-data.net:4317"


# ---------------------------------------------------------------------------
# APM provider enum
# ---------------------------------------------------------------------------


class APMProvider(Enum):
    """Supported APM provider backends."""

    DATADOG = "datadog"
    NEWRELIC = "newrelic"


# ---------------------------------------------------------------------------
# Configuration dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DatadogConfig:
    """Configuration for Datadog APM integration.

    Attributes:
        api_key: Datadog API key.  Falls back to ``DD_API_KEY`` / ``DATADOG_API_KEY``
            environment variables when ``None``.
        site: Datadog intake site (e.g. ``"datadoghq.com"`` or ``"datadoghq.eu"``).
        service: Service name reported to Datadog.
        env: Deployment environment (dev / staging / production).
        version: Service version string — shown in Datadog deployment tracking.
        agent_host: ddtrace agent host.  Set to ``None`` to use direct OTLP ingest
            instead of a local agent.
        agent_port: ddtrace agent trace port.
        tags: Additional global tags forwarded on every span and metric.
        use_otlp: When ``True``, export via OTLP to ``otlp-intake.<site>:4317``
            instead of the ddtrace agent protocol.  Requires no local agent.
    """

    api_key: str | None = None
    site: str = _DD_DEFAULT_SITE
    service: str = _SERVICE_NAME
    env: str = "production"
    version: str = ""
    agent_host: str = _DD_DEFAULT_AGENT_HOST
    agent_port: int = _DD_DEFAULT_AGENT_PORT
    tags: dict[str, str] = field(default_factory=dict)
    use_otlp: bool = False

    @classmethod
    def from_env(cls) -> DatadogConfig:
        """Build a :class:`DatadogConfig` from environment variables.

        Returns:
            Populated :class:`DatadogConfig` instance.
        """
        api_key = os.environ.get("DD_API_KEY") or os.environ.get("DATADOG_API_KEY")
        return cls(
            api_key=api_key,
            site=os.environ.get("DD_SITE", _DD_DEFAULT_SITE),
            service=os.environ.get("DD_SERVICE", _SERVICE_NAME),
            env=os.environ.get("DD_ENV", "production"),
            version=os.environ.get("DD_VERSION", ""),
            agent_host=os.environ.get("DD_AGENT_HOST", _DD_DEFAULT_AGENT_HOST),
            agent_port=int(os.environ.get("DD_TRACE_AGENT_PORT", str(_DD_DEFAULT_AGENT_PORT))),
        )


@dataclass
class NewRelicConfig:
    """Configuration for New Relic APM integration.

    Attributes:
        license_key: New Relic ingest licence key.  Falls back to
            ``NEW_RELIC_LICENSE_KEY`` / ``NEWRELIC_API_KEY`` env vars when ``None``.
        app_name: Application name as displayed in the New Relic UI.
        environment: Deployment environment label.
        otlp_endpoint: OTLP ingest endpoint.  Defaults to US datacenter;
            set to :data:`_NR_OTLP_ENDPOINT_EU` for EU accounts.
        use_otlp: When ``True`` (default), export via OTLP using the
            ``opentelemetry`` SDK instead of the ``newrelic`` agent SDK.
            Recommended for container/serverless deployments.
        distributed_tracing: Enable W3C trace context propagation.
        log_level: New Relic agent log level (``"info"``, ``"debug"``, ``"error"``).
    """

    license_key: str | None = None
    app_name: str = _SERVICE_NAME
    environment: str = "production"
    otlp_endpoint: str = _NR_OTLP_ENDPOINT
    use_otlp: bool = True
    distributed_tracing: bool = True
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> NewRelicConfig:
        """Build a :class:`NewRelicConfig` from environment variables.

        Returns:
            Populated :class:`NewRelicConfig` instance.
        """
        license_key = os.environ.get("NEW_RELIC_LICENSE_KEY") or os.environ.get("NEWRELIC_API_KEY")
        return cls(
            license_key=license_key,
            app_name=os.environ.get("NEW_RELIC_APP_NAME", _SERVICE_NAME),
            environment=os.environ.get("NEW_RELIC_ENVIRONMENT", "production"),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_dd_api_key(cfg: DatadogConfig) -> str | None:
    """Return the effective Datadog API key from config or environment."""
    return cfg.api_key or os.environ.get("DD_API_KEY") or os.environ.get("DATADOG_API_KEY")


def _resolve_nr_license_key(cfg: NewRelicConfig) -> str | None:
    """Return the effective New Relic licence key from config or environment."""
    return cfg.license_key or os.environ.get("NEW_RELIC_LICENSE_KEY") or os.environ.get("NEWRELIC_API_KEY")


# ---------------------------------------------------------------------------
# Datadog integration
# ---------------------------------------------------------------------------


def configure_datadog(cfg: DatadogConfig | None = None) -> bool:
    """Configure Datadog APM for Bernstein.

    Attempts two strategies in order:

    1. **ddtrace** — if the ``ddtrace`` package is installed, patches the
       process via ``ddtrace.patch_all()`` and registers global tags.
       Works with a local Datadog Agent (``DD_AGENT_HOST``) or the agentless
       OTLP intake (``DD_API_KEY`` + ``cfg.use_otlp=True``).
    2. **OTLP fallback** — if ``ddtrace`` is absent, falls back to
       :func:`bernstein.core.telemetry.init_telemetry_from_preset` with the
       ``"datadog"`` preset so spans still reach the Datadog Agent OTLP
       receiver on port 4317.

    Args:
        cfg: Datadog configuration.  When ``None``, reads from environment variables
            via :meth:`DatadogConfig.from_env`.

    Returns:
        ``True`` if configuration succeeded, ``False`` if no API key / agent is
        available.
    """
    if cfg is None:
        cfg = DatadogConfig.from_env()

    service = cfg.service
    env = cfg.env
    version = cfg.version

    # --- Strategy 1: ddtrace SDK ---
    try:
        import ddtrace  # type: ignore[import-untyped]
        from ddtrace import config as dd_config  # type: ignore[import-untyped]
        from ddtrace import (
            patch_all,  # type: ignore[import-untyped]
            tracer,  # type: ignore[import-untyped]
        )

        # Configure tracer target — agent or agentless
        tracer_kwargs: dict[str, Any] = {}
        if cfg.use_otlp:
            api_key = _resolve_dd_api_key(cfg)
            if not api_key:
                logger.warning(
                    "Datadog OTLP mode enabled but DD_API_KEY is not set — skipping ddtrace init"
                )
                return False
            # Route to Datadog OTLP intake endpoint
            tracer_kwargs["writer"] = None  # will be handled via ddtrace's OTLP writer
            os.environ.setdefault("DD_EXPORTER_OTLP_ENDPOINT", f"https://otlp.{cfg.site}:4317")
        else:
            tracer.configure(
                hostname=cfg.agent_host,
                port=cfg.agent_port,
            )

        # Global service/env/version tags
        dd_config.service = service  # type: ignore[attr-defined]
        dd_config.env = env  # type: ignore[attr-defined]
        dd_config.version = version  # type: ignore[attr-defined]

        # Apply user tags
        if cfg.tags:
            os.environ["DD_TAGS"] = ",".join(f"{k}:{v}" for k, v in cfg.tags.items())

        # Instrument standard libraries (requests, httpx, logging, etc.)
        patch_all()

        logger.info(
            "Datadog APM configured via ddtrace (service=%s, env=%s, version=%s, ddtrace=%s)",
            service,
            env,
            version,
            ddtrace.__version__,
        )
        return True

    except ImportError:
        logger.debug("ddtrace not installed — falling back to OTLP preset")

    # --- Strategy 2: OTLP fallback (Datadog Agent receives on port 4317) ---
    try:
        from bernstein.core.telemetry import init_telemetry_from_preset

        init_telemetry_from_preset(
            "datadog",
            endpoint_override=f"http://{cfg.agent_host}:4317",
        )
        logger.info(
            "Datadog APM configured via OTLP preset (agent=%s:4317)",
            cfg.agent_host,
        )
        return True
    except Exception as exc:
        logger.warning("Datadog OTLP fallback failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# New Relic integration
# ---------------------------------------------------------------------------


def configure_newrelic(cfg: NewRelicConfig | None = None) -> bool:
    """Configure New Relic APM for Bernstein.

    Attempts two strategies in order:

    1. **OTLP export** (``cfg.use_otlp=True``, default) — exports via the
       OpenTelemetry SDK to New Relic's OTLP endpoint using the licence key as
       the ``api-key`` HTTP header.  No New Relic agent required.
    2. **newrelic agent SDK** — if the ``newrelic`` package is installed and
       ``cfg.use_otlp=False``, initialises the New Relic Python agent with
       an in-memory INI configuration so no ``newrelic.ini`` file is needed.

    Args:
        cfg: New Relic configuration.  When ``None``, reads from environment variables
            via :meth:`NewRelicConfig.from_env`.

    Returns:
        ``True`` if configuration succeeded, ``False`` if no licence key is
        available.
    """
    if cfg is None:
        cfg = NewRelicConfig.from_env()

    license_key = _resolve_nr_license_key(cfg)
    if not license_key:
        logger.warning(
            "New Relic APM: no licence key found in config or environment "
            "(set NEW_RELIC_LICENSE_KEY or NEWRELIC_API_KEY) — skipping"
        )
        return False

    # --- Strategy 1: OTLP export (preferred, no agent required) ---
    if cfg.use_otlp:
        try:
            from bernstein.core.telemetry import _init_http_telemetry

            _init_http_telemetry(
                endpoint=f"https://{cfg.otlp_endpoint.split(':')[0]}",
                headers={"api-key": license_key},
                service_name=cfg.app_name,
            )
            logger.info(
                "New Relic APM configured via OTLP (endpoint=%s, app=%s)",
                cfg.otlp_endpoint,
                cfg.app_name,
            )
            return True
        except Exception as exc:
            logger.warning("New Relic OTLP init failed: %s — trying SDK fallback", exc)

    # --- Strategy 2: newrelic agent SDK ---
    try:
        import newrelic.agent  # type: ignore[import-untyped]

        settings = newrelic.agent.global_settings()
        settings.license_key = license_key
        settings.app_name = cfg.app_name
        settings.distributed_tracing.enabled = cfg.distributed_tracing
        settings.log_level = cfg.log_level
        if cfg.environment:
            settings.environment = cfg.environment

        newrelic.agent.initialize(config_object=settings)
        newrelic.agent.register_application(timeout=10.0)

        logger.info(
            "New Relic APM configured via agent SDK (app=%s, version=%s)",
            cfg.app_name,
            newrelic.version,  # type: ignore[attr-defined]
        )
        return True

    except ImportError:
        logger.debug("newrelic package not installed — OTLP path already attempted")
    except Exception as exc:
        logger.warning("New Relic agent SDK init failed: %s", exc)

    return False


# ---------------------------------------------------------------------------
# Auto-configure
# ---------------------------------------------------------------------------


def auto_configure_apm() -> list[APMProvider]:
    """Detect available APM credentials and configure each provider automatically.

    Checks environment variables for Datadog and New Relic credentials.
    Calls :func:`configure_datadog` and/or :func:`configure_newrelic`
    for each provider whose credentials are present.

    Returns:
        List of :class:`APMProvider` values for providers that were
        successfully configured.
    """
    configured: list[APMProvider] = []

    # Datadog: present if DD_API_KEY / DATADOG_API_KEY is set, or a local agent
    # is assumed reachable (standard Datadog Agent default port).
    dd_key = os.environ.get("DD_API_KEY") or os.environ.get("DATADOG_API_KEY")
    dd_agent_host = os.environ.get("DD_AGENT_HOST")
    if dd_key or dd_agent_host:
        cfg = DatadogConfig.from_env()
        if configure_datadog(cfg):
            configured.append(APMProvider.DATADOG)

    # New Relic: present if either key env var is set.
    nr_key = os.environ.get("NEW_RELIC_LICENSE_KEY") or os.environ.get("NEWRELIC_API_KEY")
    if nr_key:
        cfg_nr = NewRelicConfig.from_env()
        if configure_newrelic(cfg_nr):
            configured.append(APMProvider.NEWRELIC)

    if not configured:
        logger.debug(
            "auto_configure_apm: no APM credentials found — set DD_API_KEY or NEW_RELIC_LICENSE_KEY"
        )

    return configured
