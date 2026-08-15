"""Tests for ``bernstein telemetry verify-span`` (#2526, Phase 3; #3256).

``verify-span`` proves an exported OTLP span against the run journal and the
audit chain: the span id must recompute from the ``bernstein.journal.entry_hash``
it carries (the same derivation the export bridge used), that entry must exist
in the run's journal, and the ``bernstein.audit.anchor`` must resolve to the
run's ``otel.projection`` audit event. A genuine exported span is accepted
(exit 0); an input that cannot be proven either way -- missing journal,
malformed span, never-anchored run -- exits 1; a span whose id does not
recompute or whose anchor mismatches is rejected as a forgery (exit 2). The
table is the one ``trace verify-projection`` has shipped since v3.9.0, and a
cross-command test here pins the two to it. Nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd
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
from bernstein.core.security.audit import AuditLog, RetentionPolicy
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
    events = load_events(run_journal_path(project_root / ".sdd", _RUN_ID)).events
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
# Forgeries are rejected (hard fail, exit 2)                                   #
# --------------------------------------------------------------------------- #


def test_tampered_span_id_rejected_exit_two(project: Path) -> None:
    spans = _exported_spans(project)
    forged = json.loads(json.dumps(spans[0]))
    forged["spanId"] = "0000000000000000"  # id no longer recomputes from entry_hash
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 2
    assert "forged" in result.output.lower()
    assert "recompute" in result.output.lower()


def test_tampered_entry_hash_rejected_exit_two(project: Path) -> None:
    spans = _exported_spans(project)
    forged = _set_attr(spans[0], "bernstein.journal.entry_hash", "de" * 32)
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 2
    assert "forged" in result.output.lower()


def test_wrong_anchor_rejected_exit_two(project: Path) -> None:
    spans = _exported_spans(project)
    forged = _set_attr(spans[0], "bernstein.audit.anchor", "ab" * 32)
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 2
    assert "forged" in result.output.lower()
    assert "anchor" in result.output.lower()


def test_run_id_mismatch_rejected_exit_two(project: Path) -> None:
    spans = _exported_spans(project)
    forged = _set_attr(spans[0], "bernstein.run.id", "some-other-run")
    span_file = _write(project, forged)
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 2
    assert "forged" in result.output.lower()


# --------------------------------------------------------------------------- #
# Unverifiable (cannot be evaluated) exits 1, distinct from forgery's 2        #
# --------------------------------------------------------------------------- #


def test_unanchored_span_is_unverifiable_exit_one(project: Path) -> None:
    spans = _exported_spans(project, record_audit=False)  # no otel.projection event
    span_file = _write(project, spans[0])
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "unverifiable" in result.output.lower()


def test_missing_journal_is_unverifiable_exit_one(project: Path) -> None:
    spans = _exported_spans(project)
    # Drop the run-id attribute so the run cross-check does not fire; the point
    # here is that a run whose journal is absent cannot be proven either way.
    stripped = json.loads(json.dumps(spans[0]))
    stripped["attributes"] = [a for a in stripped["attributes"] if a["key"] != "bernstein.run.id"]
    span_file = _write(project, stripped)
    result = _invoke(["verify-span", "--run", "gone-run", "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "unverifiable" in result.output.lower()


def test_bad_json_errors_exit_one(project: Path) -> None:
    span_file = project / "bad.json"
    span_file.write_text("{not json", encoding="utf-8")
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1
    assert "json" in result.output.lower()


def test_a_tampered_audit_chain_row_makes_the_span_unverifiable_not_genuine(project: Path) -> None:
    """A chain row that fails HMAC verification is not evidence.

    Tampering one field of the persisted ``otel.projection`` row must flip
    the verdict from genuine (exit 0) to unverifiable (exit 1) -- never a
    pass on the tampered row's say-so, and not forged (exit 2), because it
    is the local evidence store that failed its own integrity check, not
    the span.
    """
    spans = _exported_spans(project)
    span_file = _write(project, spans[0])
    genuine = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert genuine.exit_code == 0, genuine.output

    # Tamper the persisted otel.projection chain row on disk, re-serialising
    # in the writer's canonical form (json.dumps sort_keys=True) so the line
    # still parses and only the HMAC check can catch the edit.
    tampered = False
    for log_file in sorted((project / ".sdd" / "audit").glob("*.jsonl")):
        lines = log_file.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            row = json.loads(line)
            if row.get("event_type") == "otel.projection":
                row["details"]["span_count"] = int(row["details"]["span_count"]) + 1
                lines[i] = json.dumps(row, sort_keys=True)
                tampered = True
        if tampered:
            log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            break
    assert tampered, "expected a persisted otel.projection row to tamper"

    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1, result.output
    assert "unverifiable" in result.output.lower()
    assert "audit chain" in result.output.lower()


def test_a_genuine_span_stays_verifiable_after_retention_archives_the_projection_row(project: Path) -> None:
    """Retention archiving the ``otel.projection`` row must not orphan a span.

    ``AuditLog.archive`` gzip-compresses aged segments into
    ``archive/<date>.jsonl.gz`` and unlinks the live file, and ``verify``
    still replays those segments -- so the rows the command consumes must
    cover them too. A genuine span re-checked after retention stays exit 0.
    """
    spans = _exported_spans(project)
    span_file = _write(project, spans[0])
    before = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert before.exit_code == 0, before.output

    # Age out the live segment through the store's own retention path (a
    # negative window makes today's segment archivable) -- the archive is
    # produced by the real code, not a hand-built .gz.
    audit_dir = project / ".sdd" / "audit"
    archived = AuditLog(audit_dir).archive(RetentionPolicy(retention_days=-1))
    assert archived.archived, "expected retention to archive the live segment"
    assert not list(audit_dir.glob("*.jsonl")), "expected no live segment to remain"

    after = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert after.exit_code == 0, after.output
    assert "genuine" in after.output.lower()


# --------------------------------------------------------------------------- #
# Cross-command convention: one exit-code table for both verifiers (#3256)     #
# --------------------------------------------------------------------------- #


def test_verify_span_and_verify_projection_share_one_exit_code_convention(project: Path) -> None:
    """0 = verified, 1 = could not be evaluated, 2 = verification failed.

    ``telemetry verify-span`` and ``trace verify-projection`` are scripted
    against together; this test drives both commands through all three
    outcomes and pins their documented contracts to the same table, so a
    drift in either command's behaviour or help text fails a named test.
    """
    trace_runner = CliRunner()

    # exit 0 -- verified.
    spans = _exported_spans(project)
    genuine = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(_write(project, spans[0]))])
    assert genuine.exit_code == 0, genuine.output
    projected = trace_runner.invoke(trace_cmd, ["project", _RUN_ID, "--workdir", str(project)])
    assert projected.exit_code == 0, projected.output
    projection_ok = trace_runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert projection_ok.exit_code == 0, projection_ok.output

    # exit 1 -- input could not be evaluated (run with no journal).
    stripped = json.loads(json.dumps(spans[0]))
    stripped["attributes"] = [a for a in stripped["attributes"] if a["key"] != "bernstein.run.id"]
    span_no_journal = _invoke(
        ["verify-span", "--run", "no-such-run", "-w", str(project), "--span", str(_write(project, stripped))]
    )
    assert span_no_journal.exit_code == 1, span_no_journal.output
    projection_no_journal = trace_runner.invoke(
        trace_cmd, ["verify-projection", "no-such-run", "--workdir", str(project)]
    )
    assert projection_no_journal.exit_code == 1, projection_no_journal.output

    # exit 2 -- verification failed (a span id that no longer recomputes).
    forged = json.loads(json.dumps(spans[0]))
    forged["spanId"] = "0000000000000000"
    span_forged = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(_write(project, forged))])
    assert span_forged.exit_code == 2, span_forged.output
    dest = project / ".sdd" / "runs" / _RUN_ID / "projection.otel.json"
    payload = json.loads(dest.read_text(encoding="utf-8"))
    payload["spans"][1]["span_id"] = "deadbeefdeadbeef"
    dest.write_text(json.dumps(payload), encoding="utf-8")
    projection_forged = trace_runner.invoke(trace_cmd, ["verify-projection", _RUN_ID, "--workdir", str(project)])
    assert projection_forged.exit_code == 2, projection_forged.output

    # Both documented contracts state the same table.
    span_help = " ".join(_invoke(["verify-span", "--help"]).output.split())
    projection_help = " ".join(trace_runner.invoke(trace_cmd, ["verify-projection", "--help"]).output.split())
    assert "2 = verification failed" in span_help
    assert "2 = verification failed" in projection_help
    assert "1 = could not be evaluated" in span_help
    assert "1 = could not be evaluated" in projection_help


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
    events = load_events(run_journal_path(project / ".sdd", _RUN_ID)).events
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


# --------------------------------------------------------------------------- #
# #3549: a corrupted journal must fail the verifier, not verify a filtered view #
# --------------------------------------------------------------------------- #


def test_corrupted_journal_is_unverifiable_exit_one(project: Path) -> None:
    """A journal that does not fully parse must fail the verifier (#3549).

    The tolerant reader is right for ordinary readers (a torn trailing write
    must not wedge them), but a verifier attests over the journal on disk: a
    malformed row is exactly the corruption a verifier exists to surface. The
    failure exits 1 (could not be evaluated) and names the physical line --
    never a pass over a filtered sequence.
    """
    spans = _exported_spans(project)
    span_file = _write(project, spans[0])
    genuine = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert genuine.exit_code == 0, genuine.output

    journal_path = run_journal_path(project / ".sdd", _RUN_ID)
    with journal_path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    result = _invoke(["verify-span", "--run", _RUN_ID, "-w", str(project), "--span", str(span_file)])
    assert result.exit_code == 1, result.output
    assert "corrupted" in result.output.lower()
    assert "physical line" in result.output.lower()
