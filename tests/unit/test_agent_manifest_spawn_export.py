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


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("dependency_failure", ["metadata", "import"])
def test_missing_sdk_after_claim_uses_normal_spawn_failure_policy(
    tmp_path,
    make_task,
    mock_adapter_factory,
    monkeypatch,
    resume,
    dependency_failure,
):
    import builtins
    from datetime import UTC, datetime, timedelta
    from importlib.metadata import PackageNotFoundError
    from unittest.mock import patch

    from bernstein.core.orchestrator import TickResult

    from bernstein.core.security import agent_manifest_export as export_module
    from bernstein.core.tasks import task_lifecycle as lifecycle
    from tests.unit.test_task_lifecycle import _claim_orch

    now = datetime.now(UTC)
    declared = export_module.CompositionDeclarations(
        manifest_id="019c6e27-e55b-73d1-87d8-4e01f1f75043",
        agent_id="spiffe://example.test/worker",
        issuer="spiffe://example.test/install",
        version="0.1",
        issued_at=now,
        expires_at=now + timedelta(hours=2),
        signed_at=now,
        prompt_bound_at=now,
        prompt_version="fixture-1",
        classification="internal",
        key_type="software",
        availability_reasons={slot: "Not available for this composition" for slot in export_module.UNBOUND_SLOTS},
    )
    signer = Mock()
    exporter = export_module.CompositionManifestExporter(tmp_path / "lineage", lambda hop: declared, signer)
    adapter = mock_adapter_factory()
    spawner = AgentSpawner(
        adapter, tmp_path, tmp_path, use_worktrees=False, default_model="mock-model", agent_manifest_exporter=exporter
    )
    orch = _claim_orch(tmp_path)
    orch._spawner = spawner
    task = make_task(id="manifest-sdk-missing")
    result = TickResult()
    if resume:
        preserved = tmp_path / "preserved"
        preserved.mkdir()
        orch._preserved_worktrees[task.id] = preserved
    if dependency_failure == "metadata":
        monkeypatch.setattr(export_module, "version", Mock(side_effect=PackageNotFoundError("agent-manifest")))
    else:
        monkeypatch.setattr(export_module, "version", lambda package: "0.12.0")
        original_import = builtins.__import__

        def missing_sdk_import(name, *args, **kwargs):
            if name == "agent_manifest":
                raise ModuleNotFoundError("SDK dependency unavailable")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", missing_sdk_import)
    claim = Mock(return_value=(SimpleNamespace(status_code=200), None))
    fail, release = Mock(), Mock()
    with (
        patch.object(lifecycle, "_claim_task_with_conflict_retry", claim),
        patch.object(lifecycle, "try_fast_path_batch", return_value=False),
        patch.object(lifecycle, "classify_task", return_value=SimpleNamespace(level=lifecycle.TaskLevel.L2)),
        patch.object(lifecycle, "_get_changed_files_in_worktree", return_value=[]),
        patch.object(lifecycle, "fail_task", fail),
        patch.object(lifecycle, "release_task", release),
        patch.object(lifecycle, "get_collector"),
    ):
        lifecycle.claim_and_spawn_batches(orch, [[task]], 0, set(), set(), result)
    assert claim.call_count == 1
    assert result.errors
    assert fail.called or release.called or orch._spawn_failures
    assert not result.spawned and not orch._agents
    adapter.spawn.assert_not_called()
    signer.sign.assert_not_called()
    assert not (tmp_path / "lineage").exists()
    if resume:
        assert preserved.exists()
