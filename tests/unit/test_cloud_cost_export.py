"""Tests for cloud cost management platform integration (#644)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from bernstein.core.cost.cloud_cost_export import (
    CostAllocation,
    CostExportConfig,
    CostExporter,
    CostPlatform,
    aggregate_costs_by_model,
    aggregate_costs_by_role,
    render_cost_allocation_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_archive(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    """Write JSONL archive records to a temp file and return the path."""
    archive = tmp_path / "tasks.jsonl"
    with archive.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return archive


def _sample_records() -> list[dict[str, object]]:
    """Return a small set of realistic archive records."""
    return [
        {
            "task_id": "T-001",
            "role": "backend",
            "model": "sonnet",
            "status": "done",
            "cost_usd": 0.05,
            "timestamp": 1700000000,
            "completed_at": 1700000300,
        },
        {
            "task_id": "T-002",
            "role": "frontend",
            "model": "haiku",
            "status": "done",
            "cost_usd": 0.01,
            "timestamp": 1700001000,
            "completed_at": 1700001200,
        },
        {
            "task_id": "T-003",
            "role": "backend",
            "model": "opus",
            "status": "failed",
            "cost_usd": 0.15,
            "timestamp": 1700002000,
            "completed_at": 1700002600,
        },
        {
            "task_id": "T-004",
            "role": "qa",
            "model": "sonnet",
            "status": "done",
            "cost_usd": 0.03,
            "timestamp": 1700003000,
            "completed_at": 1700003100,
        },
    ]


def _make_config(
    platform: CostPlatform = CostPlatform.GENERIC,
    **kwargs: object,
) -> CostExportConfig:
    """Build a CostExportConfig with defaults."""
    return CostExportConfig(platform=platform, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CostPlatform StrEnum
# ---------------------------------------------------------------------------


class TestCostPlatform:
    """CostPlatform enum values and behaviour."""

    def test_values(self) -> None:
        assert CostPlatform.CLOUDHEALTH == "cloudhealth"
        assert CostPlatform.KUBECOST == "kubecost"
        assert CostPlatform.SPOT_IO == "spot_io"
        assert CostPlatform.GENERIC == "generic"

    def test_is_str(self) -> None:
        assert isinstance(CostPlatform.CLOUDHEALTH, str)

    def test_member_count(self) -> None:
        assert len(CostPlatform) == 4


# ---------------------------------------------------------------------------
# CostAllocation dataclass
# ---------------------------------------------------------------------------


class TestCostAllocation:
    """CostAllocation is frozen and stores expected fields."""

    def test_frozen(self) -> None:
        alloc = CostAllocation(resource_id="r1", cost_usd=1.0)
        with pytest.raises(AttributeError):
            alloc.cost_usd = 2.0  # type: ignore[misc]

    def test_defaults(self) -> None:
        alloc = CostAllocation(resource_id="r1", cost_usd=0.5)
        assert alloc.currency == "USD"
        assert alloc.period_start == ""
        assert alloc.period_end == ""
        assert alloc.labels == {}
        assert alloc.namespace is None
        assert alloc.account_id is None

    def test_full_construction(self) -> None:
        alloc = CostAllocation(
            resource_id="T-99",
            cost_usd=0.42,
            currency="EUR",
            period_start="2024-01-01T00:00:00Z",
            period_end="2024-01-02T00:00:00Z",
            labels={"role": "backend"},
            namespace="prod",
            account_id="acc-123",
        )
        assert alloc.resource_id == "T-99"
        assert alloc.cost_usd == pytest.approx(0.42)
        assert alloc.currency == "EUR"
        assert alloc.namespace == "prod"
        assert alloc.account_id == "acc-123"
        assert alloc.labels == {"role": "backend"}


# ---------------------------------------------------------------------------
# CostExportConfig dataclass
# ---------------------------------------------------------------------------


class TestCostExportConfig:
    """CostExportConfig is frozen and stores expected fields."""

    def test_frozen(self) -> None:
        cfg = CostExportConfig(platform=CostPlatform.GENERIC)
        with pytest.raises(AttributeError):
            cfg.platform = CostPlatform.KUBECOST  # type: ignore[misc]

    def test_defaults(self) -> None:
        cfg = CostExportConfig(platform=CostPlatform.CLOUDHEALTH)
        assert cfg.platform == CostPlatform.CLOUDHEALTH
        assert cfg.api_endpoint is None
        assert cfg.cost_center is None
        assert cfg.project_tag is None
        assert cfg.namespace is None

    def test_full_construction(self) -> None:
        cfg = CostExportConfig(
            platform=CostPlatform.KUBECOST,
            api_endpoint="https://kubecost.internal/api",
            cost_center="eng-42",
            project_tag="bernstein",
            namespace="orchestration",
        )
        assert cfg.api_endpoint == "https://kubecost.internal/api"
        assert cfg.cost_center == "eng-42"
        assert cfg.project_tag == "bernstein"
        assert cfg.namespace == "orchestration"


# ---------------------------------------------------------------------------
# CostExporter.export_run_costs
# ---------------------------------------------------------------------------


class TestExportRunCosts:
    """CostExporter.export_run_costs reads archive and builds allocations."""

    def test_returns_allocations(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        config = _make_config()
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        assert len(allocs) == 4

    def test_run_id_in_labels(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        config = _make_config()
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-42", archive, config)
        for alloc in allocs:
            assert alloc.labels["run_id"] == "run-42"

    def test_cost_center_propagated(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        config = _make_config(cost_center="platform-eng")
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        for alloc in allocs:
            assert alloc.labels["cost_center"] == "platform-eng"

    def test_project_tag_propagated(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        config = _make_config(project_tag="bernstein-v2")
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        for alloc in allocs:
            assert alloc.labels["project"] == "bernstein-v2"

    def test_namespace_propagated(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        config = _make_config(namespace="prod")
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        for alloc in allocs:
            assert alloc.namespace == "prod"

    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, [])
        config = _make_config()
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        assert allocs == []

    def test_missing_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "does_not_exist.jsonl"
        config = _make_config()
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        assert allocs == []

    def test_records_without_cost_skipped(self, tmp_path: Path) -> None:
        records = [{"task_id": "T-X", "role": "qa"}]
        archive = _write_archive(tmp_path, records)
        config = _make_config()
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        assert allocs == []

    def test_records_without_id_skipped(self, tmp_path: Path) -> None:
        records = [{"cost_usd": 0.01, "role": "qa"}]
        archive = _write_archive(tmp_path, records)
        config = _make_config()
        exporter = CostExporter()
        allocs = exporter.export_run_costs("run-1", archive, config)
        assert allocs == []


# ---------------------------------------------------------------------------
# build_cloudhealth_payload
# ---------------------------------------------------------------------------


class TestBuildCloudhealthPayload:
    """CloudHealth custom charges API format."""

    def test_structure(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10, labels={"role": "qa"})]
        exporter = CostExporter()
        payload = exporter.build_cloudhealth_payload(allocs)
        assert "custom_charges" in payload
        assert "total" in payload
        assert "currency" in payload
        assert payload["currency"] == "USD"

    def test_line_items(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10, period_start="2024-01-01T00:00:00Z"),
            CostAllocation(resource_id="T-2", cost_usd=0.20, period_start="2024-01-02T00:00:00Z"),
        ]
        exporter = CostExporter()
        payload = exporter.build_cloudhealth_payload(allocs)
        assert len(payload["custom_charges"]) == 2
        assert payload["total"] == pytest.approx(0.30)

    def test_tags_included(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.05, labels={"role": "backend", "model": "sonnet"})]
        exporter = CostExporter()
        payload = exporter.build_cloudhealth_payload(allocs)
        item = payload["custom_charges"][0]
        assert item["tags"] == {"role": "backend", "model": "sonnet"}

    def test_account_id_included_when_present(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.05, account_id="acc-1")]
        exporter = CostExporter()
        payload = exporter.build_cloudhealth_payload(allocs)
        assert payload["custom_charges"][0]["account_id"] == "acc-1"

    def test_empty_allocations(self) -> None:
        exporter = CostExporter()
        payload = exporter.build_cloudhealth_payload([])
        assert payload["custom_charges"] == []
        assert payload["total"] == 0.0


# ---------------------------------------------------------------------------
# build_kubecost_payload
# ---------------------------------------------------------------------------


class TestBuildKubecostPayload:
    """Kubecost allocation API format."""

    def test_structure(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10)]
        exporter = CostExporter()
        payload = exporter.build_kubecost_payload(allocs)
        assert payload["code"] == 200
        assert "data" in payload
        assert "totalCost" in payload

    def test_namespace_defaults_to_default(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10)]
        exporter = CostExporter()
        payload = exporter.build_kubecost_payload(allocs)
        item = payload["data"][0]
        assert item["properties"]["namespace"] == "default"

    def test_custom_namespace(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10, namespace="prod")]
        exporter = CostExporter()
        payload = exporter.build_kubecost_payload(allocs)
        item = payload["data"][0]
        assert item["properties"]["namespace"] == "prod"

    def test_total_cost_summed(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10),
            CostAllocation(resource_id="T-2", cost_usd=0.25),
        ]
        exporter = CostExporter()
        payload = exporter.build_kubecost_payload(allocs)
        assert payload["totalCost"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# build_spotio_payload
# ---------------------------------------------------------------------------


class TestBuildSpotioPayload:
    """Spot.io billing events format."""

    def test_structure(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10)]
        exporter = CostExporter()
        payload = exporter.build_spotio_payload(allocs)
        assert "events" in payload
        assert "count" in payload
        assert "totalCost" in payload

    def test_event_type(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10)]
        exporter = CostExporter()
        payload = exporter.build_spotio_payload(allocs)
        assert payload["events"][0]["eventType"] == "cost_allocation"

    def test_namespace_included_when_present(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10, namespace="staging")]
        exporter = CostExporter()
        payload = exporter.build_spotio_payload(allocs)
        assert payload["events"][0]["namespace"] == "staging"

    def test_namespace_absent_when_none(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10)]
        exporter = CostExporter()
        payload = exporter.build_spotio_payload(allocs)
        assert "namespace" not in payload["events"][0]

    def test_count_matches_events(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10),
            CostAllocation(resource_id="T-2", cost_usd=0.20),
            CostAllocation(resource_id="T-3", cost_usd=0.30),
        ]
        exporter = CostExporter()
        payload = exporter.build_spotio_payload(allocs)
        assert payload["count"] == 3
        assert len(payload["events"]) == 3


# ---------------------------------------------------------------------------
# build_generic_csv
# ---------------------------------------------------------------------------


class TestBuildGenericCsv:
    """Generic CSV export."""

    def test_header_row(self) -> None:
        exporter = CostExporter()
        csv_str = exporter.build_generic_csv([])
        reader = csv.reader(io.StringIO(csv_str))
        header = next(reader)
        assert "resource_id" in header
        assert "cost_usd" in header
        assert "labels" in header

    def test_row_count(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10),
            CostAllocation(resource_id="T-2", cost_usd=0.20),
        ]
        exporter = CostExporter()
        csv_str = exporter.build_generic_csv(allocs)
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 2

    def test_cost_precision(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.123456)]
        exporter = CostExporter()
        csv_str = exporter.build_generic_csv(allocs)
        reader = csv.DictReader(io.StringIO(csv_str))
        row = next(reader)
        assert row["cost_usd"] == "0.123456"

    def test_labels_serialised_as_json(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10, labels={"role": "qa"})]
        exporter = CostExporter()
        csv_str = exporter.build_generic_csv(allocs)
        reader = csv.DictReader(io.StringIO(csv_str))
        row = next(reader)
        parsed = json.loads(row["labels"])
        assert parsed == {"role": "qa"}


# ---------------------------------------------------------------------------
# get_headers
# ---------------------------------------------------------------------------


class TestGetHeaders:
    """Per-platform HTTP header generation."""

    def test_cloudhealth_has_bearer(self) -> None:
        cfg = CostExportConfig(platform=CostPlatform.CLOUDHEALTH)
        headers = CostExporter().get_headers(cfg)
        assert "Authorization" in headers
        assert headers["Content-Type"] == "application/json"

    def test_kubecost_no_auth(self) -> None:
        cfg = CostExportConfig(platform=CostPlatform.KUBECOST)
        headers = CostExporter().get_headers(cfg)
        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/json"

    def test_spotio_has_bearer(self) -> None:
        cfg = CostExportConfig(platform=CostPlatform.SPOT_IO)
        headers = CostExporter().get_headers(cfg)
        assert "Authorization" in headers

    def test_generic_empty_headers(self) -> None:
        cfg = CostExportConfig(platform=CostPlatform.GENERIC)
        headers = CostExporter().get_headers(cfg)
        assert headers == {}


# ---------------------------------------------------------------------------
# aggregate_costs_by_role
# ---------------------------------------------------------------------------


class TestAggregateCostsByRole:
    """Role-level cost aggregation."""

    def test_groups_by_role(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        result = aggregate_costs_by_role(archive)
        assert "backend" in result
        assert "frontend" in result
        assert "qa" in result

    def test_backend_total(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        result = aggregate_costs_by_role(archive)
        # T-001 (0.05) + T-003 (0.15) = 0.20
        assert result["backend"] == pytest.approx(0.20)

    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, [])
        result = aggregate_costs_by_role(archive)
        assert result == {}

    def test_missing_archive(self, tmp_path: Path) -> None:
        result = aggregate_costs_by_role(tmp_path / "missing.jsonl")
        assert result == {}


# ---------------------------------------------------------------------------
# aggregate_costs_by_model
# ---------------------------------------------------------------------------


class TestAggregateCostsByModel:
    """Model-level cost aggregation."""

    def test_groups_by_model(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        result = aggregate_costs_by_model(archive)
        assert "sonnet" in result
        assert "haiku" in result
        assert "opus" in result

    def test_sonnet_total(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, _sample_records())
        result = aggregate_costs_by_model(archive)
        # T-001 (0.05) + T-004 (0.03) = 0.08
        assert result["sonnet"] == pytest.approx(0.08)

    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = _write_archive(tmp_path, [])
        result = aggregate_costs_by_model(archive)
        assert result == {}

    def test_falls_back_to_assigned_model(self, tmp_path: Path) -> None:
        records = [{"task_id": "T-X", "assigned_model": "opus", "cost_usd": 0.10}]
        archive = _write_archive(tmp_path, records)
        result = aggregate_costs_by_model(archive)
        assert "opus" in result
        assert result["opus"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# render_cost_allocation_report
# ---------------------------------------------------------------------------


class TestRenderCostAllocationReport:
    """Markdown report rendering."""

    def test_empty_allocations(self) -> None:
        report = render_cost_allocation_report([])
        assert "No allocations" in report

    def test_contains_header(self) -> None:
        allocs = [CostAllocation(resource_id="T-1", cost_usd=0.10, labels={"role": "qa", "model": "sonnet"})]
        report = render_cost_allocation_report(allocs)
        assert "## Cost Allocation Report" in report

    def test_contains_total(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10, labels={"role": "qa"}),
            CostAllocation(resource_id="T-2", cost_usd=0.20, labels={"role": "backend"}),
        ]
        report = render_cost_allocation_report(allocs)
        assert "$0.3000" in report

    def test_by_role_section(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10, labels={"role": "qa"}),
            CostAllocation(resource_id="T-2", cost_usd=0.20, labels={"role": "backend"}),
        ]
        report = render_cost_allocation_report(allocs)
        assert "### By Role" in report
        assert "qa" in report
        assert "backend" in report

    def test_by_model_section(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.10, labels={"model": "sonnet"}),
            CostAllocation(resource_id="T-2", cost_usd=0.20, labels={"model": "opus"}),
        ]
        report = render_cost_allocation_report(allocs)
        assert "### By Model" in report
        assert "sonnet" in report
        assert "opus" in report

    def test_percentage_column(self) -> None:
        allocs = [
            CostAllocation(resource_id="T-1", cost_usd=0.75, labels={"role": "backend"}),
            CostAllocation(resource_id="T-2", cost_usd=0.25, labels={"role": "qa"}),
        ]
        report = render_cost_allocation_report(allocs)
        assert "75.0%" in report
        assert "25.0%" in report
