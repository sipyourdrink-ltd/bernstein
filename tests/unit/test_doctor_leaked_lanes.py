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
