"""Unit tests for ``bernstein doctor sovereign`` (issue #2518).

The battery is pure functions over the process environment + the filesystem;
we isolate them with tmp_path + monkeypatch so the asserts are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import doctor as doctor_group
from bernstein.cli.commands.doctor_sovereign_cmd import run_doctor_sovereign
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.deployment_profile import (
    SOVEREIGN_PROFILE,
    build_posture_attestation,
    load_config_snapshot,
    resolve_effective_policy,
)


def _clean_workspace(tmp_path: Path) -> Path:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage:\n  backend: memory\n", encoding="utf-8")
    return tmp_path


def test_doctor_sovereign_green_on_clean_host(tmp_path: Path) -> None:
    """AC1: --profile sovereign yields a green doctor sovereign on a clean host."""
    _clean_workspace(tmp_path)
    rc = run_doctor_sovereign(workdir=tmp_path, as_json=False)
    assert rc == 0


def test_doctor_sovereign_json_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(doctor_group, ["--json", "sovereign"], obj={})
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["posture_hash"].startswith("sha256:")
    names = {c["name"] for c in payload["checks"]}
    assert "storage backend local" in names
    assert "posture attested (no drift)" in names


def test_doctor_sovereign_fails_on_cloud_storage(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bernstein.yaml").write_text(
        "goal: x\nstorage:\n  backend: postgres\n  database_url: postgres://db.cloud/x\n", encoding="utf-8"
    )
    rc = run_doctor_sovereign(workdir=tmp_path, as_json=False)
    assert rc == 1


def test_doctor_sovereign_reports_drift_after_edit(tmp_path: Path) -> None:
    """AC3 (doctor surface): a config edit after attestation shows as drift/FAIL."""
    _clean_workspace(tmp_path)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(
        workdir=tmp_path,
        policy=policy,
        timestamp=1,
        chain=AuditChainStore(tmp_path / ".sdd" / "audit"),
    )
    # Edit a residency-relevant key after attestation.
    (tmp_path / "bernstein.yaml").write_text(
        "goal: x\nstorage:\n  backend: redis\n  redis_url: redis://cache.cloud/0\n", encoding="utf-8"
    )
    rc = run_doctor_sovereign(workdir=tmp_path, as_json=False)
    assert rc == 1


def test_doctor_sovereign_green_after_matching_attestation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_workspace(tmp_path)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(
        workdir=tmp_path,
        policy=policy,
        timestamp=1,
        chain=AuditChainStore(tmp_path / ".sdd" / "audit"),
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(doctor_group, ["--json", "sovereign"], obj={})
    payload = json.loads(result.output)
    assert payload["ok"] is True
    attest_check = next(c for c in payload["checks"] if c["name"] == "posture attested (no drift)")
    assert attest_check["status"] == "PASS"
    assert payload["attested_hash"] == payload["posture_hash"]
