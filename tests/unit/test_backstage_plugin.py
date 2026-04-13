"""Tests for Backstage developer portal integration plugin."""

from __future__ import annotations

import pytest
import yaml

from bernstein.core.protocols.backstage_plugin import (
    BackstageConfig,
    BackstageEntity,
    BackstageExporter,
    CatalogEntry,
    generate_plugin_config,
    render_integration_guide,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> BackstageConfig:
    return BackstageConfig(base_url="https://backstage.example.com")


@pytest.fixture()
def exporter() -> BackstageExporter:
    return BackstageExporter()


@pytest.fixture()
def sample_metrics() -> dict[str, object]:
    return {
        "tasks_total": 10,
        "tasks_completed": 8,
        "tasks_failed": 1,
        "duration_s": 120.5,
        "status": "completed",
        "plan": "refactor.yaml",
    }


# ---------------------------------------------------------------------------
# Tests -- BackstageEntity
# ---------------------------------------------------------------------------


class TestBackstageEntity:
    def test_create_component(self) -> None:
        entity = BackstageEntity(kind="Component", name="svc", namespace="default", description="A service")
        assert entity.kind == "Component"
        assert entity.name == "svc"
        assert entity.namespace == "default"
        assert entity.description == "A service"

    def test_create_api(self) -> None:
        entity = BackstageEntity(kind="API", name="my-api", namespace="prod", description="An API")
        assert entity.kind == "API"

    def test_create_resource(self) -> None:
        entity = BackstageEntity(kind="Resource", name="run-1", namespace="default", description="A run")
        assert entity.kind == "Resource"

    def test_invalid_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid entity kind"):
            BackstageEntity(kind="Service", name="x", namespace="default", description="bad")

    def test_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name must not be empty"):
            BackstageEntity(kind="Component", name="", namespace="default", description="bad")

    def test_frozen(self) -> None:
        entity = BackstageEntity(kind="Component", name="svc", namespace="default", description="A service")
        with pytest.raises(AttributeError):
            entity.name = "other"  # type: ignore[misc]

    def test_default_labels_and_annotations(self) -> None:
        entity = BackstageEntity(kind="Component", name="svc", namespace="default", description="svc")
        assert entity.labels == {}
        assert entity.annotations == {}

    def test_custom_labels_and_annotations(self) -> None:
        entity = BackstageEntity(
            kind="Component",
            name="svc",
            namespace="default",
            description="svc",
            labels={"tier": "backend"},
            annotations={"source": "github"},
        )
        assert entity.labels == {"tier": "backend"}
        assert entity.annotations == {"source": "github"}


# ---------------------------------------------------------------------------
# Tests -- BackstageConfig
# ---------------------------------------------------------------------------


class TestBackstageConfig:
    def test_defaults(self) -> None:
        cfg = BackstageConfig(base_url="https://backstage.example.com")
        assert cfg.api_token_env == "BACKSTAGE_API_TOKEN"
        assert cfg.namespace == "default"

    def test_custom_values(self) -> None:
        cfg = BackstageConfig(
            base_url="https://bs.io",
            api_token_env="MY_TOKEN",
            namespace="staging",
        )
        assert cfg.base_url == "https://bs.io"
        assert cfg.api_token_env == "MY_TOKEN"
        assert cfg.namespace == "staging"

    def test_empty_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="base_url must not be empty"):
            BackstageConfig(base_url="")

    def test_frozen(self) -> None:
        cfg = BackstageConfig(base_url="https://bs.io")
        with pytest.raises(AttributeError):
            cfg.base_url = "https://other.io"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests -- CatalogEntry
# ---------------------------------------------------------------------------


class TestCatalogEntry:
    def test_defaults(self) -> None:
        entity = BackstageEntity(kind="Component", name="x", namespace="default", description="x")
        entry = CatalogEntry(entity=entity)
        assert entry.spec == {}
        assert entry.relations == ()

    def test_frozen(self) -> None:
        entity = BackstageEntity(kind="Component", name="x", namespace="default", description="x")
        entry = CatalogEntry(entity=entity, spec={"type": "service"})
        with pytest.raises(AttributeError):
            entry.spec = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests -- BackstageExporter
# ---------------------------------------------------------------------------


class TestExportBernsteinComponent:
    def test_returns_catalog_entry(self, exporter: BackstageExporter, config: BackstageConfig) -> None:
        entry = exporter.export_bernstein_component(config)
        assert isinstance(entry, CatalogEntry)
        assert entry.entity.kind == "Component"
        assert entry.entity.name == "bernstein"

    def test_uses_config_namespace(self, exporter: BackstageExporter) -> None:
        cfg = BackstageConfig(base_url="https://bs.io", namespace="staging")
        entry = exporter.export_bernstein_component(cfg)
        assert entry.entity.namespace == "staging"

    def test_spec_fields(self, exporter: BackstageExporter, config: BackstageConfig) -> None:
        entry = exporter.export_bernstein_component(config)
        assert entry.spec["type"] == "service"
        assert entry.spec["lifecycle"] == "production"
        assert "bernstein-task-api" in entry.spec["providesApis"]

    def test_has_relations(self, exporter: BackstageExporter, config: BackstageConfig) -> None:
        entry = exporter.export_bernstein_component(config)
        assert len(entry.relations) > 0
        assert entry.relations[0]["type"] == "providesApi"


class TestExportRunAsResource:
    def test_basic_export(self, exporter: BackstageExporter, sample_metrics: dict[str, object]) -> None:
        entry = exporter.export_run_as_resource("run-42", sample_metrics)
        assert entry.entity.kind == "Resource"
        assert entry.entity.name == "bernstein-run-run-42"
        assert "run-42" in entry.entity.description

    def test_metrics_in_spec(self, exporter: BackstageExporter, sample_metrics: dict[str, object]) -> None:
        entry = exporter.export_run_as_resource("run-42", sample_metrics)
        assert entry.spec["tasks_total"] == 10
        assert entry.spec["tasks_completed"] == 8
        assert entry.spec["tasks_failed"] == 1
        assert entry.spec["duration_s"] == 120.5

    def test_status_label(self, exporter: BackstageExporter, sample_metrics: dict[str, object]) -> None:
        entry = exporter.export_run_as_resource("run-42", sample_metrics)
        assert entry.entity.labels["app.bernstein.io/status"] == "completed"

    def test_plan_label(self, exporter: BackstageExporter, sample_metrics: dict[str, object]) -> None:
        entry = exporter.export_run_as_resource("run-42", sample_metrics)
        assert entry.entity.labels["app.bernstein.io/plan"] == "refactor.yaml"

    def test_empty_run_id_raises(self, exporter: BackstageExporter) -> None:
        with pytest.raises(ValueError, match="run_id must not be empty"):
            exporter.export_run_as_resource("", {})

    def test_minimal_metrics(self, exporter: BackstageExporter) -> None:
        entry = exporter.export_run_as_resource("r1", {})
        assert entry.entity.kind == "Resource"
        assert "0/0 tasks" in entry.entity.description


class TestExportApiSpec:
    def test_basic_api_export(self, exporter: BackstageExporter) -> None:
        entry = exporter.export_api_spec("http://127.0.0.1:8052")
        assert entry.entity.kind == "API"
        assert entry.entity.name == "bernstein-task-api"

    def test_spec_url_annotation(self, exporter: BackstageExporter) -> None:
        entry = exporter.export_api_spec("http://localhost:8052")
        assert entry.entity.annotations["backstage.io/api-spec-url"] == "http://localhost:8052/openapi.json"

    def test_spec_type_openapi(self, exporter: BackstageExporter) -> None:
        entry = exporter.export_api_spec("http://127.0.0.1:8052")
        assert entry.spec["type"] == "openapi"

    def test_definition_is_valid_yaml(self, exporter: BackstageExporter) -> None:
        entry = exporter.export_api_spec("http://127.0.0.1:8052")
        parsed = yaml.safe_load(entry.spec["definition"])
        assert parsed["openapi"] == "3.0.0"
        assert "/tasks" in parsed["paths"]

    def test_empty_server_url_raises(self, exporter: BackstageExporter) -> None:
        with pytest.raises(ValueError, match="server_url must not be empty"):
            exporter.export_api_spec("")


# ---------------------------------------------------------------------------
# Tests -- YAML generation
# ---------------------------------------------------------------------------


class TestBuildEntityYaml:
    def test_valid_yaml_output(self, exporter: BackstageExporter) -> None:
        entity = BackstageEntity(kind="Component", name="svc", namespace="default", description="My service")
        text = exporter.build_entity_yaml(entity, {"type": "service"})
        doc = yaml.safe_load(text)
        assert doc["apiVersion"] == "backstage.io/v1alpha1"
        assert doc["kind"] == "Component"
        assert doc["metadata"]["name"] == "svc"
        assert doc["spec"]["type"] == "service"

    def test_labels_included(self, exporter: BackstageExporter) -> None:
        entity = BackstageEntity(
            kind="Component",
            name="svc",
            namespace="default",
            description="svc",
            labels={"tier": "backend"},
        )
        doc = yaml.safe_load(exporter.build_entity_yaml(entity, {}))
        assert doc["metadata"]["labels"]["tier"] == "backend"

    def test_annotations_included(self, exporter: BackstageExporter) -> None:
        entity = BackstageEntity(
            kind="Component",
            name="svc",
            namespace="default",
            description="svc",
            annotations={"backstage.io/techdocs-ref": "dir:."},
        )
        doc = yaml.safe_load(exporter.build_entity_yaml(entity, {}))
        assert doc["metadata"]["annotations"]["backstage.io/techdocs-ref"] == "dir:."

    def test_relations_included(self, exporter: BackstageExporter) -> None:
        entity = BackstageEntity(kind="Component", name="svc", namespace="default", description="svc")
        relations = ({"type": "ownedBy", "targetRef": "group:default/team-a"},)
        doc = yaml.safe_load(exporter.build_entity_yaml(entity, {}, relations))
        assert len(doc["relations"]) == 1
        assert doc["relations"][0]["type"] == "ownedBy"

    def test_no_relations_key_when_empty(self, exporter: BackstageExporter) -> None:
        entity = BackstageEntity(kind="Component", name="svc", namespace="default", description="svc")
        doc = yaml.safe_load(exporter.build_entity_yaml(entity, {}))
        assert "relations" not in doc


class TestBuildCatalogInfoYaml:
    def test_single_entry(self, exporter: BackstageExporter, config: BackstageConfig) -> None:
        entry = exporter.export_bernstein_component(config)
        text = exporter.build_catalog_info_yaml([entry])
        docs = list(yaml.safe_load_all(text))
        assert len(docs) == 1
        assert docs[0]["kind"] == "Component"

    def test_multiple_entries(self, exporter: BackstageExporter, config: BackstageConfig) -> None:
        entries = [
            exporter.export_bernstein_component(config),
            exporter.export_api_spec("http://127.0.0.1:8052"),
        ]
        text = exporter.build_catalog_info_yaml(entries)
        docs = list(yaml.safe_load_all(text))
        assert len(docs) == 2
        kinds = {d["kind"] for d in docs}
        assert kinds == {"Component", "API"}

    def test_empty_entries_raises(self, exporter: BackstageExporter) -> None:
        with pytest.raises(ValueError, match="entries must not be empty"):
            exporter.build_catalog_info_yaml([])


# ---------------------------------------------------------------------------
# Tests -- Standalone functions
# ---------------------------------------------------------------------------


class TestGeneratePluginConfig:
    def test_valid_yaml(self, config: BackstageConfig) -> None:
        text = generate_plugin_config(config)
        doc = yaml.safe_load(text)
        assert "catalog" in doc
        assert "proxy" in doc

    def test_catalog_location(self, config: BackstageConfig) -> None:
        doc = yaml.safe_load(generate_plugin_config(config))
        locations = doc["catalog"]["locations"]
        assert len(locations) == 1
        assert "backstage.example.com" in locations[0]["target"]

    def test_proxy_target(self, config: BackstageConfig) -> None:
        doc = yaml.safe_load(generate_plugin_config(config))
        proxy = doc["proxy"]["/bernstein"]
        assert proxy["target"] == "http://127.0.0.1:8052"
        assert proxy["changeOrigin"] is True

    def test_custom_token_env(self) -> None:
        cfg = BackstageConfig(base_url="https://bs.io", api_token_env="MY_TOKEN")
        doc = yaml.safe_load(generate_plugin_config(cfg))
        auth_header = doc["proxy"]["/bernstein"]["headers"]["Authorization"]
        assert "MY_TOKEN" in auth_header


class TestRenderIntegrationGuide:
    def test_returns_markdown(self) -> None:
        guide = render_integration_guide()
        assert guide.startswith("# Backstage Integration Guide")

    def test_contains_key_sections(self) -> None:
        guide = render_integration_guide()
        assert "## Prerequisites" in guide
        assert "## Step 1" in guide
        assert "## Step 2" in guide
        assert "## Step 3" in guide
        assert "## Step 4" in guide
        assert "## Troubleshooting" in guide

    def test_ends_with_newline(self) -> None:
        guide = render_integration_guide()
        assert guide.endswith("\n")
