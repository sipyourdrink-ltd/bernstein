"""Doctor producers and the installation audit share one finding contract (#5073)."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints
from unittest.mock import Mock

import pytest
from beartype import beartype
from click.testing import CliRunner

from bernstein.cli.commands import doctor_cmd, status_cmd
from bernstein.cli.commit_stats import CommitStatsResult
from bernstein.cli.main import cli
from bernstein.core.checks.contract import Finding, Verdict
from bernstein.core.quality.ci_fix import check_test_dependencies as real_test_dependencies


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An installed adapter, deterministic tool probes, and no owner credentials."""
    monkeypatch.chdir(tmp_path)
    for directory in (".sdd/backlog", ".sdd/runtime"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BERNSTEIN_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("BERNSTEIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("BERNSTEIN_REDIS_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda name, **kw: f"/fixture/{name}" if name in {"claude", "agy"} else None)
    monkeypatch.setattr(doctor_cmd, "_probe_adapter_version", lambda *a, **kw: "99.0.0")
    monkeypatch.setattr("bernstein.core.preflight._claude_has_oauth_session", lambda **kw: True)
    monkeypatch.setattr("bernstein.core.preflight._codex_has_auth", lambda **kw: (False, ""))
    monkeypatch.setattr("bernstein.core.preflight.gemini_has_auth", lambda **kw: (False, ""))
    monkeypatch.setattr(
        "bernstein.core.ci_fix.check_test_dependencies",
        lambda: [{"name": "ruff", "ok": "True", "detail": "ruff 1.0", "fix": ""}],
    )
    monkeypatch.setattr(
        "bernstein.cli.commit_stats.collect_commit_stats",
        lambda **kw: CommitStatsResult(),
    )
    # Never open a host socket for the installation fixture.
    connection = Mock()
    connection.connect_ex.return_value = 1
    socket_context = Mock()
    socket_context.__enter__ = Mock(return_value=connection)
    socket_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("socket.socket", Mock(return_value=socket_context))
    return tmp_path


def test_doctor_and_installation_audit_report_identical_findings(installation: Path) -> None:
    runner = CliRunner()
    doctor = runner.invoke(cli, ["doctor", "--json"])
    assert doctor.exit_code in (0, 1), doctor.output
    payload = json.loads(doctor.output)
    assert "findings" in payload, "doctor must expose its audit findings"

    audit = runner.invoke(cli, ["govern", "audit", "--only", "INS", "--json"])
    assert audit.exit_code in (0, 1), audit.output
    assert payload["findings"] == json.loads(audit.output)
    assert {f["check_id"] for f in payload["findings"]} == {f"INS-{n:03}" for n in range(1, 21)}


def test_every_doctor_helper_is_a_finding_producer(installation: Path) -> None:
    helpers = {name: helper for name, helper in vars(status_cmd).items() if name.startswith("_doctor_check_")}
    for name, helper in helpers.items():
        assert hasattr(helper, "check_id"), f"{name} has no stable audit ID"
        rows: list[dict] = []
        kwargs = {}
        for parameter in inspect.signature(helper).parameters:
            if parameter == "checks":
                kwargs[parameter] = rows
            elif parameter == "workdir":
                kwargs[parameter] = installation
            elif parameter == "_check":
                kwargs[parameter] = lambda *args, rows=rows: status_cmd._add_check(rows, *args)
        finding = helper(**kwargs)
        assert type(finding) is Finding, name
        assert finding.check_id == helper.check_id
        assert finding.area == "INS"


def test_producer_declares_its_finding_return_type() -> None:
    helper = status_cmd._doctor_check_python
    assert get_type_hints(helper)["return"] is Finding
    assert inspect.signature(helper).return_annotation is Finding
    assert type(beartype(helper)([])) is Finding


@pytest.mark.parametrize("tool", ["uv", "ruff", "pytest", "pyright", "git"])
def test_missing_tool_is_not_measurable(tool: str, installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **kw: None)
    if tool == "git":
        monkeypatch.setattr(
            "bernstein.cli.commit_stats.collect_commit_stats",
            lambda **kw: CommitStatsResult(error="[Errno 2] No such file or directory: 'git'"),
        )
        helper = status_cmd._doctor_check_commit_attribution
        args = ([], installation)
    else:
        monkeypatch.setattr(
            "bernstein.core.ci_fix.check_test_dependencies",
            lambda: [
                {
                    "name": "ruff" if tool == "uv" else tool,
                    "ok": "False",
                    "detail": f"{tool} not found on PATH",
                    "fix": f"Install {tool}",
                }
            ],
        )
        helper = status_cmd._doctor_check_ci_tools
        args = ([],)
    finding = helper(*args)
    assert type(finding) is Finding, "a missing tool must still produce a finding"
    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert not finding.passed
    assert tool in finding.what_would_make_it_measurable


@pytest.mark.parametrize("tool", ["claude", "codex", "gcloud"])
def test_missing_auth_probe_is_not_measurable(tool: str, installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name, **kw: None if name == tool else f"/fixture/{name}")
    monkeypatch.setattr("bernstein.core.preflight._claude_has_oauth_session", lambda **kw: False)
    finding = status_cmd._doctor_check_auth([], env={})
    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert tool in finding.what_would_make_it_measurable


def test_missing_secrets_cli_is_not_measurable(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (installation / "bernstein.yaml").write_text("secrets:\n  provider: 1password\n  path: fixture/item\n")
    monkeypatch.setattr("subprocess.run", Mock(side_effect=FileNotFoundError("op")))
    finding = status_cmd._doctor_check_secrets(installation, lambda *args: None)
    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert "op" in finding.what_would_make_it_measurable


def test_ci_probe_without_uv_is_not_measurable(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bernstein.core.ci_fix.check_test_dependencies", real_test_dependencies)
    monkeypatch.setattr("subprocess.run", Mock(side_effect=FileNotFoundError("uv")))
    finding = status_cmd._doctor_check_ci_tools([])
    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert "uv" in finding.what_would_make_it_measurable


@pytest.mark.parametrize("tool", ["ruff", "pytest", "pyright"])
def test_ci_probe_without_target_is_not_measurable(
    tool: str, installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bernstein.core.ci_fix.check_test_dependencies", real_test_dependencies)

    def probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command[2] == tool:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr=f"{tool}: executable not found")
        return subprocess.CompletedProcess(command, 0, stdout="1.0", stderr="")

    monkeypatch.setattr("subprocess.run", probe)
    finding = status_cmd._doctor_check_ci_tools([])
    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert tool in finding.what_would_make_it_measurable


def test_doctor_text_still_renders_legacy_rows(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = Mock()
    monkeypatch.setattr(status_cmd, "_doctor_render_table", rendered)
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    rows = rendered.call_args.args[0]
    assert rows[0] == {
        "name": "Python version",
        "ok": True,
        "detail": f"Python {sys.version_info.major}.{sys.version_info.minor} (need 3.12+)",
        "fix": "",
        "fix_id": "",
    }
    assert any(row["name"] == "Adapter version posture" and "receipt sha256:" in row["detail"] for row in rows)
