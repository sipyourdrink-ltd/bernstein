"""OpenLineage lineage export projection (issue #4914).

Mechanical proofs required by the issue — not eyeballing JSON:

* exporting the same finished run twice is byte-identical
* every event validates against the vendored OpenLineage JSON schema
* two tasks with a dependency produce matching parent/child + dataset edges
* tampering with an exported event fails offline facet verification
* a failed task emits FAIL, not COMPLETE
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
from click.testing import CliRunner

from bernstein.cli.commands.lineage_export_cmd import lineage_export_cmd
from bernstein.core.persistence.lineage import AgentRef, ArtifactRef, LineageRecord, LineageWriter
from bernstein.core.persistence.openlineage_export import (
    export_openlineage,
    iter_lineage_chain_positions,
    openlineage_schema_path,
    task_dependency_edges,
    verify_openlineage_event,
)


def _sha(n: int) -> str:
    return f"{n:064x}"


def _emit_dependent_run(sdd: Path, run_id: str = "run-ol") -> None:
    """Two tasks: backend writes mid.py; frontend consumes mid.py → out.py."""
    writer = LineageWriter.for_run(run_id, sdd)
    writer.emit(
        LineageRecord(
            output_artifact=ArtifactRef(path="src/mid.py", sha256=_sha(1), line_start=1, line_end=10),
            inputs=[ArtifactRef(path="src/in.py", sha256=_sha(0))],
            producer=AgentRef(agent_id="backend", run_id=run_id, tick_id="t-1"),
            prompt_sha=_sha(9),
            model="gpt-4.1-mini",
            cost_usd=0.01,
            tokens=100,
            timestamp=1_700_000_000.0,
            customer_signature="c2lnbmF0dXJl",
        )
    )
    writer.emit(
        LineageRecord(
            output_artifact=ArtifactRef(path="src/out.py", sha256=_sha(2), line_start=1, line_end=20),
            inputs=[ArtifactRef(path="src/mid.py", sha256=_sha(1))],
            producer=AgentRef(agent_id="frontend", run_id=run_id, tick_id="t-2"),
            prompt_sha=_sha(8),
            model="gemini-2.5-pro",
            cost_usd=0.02,
            tokens=200,
            timestamp=1_700_000_010.0,
        )
    )


def _run_event_validator() -> jsonschema.Draft202012Validator:
    raw = json.loads(openlineage_schema_path().read_text(encoding="utf-8"))
    schema = {
        "$schema": raw["$schema"],
        "$id": raw["$id"],
        "$defs": raw["$defs"],
        "$ref": "#/$defs/RunEvent",
    }

    def _no_remote(uri: str) -> Any:
        raise jsonschema.exceptions.RefResolutionError(f"refused remote/missing ref: {uri}")

    resolver = jsonschema.RefResolver.from_schema(schema, handlers={"https": _no_remote, "http": _no_remote})
    return jsonschema.Draft202012Validator(schema, resolver=resolver)


def test_double_export_byte_identical(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    first = export_openlineage(sdd, "run-ol")
    second = export_openlineage(sdd, "run-ol")
    assert first.payload == second.payload
    assert first.payload.endswith(b"\n")
    for event in first.events:
        assert "exported_at" not in event
        assert "generated_at" not in json.dumps(event)


def test_every_event_validates_against_vendored_schema(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    result = export_openlineage(sdd, "run-ol")
    validator = _run_event_validator()
    assert result.events
    for event in result.events:
        validator.validate(event)


def test_parent_child_and_dependency_edges(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    result = export_openlineage(sdd, "run-ol")
    positions = iter_lineage_chain_positions(sdd, "run-ol")
    assert task_dependency_edges(positions) == [("backend", "frontend")]

    parent_jobs = {e["job"]["name"] for e in result.events if e["job"]["name"] == "run/run-ol"}
    assert parent_jobs == {"run/run-ol"}
    task_jobs = sorted({e["job"]["name"] for e in result.events if "/task/" in e["job"]["name"]})
    assert task_jobs == ["run/run-ol/task/backend", "run/run-ol/task/frontend"]

    frontend_complete = next(
        e for e in result.events if e["job"]["name"].endswith("/task/frontend") and e["eventType"] == "COMPLETE"
    )
    assert frontend_complete["run"]["facets"]["parent"]["job"]["name"] == "run/run-ol"
    input_names = {d["name"] for d in frontend_complete["inputs"]}
    assert "src/mid.py" in input_names


def test_tampering_fails_facet_verification(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    event = json.loads(json.dumps(export_openlineage(sdd, "run-ol").events[0]))
    assert verify_openlineage_event(event) is True
    event["job"] = {**event["job"], "name": "run/run-ol-tampered"}
    assert verify_openlineage_event(event) is False


def test_failed_task_emits_fail_not_complete(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    result = export_openlineage(sdd, "run-ol", task_outcomes={"frontend": "FAIL"})
    frontend_terminals = [
        e["eventType"]
        for e in result.events
        if e["job"]["name"].endswith("/task/frontend") and e["eventType"] != "START"
    ]
    assert frontend_terminals == ["FAIL"]
    assert "COMPLETE" not in frontend_terminals
    run_terminals = [
        e["eventType"] for e in result.events if e["job"]["name"] == "run/run-ol" and e["eventType"] != "START"
    ]
    assert run_terminals == ["FAIL"]


def test_cli_openlineage_writes_file(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    out = tmp_path / "ol.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        lineage_export_cmd,
        ["run-ol", "--format", "openlineage", "--output", str(out), "--workdir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 4
    again = export_openlineage(sdd, "run-ol").payload
    assert out.read_bytes() == again


def test_chain_facet_binds_wal_entry_hash(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _emit_dependent_run(sdd)
    positions = iter_lineage_chain_positions(sdd, "run-ol")
    hashes = {p.entry_hash for p in positions}
    result = export_openlineage(sdd, "run-ol")
    task_events = [e for e in result.events if "/task/" in e["job"]["name"]]
    for event in task_events:
        facet = event["run"]["facets"]["bernstein_chain"]
        assert facet["chain_head_hash"] in hashes
        assert facet["lineage_record_id"] == facet["chain_head_hash"]
        assert verify_openlineage_event(event, expected_chain_head=facet["chain_head_hash"])


def test_schema_file_is_vendored() -> None:
    path = openlineage_schema_path()
    assert path.is_file()
    assert '"RunEvent"' in path.read_text(encoding="utf-8")
