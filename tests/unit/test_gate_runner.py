"""Unit tests for the async quality gate runner."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.gate_runner import GatePipelineStep, GateRunner, normalize_gate_condition
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import QualityGatesConfig


def _make_task(*, owned_files: list[str] | None = None) -> Task:
    return Task(
        id="T-gates-1",
        title="Quality gates task",
        description="Exercise the gate runner.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        owned_files=owned_files or [],
    )


def test_normalize_legacy_condition() -> None:
    assert normalize_gate_condition("changed_files.any('.py')") == "python_changed"


def test_parallel_execution_preserves_pipeline_order(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "module.py").write_text("print('ok')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[
            GatePipelineStep(name="lint", required=True, condition="python_changed"),
            GatePipelineStep(name="type_check", required=True, condition="python_changed"),
        ],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/module.py"])

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return True, "ok"

    with patch("bernstein.core.quality_gates._run_command", side_effect=fake_run):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert max_active >= 2
    assert [result.name for result in report.results] == ["lint", "type_check"]


def test_changed_file_resolution_prefers_owned_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "owned.py").write_text("print('owned')\n", encoding="utf-8")
    (src / "fallback.py").write_text("print('fallback')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=["src/owned.py", "missing.py"])

    with patch("bernstein.core.quality_gates._run_command", return_value=(True, "ok")):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.changed_files == ["src/owned.py"]


def test_changed_file_resolution_uses_git_diff_fallback(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "fallback.py").write_text("print('fallback')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with (
        patch.object(GateRunner, "_git_diff_changed_files", return_value=["src/fallback.py"]),
        patch("bernstein.core.quality_gates._run_command", return_value=(True, "ok")),
    ):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.changed_files == ["src/fallback.py"]


def test_timeout_is_warning_only(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with patch("bernstein.core.quality_gates._run_command", return_value=(False, "Timed out after 30s")):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.overall_pass
    assert report.results[0].status == "timeout"
    assert not report.results[0].blocked


def test_non_required_fail_does_not_block(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=False, condition="always")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task()

    with patch("bernstein.core.quality_gates._run_command", return_value=(False, "lint failed")):
        report = asyncio.run(runner.run_all(task, tmp_path))

    assert report.overall_pass
    assert report.results[0].status == "fail"
    assert not report.results[0].blocked


def test_cache_hit_and_invalidation_by_content_hash(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = src / "cache_me.py"
    target.write_text("print('one')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        cache_enabled=True,
    )
    task = _make_task(owned_files=["src/cache_me.py"])

    run_count = 0

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal run_count
        run_count += 1
        return True, "ok"

    with patch("bernstein.core.quality_gates._run_command", side_effect=fake_run):
        report_one = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))
        report_two = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))
        target.write_text("print('two')\n", encoding="utf-8")
        report_three = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))

    assert run_count == 2
    assert not report_one.results[0].cached
    assert report_two.results[0].cached
    assert not report_three.results[0].cached


def test_timeout_and_bypass_are_not_cached(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    target = src / "skip_me.py"
    target.write_text("print('skip')\n", encoding="utf-8")
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="python_changed")],
        allow_bypass=True,
        cache_enabled=True,
    )
    task = _make_task(owned_files=["src/skip_me.py"])

    timeout_count = 0

    def fake_timeout(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal timeout_count
        timeout_count += 1
        return False, "Timed out after 5s"

    with patch("bernstein.core.quality_gates._run_command", side_effect=fake_timeout):
        asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))
        asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))

    assert timeout_count == 2

    command_count = 0

    def fake_run(_command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
        nonlocal command_count
        command_count += 1
        return True, "ok"

    with patch("bernstein.core.quality_gates._run_command", side_effect=fake_run):
        asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path, skip_gates=["lint"], bypass_reason="manual"))
        report = asyncio.run(GateRunner(config, tmp_path).run_all(task, tmp_path))

    assert command_count == 1
    assert not report.results[0].cached


def test_bypass_denied_when_disabled(tmp_path: Path) -> None:
    config = QualityGatesConfig(
        pipeline=[GatePipelineStep(name="lint", required=True, condition="always")],
        allow_bypass=False,
    )
    runner = GateRunner(config, tmp_path)

    with pytest.raises(ValueError, match="bypass is disabled"):
        asyncio.run(runner.run_all(_make_task(), tmp_path, skip_gates=["lint"]))


# ---------------------------------------------------------------------------
# Auto-format gate
# ---------------------------------------------------------------------------


def _make_auto_format_runner(tmp_path: Path, *, python_cmd: str = "ruff format") -> GateRunner:
    config = QualityGatesConfig(
        auto_format=True,
        auto_format_python_command=python_cmd,
        pipeline=[GatePipelineStep(name="auto_format", required=False, condition="any_changed")],
        cache_enabled=False,
    )
    return GateRunner(config, tmp_path)


def test_auto_format_passes_and_reports_reformatted_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """auto_format gate always passes and reports how many files were reformatted."""
    import subprocess

    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    runner = _make_auto_format_runner(tmp_path)
    task = _make_task(owned_files=["a.py"])

    fake_proc = subprocess.CompletedProcess(
        args=["ruff", "format", "a.py"],
        returncode=0,
        stdout="1 file reformatted",
        stderr="",
    )

    with patch("subprocess.run", return_value=fake_proc):
        report = asyncio.run(runner.run_all(task, tmp_path))

    result = report.results[0]
    assert result.name == "auto_format"
    assert result.status == "pass"
    assert result.blocked is False
    assert "Python" in result.details
    assert "reformatted" in result.details


def test_auto_format_skips_when_formatter_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """auto_format skips a language when its formatter binary is not on PATH."""
    import shutil

    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    runner = _make_auto_format_runner(tmp_path, python_cmd="nonexistent-fmt")
    task = _make_task(owned_files=["a.py"])

    original_which = shutil.which

    def fake_which(name: str) -> str | None:
        if name == "nonexistent-fmt":
            return None
        return original_which(name)

    monkeypatch.setattr(shutil, "which", fake_which)

    report = asyncio.run(runner.run_all(task, tmp_path))

    result = report.results[0]
    assert result.status == "pass"
    assert result.blocked is False
    assert "not found" in result.details


def test_auto_format_skips_when_no_changed_files(tmp_path: Path) -> None:
    """auto_format gate skips cleanly when no changed files are present."""
    config = QualityGatesConfig(
        auto_format=True,
        pipeline=[GatePipelineStep(name="auto_format", required=False, condition="any_changed")],
        cache_enabled=False,
    )
    runner = GateRunner(config, tmp_path)
    task = _make_task(owned_files=[])

    report = asyncio.run(runner.run_all(task, tmp_path))

    result = report.results[0]
    assert result.status == "skipped"
    assert result.blocked is False


def test_auto_format_appears_before_lint_in_default_pipeline(tmp_path: Path) -> None:
    """auto_format is inserted before lint in the default pipeline."""
    from bernstein.core.gate_runner import build_default_pipeline

    config = QualityGatesConfig(auto_format=True, lint=True)
    pipeline = build_default_pipeline(config)
    names = [step.name for step in pipeline]
    assert "auto_format" in names
    assert "lint" in names
    assert names.index("auto_format") < names.index("lint")
