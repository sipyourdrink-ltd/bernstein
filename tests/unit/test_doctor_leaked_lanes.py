"""The public doctor preserves the leak check's evidence and error semantics."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import doctor_cmd, status_cmd
from bernstein.cli.main import cli

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("status", ["PASS", "WARN", "FAIL"])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("auto_fix", [False, True])
def test_public_doctor_preserves_leak_status_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, as_json: bool, auto_fix: bool
) -> None:
    # Unrelated installation probes are outside this boundary and must not
    # read the operator's environment, credentials, filesystem or network.
    def unrelated_probe(*_args, **_kwargs):
        return SimpleNamespace(to_dict=lambda: {})

    for name in vars(status_cmd):
        if name.startswith("_doctor_check_"):
            monkeypatch.setattr(status_cmd, name, unrelated_probe)
    monkeypatch.setattr(status_cmd, "_detect_runtime_environment", lambda: "local")
    monkeypatch.setattr(
        "bernstein.core.security.declaration_quarantine.quarantine_summary", lambda *_: ("Quarantine", True, "none")
    )
    monkeypatch.chdir(tmp_path)
    row = {
        "kind": "sandbox",
        "resource_id": "sbx-old",
        "task_ids": ["T-1"],
        "task_statuses": {"T-1": "done"},
        "age_seconds": 3600,
        "age_source": "audit_session_create",
        "max_age_seconds": 60,
        "reason": "age_exceeded",
    }
    detail = "leak check unavailable" if status == "FAIL" else "sandbox sbx-old (T-1=done, 1h)"
    findings = [row] if status == "WARN" else []
    monkeypatch.setattr(
        doctor_cmd,
        "check_leaked_lanes",
        lambda *_: {"name": "Leaked lanes", "status": status, "detail": detail, "fix": "preview", "findings": findings},
    )
    result = CliRunner().invoke(cli, ["doctor", *(["--json"] if as_json else []), *(["--fix"] if auto_fix else [])])
    assert result.exit_code == (1 if status == "FAIL" else 0), result.output
    if as_json:
        payload = json.loads(result.output)
        check = next(check for check in payload["checks"] if check["name"] == "Leaked lanes")
        assert check["ok"] is (status != "FAIL")
        assert check["leaked_lanes"] == findings
        assert check["detail"] == ("WARNING: " if status == "WARN" else "") + detail
    else:
        assert "Leaked lanes" in result.output
        assert ("unavailable" if status == "FAIL" else "sbx-old") in result.output
    assert list(tmp_path.iterdir()) == [], "the leak check must not gain a --fix write action"


def test_fresh_init_audit_directories_need_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".sdd/audit/merkle").mkdir(parents=True)
    key_path = tmp_path / "absent.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    result = doctor_cmd.check_leaked_lanes(tmp_path)
    assert result["status"] == "PASS", result
    assert not key_path.exists()
    assert not list((tmp_path / ".sdd/audit").rglob("*.*"))


@pytest.mark.parametrize("defect", ["create-json", "destroy-json", "hmac", "utf8", "archived-json"])
def test_doctor_rejects_invalid_audit_evidence_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    import gzip

    from bernstein.core.security.audit import SANDBOX_SESSION_CREATE, SANDBOX_SESSION_DESTROY, AuditLog

    key = b"fixture-only-doctor-key" * 2
    key_path = tmp_path / "fixture.key"
    key_path.touch(mode=0o600)
    key_path.write_bytes(key)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    audit_dir = tmp_path / ".sdd/audit"
    audit = AuditLog(audit_dir=audit_dir, key=key)
    audit.log(
        event_type=SANDBOX_SESSION_CREATE,
        actor="fixture",
        resource_type="sandbox_session",
        resource_id="sbx-1",
        details={"session_id": "sbx-1", "backend": "docker", "agent_session_id": "backend-1", "task_ids": ["T-1"]},
    )
    audit.log(
        event_type=SANDBOX_SESSION_DESTROY,
        actor="fixture",
        resource_type="sandbox_session",
        resource_id="sbx-1",
        details={"session_id": "sbx-1", "agent_session_id": "backend-1"},
    )
    task_log = tmp_path / ".sdd/runtime/tasks.jsonl"
    task_log.parent.mkdir(parents=True)
    task_log.write_text(
        json.dumps({"id": "T-1", "title": "fixture", "description": "", "role": "backend", "status": "done"}) + "\n"
    )
    assert doctor_cmd.check_leaked_lanes(tmp_path)["status"] == "PASS"
    segment = next(audit_dir.glob("*.jsonl"))
    lines = segment.read_bytes().splitlines(keepends=True)
    index = 0 if defect == "create-json" else 1
    if defect == "hmac":
        value = json.loads(lines[index])
        value["hmac"] = "f" * 64
        lines[index] = (json.dumps(value) + "\n").encode()
    else:
        lines[index] = b"\xff\n" if defect == "utf8" else b"{\n"
    segment.write_bytes(b"".join(lines))
    if defect == "archived-json":
        archive = audit_dir / "archive"
        archive.mkdir(exist_ok=True)
        (archive / (segment.name + ".gz")).write_bytes(gzip.compress(segment.read_bytes()))
        segment.unlink()
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = doctor_cmd.check_leaked_lanes(tmp_path)
    assert result["status"] == "FAIL", result
    assert result["findings"] == []
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("shape", ["file", "dangling-symlink", "directory-symlink"])
def test_doctor_rejects_invalid_audit_root_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    audit_dir = tmp_path / ".sdd/audit"
    audit_dir.parent.mkdir()
    if shape == "file":
        audit_dir.write_text("not an audit directory")
    else:
        target = tmp_path / "audit-target"
        if shape == "directory-symlink":
            target.mkdir()
        audit_dir.symlink_to(target)
    from bernstein.core.security import audit

    monkeypatch.setattr(audit, "load_audit_key", lambda: pytest.fail("invalid root must not load a key"))
    result = doctor_cmd.check_leaked_lanes(tmp_path)
    assert result["status"] == "FAIL", result
    assert result["findings"] == []
    assert audit_dir.is_symlink() if shape != "file" else audit_dir.read_text() == "not an audit directory"
