"""Tests for tier-specific verifier implementations (#6559)."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.core.verification.contract import Verdict
from bernstein.core.verification.tiers import (
    EdgeVerifier,
    ResearchVerifier,
    SandboxVerifier,
    SearchVerifier,
    get_verifier,
)

# ---------------------------------------------------------------------------
# EdgeVerifier
# ---------------------------------------------------------------------------


class TestEdgeVerifier:
    def test_tier_name(self) -> None:
        v = EdgeVerifier()
        assert v.tier == "edge"

    def test_max_retries(self) -> None:
        v = EdgeVerifier()
        assert v.max_retries() == 2

    def test_retry_delay_zero(self) -> None:
        v = EdgeVerifier()
        assert v.retry_delay_s(1) == 0.0

    def test_no_diff_no_files_fails(self) -> None:
        v = EdgeVerifier()
        result = v.verify({"backend": "test"})
        assert result.verdict is Verdict.FAIL
        assert "no diff" in result.reason

    def test_ruff_failure(self) -> None:
        v = EdgeVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "E123 error", "stderr": ""})()
            result = v.verify(
                {
                    "diff": "--- a.py\n+++ b.py",
                    "changed_files": ["a.py"],
                    "backend": "test",
                }
            )
        assert result.verdict is Verdict.FAIL
        assert "ruff" in result.reason

    def test_pytest_failure_retries(self) -> None:
        v = EdgeVerifier()
        with patch("subprocess.run") as mock_run:
            # ruff passes, pytest fails
            def side_effect(cmd, **kwargs):
                r = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                if "pytest" in cmd[0]:
                    r.returncode = 1
                    r.stdout = "FAILED test_foo"
                return r

            mock_run.side_effect = side_effect
            result = v.verify(
                {
                    "diff": "--- a.py\n+++ b.py",
                    "changed_files": ["test_foo.py"],
                    "backend": "test",
                },
                attempt=1,
            )
        assert result.verdict is Verdict.RETRY

    def test_all_pass(self) -> None:
        v = EdgeVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            result = v.verify(
                {
                    "diff": "--- a.py\n+++ b.py",
                    "changed_files": ["a.py", "test_a.py"],
                    "backend": "test",
                }
            )
        assert result.verdict is Verdict.PASS
        assert result.ok is True

    def test_no_python_files_pass_ruff(self) -> None:
        v = EdgeVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            result = v.verify(
                {
                    "diff": "--- a.md\n+++ b.md",
                    "changed_files": ["README.md"],
                    "backend": "test",
                }
            )
        # No python files -> ruff passes, no test files -> pytest passes.
        assert result.verdict is Verdict.PASS


# ---------------------------------------------------------------------------
# SearchVerifier
# ---------------------------------------------------------------------------


class TestSearchVerifier:
    def test_tier_name(self) -> None:
        v = SearchVerifier()
        assert v.tier == "search"

    def test_max_retries(self) -> None:
        v = SearchVerifier()
        assert v.max_retries() == 1

    def test_missing_results_fails(self) -> None:
        v = SearchVerifier()
        result = v.verify({"backend": "test"})
        assert result.verdict is Verdict.FAIL
        assert "not a list" in result.reason

    def test_empty_results_fails(self) -> None:
        v = SearchVerifier()
        result = v.verify({"results": [], "backend": "test"})
        assert result.verdict is Verdict.FAIL

    def test_missing_keys_fails(self) -> None:
        v = SearchVerifier()
        result = v.verify(
            {
                "results": [{"url": "https://example.com"}],
                "backend": "test",
            }
        )
        assert result.verdict is Verdict.FAIL
        assert "schema" in result.reason

    def test_empty_content_retries(self) -> None:
        v = SearchVerifier()
        result = v.verify(
            {
                "results": [{"url": "https://example.com", "content": ""}],
                "backend": "test",
            },
            attempt=1,
        )
        assert result.verdict is Verdict.RETRY

    def test_valid_results_pass(self) -> None:
        v = SearchVerifier()
        result = v.verify(
            {
                "results": [
                    {"url": "https://example.com", "content": "some text"},
                    {"url": "https://other.com", "content": "more text"},
                ],
                "backend": "test",
            }
        )
        assert result.verdict is Verdict.PASS
        assert result.evidence.get("result_count") == 2
        assert len(result.evidence.get("content_checksums", [])) == 2

    def test_non_dict_entry_fails(self) -> None:
        v = SearchVerifier()
        result = v.verify(
            {
                "results": ["not a dict"],
                "backend": "test",
            }
        )
        assert result.verdict is Verdict.FAIL


# ---------------------------------------------------------------------------
# ResearchVerifier
# ---------------------------------------------------------------------------


class TestResearchVerifier:
    def test_tier_name(self) -> None:
        v = ResearchVerifier()
        assert v.tier == "research"

    def test_max_retries(self) -> None:
        v = ResearchVerifier()
        assert v.max_retries() == 3

    def test_exponential_backoff(self) -> None:
        v = ResearchVerifier()
        assert v.retry_delay_s(1) == 2.0
        assert v.retry_delay_s(2) == 4.0
        assert v.retry_delay_s(3) == 8.0
        # Capped at 30.
        assert v.retry_delay_s(10) == 30.0

    def test_empty_report_fails(self) -> None:
        v = ResearchVerifier()
        result = v.verify({"backend": "test"})
        assert result.verdict is Verdict.FAIL
        assert "empty" in result.reason

    def test_no_judge_structural_pass(self) -> None:
        v = ResearchVerifier()
        result = v.verify({"report": "A valid research report.", "backend": "test"})
        assert result.verdict is Verdict.PASS
        assert result.evidence.get("judge") == "none"

    def test_judge_passes(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.9

        v = ResearchVerifier(judge_fn=judge, threshold=0.7)
        result = v.verify({"report": "Good report.", "backend": "test"})
        assert result.verdict is Verdict.PASS
        assert result.evidence.get("judge_score") == 0.9

    def test_judge_below_threshold_retries(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.3

        v = ResearchVerifier(judge_fn=judge, threshold=0.7)
        result = v.verify({"report": "Weak report.", "backend": "test"}, attempt=1)
        assert result.verdict is Verdict.RETRY

    def test_judge_below_threshold_fails_on_last(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.3

        v = ResearchVerifier(judge_fn=judge, threshold=0.7)
        result = v.verify({"report": "Weak report.", "backend": "test"}, attempt=4)
        assert result.verdict is Verdict.FAIL

    def test_judge_error_retries(self) -> None:
        def judge(report: str, rubric: str) -> float:
            raise RuntimeError("model timeout")

        v = ResearchVerifier(judge_fn=judge)
        result = v.verify({"report": "Report.", "backend": "test"}, attempt=1)
        assert result.verdict is Verdict.RETRY
        assert "judge_error" in result.evidence

    def test_custom_threshold(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.5

        v = ResearchVerifier(judge_fn=judge, threshold=0.4)
        result = v.verify({"report": "Decent report.", "backend": "test"})
        assert result.verdict is Verdict.PASS


# ---------------------------------------------------------------------------
# SandboxVerifier
# ---------------------------------------------------------------------------


class TestSandboxVerifier:
    def test_tier_name(self) -> None:
        v = SandboxVerifier()
        assert v.tier == "sandbox"

    def test_max_retries(self) -> None:
        v = SandboxVerifier()
        assert v.max_retries() == 2

    def test_no_command_fails(self) -> None:
        v = SandboxVerifier()
        result = v.verify({"workdir": "/tmp", "backend": "test"})
        assert result.verdict is Verdict.FAIL
        assert "no verify_command" in result.reason

    def test_no_workdir_fails(self) -> None:
        v = SandboxVerifier()
        result = v.verify({"verify_command": ["true"], "backend": "test"})
        assert result.verdict is Verdict.FAIL
        assert "no workdir" in result.reason

    def test_command_success(self) -> None:
        v = SandboxVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                },
            )()
            result = v.verify(
                {
                    "verify_command": ["true"],
                    "workdir": "/tmp",
                    "backend": "test",
                }
            )
        assert result.verdict is Verdict.PASS

    def test_command_failure_retries(self) -> None:
        v = SandboxVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "error",
                },
            )()
            result = v.verify(
                {
                    "verify_command": ["false"],
                    "workdir": "/tmp",
                    "backend": "test",
                },
                attempt=1,
            )
        assert result.verdict is Verdict.RETRY

    def test_command_failure_fails_on_last(self) -> None:
        v = SandboxVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "error",
                },
            )()
            result = v.verify(
                {
                    "verify_command": ["false"],
                    "workdir": "/tmp",
                    "backend": "test",
                },
                attempt=3,
            )
        assert result.verdict is Verdict.FAIL

    def test_timeout_retries(self) -> None:
        v = SandboxVerifier()
        with patch("subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(cmd=["test"], timeout=300)
            result = v.verify(
                {
                    "verify_command": ["sleep", "999"],
                    "workdir": "/tmp",
                    "backend": "test",
                },
                attempt=1,
            )
        assert result.verdict is Verdict.RETRY
        assert "timed out" in result.reason

    def test_string_command_accepted(self) -> None:
        v = SandboxVerifier()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                },
            )()
            result = v.verify(
                {
                    "verify_command": "echo ok",
                    "workdir": "/tmp",
                    "backend": "test",
                }
            )
        assert result.verdict is Verdict.PASS
        # Verify it was wrapped in a list.
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["echo ok"]


# ---------------------------------------------------------------------------
# get_verifier registry
# ---------------------------------------------------------------------------


class TestGetVerifier:
    def test_all_tiers_available(self) -> None:
        for tier in ("edge", "search", "research", "sandbox"):
            v = get_verifier(tier)
            assert v.tier == tier

    def test_unknown_tier_raises(self) -> None:
        try:
            get_verifier("unknown")
            raise AssertionError("should have raised KeyError")
        except KeyError:
            pass

    def test_kwargs_forwarded(self) -> None:
        v = get_verifier("research", threshold=0.9)
        assert isinstance(v, ResearchVerifier)
        assert v.threshold == 0.9
