"""Optional composition export hooks and existing per-hop journal references."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bernstein.core.models import AgentSession

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.observability.trust_record import _build_references
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.replay.journal import EventJournal, load_events


def test_opt_in_spawn_exports_composition_before_adapter(tmp_path, make_task, mock_adapter_factory):
    adapter = mock_adapter_factory(pid=100)
    references = [
        {
            "artifact_id": "manifest/manifest.json",
            "resolver": "agent-manifests/worker/manifest.json",
            "digest": "sha256:" + "a" * 64,
        }
    ]
    callback = Mock(return_value=references)
    spawner = AgentSpawner(
        adapter, tmp_path, tmp_path, use_worktrees=False, default_model="mock-model", agent_manifest_exporter=callback
    )
    session = spawner.spawn_for_tasks([make_task()])
    callback.assert_called_once()
    hop, prompt = callback.call_args.args
    assert hop == session.id
    assert prompt == adapter.spawn.call_args.kwargs["prompt"]
    assert session.agent_manifest_references == references


def test_export_failure_does_not_launch_adapter(tmp_path, make_task, mock_adapter_factory):
    adapter = mock_adapter_factory()
    spawner = AgentSpawner(
        adapter,
        tmp_path,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
        agent_manifest_exporter=Mock(side_effect=ValueError("missing declarations")),
    )
    with pytest.raises(ValueError, match="missing declarations"):
        spawner.spawn_for_tasks([make_task()])
    adapter.spawn.assert_not_called()


def test_default_spawn_has_no_export(tmp_path, make_task, mock_adapter_factory):
    adapter = mock_adapter_factory()
    spawner = AgentSpawner(adapter, tmp_path, tmp_path, use_worktrees=False, default_model="mock-model")
    session = spawner.spawn_for_tasks([make_task()])
    assert session.agent_manifest_references == []


def test_recorder_uses_existing_artifact_events_scoped_to_one_hop(tmp_path: Path):
    refs = [
        {
            "artifact_id": "manifest/manifest.json",
            "resolver": "agent-manifests/worker/manifest.json",
            "digest": "sha256:" + hashlib.sha256(b"manifest bytes").hexdigest(),
        }
    ]
    session = AgentSession(id="worker", role="backend", agent_manifest_references=refs)
    journal = EventJournal(run_id="run", sdd_dir=tmp_path)
    stub = SimpleNamespace(
        _recorder=journal, _agents={session.id: session}, _record_mutation_capability_once=lambda session: None
    )
    Orchestrator._record_spawned_events(stub, SimpleNamespace(spawned=[session.id]))
    events = load_events(journal.path).events
    own = _build_references(events, agent_id="worker")
    assert own == [
        {
            "rel": "produced-artifact",
            "id": refs[0]["artifact_id"],
            "resolver": refs[0]["resolver"],
            "digest": refs[0]["digest"],
        }
    ]
    assert _build_references(events, agent_id="sibling") == []


def test_resume_exports_new_composition_before_adapter(tmp_path, make_task, mock_adapter_factory):
    adapter = mock_adapter_factory()
    callback = Mock(return_value=[])
    spawner = AgentSpawner(
        adapter, tmp_path, tmp_path, use_worktrees=False, default_model="mock-model", agent_manifest_exporter=callback
    )
    worktree = tmp_path / "old-session"
    worktree.mkdir()
    session = spawner.spawn_for_resume([make_task()], worktree_path=worktree, changed_files=[])
    callback.assert_called_once_with(session.id, adapter.spawn.call_args.kwargs["prompt"])


def test_actual_signed_trust_record_contains_manifest_references(tmp_path):
    import json

    from bernstein.core.models import ModelConfig
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.observability.trust_record import TrustRecordEmitter, verify_trust_record

    key = Ed25519PrivateKey.from_private_bytes(b"t" * 32)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    ref = {
        "artifact_id": "manifest/manifest.json",
        "resolver": "agent-manifests/worker/manifest.json",
        "digest": "sha256:" + "b" * 64,
    }
    session = AgentSession(
        id="worker",
        role="backend",
        model_config=ModelConfig("fixture", "high"),
        model_vendor="fixture-vendor",
        agent_manifest_references=[ref],
    )
    journal = EventJournal(run_id="run", sdd_dir=tmp_path)
    journal.record("run_started", gate_config={"lint": True})
    stub = SimpleNamespace(
        _recorder=journal, _agents={session.id: session}, _record_mutation_capability_once=lambda session: None
    )
    Orchestrator._record_spawned_events(stub, SimpleNamespace(spawned=[session.id]))
    emitter = TrustRecordEmitter(
        install_rev_getter=lambda: "fixture-revision",
        get_private_key_pem=lambda: private_pem,
        get_installed_digest=lambda: "sha256:" + "0" * 64,
    )
    exported = emitter.emit_hop_records(journal.path, "run")
    record = json.loads(exported.records[0].record)
    assert record["references"] == [
        {"rel": "produced-artifact", "id": ref["artifact_id"], "resolver": ref["resolver"], "digest": ref["digest"]}
    ]
    assert verify_trust_record(record, public_pem)
