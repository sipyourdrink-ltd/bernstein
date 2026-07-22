"""Tests for ``bernstein telemetry verify-span`` (#2526, Phase 3).

``verify-span`` proves an exported OTLP span against the run journal and the
audit chain: the span id must recompute from the ``bernstein.journal.entry_hash``
it carries (the same derivation the export bridge used), that entry must exist
in the run's journal, and the ``bernstein.audit.anchor`` must resolve to the
run's ``otel.projection`` audit event. A genuine exported span is accepted
(exit 0); a span whose id does not recompute or whose anchor mismatches is
rejected as a forgery (exit 1). Nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.telemetry_cmd import telemetry_group
from bernstein.core.observability.otel_bridge import (
    parse_exported_span,
    projection_to_otlp_json_spans,
    record_projection_audit_event,
    verify_exported_span,
)
from bernstein.core.observability.otel_projection import (
    derive_span_id,
    project_spans,
    sign_projection,
)
from bernstein.core.replay.journal import EventJournal, load_events, run_journal_path
from bernstein.core.security.audit_dsse import keyid_from_public_key
from bernstein.core.security.install_key import load_or_create_install_key, signing_key_path

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


def _exported_spans(project_root: Path, *, record_audit: bool = True) -> list[dict[str, Any]]:
    """Produce the run's genuine exported OTLP/JSON spans.

    Optionally records the ``otel.projection`` audit event so the exported
    spans' anchor resolves to the chain (the state after a real export).
    """
    events = load_events(run_journal_path(project_root / ".sdd", _RUN_ID))
    key = load_or_create_install_key(signing_key_path(project_root))
    projection = project_spans(events, run_id=_RUN_ID, keyid=keyid_from_public_key(key.public_key()))
    signed = sign_projection(projection, signing_key=key)
    if record_audit:
        record_projection_audit_event(
            workdir=project_root,
            journal_path=run_journal_path(project_root / ".sdd", _RUN_ID),
            run_id=_RUN_ID,
        )
    return projection_to_otlp_json_spans(signed, events)


def _set_attr(span: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    """Return a deep copy of ``span`` with attribute ``key`` set to ``value``."""
    mutated = json.loads(json.dumps(span))
    for item in mutated["attributes"]:
        if item["key"] == key:
            item["value"] = {"stringValue": value}
    return mutated


def _write(tmp: Path, span: dict[str, Any]) -> Path:
    path = tmp / "span.json"
    path.write_text(json.dumps(span), encoding="utf-8")
    return path


def _invoke(args: list[str], **kwargs: Any) -> Any:
    return CliRunner().invoke(telemetry_group, args, **kwargs)


# --------------------------------------------------------------------------- #
# Genuine spans are accepted                                                   #
# --------------------------------------------------------------------------- #


def test_genuine_root_span_verifies_exit_zero(project: Path) -> None:
    spans = _exported_spans(project)
    span_file = _write(project, spans[0])  # invoke_workflow root
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 0, result.output
    assert "genuine" in result.output.lower()


def test_genuine_leaf_span_verifies_exit_zero(project: Path) -> None:
    spans = _exported_spans(project)
    span_file = _write(project, spans[2])  # a non-root execute_tool span
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 0, result.output
    assert "genuine" in result.output.lower()


def test_genuine_span_from_stdin(project: Path) -> None:
    spans = _exported_spans(project)
    result = _invoke(
        ["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", "@-"],
        input=json.dumps(spans[0]),
    )
    assert result.exit_code == 0, result.output
    assert "genuine" in result.output.lower()


# --------------------------------------------------------------------------- #
# Forgeries are rejected (hard fail, nonzero)                                  #
# --------------------------------------------------------------------------- #


def test_tampered_span_id_rejected(project: Path) -> None:
    spans = _exported_spans(project)
    forged = json.loads(json.dumps(spans[0]))
    forged["spanId"] = "0000000000000000"  # id no longer recomputes from entry_hash
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "forged" in result.output.lower()
    assert "recompute" in result.output.lower()


def test_tampered_entry_hash_rejected(project: Path) -> None:
    spans = _exported_spans(project)
    forged = _set_attr(spans[0], "bernstein.journal.entry_hash", "de" * 32)
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "forged" in result.output.lower()


def test_wrong_anchor_rejected(project: Path) -> None:
    spans = _exported_spans(project)
    forged = _set_attr(spans[0], "bernstein.audit.anchor", "ab" * 32)
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "forged" in result.output.lower()
    assert "anchor" in result.output.lower()


def test_run_id_mismatch_rejected(project: Path) -> None:
    spans = _exported_spans(project)
    forged = _set_attr(spans[0], "bernstein.run.id", "some-other-run")
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "forged" in result.output.lower()


# --------------------------------------------------------------------------- #
# Unverifiable (never anchored) is nonzero and distinct from forgery           #
# --------------------------------------------------------------------------- #


def test_unanchored_span_is_unverifiable(project: Path) -> None:
    spans = _exported_spans(project, record_audit=False)  # no otel.projection event
    span_file = _write(project, spans[0])
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "unverifiable" in result.output.lower()


def test_missing_journal_is_unverifiable(project: Path) -> None:
    spans = _exported_spans(project)
    # Drop the run-id attribute so the run cross-check does not fire; the point
    # here is that a run whose journal is absent cannot be proven either way.
    stripped = json.loads(json.dumps(spans[0]))
    stripped["attributes"] = [a for a in stripped["attributes"] if a["key"] != "bernstein.run.id"]
    span_file = _write(project, stripped)
    result = _invoke(["verify-span", "--run", "gone-run", "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "unverifiable" in result.output.lower()


def test_bad_json_errors(project: Path) -> None:
    span_file = project / "bad.json"
    span_file.write_text("{not json", encoding="utf-8")
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "json" in result.output.lower()


# --------------------------------------------------------------------------- #
# Determinism                                                                  #
# --------------------------------------------------------------------------- #


def test_verdict_is_deterministic(project: Path) -> None:
    spans = _exported_spans(project)
    span_file = _write(project, spans[0])
    first = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    second = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert first.exit_code == 0
    assert first.output == second.output


# --------------------------------------------------------------------------- #
# Pure-function surface (no CLI, reuses the shared derivation)                 #
# --------------------------------------------------------------------------- #


def test_verify_exported_span_recompute_uses_shared_derivation(project: Path) -> None:
    spans = _exported_spans(project)
    events = load_events(run_journal_path(project / ".sdd", _RUN_ID))
    projections = [
        {
            "run_id": _RUN_ID,
            "trace_id": spans[0]["traceId"],
            "journal_head": str(events[-1]["event_hash"]),
        }
    ]

    genuine = verify_exported_span(parse_exported_span(spans[0]), events, projections, run_id=_RUN_ID)
    assert genuine.ok is True

    forged = json.loads(json.dumps(spans[0]))
    forged["spanId"] = derive_span_id("not-a-real-entry-hash")  # a valid-shaped but wrong id
    verdict = verify_exported_span(parse_exported_span(forged), events, projections, run_id=_RUN_ID)
    assert verdict.ok is False
    assert verdict.unverifiable is False
