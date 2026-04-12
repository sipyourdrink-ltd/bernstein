"""Tests for the SynthesisEngine and related helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.knowledge.synthesis import (
    SynthesisEngine,
    SynthesisResult,
    should_synthesize,
)


# --- SynthesisResult tests ---


class TestSynthesisResult:
    """Tests for SynthesisResult dataclass."""

    def test_fields(self) -> None:
        result = SynthesisResult(
            synthesized_summary="All workers done",
            worker_count=3,
            successful_workers=2,
            failed_workers=1,
            artifacts_merged=["a.py", "b.py"],
            conflicts_detected=["conflict A"],
        )
        assert result.worker_count == 3
        assert result.successful_workers == 2
        assert result.failed_workers == 1
        assert len(result.artifacts_merged) == 2
        assert len(result.conflicts_detected) == 1


# --- SynthesisEngine tests ---


class TestSynthesisEngine:
    """Tests for SynthesisEngine."""

    def test_synthesize_empty_results(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        result = engine.synthesize([], parent_task_id="T-1")
        assert result.worker_count == 0
        assert result.successful_workers == 0
        assert result.failed_workers == 0
        assert "No worker results" in result.synthesized_summary

    def test_synthesize_successful_workers(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        workers = [
            {"status": "completed", "result_summary": "Built auth module", "worker_id": "W-1", "subtask_id": "S-1"},
            {"status": "completed", "result_summary": "Added tests", "worker_id": "W-2", "subtask_id": "S-2"},
        ]
        result = engine.synthesize(workers, parent_task_id="T-1")
        assert result.worker_count == 2
        assert result.successful_workers == 2
        assert result.failed_workers == 0
        assert "W-1" in result.synthesized_summary
        assert "W-2" in result.synthesized_summary

    def test_synthesize_with_failures(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        workers = [
            {"status": "completed", "result_summary": "Done"},
            {"status": "failed", "error": "Timeout"},
        ]
        result = engine.synthesize(workers, parent_task_id="T-1")
        assert result.successful_workers == 1
        assert result.failed_workers == 1

    def test_synthesize_collects_artifacts(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        workers = [
            {"status": "completed", "result_summary": "Done", "artifacts": ["a.py", "b.py"]},
            {"status": "completed", "result_summary": "Done", "artifacts": ["c.py"]},
        ]
        result = engine.synthesize(workers, parent_task_id="T-1")
        assert set(result.artifacts_merged) == {"a.py", "b.py", "c.py"}

    def test_detect_conflicts(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        workers = [
            {"status": "completed", "result_summary": "All tests pass successfully"},
            {"status": "completed", "result_summary": "Found critical error in auth module"},
        ]
        result = engine.synthesize(workers, parent_task_id="T-1")
        assert len(result.conflicts_detected) >= 1

    def test_no_conflicts_when_consistent(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        workers = [
            {"status": "completed", "result_summary": "Module A implemented"},
            {"status": "completed", "result_summary": "Module B implemented"},
        ]
        result = engine.synthesize(workers, parent_task_id="T-1")
        assert result.conflicts_detected == []

    def test_save_synthesis_result(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        result = SynthesisResult(
            synthesized_summary="Summary here",
            worker_count=2,
            successful_workers=2,
            failed_workers=0,
            artifacts_merged=["file.py"],
            conflicts_detected=[],
        )
        path = engine.save_synthesis_result(result, parent_task_id="T-42")
        assert path.exists()
        content = path.read_text()
        assert "Summary here" in content
        assert "T-42" in content
        assert "file.py" in content

    def test_save_synthesis_result_custom_path(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path)
        result = SynthesisResult(
            synthesized_summary="Custom",
            worker_count=1,
            successful_workers=1,
            failed_workers=0,
            artifacts_merged=[],
            conflicts_detected=[],
        )
        custom = tmp_path / "custom_output.md"
        path = engine.save_synthesis_result(result, "T-1", output_path=custom)
        assert path == custom
        assert custom.exists()

    def test_llm_mode_falls_back(self, tmp_path: Path) -> None:
        engine = SynthesisEngine(tmp_path, use_llm=True)
        workers = [
            {"status": "completed", "result_summary": "Done"},
        ]
        result = engine.synthesize(workers, parent_task_id="T-1")
        # LLM mode falls back to deterministic currently
        assert result.synthesized_summary != ""


# --- should_synthesize tests ---


class TestShouldSynthesize:
    """Tests for should_synthesize()."""

    def test_coordinator_mode_multiple_workers(self) -> None:
        assert should_synthesize(worker_count=3, coordinator_mode=True, explicit_flag=False) is True

    def test_coordinator_mode_single_worker(self) -> None:
        assert should_synthesize(worker_count=1, coordinator_mode=True, explicit_flag=False) is False

    def test_explicit_flag(self) -> None:
        assert should_synthesize(worker_count=1, coordinator_mode=False, explicit_flag=True) is True

    def test_no_coordinator_no_explicit(self) -> None:
        assert should_synthesize(worker_count=5, coordinator_mode=False, explicit_flag=False) is False

    def test_zero_workers_coordinator_mode(self) -> None:
        assert should_synthesize(worker_count=0, coordinator_mode=True, explicit_flag=False) is False
