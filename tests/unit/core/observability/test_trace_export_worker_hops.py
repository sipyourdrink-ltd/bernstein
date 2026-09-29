"""Tests for multi-worker hop export of TRACE 0.2 Trust Records (issue #6045 slice 2).

Builds every journal with the real :class:`EventJournal` recorder - never a
hand-written row - so the defect a hand-built journal hides (the emitter's key
names not matching what the orchestrator writes) cannot recur.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd
from bernstein.core.observability.trust_record import TrustRecordEmitter
from bernstein.core.replay.journal import EventJournal

_KNOWN_KEY_PEM: bytes | None = None


def _known_private_key_pem() -> bytes:
    """Deterministic Ed25519 PKCS8 PEM so two hops sign with one stable key."""
    global _KNOWN_KEY_PEM
    if _KNOWN_KEY_PEM is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(b"s" * 32)
        _KNOWN_KEY_PEM = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    return _KNOWN_KEY_PEM


def _emitter() -> TrustRecordEmitter:
    return TrustRecordEmitter(
        install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
        get_private_key_pem=_known_private_key_pem,
        get_installed_digest=lambda: "sha256:" + "ab" * 32,
    )


def _journal(tmp_path: Path, name: str = "run-1") -> EventJournal:
    """Return a fresh real recorder backed by ``<tmp_path>/.sdd/runs/<name>``."""
    return EventJournal(name, tmp_path / ".sdd")


def _two_worker_journal(tmp_path: Path) -> Path:
    """A journal with two spawned workers, each on its own model, plus tool calls for one."""
    journal = _journal(tmp_path)
    journal.record(
        "run_started",
        run_id="run-1",
        max_agents=2,
        gate_config={"rules": ["deny-network"], "version": 1},
    )
    journal.record("agent_spawned", agent_id="agent-a", role="backend", model_provider="anthropic", model_id="claude-3")
    journal.record("task_claimed", task_id="t1", agent_id="agent-a")
    journal.record("tool_call", agent_id="agent-a", tool="fs.read", path="/x")
    journal.record("task_completed", task_id="t1", agent_id="agent-a")
    journal.record("agent_spawned", agent_id="agent-b", role="qa", model_provider="openai", model_id="gpt-5")
    journal.record("task_claimed", task_id="t2", agent_id="agent-b")
    journal.record("task_completed", task_id="t2", agent_id="agent-b")
    journal.record("run_completed", run_id="run-1", ticks=1)
    return journal.path


class TestWorkerHops:
    def test_export_emits_one_record_per_spawned_agent_and_an_aggregate(self, tmp_path: Path) -> None:
        journal_path = _two_worker_journal(tmp_path)
        emitter = _emitter()
        result = emitter.emit_hop_records(journal_path, "run-1")

        assert len(result.records) == 2
        assert result.records[0].exec_id == "agent-a"
        assert result.records[1].exec_id == "agent-b"
        assert json.loads(result.aggregate)["subject"] == "spiffe://bernstein.run/run/run-1"

    def test_each_hop_record_names_its_own_model(self, tmp_path: Path) -> None:
        # Spawn order b-then-a: the hop's model must come from its own spawn, not the last one.
        journal = _journal(tmp_path)
        journal.record("run_started", gate_config={"rules": ["r"]})
        journal.record("agent_spawned", agent_id="agent-b", model_provider="openai", model_id="gpt-5")
        journal.record("task_completed", task_id="t2", agent_id="agent-b")
        journal.record("agent_spawned", agent_id="agent-a", model_provider="anthropic", model_id="claude-3")
        journal.record("task_completed", task_id="t1", agent_id="agent-a")

        emitter = _emitter()
        result = emitter.emit_hop_records(journal.path, "run-1")

        by_id = {r.exec_id: json.loads(r.record) for r in result.records}
        assert by_id["agent-a"]["model"] == {"provider": "anthropic", "model_id": "claude-3"}
        assert by_id["agent-b"]["model"] == {"provider": "openai", "model_id": "gpt-5"}

    def test_a_hop_with_no_model_refuses_and_names_the_agent(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        journal.record("run_started", gate_config={"rules": ["r"]})
        journal.record("agent_spawned", agent_id="agent-a", role="backend")  # no model keys
        journal.record("task_completed", task_id="t1", agent_id="agent-a")

        with pytest.raises(ValueError, match="agent-a"):
            _emitter().emit_hop_records(journal.path, "run-1")

    def test_a_hop_without_tool_call_evidence_omits_tool_transcript(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path)
        journal.record("run_started", gate_config={"rules": ["r"]})
        journal.record("agent_spawned", agent_id="agent-a", model_provider="anthropic", model_id="claude-3")
        journal.record("task_completed", task_id="t1", agent_id="agent-a")

        result = _emitter().emit_hop_records(journal.path, "run-1")
        parsed = json.loads(result.records[0].record)
        assert "tool_transcript" not in parsed

    def test_a_hop_with_tool_call_events_folds_only_its_own_calls(self, tmp_path: Path) -> None:
        journal_path = _two_worker_journal(tmp_path)
        result = _emitter().emit_hop_records(journal_path, "run-1")

        by_id = {r.exec_id: json.loads(r.record) for r in result.records}
        # agent-a called one tool; agent-b called none.
        assert by_id["agent-a"]["tool_transcript"]["call_count"] == 1
        assert "tool_transcript" not in by_id["agent-b"]

    def test_aggregate_omits_tool_transcript_unless_every_member_has_one(self, tmp_path: Path) -> None:
        journal_path = _two_worker_journal(tmp_path)
        emitter = _emitter()
        result = emitter.emit_hop_records(journal_path, "run-1")

        # one member omits tool_transcript -> aggregate must omit it too
        assert "tool_transcript" not in json.loads(result.aggregate)

        # both members carry tool_transcript -> aggregate rolls it up
        journal = _journal(tmp_path, "run-both")
        journal.record("run_started", gate_config={"rules": ["r"]})
        journal.record("agent_spawned", agent_id="a1", model_provider="p", model_id="m1")
        journal.record("tool_call", agent_id="a1", tool="t")
        journal.record("task_completed", task_id="t1", agent_id="a1")
        journal.record("agent_spawned", agent_id="a2", model_provider="p", model_id="m2")
        journal.record("tool_call", agent_id="a2", tool="t")
        journal.record("task_completed", task_id="t2", agent_id="a2")
        result2 = emitter.emit_hop_records(journal.path, "run-both")
        aggregate = json.loads(result2.aggregate)
        assert aggregate["tool_transcript"]["call_count"] == 2

    def test_aggregate_references_every_hop_record_by_digest(self, tmp_path: Path) -> None:
        journal_path = _two_worker_journal(tmp_path)
        emitter = _emitter()
        result = emitter.emit_hop_records(journal_path, "run-1")

        aggregate = json.loads(result.aggregate)
        refs = aggregate["references"]
        assert len(refs) == 2
        for raw, ref in zip([r.record for r in result.records], refs, strict=True):
            member = json.loads(raw)
            expected = (
                "sha256:"
                + hashlib.sha256(
                    __import__(
                        "bernstein.core.security.agent_card_signer", fromlist=["canonicalize_jcs"]
                    ).canonicalize_jcs(member)
                ).hexdigest()
            )
            assert ref["digest"] == expected
            assert ref["id"] == member["subject"]

    def test_every_emitted_record_verifies_at_level_0(self, tmp_path: Path) -> None:
        if shutil.which("uv") is None:
            pytest.skip("uv is not on PATH; agentrust-trace-tests cannot be resolved")
        probe = subprocess.run(
            ["uv", "run", "--no-sync", "--with", "agentrust-trace-tests==0.5.1", "trace-tests", "--version"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if probe.returncode != 0:
            pytest.skip("agentrust-trace-tests could not be resolved (no network to PyPI)")

        journal_path = _two_worker_journal(tmp_path)
        emitter = _emitter()
        result = emitter.emit_hop_records(journal_path, "run-1")
        for hop in result.records:
            record_file = tmp_path / f"{hop.exec_id}.json"
            record_file.write_text(hop.record, encoding="utf-8")
            run = subprocess.run(
                [
                    "uv",
                    "run",
                    "--no-sync",
                    "--with",
                    "agentrust-trace-tests==0.5.1",
                    "trace-tests",
                    "verify",
                    "--record",
                    str(record_file),
                    "--level",
                    "0",
                    "--max-age",
                    "999999999999",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert "Result: PASS" in run.stdout, run.stdout + run.stderr

    def test_legacy_journal_without_agent_spawned_exports_exactly_as_before(self, tmp_path: Path) -> None:
        journal = _journal(tmp_path, "legacy-run")
        journal.record("run_started", model_provider="anthropic", model_id="claude-3", gate_config={"rules": ["r"]})
        journal.record("tool_call", tool="fs.read")
        journal.record("task_completed", task_id="t1")

        emitter = _emitter()
        hop_result = emitter.emit_hop_records(journal.path, "legacy-run")
        # legacy path: a single synthetic hop whose exec_id equals the passed run_id
        assert len(hop_result.records) == 1
        assert hop_result.records[0].exec_id == "legacy-run"

        direct = emitter.emit_trust_record(journal.path, "legacy-run", "legacy-run")
        assert hop_result.records[0].record == direct
        parsed = json.loads(direct)
        assert parsed["tool_transcript"]["call_count"] == 1


class TestCliOutDir:
    def test_cli_out_dir_writes_one_file_per_hop_and_the_aggregate(self, tmp_path: Path) -> None:
        journal_path = _two_worker_journal(tmp_path)
        emitter = _emitter()
        hops = emitter.emit_hop_records(journal_path, "run-1")

        runner = CliRunner()
        with runner.isolated_filesystem():
            mock_trace_module = MagicMock()
            out_dir = Path("out")
            with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
                with patch("bernstein.core.replay.journal.run_journal_path", return_value=journal_path):
                    with patch("bernstein.core.replay.journal.verify_journal") as mock_verify:
                        mock_verification = MagicMock()
                        mock_verification.chain_consistent = True
                        mock_verify.return_value = mock_verification
                        with patch("bernstein.core.observability.trust_record.TrustRecordEmitter") as mock_emitter:
                            mock_emitter.return_value = emitter
                            result = runner.invoke(
                                trace_cmd,
                                ["export", "run-1", "--sdd-dir", str(tmp_path / ".sdd"), "--out-dir", str(out_dir)],
                            )
            assert result.exit_code == 0, result.output
            for hop in hops.records:
                assert (out_dir / f"{hop.exec_id}.json").exists()
            assert (out_dir / "aggregate.json").exists()
