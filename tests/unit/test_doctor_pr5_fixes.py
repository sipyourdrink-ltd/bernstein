"""Regression tests for B7 PR-5: doctor read-only, context-aware, and accurate output."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import status_cmd
from bernstein.cli.first_run_readiness import operator_installation_checks
from bernstein.cli.main import cli
from bernstein.cli.utils.error_suggestions import suggest
from bernstein.core.agents.agent_discovery import _detect_gemini
from bernstein.core.orchestration.preflight import _check_gemini_auth
from bernstein.core.quality.ci_fix import _check_tool_version


def test_doctor_creates_zero_files_or_dirs_in_empty_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running ``bernstein doctor --json`` in an empty directory writes nothing."""
    real_git = shutil.which("git")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name, **kw: real_git if name == "git" else None)

    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert result.exit_code in (0, 1), result.output
    assert list(tmp_path.rglob("*")) == []

    payload = json.loads(result.output)
    checks_by_name = {c["name"]: c for c in payload["checks"]}
    assert checks_by_name["CI tools"]["ok"] is True
    assert "not a Python project" in checks_by_name["CI tools"]["detail"]
    assert checks_by_name["Commit attribution"]["ok"] is True
    assert "not a git repository" in checks_by_name["Commit attribution"]["detail"]
    assert checks_by_name["Audit anchoring"]["ok"] is True
    assert checks_by_name["Audit anchoring"]["detail"] == "no audit log yet"
    assert "bernstein init" in checks_by_name[".sdd workspace"]["fix"]
    assert "bernstein -g" not in checks_by_name[".sdd workspace"]["fix"]

    plain_res = CliRunner().invoke(cli, ["doctor"])
    assert plain_res.exit_code in (0, 1), plain_res.output
    assert list(tmp_path.rglob("*")) == []


def test_doctor_preserves_clean_initialized_dir_without_creating_subdirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bernstein doctor`` after ``bernstein init`` leaves .sdd/ untouched (no audit/schedules dirs)."""
    real_git = shutil.which("git")
    monkeypatch.chdir(tmp_path)
    init_res = CliRunner().invoke(cli, ["init", "--dir", str(tmp_path), "--remote"])
    assert init_res.exit_code == 0, init_res.output
    before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}

    monkeypatch.setattr("shutil.which", lambda name, **kw: real_git if name == "git" else None)
    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert result.exit_code in (0, 1), result.output
    after = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")}
    assert after == before

    payload = json.loads(result.output)
    checks_by_name = {c["name"]: c for c in payload["checks"]}
    assert checks_by_name["Audit anchoring"]["detail"] == "no audit log yet"
    assert checks_by_name["Schedule supervisor"]["detail"] == "no schedules registered"


def test_doctor_skips_uv_run_outside_python_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-Python projects (e.g. package.json) must not invoke ``uv run``."""
    (tmp_path / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    calls: list[object] = []

    def fail_if_called(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("uv run should not be called outside a Python repo")

    monkeypatch.setattr("bernstein.core.quality.ci_fix.subprocess.run", fail_if_called)
    checks: list[dict[str, object]] = []
    status_cmd._doctor_check_ci_tools(checks, tmp_path)
    assert calls == []
    assert checks == [
        {
            "name": "CI tools",
            "ok": True,
            "detail": "skipped (not a Python project)",
            "fix": "",
            "fix_id": "",
        }
    ]


def test_ci_tool_error_is_not_truncated_at_80_chars() -> None:
    """Long stderr / OSError messages must survive intact for doctor --json."""
    long_stderr = "error: Failed to spawn `pyright` — Caused by: " + ("x" * 120)
    mock_result = Mock(returncode=1, stdout="", stderr=long_stderr)
    with patch("bernstein.core.quality.ci_fix.subprocess.run", return_value=mock_result):
        row = _check_tool_version("pyright")
    assert row["ok"] == "False"
    assert row["detail"] == long_stderr
    assert len(row["detail"]) > 80

    long_err = PermissionError(13, "Permission denied: " + ("/very/long/path" * 10))
    with patch("bernstein.core.quality.ci_fix.subprocess.run", side_effect=long_err):
        err_row = _check_tool_version("pyright")
    assert err_row["ok"] == "False"
    assert ("/very/long/path" * 10) in err_row["detail"]
    assert len(err_row["detail"]) > 80


def test_ready_to_run_distinguishes_python_pass_from_missing_worker_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Python 3.12+ passes and worker login fails, do not blame Python."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rows = [
        {"name": "Python version", "ok": True, "detail": "Python 3.14 (need 3.12+)", "fix": ""},
        {"name": "Adapter: claude", "ok": False, "detail": "not found", "fix": ""},
        {"name": "Auth: claude", "ok": False, "detail": "not authenticated", "fix": ""},
        {"name": "Ready to run", "ok": False, "detail": "missing", "fix": ""},
    ]
    out = operator_installation_checks(rows, env={"PATH": str(tmp_path), "HOME": str(tmp_path)})
    ready = next(r for r in out if r["name"] == "Ready to run")
    assert ready["ok"] is False
    assert ready["detail"] == "matching worker login is missing"
    assert "Python" not in ready["detail"]


def test_gemini_auth_guidance_does_not_recommend_bare_gcloud_auth_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini guidance must not give broken bare ``gcloud auth login`` advice."""
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    monkeypatch.setattr("bernstein.core.preflight.gemini_has_auth", lambda **_kw: (False, ""))
    monkeypatch.setattr("bernstein.core.preflight._claude_has_oauth_session", lambda **_kw: False)
    monkeypatch.setattr("bernstein.core.preflight._codex_has_auth", lambda **_kw: (False, ""))
    rows: list[dict[str, object]] = []
    status_cmd._doctor_check_auth(rows, env=env)
    gemini_row = next(r for r in rows if r["name"] == "Auth: gemini")
    assert "gcloud auth login" not in str(gemini_row["fix"])
    assert "GEMINI_API_KEY" in str(gemini_row["fix"])

    preflight_err = _check_gemini_auth(env=env)
    assert preflight_err is not None
    assert "gcloud auth login" not in preflight_err[2]

    suggestion = suggest("GOOGLE_API_KEY not set for gemini")
    assert suggestion is not None
    assert all("gcloud auth login" not in step for step in suggestion.steps)

    fake_gemini = tmp_path / "gemini"
    fake_gemini.write_text("#!/bin/sh\necho 0.1.0\n", encoding="utf-8")
    fake_gemini.chmod(0o755)
    with patch.dict("os.environ", env, clear=True):
        _caps, warnings = _detect_gemini()
    assert warnings
    assert all("gcloud auth login" not in w for w in warnings)


def test_config_validate_succeeds_on_fresh_init_and_uncommented_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bernstein config validate`` works on fresh ``init`` output before and after setting ``goal``."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--dir", str(tmp_path), "--remote"]).exit_code == 0

    fresh_validate = runner.invoke(cli, ["config", "validate"])
    assert fresh_validate.exit_code == 0, fresh_validate.output
    assert "valid" in fresh_validate.output.lower()

    yaml_path = tmp_path / "bernstein.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace(
            '# goal: "Describe what you want the agents to build or improve"',
            'goal: "Add unit tests"',
        ),
        encoding="utf-8",
    )
    active_validate = runner.invoke(cli, ["config", "validate"])
    assert active_validate.exit_code == 0, active_validate.output
    assert "Configuration is valid" in active_validate.output

    yaml_path.write_text("goal: Test\nbudget: []\n", encoding="utf-8")
    invalid_validate = runner.invoke(cli, ["config", "validate"])
    assert invalid_validate.exit_code == 1
    assert "budget" in invalid_validate.output.lower()


def test_commit_attribution_handles_empty_git_repo_gracefully(tmp_path: Path) -> None:
    """An initialized git repo with 0 commits must not fail Commit attribution with exit 128."""
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    checks: list[dict[str, object]] = []
    status_cmd._doctor_check_commit_attribution(checks, tmp_path)
    assert len(checks) == 1
    assert checks[0]["ok"] is True
    assert "no commits" in str(checks[0]["detail"])
