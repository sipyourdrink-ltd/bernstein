"""Tests for the adapter conformance harness.

Covers golden-transcript loading, per-step replay, regression detection,
and the scaffold generator.  One test intentionally injects a broken adapter
to prove the harness catches regressions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.conformance import (
    ConformanceHarness,
    ConformanceReport,
    GoldenTranscript,
    StepResult,
    TranscriptResult,
    TranscriptStep,
    _load_adapter,
    _popen_target_for,
    generate_adapter_scaffold,
    load_golden_transcripts,
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _write_transcript_yaml(directory: Path, filename: str, data: dict[str, Any]) -> Path:
    path = directory / filename
    path.write_text(yaml.safe_dump(data))
    return path


def _write_transcript_json(directory: Path, filename: str, data: dict[str, Any]) -> Path:
    path = directory / filename
    path.write_text(json.dumps(data))
    return path


def _minimal_transcript_dict(
    name: str = "test",
    adapter_class: str = "bernstein.adapters.codex.CodexAdapter",
) -> dict[str, Any]:
    return {
        "name": name,
        "adapter_class": adapter_class,
        "steps": [{"prompt": "do it", "model": "sonnet"}],
    }


def _make_popen(pid: int = 99) -> MagicMock:
    m = MagicMock()
    m.pid = pid
    m.stdout = MagicMock()
    return m


# ---------------------------------------------------------------------------
# TranscriptStep.from_dict
# ---------------------------------------------------------------------------


def test_transcript_step_defaults() -> None:
    step = TranscriptStep.from_dict({})
    assert step.prompt == "do the thing"
    assert step.model == "sonnet"
    assert step.expected_pid is None
    assert step.expect_exception is None


def test_transcript_step_parses_all_fields() -> None:
    step = TranscriptStep.from_dict(
        {
            "prompt": "fix bug",
            "model": "opus",
            "expected_pid": 42,
            "expect_exception": "SpawnError",
            "expected_log_suffix": ".log",
        }
    )
    assert step.prompt == "fix bug"
    assert step.model == "opus"
    assert step.expected_pid == 42
    assert step.expect_exception == "SpawnError"
    assert step.expected_log_suffix == ".log"


# ---------------------------------------------------------------------------
# GoldenTranscript.from_dict
# ---------------------------------------------------------------------------


def test_golden_transcript_from_dict() -> None:
    raw = {
        "name": "my-transcript",
        "adapter_class": "bernstein.adapters.codex.CodexAdapter",
        "steps": [{"prompt": "hi"}],
    }
    t = GoldenTranscript.from_dict(raw)
    assert t.name == "my-transcript"
    assert t.adapter_class == "bernstein.adapters.codex.CodexAdapter"
    assert len(t.steps) == 1


def test_golden_transcript_empty_steps() -> None:
    raw = {"name": "empty", "adapter_class": "bernstein.adapters.codex.CodexAdapter", "steps": []}
    t = GoldenTranscript.from_dict(raw)
    assert t.steps == []


# ---------------------------------------------------------------------------
# load_golden_transcripts
# ---------------------------------------------------------------------------


def test_load_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    result = load_golden_transcripts(tmp_path / "does_not_exist")
    assert result == []


def test_load_returns_empty_for_empty_dir(tmp_path: Path) -> None:
    result = load_golden_transcripts(tmp_path)
    assert result == []


def test_load_parses_yaml_transcript(tmp_path: Path) -> None:
    _write_transcript_yaml(tmp_path, "t1.yaml", _minimal_transcript_dict("t1"))
    transcripts = load_golden_transcripts(tmp_path)
    assert len(transcripts) == 1
    assert transcripts[0].name == "t1"


def test_load_parses_json_transcript(tmp_path: Path) -> None:
    _write_transcript_json(tmp_path, "t2.json", _minimal_transcript_dict("t2"))
    transcripts = load_golden_transcripts(tmp_path)
    assert len(transcripts) == 1
    assert transcripts[0].name == "t2"


def test_load_skips_malformed_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: valid: yaml: [[[")
    result = load_golden_transcripts(tmp_path)
    assert result == []


def test_load_skips_yaml_missing_required_keys(tmp_path: Path) -> None:
    # missing 'adapter_class'
    _write_transcript_yaml(tmp_path, "bad.yaml", {"name": "x", "steps": []})
    result = load_golden_transcripts(tmp_path)
    assert result == []


def test_load_sorts_by_name(tmp_path: Path) -> None:
    for name in ("z-last", "a-first", "m-middle"):
        _write_transcript_yaml(tmp_path, f"{name}.yaml", _minimal_transcript_dict(name))
    transcripts = load_golden_transcripts(tmp_path)
    names = [t.name for t in transcripts]
    assert names == sorted(names)


def test_load_from_real_golden_dir() -> None:
    """The bundled golden transcripts must load without errors."""
    real_dir = Path(__file__).parent.parent / "golden"
    if not real_dir.exists():
        pytest.skip("tests/golden/ not found")
    transcripts = load_golden_transcripts(real_dir)
    assert len(transcripts) >= 1


# ---------------------------------------------------------------------------
# _load_adapter
# ---------------------------------------------------------------------------


def test_load_adapter_instantiates_codex() -> None:
    adapter = _load_adapter("bernstein.adapters.codex.CodexAdapter")
    assert isinstance(adapter, CLIAdapter)


def test_load_adapter_instantiates_generic_with_kwargs() -> None:
    adapter = _load_adapter("bernstein.adapters.generic.GenericAdapter", {"cli_command": "test-cmd"})
    assert isinstance(adapter, CLIAdapter)


def test_load_adapter_raises_for_bad_class_path() -> None:
    with pytest.raises(ImportError):
        _load_adapter("no_dots_here")


def test_load_adapter_raises_for_missing_module() -> None:
    with pytest.raises((ImportError, ModuleNotFoundError)):
        _load_adapter("bernstein.adapters.nonexistent.Foo")


# ---------------------------------------------------------------------------
# _popen_target_for
# ---------------------------------------------------------------------------


def test_popen_target_contains_module_path() -> None:
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    target = _popen_target_for(adapter)
    assert "bernstein.adapters.codex" in target
    assert "subprocess.Popen" in target


# ---------------------------------------------------------------------------
# ConformanceHarness.replay_step — happy path
# ---------------------------------------------------------------------------


def test_replay_step_passes_for_valid_spawn(tmp_path: Path) -> None:
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    step = TranscriptStep(prompt="test", model="o1-mini")
    harness = ConformanceHarness()

    popen_target = _popen_target_for(adapter)
    with patch(popen_target, side_effect=[_make_popen(42), _make_popen(43)]):
        result = harness.replay_step(adapter, step, 0, tmp_path)

    assert result.passed
    assert result.step_index == 0


def test_replay_step_passes_for_generic_adapter(tmp_path: Path) -> None:
    from bernstein.adapters.generic import GenericAdapter

    adapter = GenericAdapter(cli_command="echo")
    step = TranscriptStep(prompt="hello", model="sonnet")
    harness = ConformanceHarness()

    popen_target = _popen_target_for(adapter)
    with patch(popen_target, side_effect=[_make_popen(77)]):
        result = harness.replay_step(adapter, step, 0, tmp_path)

    assert result.passed


# ---------------------------------------------------------------------------
# Broken adapter stub — used to test exception detection
# ---------------------------------------------------------------------------


class _BrokenSpawnAdapter(CLIAdapter):
    """Stub adapter whose spawn always raises RuntimeError."""

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Any,
        model_config: Any,
        session_id: str,
        mcp_config: Any = None,
        timeout_seconds: int = 1800,
    ) -> SpawnResult:
        raise RuntimeError("broken adapter")

    def name(self) -> str:
        return "broken"


class _WrongExceptionAdapter(CLIAdapter):
    """Stub adapter whose spawn raises a ValueError instead of RuntimeError."""

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Any,
        model_config: Any,
        session_id: str,
        mcp_config: Any = None,
        timeout_seconds: int = 1800,
    ) -> SpawnResult:
        raise ValueError("wrong type")

    def name(self) -> str:
        return "wrong-exc"


# ---------------------------------------------------------------------------
# ConformanceHarness.replay_step — exception handling
# ---------------------------------------------------------------------------


def test_replay_step_fails_when_unexpected_exception(tmp_path: Path) -> None:
    adapter = _BrokenSpawnAdapter()
    step = TranscriptStep(prompt="bad", model="o1")
    harness = ConformanceHarness()
    result = harness.replay_step(adapter, step, 0, tmp_path)
    assert not result.passed
    assert "RuntimeError" in result.message


def test_replay_step_passes_when_expected_exception_raised(tmp_path: Path) -> None:
    adapter = _BrokenSpawnAdapter()
    step = TranscriptStep(prompt="bad", model="o1", expect_exception="RuntimeError")
    harness = ConformanceHarness()
    result = harness.replay_step(adapter, step, 0, tmp_path)
    assert result.passed


def test_replay_step_fails_when_wrong_exception_raised(tmp_path: Path) -> None:
    adapter = _WrongExceptionAdapter()
    step = TranscriptStep(prompt="bad", model="o1", expect_exception="RuntimeError")
    harness = ConformanceHarness()
    result = harness.replay_step(adapter, step, 0, tmp_path)
    assert not result.passed
    assert "RuntimeError" in result.message


def test_replay_step_fails_when_exception_expected_but_not_raised(tmp_path: Path) -> None:
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    step = TranscriptStep(prompt="ok", model="o1", expect_exception="RuntimeError")
    harness = ConformanceHarness()

    popen_target = _popen_target_for(adapter)
    with patch(popen_target, side_effect=[_make_popen(42), _make_popen(43)]):
        result = harness.replay_step(adapter, step, 0, tmp_path)

    assert not result.passed
    assert "Expected RuntimeError" in result.message


# ---------------------------------------------------------------------------
# ConformanceHarness.replay_transcript
# ---------------------------------------------------------------------------


def test_replay_transcript_passes_for_real_adapter(tmp_path: Path) -> None:
    transcript = GoldenTranscript(
        name="codex-basic",
        adapter_class="bernstein.adapters.codex.CodexAdapter",
        steps=[TranscriptStep(prompt="do it", model="o1-mini")],
    )
    harness = ConformanceHarness()

    popen_target = "bernstein.adapters.codex.subprocess.Popen"
    with patch(popen_target, side_effect=[_make_popen(55), _make_popen(56)]):
        result = harness.replay_transcript(transcript, workdir=tmp_path)

    assert result.passed
    assert len(result.step_results) == 1


def test_replay_transcript_fails_for_bad_adapter_class(tmp_path: Path) -> None:
    transcript = GoldenTranscript(
        name="bad-class",
        adapter_class="bernstein.adapters.nonexistent.NoSuchAdapter",
        steps=[TranscriptStep()],
    )
    harness = ConformanceHarness()
    result = harness.replay_transcript(transcript, workdir=tmp_path)
    assert not result.passed
    assert "Failed to instantiate" in result.step_results[0].message


def test_replay_transcript_empty_steps_passes(tmp_path: Path) -> None:
    transcript = GoldenTranscript(
        name="empty",
        adapter_class="bernstein.adapters.codex.CodexAdapter",
        steps=[],
    )
    harness = ConformanceHarness()
    result = harness.replay_transcript(transcript, workdir=tmp_path)
    assert result.passed
    assert result.step_results == []


def test_transcript_result_passed_only_when_all_steps_pass() -> None:
    tr = TranscriptResult(transcript_name="x", adapter_class="y")
    tr.step_results = [
        StepResult(step_index=0, passed=True, message="ok"),
        StepResult(step_index=1, passed=False, message="fail"),
    ]
    assert not tr.passed


def test_transcript_result_passed_when_all_pass() -> None:
    tr = TranscriptResult(transcript_name="x", adapter_class="y")
    tr.step_results = [StepResult(step_index=0, passed=True, message="ok")]
    assert tr.passed


def test_transcript_result_serializes() -> None:
    tr = TranscriptResult(transcript_name="x", adapter_class="y")
    tr.step_results = [StepResult(step_index=0, passed=True, message="ok")]
    d = tr.to_dict()
    assert d["transcript_name"] == "x"
    assert d["passed"] is True
    assert len(d["steps"]) == 1


# ---------------------------------------------------------------------------
# ConformanceHarness.run_all
# ---------------------------------------------------------------------------


def test_run_all_empty_transcripts() -> None:
    harness = ConformanceHarness()
    report = harness.run_all([])
    assert report.passed
    assert report.regressions == []


def test_run_all_with_passing_transcript(tmp_path: Path) -> None:
    transcript = GoldenTranscript(
        name="pass-test",
        adapter_class="bernstein.adapters.codex.CodexAdapter",
        steps=[TranscriptStep(prompt="go", model="o1-mini")],
    )
    harness = ConformanceHarness()
    popen_target = "bernstein.adapters.codex.subprocess.Popen"
    with patch(popen_target, side_effect=[_make_popen(10), _make_popen(11)]):
        report = harness.run_all([transcript], workdir=tmp_path)
    assert report.passed
    assert report.regressions == []


def test_run_all_detects_regression_when_adapter_fails(tmp_path: Path) -> None:
    """Prove the harness catches a broken adapter (key regression test).

    Uses a non-existent adapter class path, which fails instantiation and
    therefore produces a conformance failure the harness must detect.
    """
    transcript = GoldenTranscript(
        name="broken-adapter",
        adapter_class="bernstein.adapters.nonexistent.BrokenAdapter",
        steps=[TranscriptStep(prompt="should work", model="o1")],
    )
    harness = ConformanceHarness()
    report = harness.run_all([transcript], workdir=tmp_path)
    assert not report.passed
    assert "broken-adapter" in report.regressions


def test_run_all_regressions_property_lists_failures(tmp_path: Path) -> None:
    harness = ConformanceHarness()
    transcripts = [
        GoldenTranscript(
            name="failing",
            adapter_class="bernstein.adapters.nonexistent.Boom",
            steps=[TranscriptStep()],
        ),
        GoldenTranscript(
            name="passing",
            adapter_class="bernstein.adapters.codex.CodexAdapter",
            steps=[],
        ),
    ]
    with patch("bernstein.adapters.codex.subprocess.Popen", side_effect=[_make_popen(1)]):
        report = harness.run_all(transcripts, workdir=tmp_path)
    assert "failing" in report.regressions
    assert "passing" not in report.regressions


def test_conformance_report_to_dict() -> None:
    report = ConformanceReport()
    d = report.to_dict()
    assert d["passed"] is True
    assert d["regressions"] == []
    assert d["results"] == []


# ---------------------------------------------------------------------------
# Real golden transcripts integration test
# ---------------------------------------------------------------------------


def test_golden_transcripts_all_pass(tmp_path: Path) -> None:
    """All bundled golden transcripts must pass conformance."""
    real_dir = Path(__file__).parent.parent / "golden"
    if not real_dir.exists():
        pytest.skip("tests/golden/ not found")

    transcripts = load_golden_transcripts(real_dir)
    if not transcripts:
        pytest.skip("No golden transcripts found")

    harness = ConformanceHarness()
    # Patch subprocess at the base level for all adapters
    with patch("bernstein.adapters.codex.subprocess.Popen", side_effect=[_make_popen(i) for i in range(100)]):
        with patch("bernstein.adapters.generic.subprocess.Popen", side_effect=[_make_popen(i) for i in range(100)]):
            report = harness.run_all(transcripts, workdir=tmp_path)

    failed = report.regressions
    assert not failed, f"Golden transcript conformance FAILED: {failed}"


# ---------------------------------------------------------------------------
# generate_adapter_scaffold
# ---------------------------------------------------------------------------


def test_scaffold_contains_class_name() -> None:
    code = generate_adapter_scaffold("MyAgent", "MyAgentAdapter", "myagent", "myagent")
    assert "class MyAgentAdapter" in code


def test_scaffold_contains_name_method() -> None:
    code = generate_adapter_scaffold("MyAgent", "MyAgentAdapter", "myagent", "myagent")
    assert "def name(" in code
    assert "myagent" in code


def test_scaffold_contains_spawn_method() -> None:
    code = generate_adapter_scaffold("MyAgent", "MyAgentAdapter", "myagent", "myagent")
    assert "def spawn(" in code


def test_scaffold_is_valid_python_syntax() -> None:
    import ast

    code = generate_adapter_scaffold("TestCLI", "TestCLIAdapter", "testcli", "testcli")
    # Should parse without SyntaxError
    ast.parse(code)


def test_scaffold_subclasses_cli_adapter() -> None:
    code = generate_adapter_scaffold("Foo", "FooAdapter", "foo", "foo-cli")
    assert "CLIAdapter" in code


def test_scaffold_includes_cli_command() -> None:
    code = generate_adapter_scaffold("Bar", "BarAdapter", "bar", "bar-runner")
    assert "bar-runner" in code
