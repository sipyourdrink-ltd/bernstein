"""Tests for SandboxValidator."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.evolution.sandbox import SandboxValidator
from bernstein.evolution.types import RiskLevel, SandboxResult, UpgradeProposal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    id: str = "UPG-001",
    risk_level: RiskLevel = RiskLevel.L1_TEMPLATE,
    diff: str = "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
    target_files: list[str] | None = None,
    confidence: float = 0.9,
) -> UpgradeProposal:
    return UpgradeProposal(
        id=id,
        title="Test proposal",
        description="A sandbox test",
        risk_level=risk_level,
        target_files=target_files or ["templates/roles/backend.md"],
        diff=diff,
        rationale="Testing",
        expected_impact="Improvement",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# L0 validation (no worktree)
# ---------------------------------------------------------------------------


class TestL0Validation:
    def test_l0_with_valid_diff_passes(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(
            risk_level=RiskLevel.L0_CONFIG,
            diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        )
        result = validator.create_sandbox(proposal)

        assert result.passed
        assert result.tests_total == 1
        assert result.error is None

    def test_l0_empty_diff_fails(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L0_CONFIG, diff="")
        result = validator.create_sandbox(proposal)

        assert not result.passed
        assert result.error is not None
        assert "Empty diff" in result.error

    def test_l0_result_has_correct_proposal_id(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(id="L0-XYZ", risk_level=RiskLevel.L0_CONFIG)
        result = validator.create_sandbox(proposal)
        assert result.proposal_id == "L0-XYZ"


# ---------------------------------------------------------------------------
# L3 validation (blocked)
# ---------------------------------------------------------------------------


class TestL3Blocked:
    def test_l3_always_fails(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L3_STRUCTURAL)
        result = validator.create_sandbox(proposal)

        assert not result.passed
        assert result.error is not None
        assert "L3_STRUCTURAL" in result.error

    def test_l3_does_not_call_subprocess(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L3_STRUCTURAL)
        with patch("subprocess.run") as mock_run:
            validator.create_sandbox(proposal)
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Worktree-based validation (L1/L2) — subprocess mocked
# ---------------------------------------------------------------------------


def _mock_run_success(passed: int = 5, failed: int = 0) -> MagicMock:
    """Return a mock subprocess.run that simulates successful git + pytest."""
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        mock.returncode = 0

        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "worktree" in cmd:
            # git worktree add / remove / branch -D
            mock.stderr = ""
            mock.stdout = ""
        elif isinstance(cmd, str) and "pytest" in cmd:
            # test run
            summary = f"{passed} passed"
            if failed:
                summary = f"{passed} passed, {failed} failed"
            mock.stdout = f"collected {passed + failed} items\n{summary} in 1.23s\n"
            mock.stderr = ""
        else:
            mock.stderr = ""
            mock.stdout = ""
        return mock

    m = MagicMock(side_effect=side_effect)
    return m


class TestWorktreeValidation:
    def test_l1_passes_when_tests_pass(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path, test_command="uv run pytest tests/ -x -q")
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        with patch("subprocess.run", side_effect=_mock_run_success(5, 0).side_effect):
            result = validator.create_sandbox(proposal)

        assert result.passed
        assert result.tests_passed == 5
        assert result.tests_failed == 0
        assert result.proposal_id == "UPG-001"

    def test_l2_passes_when_tests_pass(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L2_LOGIC)

        with patch("subprocess.run", side_effect=_mock_run_success(10, 0).side_effect):
            result = validator.create_sandbox(proposal)

        assert result.passed
        assert result.tests_passed == 10

    def test_fails_when_tests_fail(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        with patch("subprocess.run", side_effect=_mock_run_success(3, 2).side_effect):
            result = validator.create_sandbox(proposal)

        assert not result.passed
        assert result.tests_failed == 2

    def test_fails_when_worktree_creation_fails(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        def fail_worktree(*args, **kwargs):
            cmd = args[0] if args else []
            m = MagicMock()
            if isinstance(cmd, list) and "worktree" in cmd and "add" in cmd:
                m.returncode = 1
                m.stderr = "branch already exists"
            else:
                m.returncode = 0
                m.stderr = ""
                m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=fail_worktree):
            result = validator.create_sandbox(proposal)

        assert not result.passed
        assert result.error is not None
        assert "git worktree add failed" in result.error

    def test_timeout_returns_error_result(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        def timeout_on_tests(*args, **kwargs):
            cmd = args[0] if args else []
            if isinstance(cmd, str) and "pytest" in cmd:
                raise subprocess.TimeoutExpired(cmd, 300)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=timeout_on_tests):
            result = validator.create_sandbox(proposal)

        assert not result.passed
        assert result.error is not None
        assert "timed out" in result.error

    def test_delta_is_negative_when_tests_fail(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L2_LOGIC)

        with patch("subprocess.run", side_effect=_mock_run_success(0, 5).side_effect):
            result = validator.create_sandbox(proposal)

        assert result.delta < 0

    def test_worktree_cleanup_called_on_success(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        calls: list[list[str]] = []

        def tracking_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list):
                calls.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = "5 passed in 1.0s\n"
            return m

        with patch("subprocess.run", side_effect=tracking_run):
            result = validator.create_sandbox(proposal)

        assert result.passed
        remove_calls = [c for c in calls if "worktree" in c and "remove" in c]
        assert remove_calls, "worktree remove should be called during cleanup"

    def test_worktree_cleanup_called_on_test_failure(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        calls: list[list[str]] = []

        def tracking_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list):
                calls.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = "0 passed, 3 failed in 1.0s\n"
            return m

        with patch("subprocess.run", side_effect=tracking_run):
            result = validator.create_sandbox(proposal)

        assert not result.passed
        remove_calls = [c for c in calls if "worktree" in c and "remove" in c]
        assert remove_calls, "worktree remove should be called even when tests fail"

    def test_diff_apply_failure_returns_error(self, tmp_path: Path) -> None:
        """Proposal diff targets files not present in the worktree — git apply fails."""
        validator = SandboxValidator(tmp_path)
        proposal = _make_proposal(
            risk_level=RiskLevel.L1_TEMPLATE,
            diff="--- a/nonexistent_file.py\n+++ b/nonexistent_file.py\n@@ -1 +1 @@\n-old\n+new\n",
        )

        def fail_on_apply(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            m = MagicMock()
            if isinstance(cmd, list) and "apply" in cmd:
                m.returncode = 1
                m.stderr = "error: nonexistent_file.py: No such file or directory"
                m.stdout = ""
            else:
                m.returncode = 0
                m.stderr = ""
                m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=fail_on_apply):
            result = validator.create_sandbox(proposal)

        assert not result.passed
        assert result.error is not None

    def test_l1_uses_unit_tests_only(self, tmp_path: Path) -> None:
        """L1 validation runs unit tests only, not the full suite."""
        validator = SandboxValidator(tmp_path, test_command="uv run pytest tests/ -x -q")
        proposal = _make_proposal(risk_level=RiskLevel.L1_TEMPLATE)

        test_commands_used: list[str] = []

        def capture_test_cmd(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, str):
                test_commands_used.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = "5 passed in 1.0s\n"
            return m

        with patch("subprocess.run", side_effect=capture_test_cmd):
            result = validator.create_sandbox(proposal)

        assert result.passed
        assert test_commands_used, "at least one shell test command should be run"
        assert any("unit" in cmd for cmd in test_commands_used), "L1 should run unit tests only, got: " + str(
            test_commands_used
        )

    def test_l2_uses_full_test_suite(self, tmp_path: Path) -> None:
        """L2 validation uses the configured full test command."""
        full_cmd = "uv run pytest tests/ -x -q"
        validator = SandboxValidator(tmp_path, test_command=full_cmd)
        proposal = _make_proposal(risk_level=RiskLevel.L2_LOGIC)

        test_commands_used: list[str] = []

        def capture_test_cmd(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, str):
                test_commands_used.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = "10 passed in 2.0s\n"
            return m

        with patch("subprocess.run", side_effect=capture_test_cmd):
            result = validator.create_sandbox(proposal)

        assert result.passed
        assert any(full_cmd in cmd for cmd in test_commands_used), "L2 should use full test command, got: " + str(
            test_commands_used
        )


# ---------------------------------------------------------------------------
# Legacy validate() interface
# ---------------------------------------------------------------------------


class TestLegacyValidate:
    def test_validate_interface_works(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)

        with patch("subprocess.run", side_effect=_mock_run_success(3, 0).side_effect):
            result = validator.validate("PROP-01", "--- a\n+++ b\n")

        assert isinstance(result, SandboxResult)
        assert result.proposal_id == "PROP-01"

    def test_validate_returns_error_result_on_exception(self, tmp_path: Path) -> None:
        validator = SandboxValidator(tmp_path)

        def always_raise(*args, **kwargs):
            raise RuntimeError("git not found")

        with patch("subprocess.run", side_effect=always_raise):
            result = validator.validate("PROP-ERR", "some diff")

        assert not result.passed
        assert result.error is not None
