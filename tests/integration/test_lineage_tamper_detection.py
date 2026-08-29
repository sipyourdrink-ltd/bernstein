"""Integration tests for lineage tamper-loud surface (Phase 2).

Asserts that:

1. The janitor's ``verify_lineage_chains`` emits the audit event,
   Prometheus counter increment, and webhook call when a chain is
   tampered.
2. The janitor exits cleanly when the chain validates (no false
   positives).
3. ``bernstein lineage verify`` exits 0 on clean runs and 2 on
   tampered runs.
4. A broken SIEM sink does not crash the janitor.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.lineage_verify_cmd import lineage_verify_cmd
from bernstein.core.observability.lineage_alert import (
    NullAlertSink,
    WebhookAlertSink,
)
from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageRecord,
    LineageWriter,
)
from bernstein.core.persistence.lineage_signer import Ed25519FileKeySigner
from bernstein.core.quality.janitor import verify_lineage_chains


def _make_signed_run(tmp_path: Path, run_id: str = "run-tamper") -> tuple[Path, Path]:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "customer.pem"
    key_path.write_bytes(pem)

    signer = Ed25519FileKeySigner.from_path(key_path)
    writer = LineageWriter.for_run(run_id, sdd, signer=signer)

    for i in range(3):
        writer.emit(
            LineageRecord(
                output_artifact=ArtifactRef(path=f"src/f{i}.py", sha256="a" * 64),
                inputs=[],
                producer=AgentRef(agent_id=f"agent-{i}", run_id=run_id),
                prompt_sha="p" * 64,
                model="claude-sonnet",
                cost_usd=0.01,
                tokens=100,
                timestamp=1700000000.0 + i,
                regulatory_class="production_detection_rule",
            )
        )
    return sdd, key_path


def _wal_path(sdd: Path, run_id: str) -> Path:
    return sdd / "runtime" / "wal" / f"{run_id}.wal.jsonl"


def _tamper_record(wal: Path) -> None:
    """Flip a byte in the second record so the WAL chain breaks."""
    lines = wal.read_text().splitlines()
    entry = json.loads(lines[1])
    output = entry.get("output", {})
    output["cost_usd"] = 999.99
    entry["output"] = output
    lines[1] = json.dumps(entry, sort_keys=True)
    wal.write_text("\n".join(lines) + "\n")


class _FakeAuditLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details or {},
            }
        )


class _RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.received_bodies.append(body)  # type: ignore[attr-defined]
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def siem_server() -> Any:
    server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    server.received_bodies = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def test_janitor_clean_chain_emits_no_alert(tmp_path: Path) -> None:
    _make_signed_run(tmp_path)
    audit = _FakeAuditLog()
    sink = NullAlertSink()

    results = verify_lineage_chains(tmp_path, audit_log=audit, sink=sink)

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].record_count == 3
    assert audit.events == []


def test_janitor_detects_tamper_and_emits_audit_event(tmp_path: Path) -> None:
    sdd, _ = _make_signed_run(tmp_path)
    _tamper_record(_wal_path(sdd, "run-tamper"))

    audit = _FakeAuditLog()
    results = verify_lineage_chains(tmp_path, audit_log=audit, sink=NullAlertSink())

    assert len(results) == 1
    assert results[0].ok is False
    assert any("seq" in e or "wal" in e for e in results[0].errors)

    tamper_events = [e for e in audit.events if e["event_type"] == "lineage_tamper_detected"]
    assert len(tamper_events) == 1
    assert tamper_events[0]["resource_id"] == "run-tamper"
    assert tamper_events[0]["details"]["error_count"] >= 1


def test_janitor_calls_webhook_on_tamper(tmp_path: Path, siem_server: Any) -> None:
    sdd, _ = _make_signed_run(tmp_path)
    _tamper_record(_wal_path(sdd, "run-tamper"))

    url = f"http://127.0.0.1:{siem_server.server_address[1]}/hec"
    sink = WebhookAlertSink(url, timeout_secs=2.0, max_retries=0)

    verify_lineage_chains(tmp_path, audit_log=_FakeAuditLog(), sink=sink)

    assert len(siem_server.received_bodies) == 1
    payload = json.loads(siem_server.received_bodies[0].decode())
    assert payload["type"] == "lineage_tamper_detected"
    assert payload["run_id"] == "run-tamper"


def test_janitor_increments_prometheus_counter_on_tamper(tmp_path: Path) -> None:
    from bernstein.core.observability.prometheus import lineage_tamper_total

    before = _counter_value(lineage_tamper_total, run_id="run-tamper-metric")

    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    writer = LineageWriter.for_run("run-tamper-metric", sdd)
    writer.emit(
        LineageRecord(
            output_artifact=ArtifactRef(path="x.py", sha256="a" * 64),
            inputs=[],
            producer=AgentRef(agent_id="a", run_id="run-tamper-metric"),
        )
    )
    writer.emit(
        LineageRecord(
            output_artifact=ArtifactRef(path="y.py", sha256="b" * 64),
            inputs=[],
            producer=AgentRef(agent_id="a", run_id="run-tamper-metric"),
        )
    )
    _tamper_record(_wal_path(sdd, "run-tamper-metric"))

    verify_lineage_chains(tmp_path, audit_log=None, sink=NullAlertSink())

    after = _counter_value(lineage_tamper_total, run_id="run-tamper-metric")
    assert after - before == pytest.approx(1.0)


def test_janitor_swallows_broken_sink(tmp_path: Path) -> None:
    sdd, _ = _make_signed_run(tmp_path)
    _tamper_record(_wal_path(sdd, "run-tamper"))

    class BrokenSink:
        def emit(self, event: Any) -> bool:
            raise RuntimeError("sink exploded")

    # Should not raise -- fail-closed contract
    verify_lineage_chains(tmp_path, audit_log=_FakeAuditLog(), sink=BrokenSink())


def test_lineage_verify_cli_exits_zero_on_clean_chain(tmp_path: Path) -> None:
    _make_signed_run(tmp_path, run_id="run-clean")
    runner = CliRunner()
    result = runner.invoke(
        lineage_verify_cmd,
        ["run-clean", "--workdir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_lineage_verify_cli_exits_nonzero_on_tamper(tmp_path: Path) -> None:
    sdd, _ = _make_signed_run(tmp_path, run_id="run-broken")
    _tamper_record(_wal_path(sdd, "run-broken"))

    runner = CliRunner()
    result = runner.invoke(
        lineage_verify_cmd,
        ["run-broken", "--workdir", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "TAMPER DETECTED" in result.output


def test_lineage_verify_cli_validates_signatures_with_pubkey(tmp_path: Path) -> None:
    """With a public key, signatures are checked. Tampering the *value* on a
    signed record (not the wal-chain bytes themselves) breaks signature, not chain."""
    _, key_path = _make_signed_run(tmp_path, run_id="run-sig")

    # Derive a public key file the verifier can read.
    private = Ed25519PrivateKey.from_private_bytes(
        serialization.load_pem_private_key(key_path.read_bytes(), password=None).private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    pub_path = tmp_path / "pub.raw"
    pub_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        lineage_verify_cmd,
        ["run-sig", "--workdir", str(tmp_path), "--public-key", str(pub_path)],
    )
    assert result.exit_code == 0, result.output


def _counter_value(counter: Any, **labels: str) -> float:
    """Read a counter's current value through the prometheus_client API."""
    try:
        return float(counter.labels(**labels)._value.get())
    except AttributeError:
        return 0.0


# ---------------------------------------------------------------------------
# The same sweep, applied to a fan-out (#3761)
#
# A fan-out's branches are sealed into one run-graph receipt, and the sweep
# above had no knowledge of those: a branch edited inside a fan-out was
# invisible to the same pass that catches an edited single-run spine. These
# assert the parity -- same event, same counter -- and, just as importantly,
# the two states where the sweep must stay quiet, because an alert that fires
# on healthy state is one an operator learns to skip.
# ---------------------------------------------------------------------------

_FANOUT_HMAC_KEY = b"k" * 32
_FANOUT_TIMESTAMP = 1_700_000_000
_FANOUT_SESSIONS = ("sess-alpha", "sess-beta")
_FANOUT_RUN_IDS = {session: f"run-{session.split('-')[1]}" for session in _FANOUT_SESSIONS}


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
        },
    )


def _seal_fanout(workdir: Path) -> tuple[str, bytes]:
    """Two branches, their spines, and one sealed receipt on disk.

    The branches are real git repositories because the head sha is one of the
    three things the graph root commits to, and it is resolved through git
    both when sealing and when checking. A fixture that stubbed it would seal
    a head nothing could re-derive, and every receipt would then look
    tampered -- which is a fixture failing, dressed as the finding under test.

    Returns ``(receipt_hash, public_key_pem)``.
    """
    from bernstein.core.lineage.run_graph import build_run_graph, build_run_graph_receipt
    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

    worktrees = workdir / ".sdd" / "runtime" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    lineage_root = workdir / ".sdd" / "lineage"
    for session in _FANOUT_SESSIONS:
        branch = worktrees / session
        branch.mkdir(exist_ok=True)
        _git(branch, "init", "-q", "-b", "main")
        (branch / "out.txt").write_text(session, encoding="utf-8")
        _git(branch, "add", "out.txt")
        _git(branch, "commit", "-q", "-m", f"work in {session}")
        LineageSpine(lineage_root, run_id=_FANOUT_RUN_IDS[session], hmac_key=_FANOUT_HMAC_KEY).record(
            artifact_path=f"out/{session}.txt",
            content=session.encode(),
            actor="tester",
            step_id="step-1",
            model="test-model",
            timestamp=_FANOUT_TIMESTAMP,
        )

    private_key_pem, public_key_pem = generate_ed25519_keypair()
    graph = build_run_graph(
        workdir,
        run_ids=_FANOUT_RUN_IDS,
        lineage_root=lineage_root,
        hmac_key=_FANOUT_HMAC_KEY,
    )
    receipt = build_run_graph_receipt(
        graph=graph,
        workdir=workdir,
        lineage_root=lineage_root,
        hmac_key=_FANOUT_HMAC_KEY,
        timestamp=_FANOUT_TIMESTAMP,
        private_key_pem=private_key_pem,
        chain=None,
    )
    return receipt.receipt_hash, public_key_pem


def _fanout_inputs(workdir: Path, public_key_pem: bytes, *, run_ids: dict[str, str] | None = None) -> Any:
    from bernstein.core.quality.janitor import RunGraphSweepInputs

    return RunGraphSweepInputs(
        repo_root=workdir,
        run_ids=_FANOUT_RUN_IDS if run_ids is None else run_ids,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_FANOUT_HMAC_KEY,
        public_key_pem=public_key_pem,
    )


def _tamper_branch(workdir: Path, session: str) -> None:
    """Edit a row inside one branch's spine, leaving its stored head alone.

    The stored head is what a node hash carries, so this edit is invisible to
    the hash comparison and only the chain walk sees it.
    """
    journal = next((workdir / ".sdd" / "lineage" / _FANOUT_RUN_IDS[session]).rglob("*.jsonl"))
    raw = journal.read_bytes()
    assert b'"actor":"tester"' in raw, "fixture no longer carries the field this test edits"
    journal.write_bytes(raw.replace(b'"actor":"tester"', b'"actor":"testeR"'))


def _tamper_counter(receipt_hash: str) -> float:
    """The same counter the single-run tests read, keyed on the fan-out."""
    from bernstein.core.observability.prometheus import lineage_tamper_total

    return _counter_value(lineage_tamper_total, run_id=receipt_hash)


def test_janitor_detects_a_tampered_fan_out_branch(tmp_path: Path) -> None:
    """A branch edited inside a fan-out reaches the same alert as a run does."""
    receipt_hash, public_key_pem = _seal_fanout(tmp_path)
    _tamper_branch(tmp_path, "sess-beta")
    before = _tamper_counter(receipt_hash)

    audit = _FakeAuditLog()
    verify_lineage_chains(
        tmp_path,
        audit_log=audit,
        sink=NullAlertSink(),
        run_graph=_fanout_inputs(tmp_path, public_key_pem),
    )

    tamper_events = [e for e in audit.events if e["event_type"] == "lineage_tamper_detected"]
    assert len(tamper_events) == 1
    assert tamper_events[0]["resource_id"] == receipt_hash
    assert "sess-beta" in tamper_events[0]["details"]["errors"][0]
    assert _tamper_counter(receipt_hash) == before + 1


def test_janitor_leaves_an_intact_fan_out_alone(tmp_path: Path) -> None:
    """No finding on a healthy fan-out, or the alert means nothing."""
    _receipt_hash, public_key_pem = _seal_fanout(tmp_path)

    audit = _FakeAuditLog()
    verify_lineage_chains(
        tmp_path,
        audit_log=audit,
        sink=NullAlertSink(),
        run_graph=_fanout_inputs(tmp_path, public_key_pem),
    )

    assert audit.events == []


def test_janitor_does_not_call_a_reaped_fan_out_tampering(tmp_path: Path) -> None:
    """A receipt stops verifying once its worktrees are reaped.

    From the filesystem a branch removed by routine cleanup and a branch
    removed to hide what it held are the same observation -- and the janitor
    is the process that does the reaping, so alerting on this would fire on
    every fan-out it cleans up after.
    """
    receipt_hash, public_key_pem = _seal_fanout(tmp_path)
    shutil.rmtree(tmp_path / ".sdd" / "runtime" / "worktrees" / "sess-beta")
    before = _tamper_counter(receipt_hash)

    audit = _FakeAuditLog()
    verify_lineage_chains(
        tmp_path,
        audit_log=audit,
        sink=NullAlertSink(),
        run_graph=_fanout_inputs(tmp_path, public_key_pem),
    )

    assert audit.events == []
    assert _tamper_counter(receipt_hash) == before


def test_janitor_does_not_call_a_missing_run_id_tampering(tmp_path: Path) -> None:
    """An incomplete mapping is a gap in the inputs, not in the tree."""
    receipt_hash, public_key_pem = _seal_fanout(tmp_path)
    before = _tamper_counter(receipt_hash)

    audit = _FakeAuditLog()
    verify_lineage_chains(
        tmp_path,
        audit_log=audit,
        sink=NullAlertSink(),
        run_graph=_fanout_inputs(tmp_path, public_key_pem, run_ids={"sess-alpha": "run-alpha"}),
    )

    assert audit.events == []
    assert _tamper_counter(receipt_hash) == before


def test_janitor_without_fan_out_inputs_sweeps_exactly_what_it_did_before(tmp_path: Path) -> None:
    """The coupling is opt-in: no inputs, no fan-out sweep, same verdict.

    A tampered fan-out sitting on disk must not change the answer for a caller
    that never asked about fan-outs -- otherwise every existing janitor call
    silently acquires a new failure mode.
    """
    _make_signed_run(tmp_path)
    receipt_hash, _ = _seal_fanout(tmp_path)
    _tamper_branch(tmp_path, "sess-beta")
    before = _tamper_counter(receipt_hash)

    audit = _FakeAuditLog()
    results = verify_lineage_chains(tmp_path, audit_log=audit, sink=NullAlertSink())

    assert [r.ok for r in results] == [True]
    assert audit.events == []
    assert _tamper_counter(receipt_hash) == before
