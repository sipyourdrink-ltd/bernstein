"""CLI tests for ``bernstein run-service`` (issue #2352).

``submit --foreground`` opens the run and advances it in-process; ``attach``
proves ledger continuity across the detach boundary and renders the live
projection; ``status`` reports daemon liveness plus the ledger projection;
``verify`` re-checks the HMAC audit chain, the ledger chain, and every
reattach continuity proof offline. The thin client never rebuilds scheduler
state -- it replays the ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.run_service_cmd import run_service_group


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _run(args: list[str]) -> object:
    return CliRunner().invoke(run_service_group, args, catch_exceptions=False)


def _submit_foreground(project: Path, tasks: list[str]) -> str:
    args = ["submit", "a real goal", "--foreground", "--json"]
    for t in tasks:
        args += ["--task", t]
    result = _run(args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["run_id"]


def test_submit_foreground_runs_to_completion(project: Path) -> None:
    run_id = _submit_foreground(project, ["t0", "t1"])
    result = _run(["status", run_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["completed_tasks"] == ["t0", "t1"]
    assert payload["run_closed"] is True


def test_attach_reports_continuity_ok(project: Path) -> None:
    run_id = _submit_foreground(project, ["t0"])
    result = _run(["attach", run_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["continuity"]["ok"] is True
    assert payload["completed_tasks"] == ["t0"]


def test_verify_passes_across_lifecycle(project: Path) -> None:
    run_id = _submit_foreground(project, ["t0", "t1"])
    _run(["attach", run_id])  # records a reattach receipt
    result = _run(["verify", run_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["audit_ok"] is True
    assert payload["ledger_ok"] is True
    assert payload["continuity_ok"] is True


def test_attach_unknown_run_exits_nonzero(project: Path) -> None:
    result = CliRunner().invoke(run_service_group, ["attach", "run-nope"], catch_exceptions=False)
    assert result.exit_code != 0


def test_status_without_run_id_lists_runs(project: Path) -> None:
    run_id = _submit_foreground(project, ["t0"])
    result = _run(["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert run_id in [r["run_id"] for r in payload["runs"]]


def test_verify_detects_ledger_tamper(project: Path) -> None:
    from bernstein.core.persistence.work_ledger import run_ledger_dir

    run_id = _submit_foreground(project, ["t0"])
    bucket = run_ledger_dir(project / ".sdd", run_id) / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("run.open", "run.tampered")
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _run(["verify", run_id, "--json"])
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ledger_ok"] is False
