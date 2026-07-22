"""CLI tests for ``bernstein telemetry export-otel`` (#2526).

The backfill command projects a completed run's journal into the
deterministic span set and pushes it over OTLP. Tests inject an in-memory
span exporter (or use ``--dry-run``) so nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from bernstein.cli.commands.telemetry_cmd import telemetry_group
from bernstein.core.observability.otel_bridge import JournalOTLPBridge
from bernstein.core.replay.journal import EventJournal

_RUN_ID = "run-1"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a recorded run journal and isolated keys."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.setenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", str(tmp_path / "install.key"))
    monkeypatch.delenv("BERNSTEIN_OTEL_ENDPOINT", raising=False)

    journal = EventJournal(_RUN_ID, tmp_path / ".sdd")
    journal.record("run_started", goal="ship")
    journal.record("agent_spawned", agent_id="a1")
    journal.record("task_claimed", task_id="t1")
    journal.record("task_completed", task_id="t1")
    journal.record("agent_reaped", agent_id="a1")
    journal.record("run_completed", ok=True)
    return tmp_path


@pytest.fixture
def in_memory_wire(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route the command's bridge onto an in-memory exporter (no network)."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(JournalOTLPBridge, "_build_otlp_exporter", lambda self: exporter)
    return exporter


def test_export_pushes_journal_anchored_spans(project: Path, in_memory_wire: InMemorySpanExporter) -> None:
    runner = CliRunner()
    result = runner.invoke(
        telemetry_group,
        ["export-otel", "--run", _RUN_ID, "-w", str(project), "--endpoint", "http://collector:4317"],
    )
    assert result.exit_code == 0, result.output
    assert "exported 6 journal-anchored spans" in result.output

    spans = in_memory_wire.get_finished_spans()
    assert len(spans) == 6
    for span in spans:
        assert span.attributes["bernstein.journal.entry_hash"]
        assert span.attributes["bernstein.audit.anchor"]
        assert span.attributes["bernstein.run.id"] == _RUN_ID


def test_export_records_audit_event(project: Path, in_memory_wire: InMemorySpanExporter) -> None:
    runner = CliRunner()
    result = runner.invoke(
        telemetry_group,
        ["export-otel", "--run", _RUN_ID, "-w", str(project), "--endpoint", "http://collector:4317"],
    )
    assert result.exit_code == 0, result.output

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    chain = AuditChainStore(project / ".sdd" / "audit", key=load_or_create_audit_key())
    events = chain.query(event_type=EVENT_OTEL_PROJECTION)
    assert len(events) == 1
    assert events[0].details["run_id"] == _RUN_ID
    assert events[0].details["span_count"] == 6


def test_dry_run_prints_deterministic_otlp_json(project: Path) -> None:
    """Two dry runs over the same journal print byte-identical OTLP JSON."""
    runner = CliRunner()
    first = runner.invoke(telemetry_group, ["export-otel", "--run", _RUN_ID, "-w", str(project), "--dry-run"])
    second = runner.invoke(telemetry_group, ["export-otel", "--run", _RUN_ID, "-w", str(project), "--dry-run"])
    assert first.exit_code == 0, first.output
    assert first.output == second.output

    spans = json.loads(first.output)
    assert len(spans) == 6
    for span in spans:
        assert len(span["traceId"]) == 32
        assert len(span["spanId"]) == 16
        attribute_keys = {attribute["key"] for attribute in span["attributes"]}
        assert "bernstein.audit.anchor" in attribute_keys
        assert "bernstein.run.id" in attribute_keys
        assert span["startTimeUnixNano"] == span["endTimeUnixNano"]


def test_dry_run_records_no_audit_event(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(telemetry_group, ["export-otel", "--run", _RUN_ID, "-w", str(project), "--dry-run"])
    assert result.exit_code == 0, result.output

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    chain = AuditChainStore(project / ".sdd" / "audit", key=load_or_create_audit_key())
    assert chain.query(event_type=EVENT_OTEL_PROJECTION) == []


def test_missing_journal_errors(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(telemetry_group, ["export-otel", "--run", "no-such-run", "-w", str(project)])
    assert result.exit_code == 1
    assert "no event journal" in result.output


def test_traversal_run_id_refused_before_any_read(project: Path) -> None:
    """A run id that escapes the runs root is refused by the containment barrier."""
    runner = CliRunner()
    result = runner.invoke(telemetry_group, ["export-otel", "--run", "../evil", "-w", str(project)])
    assert result.exit_code == 1
    assert "unsafe run_id" in result.output


def test_no_endpoint_errors_with_guidance(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(telemetry_group, ["export-otel", "--run", _RUN_ID, "-w", str(project)])
    assert result.exit_code == 1
    assert "no OTLP endpoint configured" in result.output


def test_endpoint_override_preserves_headers_and_insecure(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--endpoint`` overrides only the endpoint, keeping env-derived auth/TLS.

    Rebuilding a fresh config from endpoint + service_name alone would drop
    the ``headers`` (auth) and ``insecure`` (TLS) fields; the override must
    preserve them.
    """
    from bernstein.core.observability.otlp_exporter import OTLPExporterConfig

    base = OTLPExporterConfig(
        endpoint="http://env-default:4317",
        service_name="svc",
        insecure=False,
        headers={"authorization": "Bearer secret"},
    )
    monkeypatch.setattr(
        OTLPExporterConfig,
        "from_env",
        classmethod(lambda cls, env=None: base),
    )

    captured: dict[str, OTLPExporterConfig] = {}
    exporter = InMemorySpanExporter()

    def build(self: JournalOTLPBridge) -> InMemorySpanExporter:
        captured["config"] = self._config
        return exporter

    monkeypatch.setattr(JournalOTLPBridge, "_build_otlp_exporter", build)

    runner = CliRunner()
    result = runner.invoke(
        telemetry_group,
        ["export-otel", "--run", _RUN_ID, "-w", str(project), "--endpoint", "http://override:4317"],
    )
    assert result.exit_code == 0, result.output

    config = captured["config"]
    assert config.endpoint == "http://override:4317"
    assert config.service_name == "svc"
    assert config.headers == {"authorization": "Bearer secret"}
    assert config.insecure is False


def test_failed_export_exits_nonzero_and_records_no_audit_event(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A collector FAILURE must exit nonzero and record no success audit event."""
    from opentelemetry.sdk.trace.export import SpanExportResult

    class _FailingExporter:
        def export(self, spans: object) -> SpanExportResult:
            return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(JournalOTLPBridge, "_build_otlp_exporter", lambda self: _FailingExporter())

    runner = CliRunner()
    result = runner.invoke(
        telemetry_group,
        ["export-otel", "--run", _RUN_ID, "-w", str(project), "--endpoint", "http://collector:4317"],
    )
    assert result.exit_code == 1
    assert "OTLP export failed" in result.output
    assert "exported" not in result.output

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import EVENT_OTEL_PROJECTION, AuditChainStore

    chain = AuditChainStore(project / ".sdd" / "audit", key=load_or_create_audit_key())
    assert chain.query(event_type=EVENT_OTEL_PROJECTION) == []


def test_export_ids_match_offline_projection(project: Path, in_memory_wire: InMemorySpanExporter) -> None:
    """The backfilled wire ids equal the offline signed projection's ids."""
    from bernstein.core.observability.otel_projection import project_spans
    from bernstein.core.replay.journal import JOURNAL_FILENAME, load_events

    runner = CliRunner()
    result = runner.invoke(
        telemetry_group,
        ["export-otel", "--run", _RUN_ID, "-w", str(project), "--endpoint", "http://collector:4317"],
    )
    assert result.exit_code == 0, result.output

    events = load_events(project / ".sdd" / "runs" / _RUN_ID / JOURNAL_FILENAME)
    projection = project_spans(events, run_id=_RUN_ID)
    wire_ids = [format(s.context.span_id, "016x") for s in in_memory_wire.get_finished_spans()]
    assert wire_ids == [s.span_id for s in projection.spans]
