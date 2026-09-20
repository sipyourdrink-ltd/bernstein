"""Tests for :mod:`bernstein.core.observability.trust_record` over journals written by the orchestrator.

The committed vectors and the hand-built journals in
:mod:`tests.unit.core.observability.test_trust_record` all carry the emitter's
own key names (``model_id`` / ``model_provider`` / ``gate_config``). The
orchestrator wrote ``model`` / ``provider`` on ``agent_spawned`` and no
``gate_config`` at all, so no test exercised the real producer path until
issue #6045.

Every journal here is built with the real recorder and by calling the
orchestrator's genuine write-site methods (``_record_run_started`` and
``_record_spawned_events``) bound onto a stub. No ``.jsonl`` row is
hand-written and no keyword is mirrored by the test.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, ModelConfig
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd
from bernstein.core.observability.trust_record import TrustRecordEmitter
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.persistence.runtime_state import SessionReplayMetadata
from bernstein.core.quality.quality_gates import QualityGatesConfig
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.security.agent_card_signer import canonicalize_jcs

_KNOWN_KEY_PEM: bytes | None = None


def _known_private_key_pem() -> bytes:
    """Deterministic Ed25519 PKCS8 PEM so the record verifies against a fixed key."""
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


def _spawn_stub(journal: EventJournal, session: AgentSession) -> SimpleNamespace:
    """Return a stub carrying just what ``_record_spawned_events`` reads."""
    return SimpleNamespace(
        _recorder=journal,
        _agents={session.id: session},
        _record_mutation_capability_once=lambda _session: None,
    )


def _record_spawned_events(journal: EventJournal, session: AgentSession) -> None:
    """Drive the orchestrator's real ``agent_spawned`` write site."""
    stub = _spawn_stub(journal, session)
    result = SimpleNamespace(spawned=[session.id])
    Orchestrator._record_spawned_events(stub, result)  # type: ignore[arg-type]


def _run_started_stub(
    journal: EventJournal, run_id: str, gate_config: QualityGatesConfig | None = None
) -> SimpleNamespace:
    """Return a stub carrying just what ``_record_run_started`` reads."""
    return SimpleNamespace(
        _recorder=journal,
        _workflow_executor=None,
        _run_id=run_id,
        _config=SimpleNamespace(max_agents=6, budget_usd=10.0),
        _replay_metadata=SessionReplayMetadata(
            run_id=run_id,
            started_at=0.0,
            git_sha="",
            git_branch="",
            config_hash="",
        ),
        _quality_gate_config=gate_config,
    )


def _record_run_started(journal: EventJournal, run_id: str, gate_config: QualityGatesConfig | None = None) -> None:
    """Drive the orchestrator's real ``run_started`` write site."""
    stub = _run_started_stub(journal, run_id, gate_config)
    Orchestrator._record_run_started(stub)  # type: ignore[arg-type]


def _session() -> AgentSession:
    return AgentSession(
        id="session-1",
        role="backend",
        task_ids=["t-1"],
        provider="anthropic",
    )


def _session_with_model(model: str, provider: str | None) -> AgentSession:
    return AgentSession(
        id="session-1",
        role="backend",
        task_ids=["t-1"],
        provider=provider,
        model_config=ModelConfig(model=model, effort="high"),
    )


def _orchestrator_written_journal(sdd_dir: Path, run_id: str) -> EventJournal:
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    _record_run_started(journal, run_id, QualityGatesConfig(lint=True))
    _record_spawned_events(journal, _session())
    journal.record("run_completed", run_id=run_id, ticks=1)
    return journal


def _emitter() -> TrustRecordEmitter:
    return TrustRecordEmitter(
        install_rev_getter=lambda: "aaaaaaaaaaaaaaaa",
        get_private_key_pem=_known_private_key_pem,
        get_installed_digest=lambda: "sha256:" + "0" * 64,
    )


class TestExportAcceptsAJournalWrittenByTheOrchestrator:
    def test_export_accepts_a_journal_written_by_the_orchestrator(self, tmp_path: Path) -> None:
        journal = _orchestrator_written_journal(tmp_path / ".sdd", "orchestrated-run")
        record = json.loads(_emitter().emit_trust_record(journal.path, "orchestrated-run", "orchestrated-run"))

        assert record["model"]["provider"] == "anthropic"
        assert record["model"]["model_id"] == "sonnet"
        assert record["policy"]["bundle_hash"].startswith("sha256:")


class TestAgentSpawnedCarriesModelIdentity:
    def test_agent_spawned_carries_model_id_and_model_provider(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="spawn-facts", sdd_dir=tmp_path / ".sdd")
        _record_spawned_events(journal, _session())

        events = load_events(journal.path).events
        spawned = next(event for event in events if event["event"] == "agent_spawned")
        assert spawned["model_id"] == "sonnet"
        assert spawned["model_provider"] == "anthropic"
        # The existing keys stay: other readers may depend on them.
        assert spawned["model"] == "sonnet"
        assert spawned["provider"] == "anthropic"


class TestRunStartJournalsGateConfig:
    def test_run_start_journals_the_resolved_gate_config(self, tmp_path: Path) -> None:
        config = QualityGatesConfig(lint=True, type_check=True, tests=True)
        journal = EventJournal(run_id="gate-facts", sdd_dir=tmp_path / ".sdd")

        stub = _run_started_stub(journal, "gate-facts", config)
        Orchestrator._record_run_started(stub)  # type: ignore[arg-type]

        events = load_events(journal.path).events
        assert events[0]["gate_config"]["lint"] is True
        assert events[0]["gate_config"]["type_check"] is True
        assert events[0]["gate_config"]["tests"] is True


class TestGateConfigDigestStable:
    def test_gate_config_digest_is_stable_across_key_order(self) -> None:
        config_a = {"lint": True, "timeout_s": 120.0, "flaky_threshold": 0.15, "pipeline": None}
        config_b = {"pipeline": None, "flaky_threshold": 0.15, "lint": True, "timeout_s": 120.0}

        digest_a = hashlib.sha256(canonicalize_jcs(config_a)).hexdigest()
        digest_b = hashlib.sha256(canonicalize_jcs(config_b)).hexdigest()
        assert digest_a == digest_b


class TestExportStillRefusesWithoutModel:
    def test_export_still_refuses_a_journal_with_no_model(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="no-model", sdd_dir=tmp_path / ".sdd")
        journal.record("run_started", run_id="no-model", gate_config={"rules": []})
        journal.record("run_completed", run_id="no-model", ticks=1)

        with pytest.raises(ValueError, match="names no model_provider/model_id"):
            _emitter().emit_trust_record(journal.path, "no-model", "no-model")


class TestReplayAndRunReceiptIgnoreAddedKeys:
    def test_replay_and_run_receipt_ignore_the_added_keys(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from bernstein.core.lineage.spine import LineageSpine
        from bernstein.core.replay.run_receipt import build_run_receipt, verify_run_receipt
        from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

        sdd = tmp_path / ".sdd"
        run_id = "receipt-added-keys"
        journal = EventJournal(run_id=run_id, sdd_dir=sdd)
        _record_run_started(journal, run_id)
        _record_spawned_events(journal, _session())
        journal.record("run_completed", run_id=run_id, ticks=1)

        spine = LineageSpine(sdd / "lineage", run_id=run_id, hmac_key=b"x" * 32)
        spine.record(
            artifact_path="src/app.py",
            content=b"print('hi')\n",
            actor="backend",
            step_id="t-1",
            model="m1",
            timestamp=1111,
        )

        key_path = tmp_path / "sign.pem"
        key = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        kms = FileBasedKMSAdapter(key_path, kid="test-key")

        receipt = build_run_receipt(run_id, sdd, kms)
        assert receipt.receipt_path is not None
        result = verify_run_receipt(receipt.receipt_path.read_bytes())
        assert result.ok
        assert result.run_id == run_id

        # Replay (tolerant load) keeps the rows intact and does not reject
        # the added keys.
        loaded = load_events(journal.path)
        assert loaded.discarded_line_indices == ()


def _spawned_event(journal: EventJournal) -> dict[str, object]:
    events = load_events(journal.path).events
    return next(event for event in events if event["event"] == "agent_spawned")


class TestModelNamespaceBecomesProvider:
    def test_namespaced_model_without_provider_journals_the_namespace(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="ns-facts", sdd_dir=tmp_path / ".sdd")
        _record_spawned_events(journal, _session_with_model("omnilab/fleet-hard", None))

        spawned = _spawned_event(journal)
        assert spawned["model_id"] == "omnilab/fleet-hard"
        assert spawned["model_provider"] == "omnilab"
        assert spawned["model"] == "omnilab/fleet-hard"
        assert spawned["provider"] is None

    def test_bare_model_without_provider_journals_no_provider(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="bare-facts", sdd_dir=tmp_path / ".sdd")
        _record_spawned_events(journal, _session_with_model("sonnet", None))

        spawned = _spawned_event(journal)
        assert "model_id" not in spawned
        assert "model_provider" not in spawned

    def test_empty_namespace_is_not_a_provider(self, tmp_path: Path) -> None:
        for index, model in enumerate(("/fleet-hard", "omnilab/")):
            journal = EventJournal(run_id=f"empty-ns-facts-{index}", sdd_dir=tmp_path / ".sdd")
            _record_spawned_events(journal, _session_with_model(model, None))

            spawned = _spawned_event(journal)
            assert "model_id" not in spawned, model
            assert "model_provider" not in spawned, model

    def test_resolved_provider_is_never_overwritten_by_the_namespace(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="resolved-facts", sdd_dir=tmp_path / ".sdd")
        _record_spawned_events(journal, _session_with_model("omnilab/fleet-hard", "anthropic"))

        spawned = _spawned_event(journal)
        assert spawned["model_id"] == "omnilab/fleet-hard"
        assert spawned["model_provider"] == "anthropic"


class TestExportNamespacedEndpointRunVerifies:
    def test_export_of_a_namespaced_endpoint_run_verifies_at_level_0(self, tmp_path: Path) -> None:
        journal = EventJournal(run_id="ns-run", sdd_dir=tmp_path / ".sdd")
        _record_run_started(journal, "ns-run", QualityGatesConfig(lint=True))
        session = _session_with_model("omnilab/fleet-hard", None)
        session.endpoint_adapter_name = "OpenCode"
        session.endpoint_model = "omnilab/fleet-hard"
        _record_spawned_events(journal, session)
        journal.record("run_completed", run_id="ns-run", ticks=1)

        record_path = tmp_path / "record.json"
        runner = CliRunner()
        mock_trace_module = MagicMock()
        env = {"BERNSTEIN_AGENT_CARD_KEY_DIR": str(tmp_path / "keys")}
        with patch.dict("sys.modules", {"agentrust_trace": mock_trace_module}):
            result = runner.invoke(
                trace_cmd,
                ["export", "ns-run", "--sdd-dir", str(tmp_path / ".sdd"), "--out", str(record_path)],
                env=env,
            )
        assert result.exit_code == 0, result.output
        assert record_path.exists()

        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["model"] == {"provider": "omnilab", "model_id": "omnilab/fleet-hard"}

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

        verify = subprocess.run(
            [
                "uv",
                "run",
                "--no-sync",
                "--with",
                "agentrust-trace-tests==0.5.1",
                "trace-tests",
                "verify",
                "--record",
                str(record_path),
                "--level",
                "0",
                "--max-age",
                "999999999",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "Result: PASS" in verify.stdout, verify.stdout + verify.stderr
