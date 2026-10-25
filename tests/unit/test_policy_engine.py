"""Tests for the policy-as-code engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from bernstein.cli.policy_cmd import policy_group
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.policy_engine import (
    PolicyDiff,
    PolicyEngine,
    PolicyFile,
    PolicySubject,
    _run_opa_eval,
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


class TestRunOpaEvalFailures:
    """`_run_opa_eval` raising is the only channel an `opa` failure has.

    Every existing Rego test monkeypatches `_run_opa_eval` away, so the error
    path inside it never ran: the fallback chain that picks stderr, then
    stdout, then a generic string could be broken without turning anything red.
    """

    @staticmethod
    def _fake_run(monkeypatch: MonkeyPatch, *, stdout: str, stderr: str, seen: list[list[str]] | None = None) -> None:
        """Make `opa eval` fail with the given output, recording its argv."""

        def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if seen is not None:
                seen.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=stdout, stderr=stderr)

        monkeypatch.setattr("bernstein.core.policy_engine.subprocess.run", _run)

    def test_opa_failure_surfaces_stderr_when_stdout_empty(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """The common CLI failure shape: a Rego error on stderr, nothing on stdout."""
        self._fake_run(monkeypatch, stdout="", stderr="rego_parse_error: unexpected eof\n")

        with pytest.raises(OSError) as excinfo:
            _run_opa_eval(tmp_path / "limits.rego", {"files": 1})

        assert "rego_parse_error: unexpected eof" in str(excinfo.value)

    def test_opa_failure_surfaces_stdout_when_stderr_empty(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """Some failures report on stdout instead; that text must survive too."""
        self._fake_run(monkeypatch, stdout="undefined function data.bernstein.deny\n", stderr="")

        with pytest.raises(OSError) as excinfo:
            _run_opa_eval(tmp_path / "limits.rego", {"files": 1})

        assert "undefined function data.bernstein.deny" in str(excinfo.value)

    def test_opa_failure_falls_back_to_generic_message(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """With both streams empty there is nothing to quote, so say so plainly."""
        self._fake_run(monkeypatch, stdout="   ", stderr="\n")

        with pytest.raises(OSError) as excinfo:
            _run_opa_eval(tmp_path / "limits.rego", {"files": 1})

        assert str(excinfo.value) == "opa eval failed"

    def test_opa_failure_unlinks_input_file(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        """The payload temp file is removed even when the run failed.

        It is written with `delete=False`, so only the `finally` removes it; a
        failing policy run on a long-lived host would otherwise leak one file
        per evaluation.
        """
        seen: list[list[str]] = []
        self._fake_run(monkeypatch, stdout="", stderr="boom", seen=seen)

        with pytest.raises(OSError):
            _run_opa_eval(tmp_path / "limits.rego", {"files": 1})

        assert len(seen) == 1
        argv = seen[0]
        input_path = Path(argv[argv.index("--input") + 1])
        assert not input_path.exists()
