"""One run selection reaches journal, spine and receipt (#5274)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from bernstein.adapters.base import CLIAdapter, record_artifact_write
from bernstein.core.agents.spawner import AgentSpawner
from bernstein.core.config.config_schema import BernsteinConfig
from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import parse_seed
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.replay.journal import EventJournal, verify_journal
from bernstein.core.replay.run_receipt import (
    RunReceiptError,
    build_run_receipt,
    verify_run_receipt,
    write_run_receipt_if_configured,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from bernstein.core.tasks.models import OrchestratorConfig

_KEY = b"test-profile-key" * 2


def _signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileBasedKMSAdapter:
    key = Ed25519PrivateKey.from_private_bytes(b"p" * 32)
    path = tmp_path / "fixture-key.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    monkeypatch.setenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", str(path))
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR", raising=False)
    return FileBasedKMSAdapter(path, kid="profile-test")


def _artifact(sdd: Path, run_id: str) -> None:
    record_artifact_write(
        artifact_path="reports/задача-🚀.json",
        content='{"title":"задача 🚀","n":1e21,"fraction":0.1}'.encode(),
        actor="analyst",
        step_id="report",
        model="test",
        lineage_root=sdd / "lineage",
        run_id=run_id,
        hmac_key=_KEY,
        timestamp=123,
    )


@pytest.mark.parametrize("profile", ["py-json-v1", "jcs-v2"])
def test_configured_run_uses_one_profile_in_all_three_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    """Exercise startup, the shared artifact boundary and finalization, offline."""
    seed_path = tmp_path / "bernstein.yaml"
    seed_path.write_text(f"goal: test profile\nhash_profile: {profile}\n", encoding="utf-8")
    seed = parse_seed(seed_path)
    config = OrchestratorConfig(hash_profile=seed.hash_profile, evolution_enabled=False)
    monkeypatch.setenv("BERNSTEIN_RUN_ID", "profile-run")
    monkeypatch.setenv("BERNSTEIN_AUDIT", "0")
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: _KEY)
    _signer(tmp_path, monkeypatch)
    adapter = MagicMock(spec=CLIAdapter)
    spawner = AgentSpawner(adapter, tmp_path / "templates", tmp_path, default_model="test")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))) as client:
        orch = Orchestrator(config, spawner, tmp_path, client=client)
        try:
            orch._record_run_started()
            orch._recorder.record("task_completed", title="задача 🚀", n=1e21, fraction=0.1)
            sdd = tmp_path / ".sdd"
            _artifact(sdd, "profile-run")
            # A fresh caller must retain the same spine profile on the next append.
            _artifact(sdd, "profile-run")
            orch._recorder.record("run_completed")
            orch._seal_journal_into_lineage_spine()
            journal = orch._recorder.path
            head_path = sdd / "lineage" / "profile-run" / "spine.head"
            receipt_path = sdd / "runs" / "profile-run" / "run-receipt.json"
            rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
            receipt = json.loads(receipt_path.read_bytes())
            head = json.loads(head_path.read_bytes())
            assert {row.get("hash_profile", "py-json-v1") for row in rows} == {profile}
            assert head.get("hash_profile", "py-json-v1") == profile
            assert receipt.get("hash_profile", "py-json-v1") == profile
            assert verify_journal(journal).chain_consistent
            assert LineageSpine(sdd / "lineage", run_id="profile-run", hmac_key=_KEY).verify().ok
            assert verify_run_receipt(receipt_path.read_bytes()).ok
            # The CLI's direct receipt builder follows the same recorded run selection.
            rebuilt = build_run_receipt("profile-run", sdd, _signer(tmp_path, monkeypatch), write=False)
            assert rebuilt.receipt.get("hash_profile", "py-json-v1") == profile
            adapter.spawn.assert_not_called()
        finally:
            orch._executor.shutdown(wait=True, cancel_futures=True)


def test_default_remains_legacy() -> None:
    assert BernsteinConfig(goal="test").hash_profile == "py-json-v1"
    assert OrchestratorConfig().hash_profile == "py-json-v1"


@pytest.mark.parametrize("bad", ["unknown-v9", "", None, 2, ["jcs-v2"]])
def test_unknown_config_profile_fails_closed(tmp_path: Path, bad: object) -> None:
    with pytest.raises(ValidationError):
        BernsteinConfig.model_validate({"goal": "test", "hash_profile": bad})
    path = tmp_path / "bernstein.yaml"
    path.write_text("goal: test\nhash_profile: " + json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(SeedError, match="hash_profile"):
        parse_seed(path)
    assert not (tmp_path / ".sdd").exists()


@pytest.mark.parametrize("recorded", ["py-json-v1", "jcs-v2"])
def test_existing_records_reopen_and_verify_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str
) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("old-run", sdd, hash_profile=recorded)
    journal.record("run_started", title="задача 🚀", n=1e21, fraction=0.1)
    spine = LineageSpine(sdd / "lineage", run_id="old-run", hmac_key=_KEY, hash_profile=recorded)
    spine.record(artifact_path="old.json", content=b"{}", actor="test", step_id="old", model="test", timestamp=1)
    receipt = build_run_receipt("old-run", sdd, _signer(tmp_path, monkeypatch), hash_profile=recorded)
    paths = [journal.path, spine.spine_path, spine.head_path, receipt.receipt_path]
    before = {p: p.read_bytes() for p in paths}
    # Even a changed new-run config cannot change an existing journal's profile.
    changed_selection = "jcs-v2" if recorded == "py-json-v1" else "py-json-v1"
    resumed = EventJournal.resume("old-run", sdd, hash_profile=changed_selection)
    assert resumed.hash_profile == recorded
    assert verify_journal(resumed.path).chain_consistent
    assert LineageSpine(sdd / "lineage", run_id="old-run", hmac_key=_KEY).verify().ok
    assert verify_run_receipt(receipt.receipt_path.read_bytes()).ok
    assert {p: p.read_bytes() for p in paths} == before
    # Appending with omitted selection also retains the existing spine's profile.
    _artifact(sdd, "old-run")
    assert spine.verify().ok
    assert spine.spine_path.read_bytes().startswith(before[spine.spine_path])
    assert json.loads(spine.head_path.read_bytes()).get("hash_profile", "py-json-v1") == recorded


def test_unknown_recorded_spine_profile_refuses_append_without_rewrite(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _artifact(sdd, "unknown-run")
    spine = LineageSpine(sdd / "lineage", run_id="unknown-run", hmac_key=_KEY)
    head = json.loads(spine.head_path.read_bytes())
    head["hash_profile"] = "unknown-v9"
    spine.head_path.write_text(json.dumps(head), encoding="utf-8")
    before = (spine.spine_path.read_bytes(), spine.head_path.read_bytes())
    with pytest.raises(ValueError, match="hash_profile"):
        _artifact(sdd, "unknown-run")
    assert (spine.spine_path.read_bytes(), spine.head_path.read_bytes()) == before


def test_unknown_runtime_profile_is_refused_before_startup(tmp_path: Path) -> None:
    config = OrchestratorConfig(hash_profile="unknown-v9")
    with pytest.raises(ValueError, match="hash_profile"):
        Orchestrator(config, MagicMock(), tmp_path)
    assert not (tmp_path / ".sdd").exists()


def test_unknown_receipt_profile_is_refused_even_without_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH", raising=False)
    monkeypatch.delenv("BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR", raising=False)
    with pytest.raises(RunReceiptError, match="hash_profile"):
        write_run_receipt_if_configured("bad-run", tmp_path / ".sdd", hash_profile="unknown-v9")
    assert not (tmp_path / ".sdd").exists()


def test_conflicting_spine_profile_refuses_append_without_rewrite(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("conflict-run", sdd, hash_profile="jcs-v2")
    journal.record("run_started")
    _artifact(sdd, "conflict-run")
    spine = LineageSpine(sdd / "lineage", run_id="conflict-run", hmac_key=_KEY, hash_profile="py-json-v1")
    before = (spine.spine_path.read_bytes(), spine.head_path.read_bytes())
    with pytest.raises(ValueError, match="hash_profile conflicts"):
        spine.record(artifact_path="bad.json", content=b"{}", actor="test", step_id="bad", model="test", timestamp=2)
    assert (spine.spine_path.read_bytes(), spine.head_path.read_bytes()) == before


def test_unknown_recorded_journal_profile_refuses_resume_and_spine_write(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("bad-journal", sdd)
    journal.record("run_started")
    row = json.loads(journal.path.read_bytes())
    row["hash_profile"] = "unknown-v9"
    journal.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = journal.path.read_bytes()
    with pytest.raises(ValueError, match="hash_profile"):
        EventJournal.resume("bad-journal", sdd, hash_profile="jcs-v2")
    with pytest.raises(ValueError, match="hash_profile"):
        _artifact(sdd, "bad-journal")
    assert journal.path.read_bytes() == before
    assert not (sdd / "lineage" / "bad-journal" / "spine.jsonl").exists()
