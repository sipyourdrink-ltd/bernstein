"""Datadog and New Relic APM payload generation.

Builds vendor-specific payloads (spans, metrics, logs) for the Datadog and
New Relic HTTP ingest APIs *without* importing ``ddtrace`` or ``newrelic``.
This lets Bernstein export APM data from any environment -- containers,
serverless, CI -- using plain HTTPS requests.

Supported providers:

* **Datadog** -- v2 Series API for metrics, v0.2 traces API for spans, and
  the log intake HTTP API for structured logs.
* **New Relic** -- Metric API v1, Trace API (zipkin-v2 compatible), and
  Log API v1.
* **GENERIC** -- a pass-through format for custom or unsupported providers.

Usage::

    from bernstein.core.observability.apm_export import (
        APMExporter,
        get_datadog_config,
        get_newrelic_config,
    )

    exporter = APMExporter(get_datadog_config())
    span = exporter.export_span("task.run", duration_ms=142.5, attributes={"task_id": "t-1"})
    metric = exporter.export_metric("task.duration", 142.5, tags={"role": "backend"})
    log = exporter.export_log("info", "task completed", {"task_id": "t-1"})
    payload = exporter.build_payload([span, metric, log])
    headers = exporter.get_headers()
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Provider enum
# ---------------------------------------------------------------------------


class APMProvider(StrEnum):
    """Supported APM provider backends."""

    DATADOG = "datadog"
    NEW_RELIC = "new_relic"
    GENERIC = "generic"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class APMConfig:
    """Immutable configuration for an APM provider.

    Attributes:
        provider: Target APM provider.
        api_key_env_var: Name of the environment variable holding the API key.
        endpoint: Override ingest endpoint URL.  ``None`` uses provider defaults.
        service_name: Service name attached to all exported telemetry.
        tags: Static key-value tags appended to every payload.
    """

    provider: APMProvider
    api_key_env_var: str
    endpoint: str | None = None
    service_name: str = "bernstein"
    tags: dict[str, str] = field(default_factory=dict[str, str])


# ---------------------------------------------------------------------------
# Event container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class APMEvent:
    """Immutable container for a single APM event (span, metric, or log).

    Attributes:
        name: Event name / metric name / log message.
        timestamp: Unix epoch seconds (float).
        duration_ms: Duration in milliseconds (0.0 for point-in-time events).
        attributes: Free-form key-value metadata.
        provider: Originating provider -- used during payload assembly.
    """

    name: str
    timestamp: float
    duration_ms: float
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])
    provider: APMProvider = APMProvider.GENERIC


# ---------------------------------------------------------------------------
# Default configs
# ---------------------------------------------------------------------------


def get_datadog_config(
    *,
    service_name: str = "bernstein",
    endpoint: str | None = None,
    tags: dict[str, str] | None = None,
) -> APMConfig:
    """Return a default :class:`APMConfig` for Datadog.

    Args:
        service_name: Service name reported to Datadog.
        endpoint: Custom intake endpoint.  Defaults to Datadog US intake.
        tags: Extra static tags.

    Returns:
        Frozen :class:`APMConfig` targeting Datadog.
    """
    return APMConfig(
        provider=APMProvider.DATADOG,
        api_key_env_var="DD_API_KEY",
        endpoint=endpoint or "https://api.datadoghq.com",
        service_name=service_name,
        tags=tags or {},
    )


def get_newrelic_config(
    *,
    service_name: str = "bernstein",
    endpoint: str | None = None,
    tags: dict[str, str] | None = None,
) -> APMConfig:
    """Return a default :class:`APMConfig` for New Relic.

    Args:
        service_name: Application name in New Relic UI.
        endpoint: Custom ingest endpoint.  Defaults to New Relic US.
        tags: Extra static tags.

    Returns:
        Frozen :class:`APMConfig` targeting New Relic.
    """
    return APMConfig(
        provider=APMProvider.NEW_RELIC,
        api_key_env_var="NEW_RELIC_LICENSE_KEY",
        endpoint=endpoint or "https://metric-api.newrelic.com",
        service_name=service_name,
        tags=tags or {},
    )


# ---------------------------------------------------------------------------
# Integration guide
# ---------------------------------------------------------------------------

_DATADOG_GUIDE = """\
# Datadog APM Integration Guide

## Prerequisites
- A Datadog account with APM enabled.
- An API key (found in Organization Settings > API Keys).

## Environment variables
```bash
export DD_API_KEY="<your-api-key>"
```

## Usage
```python
from bernstein.core.observability.apm_export import APMExporter, get_datadog_config

exporter = APMExporter(get_datadog_config())
span = exporter.export_span("task.run", duration_ms=150.0, attributes={"task_id": "t-1"})
payload = exporter.build_payload([span])
headers = exporter.get_headers()
# POST payload to the appropriate Datadog intake endpoint with headers.
```

## Payload format
- Metrics use the Datadog v2 Series API format.
- Spans use the Datadog v0.2 trace format (MessagePack-compatible JSON).
- Logs use the Datadog HTTP log intake format.
"""

_NEWRELIC_GUIDE = """\
# New Relic APM Integration Guide

## Prerequisites
- A New Relic account.
- A licence key (found in API Keys settings).

## Environment variables
```bash
export NEW_RELIC_LICENSE_KEY="<your-license-key>"
```

## Usage
```python
from bernstein.core.observability.apm_export import APMExporter, get_newrelic_config

exporter = APMExporter(get_newrelic_config())
metric = exporter.export_metric("task.duration", 142.5, tags={"role": "backend"})
payload = exporter.build_payload([metric])
headers = exporter.get_headers()
# POST payload to the New Relic ingest endpoint with headers.
```

## Payload format
- Metrics use the New Relic Metric API v1 format.
- Spans use the New Relic Trace API (Zipkin v2 compatible) format.
- Logs use the New Relic Log API v1 format.
"""

_GENERIC_GUIDE = """\
# Generic APM Integration Guide

The generic provider emits a simple JSON payload suitable for custom backends.
Set the `APM_API_KEY` environment variable and point `endpoint` at your ingest URL.
"""


def render_integration_guide(provider: APMProvider) -> str:
    """Return a Markdown setup guide for the given APM provider.

    Args:
        provider: Target APM provider.

    Returns:
        Markdown-formatted integration guide string.
    """
    guides: dict[APMProvider, str] = {
        APMProvider.DATADOG: _DATADOG_GUIDE,
        APMProvider.NEW_RELIC: _NEWRELIC_GUIDE,
        APMProvider.GENERIC: _GENERIC_GUIDE,
    }
    return guides.get(provider, _GENERIC_GUIDE)


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class APMExporter:
    """Formats spans, metrics, and logs as provider-native API payloads.

    Does **not** perform network I/O or import vendor SDKs.  Callers are
    responsible for posting the resulting payloads to the provider endpoint.

    Args:
        config: Provider configuration.
    """

    def __init__(self, config: APMConfig) -> None:
        self._config = config

    @property
    def config(self) -> APMConfig:
        """Return the exporter configuration."""
        return self._config

    # -- Span ----------------------------------------------------------------

    def export_span(
        self,
        name: str,
        duration_ms: float,
        attributes: dict[str, Any] | None = None,
    ) -> APMEvent:
        """Create an :class:`APMEvent` representing a trace span.

        Args:
            name: Span / operation name.
            duration_ms: Span duration in milliseconds.
            attributes: Optional span-level key-value metadata.

        Returns:
            An :class:`APMEvent` tagged with the provider.
        """
        merged = dict(self._config.tags)
        merged["type"] = "span"
        merged["service"] = self._config.service_name
        if attributes:
            merged.update(attributes)

        return APMEvent(
            name=name,
            timestamp=time.time(),
            duration_ms=duration_ms,
            attributes=merged,
            provider=self._config.provider,
        )

    # -- Metric --------------------------------------------------------------

    def export_metric(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> APMEvent:
        """Create an :class:`APMEvent` representing a metric data point.

        Args:
            name: Metric name.
            value: Numeric metric value.
            tags: Optional metric tags (merged with config tags).

        Returns:
            An :class:`APMEvent` tagged with the provider.
        """
        merged: dict[str, Any] = dict(self._config.tags)
        merged["type"] = "metric"
        merged["metric_value"] = value
        merged["service"] = self._config.service_name
        if tags:
            merged.update(tags)

        return APMEvent(
            name=name,
            timestamp=time.time(),
            duration_ms=0.0,
            attributes=merged,
            provider=self._config.provider,
        )

    # -- Log -----------------------------------------------------------------

    def export_log(
        self,
        level: str,
        message: str,
        attributes: dict[str, Any] | None = None,
    ) -> APMEvent:
        """Create an :class:`APMEvent` representing a structured log line.

        Args:
            level: Log level (e.g. ``"info"``, ``"error"``).
            message: Log message body.
            attributes: Optional structured context.

        Returns:
            An :class:`APMEvent` tagged with the provider.
        """
        merged: dict[str, Any] = dict(self._config.tags)
        merged["type"] = "log"
        merged["level"] = level
        merged["service"] = self._config.service_name
        if attributes:
            merged.update(attributes)

        return APMEvent(
            name=message,
            timestamp=time.time(),
            duration_ms=0.0,
            attributes=merged,
            provider=self._config.provider,
        )

    # -- Batch payload -------------------------------------------------------

    def build_payload(self, events: list[APMEvent]) -> dict[str, Any]:
        """Batch events into a provider-specific JSON payload.

        The returned dictionary mirrors the structure expected by each
        provider's HTTP ingest API so callers can ``json.dumps`` and POST
        directly.

        Args:
            events: List of :class:`APMEvent` instances to batch.

        Returns:
            Provider-native payload dictionary.
        """
        provider = self._config.provider
        if provider == APMProvider.DATADOG:
            return self._build_datadog_payload(events)
        if provider == APMProvider.NEW_RELIC:
            return self._build_newrelic_payload(events)
        return self._build_generic_payload(events)

    # -- Auth headers --------------------------------------------------------

    def get_headers(self) -> dict[str, str]:
        """Return authentication headers for the configured provider API.

        The API key value is read from the environment variable named in
        :attr:`APMConfig.api_key_env_var`.  If the variable is unset, the
        header value will be an empty string.

        Returns:
            Dictionary of HTTP headers including authentication.
        """
        api_key = os.environ.get(self._config.api_key_env_var, "")
        provider = self._config.provider

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }

        if provider == APMProvider.DATADOG:
            headers["DD-API-KEY"] = api_key
        elif provider == APMProvider.NEW_RELIC:
            headers["Api-Key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        return headers

    # -- Private builders ----------------------------------------------------

    def _build_datadog_payload(self, events: list[APMEvent]) -> dict[str, Any]:
        """Build a Datadog-compatible ingest payload.

        Organises events into three top-level keys matching the Datadog HTTP
        intake APIs:

        * ``series`` -- v2 Series API format for metrics.
        * ``traces`` -- v0.2 trace format for spans.
        * ``logs`` -- HTTP log intake format.
        """
        series: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        logs: list[dict[str, Any]] = []

        for ev in events:
            ev_type = ev.attributes.get("type", "metric")

            if ev_type == "span":
                traces.append(
                    {
                        "name": ev.name,
                        "service": ev.attributes.get("service", self._config.service_name),
                        "resource": ev.name,
                        "start": int(ev.timestamp * 1_000_000_000),  # nanoseconds
                        "duration": int(ev.duration_ms * 1_000_000),  # nanoseconds
                        "meta": {
                            k: str(v)
                            for k, v in ev.attributes.items()
                            if k not in ("type", "service")
                        },
                    }
                )
            elif ev_type == "log":
                logs.append(
                    {
                        "message": ev.name,
                        "ddsource": "bernstein",
                        "ddtags": ",".join(
                            f"{k}:{v}"
                            for k, v in ev.attributes.items()
                            if k not in ("type", "level", "service")
                        ),
                        "hostname": "bernstein",
                        "service": ev.attributes.get("service", self._config.service_name),
                        "status": ev.attributes.get("level", "info"),
                        "timestamp": int(ev.timestamp * 1000),  # milliseconds
                    }
                )
            else:
                # Metric (default)
                metric_value = ev.attributes.get("metric_value", 0.0)
                tag_list = [
                    f"{k}:{v}"
                    for k, v in ev.attributes.items()
                    if k not in ("type", "metric_value", "service")
                ]
                series.append(
                    {
                        "metric": ev.name,
                        "type": "gauge",
                        "points": [[int(ev.timestamp), metric_value]],
                        "tags": tag_list,
                        "host": "bernstein",
                    }
                )

        return {"series": series, "traces": traces, "logs": logs}

    def _build_newrelic_payload(self, events: list[APMEvent]) -> dict[str, Any]:
        """Build a New Relic-compatible ingest payload.

        Organises events into three top-level keys matching New Relic's
        HTTP ingest APIs:

        * ``metrics`` -- Metric API v1 format.
        * ``spans`` -- Trace API (Zipkin v2) format.
        * ``logs`` -- Log API v1 format.
        """
        metrics: list[dict[str, Any]] = []
        spans: list[dict[str, Any]] = []
        nr_logs: list[dict[str, Any]] = []

        for ev in events:
            ev_type = ev.attributes.get("type", "metric")
            common_attrs = {
                k: v
                for k, v in ev.attributes.items()
                if k not in ("type", "metric_value", "service", "level")
            }

            if ev_type == "span":
                spans.append(
                    {
                        "trace.id": common_attrs.get("trace_id", ""),
                        "id": common_attrs.get("span_id", ""),
                        "attributes": {
                            "name": ev.name,
                            "service.name": ev.attributes.get(
                                "service", self._config.service_name
                            ),
                            "duration.ms": ev.duration_ms,
                            "timestamp": int(ev.timestamp * 1000),
                            **common_attrs,
                        },
                    }
                )
            elif ev_type == "log":
                nr_logs.append(
                    {
                        "timestamp": int(ev.timestamp * 1000),
                        "message": ev.name,
                        "attributes": {
                            "service": ev.attributes.get(
                                "service", self._config.service_name
                            ),
                            "level": ev.attributes.get("level", "info"),
                            **common_attrs,
                        },
                    }
                )
            else:
                metric_value = ev.attributes.get("metric_value", 0.0)
                metrics.append(
                    {
                        "name": ev.name,
                        "type": "gauge",
                        "value": metric_value,
                        "timestamp": int(ev.timestamp * 1000),
                        "attributes": {
                            "service": ev.attributes.get(
                                "service", self._config.service_name
                            ),
                            **common_attrs,
                        },
                    }
                )

        return {
            "metrics": [{"metrics": metrics}] if metrics else [],
            "spans": spans,
            "logs": [{"logs": nr_logs}] if nr_logs else [],
        }

    def _build_generic_payload(self, events: list[APMEvent]) -> dict[str, Any]:
        """Build a simple JSON payload for custom/unsupported providers."""
        return {
            "service": self._config.service_name,
            "events": [
                {
                    "name": ev.name,
                    "timestamp": ev.timestamp,
                    "duration_ms": ev.duration_ms,
                    "attributes": ev.attributes,
                }
                for ev in events
            ],
        }
