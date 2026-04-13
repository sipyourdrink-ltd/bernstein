"""Backstage developer portal integration plugin.

Generates Backstage catalog entities and configuration for integrating
Bernstein orchestration data into a Backstage developer portal. No
Backstage SDK dependency required -- all output is plain YAML.

Features:
- Export Bernstein itself as a Backstage Component entity.
- Export orchestration runs as Backstage Resource entities.
- Export the task server API as a Backstage API entity.
- Generate ``catalog-info.yaml`` files for the Backstage catalog.
- Generate ``app-config.yaml`` snippets for Backstage integration.
- Render a Markdown integration guide.

Usage::

    from bernstein.core.protocols.backstage_plugin import (
        BackstageExporter,
        BackstageConfig,
        generate_plugin_config,
        render_integration_guide,
    )

    cfg = BackstageConfig(base_url="https://backstage.example.com")
    exporter = BackstageExporter()
    entry = exporter.export_bernstein_component(cfg)
    yaml_text = exporter.build_catalog_info_yaml([entry])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_VALID_ENTITY_KINDS = frozenset({"Component", "API", "Resource"})


@dataclass(frozen=True)
class BackstageEntity:
    """Core Backstage entity descriptor.

    Attributes:
        kind: Entity kind -- one of ``Component``, ``API``, ``Resource``.
        name: Machine-readable entity name (kebab-case recommended).
        namespace: Backstage namespace for the entity.
        description: Human-readable summary.
        labels: Arbitrary key/value labels for filtering.
        annotations: Backstage annotations (e.g. source-location URLs).
    """

    kind: str
    name: str
    namespace: str
    description: str
    labels: dict[str, str] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    annotations: dict[str, str] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]

    def __post_init__(self) -> None:
        if self.kind not in _VALID_ENTITY_KINDS:
            msg = f"Invalid entity kind {self.kind!r}; expected one of {sorted(_VALID_ENTITY_KINDS)}"
            raise ValueError(msg)
        if not self.name:
            msg = "Entity name must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True)
class BackstageConfig:
    """Configuration for Backstage integration.

    Attributes:
        base_url: Base URL of the Backstage instance.
        api_token_env: Environment variable holding the Backstage API token.
        namespace: Default Backstage namespace for exported entities.
    """

    base_url: str
    api_token_env: str = "BACKSTAGE_API_TOKEN"
    namespace: str = "default"

    def __post_init__(self) -> None:
        if not self.base_url:
            msg = "base_url must not be empty"
            raise ValueError(msg)


@dataclass(frozen=True)
class CatalogEntry:
    """A complete Backstage catalog entry ready for YAML serialization.

    Attributes:
        entity: The entity descriptor.
        spec: Entity-specific specification fields.
        relations: Relationships to other catalog entities.
    """

    entity: BackstageEntity
    spec: dict[str, Any] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]
    relations: tuple[dict[str, str], ...] = ()


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class BackstageExporter:
    """Generates Backstage catalog entities from Bernstein data.

    All methods are pure -- no network calls. They produce data structures
    or YAML strings suitable for writing to disk or posting to a Backstage
    catalog API.
    """

    def export_bernstein_component(self, config: BackstageConfig) -> CatalogEntry:
        """Generate a Backstage Component entity for Bernstein itself.

        Args:
            config: Backstage configuration.

        Returns:
            A catalog entry describing Bernstein as a Backstage Component.
        """
        entity = BackstageEntity(
            kind="Component",
            name="bernstein",
            namespace=config.namespace,
            description="Multi-agent orchestration system for CLI coding agents",
            labels={
                "app.bernstein.io/tier": "orchestrator",
            },
            annotations={
                "backstage.io/techdocs-ref": "dir:.",
                "backstage.io/source-location": f"url:{config.base_url}/catalog/default/component/bernstein",
            },
        )
        spec: dict[str, Any] = {
            "type": "service",
            "lifecycle": "production",
            "owner": "platform-team",
            "system": "bernstein",
            "providesApis": ["bernstein-task-api"],
        }
        relations: tuple[dict[str, str], ...] = (
            {
                "type": "providesApi",
                "targetRef": f"api:{config.namespace}/bernstein-task-api",
            },
        )
        return CatalogEntry(entity=entity, spec=spec, relations=relations)

    def export_run_as_resource(
        self,
        run_id: str,
        metrics: dict[str, Any],
    ) -> CatalogEntry:
        """Export an orchestration run as a Backstage Resource entity.

        Args:
            run_id: Unique identifier for the orchestration run.
            metrics: Run metrics (tasks_total, tasks_completed, duration_s, etc.).

        Returns:
            A catalog entry describing the run as a Backstage Resource.

        Raises:
            ValueError: If *run_id* is empty.
        """
        if not run_id:
            msg = "run_id must not be empty"
            raise ValueError(msg)

        safe_name = f"bernstein-run-{run_id}"
        description = (
            f"Bernstein orchestration run {run_id} "
            f"({metrics.get('tasks_completed', 0)}/{metrics.get('tasks_total', 0)} tasks)"
        )
        labels: dict[str, str] = {
            "app.bernstein.io/run-id": run_id,
        }
        for key in ("status", "plan"):
            val = metrics.get(key)
            if isinstance(val, str) and val:
                labels[f"app.bernstein.io/{key}"] = val

        entity = BackstageEntity(
            kind="Resource",
            name=safe_name,
            namespace="default",
            description=description,
            labels=labels,
            annotations={
                "bernstein.io/run-id": run_id,
            },
        )

        spec: dict[str, Any] = {
            "type": "orchestration-run",
            "owner": "platform-team",
            "lifecycle": "production",
        }
        # Embed numeric metrics in spec for dashboard consumption.
        for metric_key in ("tasks_total", "tasks_completed", "tasks_failed", "duration_s"):
            val = metrics.get(metric_key)
            if val is not None:
                spec[metric_key] = val

        return CatalogEntry(entity=entity, spec=spec)

    def export_api_spec(self, server_url: str) -> CatalogEntry:
        """Generate a Backstage API entity for the Bernstein task server.

        Args:
            server_url: Base URL of the running task server
                        (e.g. ``http://127.0.0.1:8052``).

        Returns:
            A catalog entry describing the task server REST API.

        Raises:
            ValueError: If *server_url* is empty.
        """
        if not server_url:
            msg = "server_url must not be empty"
            raise ValueError(msg)

        entity = BackstageEntity(
            kind="API",
            name="bernstein-task-api",
            namespace="default",
            description="Bernstein task server REST API",
            labels={
                "app.bernstein.io/tier": "api",
            },
            annotations={
                "backstage.io/api-spec-url": f"{server_url}/openapi.json",
            },
        )
        spec: dict[str, Any] = {
            "type": "openapi",
            "lifecycle": "production",
            "owner": "platform-team",
            "definition": _task_api_openapi_stub(server_url),
        }
        return CatalogEntry(entity=entity, spec=spec)

    # -----------------------------------------------------------------
    # YAML generation
    # -----------------------------------------------------------------

    def build_catalog_info_yaml(self, entries: list[CatalogEntry]) -> str:
        """Generate a multi-document ``catalog-info.yaml`` from entries.

        Args:
            entries: One or more catalog entries.

        Returns:
            YAML string with ``---`` separators between documents.

        Raises:
            ValueError: If *entries* is empty.
        """
        if not entries:
            msg = "entries must not be empty"
            raise ValueError(msg)

        docs: list[str] = []
        for entry in entries:
            docs.append(self.build_entity_yaml(entry.entity, entry.spec, entry.relations))
        return "---\n".join(docs)

    def build_entity_yaml(
        self,
        entity: BackstageEntity,
        spec: dict[str, Any],
        relations: tuple[dict[str, str], ...] = (),
    ) -> str:
        """Generate YAML for a single Backstage entity.

        Args:
            entity: The entity descriptor.
            spec: The entity spec section.
            relations: Optional entity relations.

        Returns:
            YAML string for one entity document.
        """
        doc: dict[str, Any] = {
            "apiVersion": "backstage.io/v1alpha1",
            "kind": entity.kind,
            "metadata": {
                "name": entity.name,
                "namespace": entity.namespace,
                "description": entity.description,
            },
            "spec": dict(spec),
        }
        if entity.labels:
            doc["metadata"]["labels"] = dict(entity.labels)
        if entity.annotations:
            doc["metadata"]["annotations"] = dict(entity.annotations)
        if relations:
            doc["relations"] = [dict(r) for r in relations]

        return yaml.dump(doc, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------


def generate_plugin_config(config: BackstageConfig) -> str:
    """Generate an ``app-config.yaml`` snippet for Backstage integration.

    The snippet configures the Backstage catalog to ingest Bernstein
    entities and sets up the proxy for the task server API.

    Args:
        config: Backstage configuration.

    Returns:
        YAML string suitable for merging into ``app-config.yaml``.
    """
    snippet: dict[str, Any] = {
        "catalog": {
            "locations": [
                {
                    "type": "url",
                    "target": f"{config.base_url}/catalog-info.yaml",
                    "rules": [{"allow": ["Component", "API", "Resource"]}],
                },
            ],
        },
        "proxy": {
            "/bernstein": {
                "target": "http://127.0.0.1:8052",
                "changeOrigin": True,
                "headers": {
                    "Authorization": f"Bearer ${{{config.api_token_env}}}",
                },
            },
        },
    }
    return yaml.dump(snippet, default_flow_style=False, sort_keys=False)


def render_integration_guide() -> str:
    """Render a Markdown setup guide for Backstage integration.

    Returns:
        Markdown text describing how to integrate Bernstein with Backstage.
    """
    return _INTEGRATION_GUIDE.strip() + "\n"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _task_api_openapi_stub(server_url: str) -> str:
    """Return a minimal OpenAPI 3.0 spec stub for the task server.

    This is embedded as a string in the Backstage API entity ``definition``
    field. A real deployment would point to the live ``/openapi.json``
    endpoint instead.
    """
    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {
            "title": "Bernstein Task Server",
            "version": "1.0.0",
            "description": "Multi-agent orchestration task server API",
        },
        "servers": [{"url": server_url}],
        "paths": {
            "/tasks": {
                "get": {"summary": "List tasks by status", "operationId": "listTasks"},
                "post": {"summary": "Create a new task", "operationId": "createTask"},
            },
            "/tasks/{id}/complete": {
                "post": {"summary": "Mark task completed", "operationId": "completeTask"},
            },
            "/tasks/{id}/fail": {
                "post": {"summary": "Mark task failed", "operationId": "failTask"},
            },
            "/tasks/{id}/progress": {
                "post": {"summary": "Report task progress", "operationId": "reportProgress"},
            },
            "/status": {
                "get": {"summary": "Dashboard summary", "operationId": "getStatus"},
            },
            "/bulletin": {
                "get": {"summary": "Read recent bulletins", "operationId": "getBulletins"},
                "post": {"summary": "Post a bulletin", "operationId": "postBulletin"},
            },
        },
    }
    return yaml.dump(spec, default_flow_style=False, sort_keys=False)


_INTEGRATION_GUIDE = """
# Backstage Integration Guide for Bernstein

## Prerequisites

- A running [Backstage](https://backstage.io) instance (v1.20+).
- The Bernstein task server running at `http://127.0.0.1:8052`.

## Step 1: Generate catalog entities

```python
from bernstein.core.protocols.backstage_plugin import (
    BackstageConfig,
    BackstageExporter,
)

config = BackstageConfig(base_url="https://backstage.example.com")
exporter = BackstageExporter()

entries = [
    exporter.export_bernstein_component(config),
    exporter.export_api_spec("http://127.0.0.1:8052"),
]
yaml_text = exporter.build_catalog_info_yaml(entries)

with open("catalog-info.yaml", "w") as f:
    f.write(yaml_text)
```

## Step 2: Configure Backstage

Add the following to your `app-config.yaml`:

```python
from bernstein.core.protocols.backstage_plugin import (
    BackstageConfig,
    generate_plugin_config,
)

config = BackstageConfig(base_url="https://backstage.example.com")
print(generate_plugin_config(config))
```

Merge the output into your existing `app-config.yaml`.

## Step 3: Register run resources

After each orchestration run, export it as a Backstage Resource:

```python
run_entry = exporter.export_run_as_resource(
    run_id="abc-123",
    metrics={"tasks_total": 10, "tasks_completed": 8, "duration_s": 120},
)
yaml_text = exporter.build_entity_yaml(run_entry.entity, run_entry.spec)
```

## Step 4: Verify in Backstage

1. Navigate to your Backstage catalog.
2. Search for "bernstein" -- you should see the Component and API entities.
3. After registering runs, they appear as Resource entities.

## Environment variables

| Variable | Description |
|---|---|
| `BACKSTAGE_API_TOKEN` | API token for authenticated Backstage access |

## Troubleshooting

- Ensure the `catalog-info.yaml` is accessible at the URL configured in `app-config.yaml`.
- Check Backstage logs for catalog ingestion errors.
- Verify the task server is reachable from the Backstage proxy.
"""
