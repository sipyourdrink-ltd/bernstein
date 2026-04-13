"""Tests for the policy-as-code engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

from bernstein.cli.policy_cmd import policy_group
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.policy_engine import (
    PolicyDiff,
    PolicyEngine,
    PolicyFile,
    PolicySubject,
    load_policy_engine,
    run_policy_engine,
)
from click.testing import CliRunner
from pytest import MonkeyPatch


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_task() -> Task:
    return Task(
        id="task-policy",
        title="Policy task",
        description="Apply policy checks to modified files.",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
    )


def _prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bernstein Tests")
    _git(repo, "config", "user.email", "tests@example.com")
    _write_file(repo / "app.py", "def safe() -> int:\n    return 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "agent/test")
    return repo


def _mock_opa_path(_name: str) -> str:
    return "/usr/bin/opa"


def _mock_rego_eval(_path: Path, _payload: dict[str, object]) -> list[str]:
    return ["too many files"]


class TestPolicyEngine:
    def test_yaml_policy_blocks_eval_usage(self, tmp_path: Path) -> None:
        repo = _prepare_repo(tmp_path)
        _write_file(
            repo / ".sdd" / "policies" / "no_eval.yaml",
            'name: no_eval\nrule: "file_content !~ /eval\\\\(/"\nseverity: block\n',
        )
        _write_file(repo / "app.py", "def unsafe(expr: str) -> int:\n    return eval(expr)\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "introduce eval")

        engine = load_policy_engine(repo)

        assert engine is not None
        result = run_policy_engine(_make_task(), repo, repo, engine)

        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].policy_name == "no_eval"
        assert result.violations[0].blocked is True
        assert (repo / ".sdd" / "metrics" / "policy_violations.jsonl").exists()

    def test_rego_policy_emits_blocking_violation(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        policies_dir = tmp_path / ".sdd" / "policies"
        _write_file(policies_dir / "limits.rego", "package bernstein\n\ndeny := []\n")

        engine = PolicyEngine.from_directory(policies_dir)

        assert engine is not None
        monkeypatch.setattr("bernstein.core.policy_engine.shutil.which", _mock_opa_path)
        monkeypatch.setattr("bernstein.core.policy_engine._run_opa_eval", _mock_rego_eval)

        violations = engine.check(
            PolicySubject(id="manual", title="Manual", description="Manual audit", role="backend"),
            PolicyDiff(diff_text="", files=(PolicyFile(path="a.py", content="print('x')"),)),
        )

        assert len(violations) == 1
        assert violations[0].source == "rego"
        assert violations[0].blocked is True

    def test_policy_cli_check_reports_blocking_violation(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        repo = _prepare_repo(tmp_path)
        _write_file(
            repo / ".sdd" / "policies" / "no_eval.yaml",
            'name: no_eval\nrule: "file_content !~ /eval\\\\(/"\nseverity: block\n',
        )
        _write_file(repo / "app.py", "def unsafe(expr: str) -> int:\n    return eval(expr)\n")
        _git(repo, "add", "app.py")
        _git(repo, "commit", "-m", "introduce eval")

        runner = CliRunner()
        monkeypatch.chdir(repo)

        result = runner.invoke(policy_group, ["check"])

        assert result.exit_code != 0
        assert "no_eval" in result.output
        assert "blocked" in result.output.lower()
