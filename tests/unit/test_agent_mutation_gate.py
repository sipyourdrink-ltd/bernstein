"""ROAD-170: Agent-written test mutation verification.

Tests for the quality gate that verifies agent-produced tests actually catch
bugs by running targeted mutation testing.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------


class TestExtractAgentTestFiles:
    """Test _extract_agent_test_files() diff parsing."""

    def test_detects_tests_unit_file(self) -> None:
        from bernstein.core.quality_gates import _extract_agent_test_files

        diff = (
            "diff --git a/tests/unit/test_foo.py b/tests/unit/test_foo.py\n"
            "--- a/tests/unit/test_foo.py\n"
            "+++ b/tests/unit/test_foo.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+import foo\n"
        )
        files = _extract_agent_test_files(diff)
        assert "tests/unit/test_foo.py" in files

    def test_detects_nested_tests_directory(self) -> None:
        from bernstein.core.quality_gates import _extract_agent_test_files

        diff = (
            "diff --git a/bernstein/tests/test_bar.py b/bernstein/tests/test_bar.py\n"
            "--- /dev/null\n"
            "+++ b/bernstein/tests/test_bar.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def test_bar(): pass\n"
        )
        files = _extract_agent_test_files(diff)
        assert "bernstein/tests/test_bar.py" in files

    def test_ignores_non_test_files(self) -> None:
        from bernstein.core.quality_gates import _extract_agent_test_files

        diff = (
            "diff --git a/src/bernstein/core/foo.py b/src/bernstein/core/foo.py\n"
            "--- a/src/bernstein/core/foo.py\n"
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def foo(): pass\n"
        )
        files = _extract_agent_test_files(diff)
        assert files == []

    def test_empty_diff_returns_empty(self) -> None:
        from bernstein.core.quality_gates import _extract_agent_test_files

        assert _extract_agent_test_files("") == []

    def test_multiple_test_files(self) -> None:
        from bernstein.core.quality_gates import _extract_agent_test_files

        diff = (
            "+++ b/tests/unit/test_alpha.py\n"
            "@@ -0,0 +1 @@\n"
            "+pass\n"
            "+++ b/tests/unit/test_beta.py\n"
            "@@ -0,0 +1 @@\n"
            "+pass\n"
        )
        files = _extract_agent_test_files(diff)
        assert "tests/unit/test_alpha.py" in files
        assert "tests/unit/test_beta.py" in files


# ---------------------------------------------------------------------------
# Source file inference
# ---------------------------------------------------------------------------


class TestInferSourceFiles:
    """Test _infer_source_files() path-to-source mapping."""

    def test_finds_existing_source_file(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import _infer_source_files

        # Create a fake source file structure
        src = tmp_path / "src" / "bernstein" / "core"
        src.mkdir(parents=True)
        (src / "guardrails.py").write_text("# source")

        test_files = ["tests/unit/test_guardrails.py"]
        result = _infer_source_files(test_files, tmp_path)
        assert any("guardrails.py" in r for r in result)

    def test_returns_empty_when_no_match(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import _infer_source_files

        test_files = ["tests/unit/test_nonexistent_module.py"]
        result = _infer_source_files(test_files, tmp_path)
        assert result == []

    def test_deduplicates_results(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import _infer_source_files

        src = tmp_path / "src" / "bernstein" / "core"
        src.mkdir(parents=True)
        (src / "lifecycle.py").write_text("# source")

        # Two test files both mapping to lifecycle.py
        test_files = ["tests/unit/test_lifecycle.py", "tests/integration/test_lifecycle.py"]
        result = _infer_source_files(test_files, tmp_path)
        lifecycle_matches = [r for r in result if "lifecycle.py" in r]
        assert len(lifecycle_matches) == 1  # deduplicated

    def test_test_file_without_matching_name_pattern(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import _infer_source_files

        # "test_" prefix is required — "integration_test.py" won't match
        test_files = ["tests/integration_suite.py"]
        result = _infer_source_files(test_files, tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# Mutation command builder
# ---------------------------------------------------------------------------


class TestBuildAgentMutationCommand:
    """Test _build_agent_mutation_command() command construction."""

    def test_includes_source_and_test_paths(self) -> None:
        from bernstein.core.quality_gates import _build_agent_mutation_command

        cmd = _build_agent_mutation_command(
            source_files=["src/bernstein/core/foo.py"],
            test_files=["tests/unit/test_foo.py"],
        )
        assert "src/bernstein/core/foo.py" in cmd
        assert "tests/unit/test_foo.py" in cmd
        assert "mutmut" in cmd

    def test_multiple_files_space_separated(self) -> None:
        from bernstein.core.quality_gates import _build_agent_mutation_command

        cmd = _build_agent_mutation_command(
            source_files=["src/a.py", "src/b.py"],
            test_files=["tests/test_a.py", "tests/test_b.py"],
        )
        assert "src/a.py src/b.py" in cmd
        assert "tests/test_a.py tests/test_b.py" in cmd


# ---------------------------------------------------------------------------
# Gate integration — no-op paths (no subprocess required)
# ---------------------------------------------------------------------------


class TestRunAgentTestMutationGateSync:
    """Test run_agent_test_mutation_gate_sync() high-level integration."""

    def _make_config(self, threshold: float = 0.70, timeout: int = 5) -> object:
        from bernstein.core.quality_gates import QualityGatesConfig

        return QualityGatesConfig(
            agent_test_mutation=True,
            agent_test_mutation_threshold=threshold,
            agent_test_mutation_timeout_s=timeout,
        )

    def _make_task(self, owned_files: list[str] | None = None) -> object:
        from bernstein.core.models import Task

        return Task(id="t1", title="test", description="d", role="qa", owned_files=owned_files or [])

    def test_returns_pass_when_no_test_files_in_diff(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import run_agent_test_mutation_gate_sync

        config = self._make_config()
        task = self._make_task()
        # Patch _get_intent_diff so no subprocess is needed
        diff = "--- a/src/foo.py\n+++ b/src/foo.py\n+def foo(): pass\n"
        import bernstein.core.quality_gates as qg

        original = qg._get_intent_diff
        try:
            qg._get_intent_diff = lambda *_: diff  # type: ignore[attr-defined]
            passed, detail, score = run_agent_test_mutation_gate_sync(config, task, tmp_path)
        finally:
            qg._get_intent_diff = original

        assert passed is True
        assert score is None
        assert "No agent-written test files" in detail

    def test_returns_pass_when_no_source_inferred(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import run_agent_test_mutation_gate_sync

        config = self._make_config()
        task = self._make_task()
        diff = "+++ b/tests/unit/test_very_obscure_module.py\n@@ -0,0 +1 @@\n+pass\n"
        import bernstein.core.quality_gates as qg

        original = qg._get_intent_diff
        try:
            qg._get_intent_diff = lambda *_: diff  # type: ignore[attr-defined]
            passed, detail, score = run_agent_test_mutation_gate_sync(config, task, tmp_path)
        finally:
            qg._get_intent_diff = original

        assert passed is True
        assert score is None
        assert "Could not infer source files" in detail or "No agent-written test files" in detail

    def test_config_fields_exist(self) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig

        config = QualityGatesConfig()
        assert hasattr(config, "agent_test_mutation")
        assert hasattr(config, "agent_test_mutation_threshold")
        assert hasattr(config, "agent_test_mutation_timeout_s")
        assert config.agent_test_mutation is False
        assert 0.0 < config.agent_test_mutation_threshold <= 1.0
        assert config.agent_test_mutation_timeout_s > 0

    def test_gate_defaults_disabled(self) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig

        config = QualityGatesConfig()
        assert config.agent_test_mutation is False, "Gate must be opt-in"


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------


class TestAgentMutationPipelineStep:
    """Verify the agent_test_mutation step is wired into the gate pipeline."""

    def test_step_added_when_enabled(self) -> None:
        from bernstein.core.gate_runner import build_default_pipeline
        from bernstein.core.quality_gates import QualityGatesConfig

        config = QualityGatesConfig(agent_test_mutation=True, lint=False)
        pipeline = build_default_pipeline(config)
        names = [s.name for s in pipeline]
        assert "agent_test_mutation" in names

    def test_step_absent_when_disabled(self) -> None:
        from bernstein.core.gate_runner import build_default_pipeline
        from bernstein.core.quality_gates import QualityGatesConfig

        config = QualityGatesConfig(agent_test_mutation=False, lint=False)
        pipeline = build_default_pipeline(config)
        names = [s.name for s in pipeline]
        assert "agent_test_mutation" not in names

    def test_step_condition_is_tests_changed(self) -> None:
        from bernstein.core.gate_runner import build_default_pipeline
        from bernstein.core.quality_gates import QualityGatesConfig

        config = QualityGatesConfig(agent_test_mutation=True, lint=False)
        pipeline = build_default_pipeline(config)
        step = next(s for s in pipeline if s.name == "agent_test_mutation")
        assert step.condition == "tests_changed"

    def test_step_is_required(self) -> None:
        from bernstein.core.gate_runner import build_default_pipeline
        from bernstein.core.quality_gates import QualityGatesConfig

        config = QualityGatesConfig(agent_test_mutation=True, lint=False)
        pipeline = build_default_pipeline(config)
        step = next(s for s in pipeline if s.name == "agent_test_mutation")
        assert step.required is True
