"""One run selection reaches journal, spine and receipt (#5274)."""

from __future__ import annotations

import json
import runpy
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
from bernstein.core.orchestration.multi_cell import MultiCellOrchestrator
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.replay.journal import EventJournal, JournalPathError, verify_journal
from bernstein.core.replay.rederive import rederive_run
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


def test_production_startup_refuses_unknown_profile_before_adapter_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bernstein.yaml"
    path.write_text("goal: test\nhash_profile: unknown-v9\n", encoding="utf-8")
    lookup = MagicMock(side_effect=AssertionError("no adapter lookup for invalid config"))
    monkeypatch.setattr("bernstein.adapters.registry.get_adapter", lookup)
    monkeypatch.setattr("bernstein.core.json_logging.setup_json_logging", lambda: None)
    monkeypatch.setattr("bernstein.core.observability.log_redact.install_pii_filter", lambda: None)
    monkeypatch.delenv("BERNSTEIN_REPLAY_RUN_ID", raising=False)
    monkeypatch.delenv("BERNSTEIN_DETERMINISTIC_SEED", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["orchestrator", "--adapter", "mock", "--seed-path", str(path)])
    source = Path(__file__).resolve().parents[2] / "src/bernstein/core/orchestration/orchestrator.py"
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(source), run_name="__main__")
    assert stopped.value.code == 1
    lookup.assert_not_called()
    assert not (tmp_path / ".sdd" / "runs").exists()


@pytest.mark.parametrize("profile", ["py-json-v1", "jcs-v2"])
def test_multi_cell_selection_reaches_all_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setenv("BERNSTEIN_RUN_ID", "multi-profile")
    monkeypatch.setenv("BERNSTEIN_LINEAGE_ENABLED", "1")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))) as client:
        orch = MultiCellOrchestrator(OrchestratorConfig(hash_profile=profile), MagicMock(), tmp_path, client=client)
        orch._recorder.record("run_started", mode="multi_cell")
        sdd = tmp_path / ".sdd"
        _artifact(sdd, "multi-profile")
        receipt = build_run_receipt("multi-profile", sdd, _signer(tmp_path, monkeypatch))
        assert orch._recorder.hash_profile == profile
        head = sdd / "lineage" / "multi-profile" / "spine.head"
        assert json.loads(head.read_bytes()).get("hash_profile", "py-json-v1") == profile
        assert receipt.receipt.get("hash_profile", "py-json-v1") == profile
        assert verify_run_receipt(receipt.receipt_path.read_bytes()).ok
        before = orch._recorder.path.read_bytes()
        changed = "jcs-v2" if profile == "py-json-v1" else "py-json-v1"
        reopened = MultiCellOrchestrator(OrchestratorConfig(hash_profile=changed), MagicMock(), tmp_path, client=client)
        assert reopened._recorder.hash_profile == profile
        assert reopened._recorder.path.read_bytes() == before


def test_multi_cell_unknown_profile_is_refused_before_startup(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hash_profile"):
        MultiCellOrchestrator(OrchestratorConfig(hash_profile="unknown-v9"), MagicMock(), tmp_path)
    assert not (tmp_path / ".sdd").exists()


@pytest.mark.parametrize("explicit", [None, "jcs-v2"])
def test_durable_jcs_row_recovers_after_header_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: str | None
) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("crash-run", sdd, hash_profile="jcs-v2")
    journal.record("run_started")
    spine = LineageSpine(sdd / "lineage", run_id="crash-run", hmac_key=_KEY, hash_profile="jcs-v2")

    def crash(*_args: object) -> None:
        raise OSError("simulated failure after row fsync")

    monkeypatch.setattr(spine, "_write_head", crash)
    with pytest.raises(OSError, match="after row fsync"):
        spine.record(artifact_path="first.json", content=b"{}", actor="test", step_id="1", model="test", timestamp=1)
    before = spine.spine_path.read_bytes()
    assert not spine.head_path.exists()
    reopened = LineageSpine(sdd / "lineage", run_id="crash-run", hmac_key=_KEY, hash_profile=explicit)
    reopened.record(artifact_path="next.json", content=b"{}", actor="test", step_id="2", model="test", timestamp=2)
    assert reopened.spine_path.read_bytes().startswith(before)
    assert json.loads(reopened.head_path.read_bytes())["hash_profile"] == "jcs-v2"
    assert reopened.verify().ok


def test_unknown_head_only_profile_is_not_overwritten(tmp_path: Path) -> None:
    spine = LineageSpine(tmp_path / "lineage", run_id="head-only", hmac_key=_KEY)
    spine.run_dir.mkdir(parents=True)
    before = b'{"hash_profile":"unknown-v9"}\n'
    spine.head_path.write_bytes(before)
    with pytest.raises(ValueError, match="hash_profile"):
        spine.record(artifact_path="new.json", content=b"{}", actor="test", step_id="1", model="test", timestamp=1)
    assert spine.head_path.read_bytes() == before
    assert not spine.spine_path.exists()


@pytest.mark.parametrize("run_id", ["release:2026", "задача-🚀", "long-" * 16])
def test_standalone_spine_keeps_its_existing_run_id_contract(tmp_path: Path, run_id: str) -> None:
    spine = LineageSpine(tmp_path / "lineage", run_id=run_id, hmac_key=_KEY)
    spine.record(artifact_path="result.json", content=b"{}", actor="test", step_id="1", model="test", timestamp=1)
    assert spine.verify().ok


def test_optional_journal_lookup_preserves_symlink_containment(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    runs = sdd / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "journal.jsonl"
    target.write_bytes(b'{"hash_profile":"jcs-v2"}\n')
    try:
        (runs / "escape-run").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(JournalPathError, match="escapes"):
        _artifact(sdd, "escape-run")
    assert target.read_bytes() == b'{"hash_profile":"jcs-v2"}\n'
    assert not (sdd / "lineage" / "escape-run" / "spine.jsonl").exists()


@pytest.mark.parametrize("profile", ["py-json-v1", "jcs-v2"])
def test_blank_lines_before_first_journal_event_do_not_change_selection(tmp_path: Path, profile: str) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("blank-run", sdd, hash_profile=profile)
    journal.record("run_started")
    journal.path.write_bytes(b"\n \t\n" + journal.path.read_bytes())
    before = journal.path.read_bytes()
    assert verify_journal(journal.path).chain_consistent
    _artifact(sdd, "blank-run")
    spine = LineageSpine(sdd / "lineage", run_id="blank-run", hmac_key=_KEY)
    assert spine.verify().ok
    assert json.loads(spine.head_path.read_bytes()).get("hash_profile", "py-json-v1") == profile
    assert journal.path.read_bytes() == before


@pytest.mark.parametrize("profile", ["py-json-v1", "jcs-v2"])
def test_rederive_preserves_recorded_profile_and_source_bytes(tmp_path: Path, profile: str) -> None:
    journal = EventJournal("rederive-profile", tmp_path / ".sdd", hash_profile=profile)
    journal.record("run_started")
    journal.record("plan.graph.full", goal="задача 🚀", nodes=[{"id": "T-1", "depends_on": []}])
    journal.record("task_claimed", task_id="T-1")
    journal.record("task_completed", task_id="T-1", cost_usd=0.1)
    journal.record("run_completed")
    before = journal.path.read_bytes()
    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")
    assert result.ok, result.reason
    assert result.derived_head == journal.head()
    assert journal.path.read_bytes() == before


def test_rederive_unknown_profile_fails_closed_without_writes(tmp_path: Path) -> None:
    journal = EventJournal("unknown-rederive", tmp_path / ".sdd")
    journal.record("plan.graph.full", nodes=[])
    row = json.loads(journal.path.read_bytes())
    row["hash_profile"] = "unknown-v9"
    journal.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = journal.path.read_bytes()
    result = rederive_run(journal.path, sandbox=tmp_path / "sandbox")
    assert not result.ok
    assert "hash_profile" in result.reason
    assert journal.path.read_bytes() == before
    assert not (tmp_path / "sandbox").exists()
