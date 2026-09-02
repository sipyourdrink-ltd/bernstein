"""CLI tests for ``bernstein governance ingest`` (#4962).

The ingest boundary shipped as a library with no way to reach it: an operator
holding a file of OTLP spans from a runtime Bernstein did not schedule had no
command that would anchor them. These tests pin the first transport -- a file
and stdin -- and the two properties the boundary owes a caller: a malformed
payload is rejected in its entirety, and a repeated submission does not grow
the chain.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import governance_group
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path

SPAN_EVENT = "otlp_ingest_receipt.foreign_span"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with an isolated, repo-local audit key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _spans() -> list[dict[str, Any]]:
    return [
        {
            "traceId": "a" * 32,
            "spanId": "b" * 16,
            "name": "gen_ai.chat",
            "kind": "SPAN_KIND_CLIENT",
            "attributes": {
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": "claude-sonnet-4-6",
                "gen_ai.operation.name": "chat",
            },
        }
    ]


def _chain(project: Path) -> AuditChainStore:
    return AuditChainStore(project / ".sdd" / "audit", key=load_or_create_audit_key(project / "audit.key"))


def _span_events(project: Path) -> list[Any]:
    return _chain(project).query(event_type=SPAN_EVENT)


def test_ingest_from_a_file_anchors_spans_and_prints_a_receipt(project: Path) -> None:
    """A well-formed file lands in the chain and the receipt names its coverage."""
    payload = project / "spans.json"
    payload.write_text(json.dumps(_spans()), encoding="utf-8")

    result = CliRunner().invoke(
        governance_group,
        ["ingest", "--spans", str(payload), "--source", "collector-prod", "--workdir", str(project), "--json"],
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["source_label"] == "collector-prod"
    assert receipt["span_count"] == 1
    assert receipt["coverage"] == "not_scheduled_by_bernstein"
    assert receipt["signature"]
    assert len(_span_events(project)) == 1

    ok, problems = _chain(project).verify()
    assert ok, problems


def test_ingest_from_stdin_anchors_spans(project: Path) -> None:
    """``--spans -`` reads the payload from stdin, so a collector can pipe into the boundary."""
    result = CliRunner().invoke(
        governance_group,
        ["ingest", "--spans", "-", "--source", "collector-prod", "--workdir", str(project), "--json"],
        input=json.dumps(_spans()),
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["span_count"] == 1
    assert len(_span_events(project)) == 1


def test_malformed_payload_is_rejected_and_appends_nothing(project: Path) -> None:
    """A span missing its span id fails the whole submission; the chain is untouched."""
    payload = project / "spans.json"
    payload.write_text(json.dumps([{"traceId": "a" * 32, "name": "gen_ai.chat"}]), encoding="utf-8")

    result = CliRunner().invoke(
        governance_group,
        ["ingest", "--spans", str(payload), "--source", "collector-prod", "--workdir", str(project)],
    )

    assert result.exit_code == 1
    assert _span_events(project) == []


def test_repeated_ingest_of_the_same_file_does_not_grow_the_chain(project: Path) -> None:
    """Running the command twice over one file anchors the spans once."""
    payload = project / "spans.json"
    payload.write_text(json.dumps(_spans()), encoding="utf-8")
    argv = ["ingest", "--spans", str(payload), "--source", "collector-prod", "--workdir", str(project), "--json"]

    runner = CliRunner()
    first = runner.invoke(governance_group, argv)
    second = runner.invoke(governance_group, argv)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert len(_span_events(project)) == 1
    assert json.loads(second.output)["signature"] == json.loads(first.output)["signature"]
