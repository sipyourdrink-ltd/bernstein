"""Tests for tier-specific verifier implementations (#6559)."""

from __future__ import annotations

import hashlib
import sys

from bernstein.core.verification import run_with_retries
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

    def test_ruff_failure(self, candidate) -> None:
        _, make = candidate
        result = EdgeVerifier().verify(make("VALUE = (\n"))
        assert result.verdict is Verdict.FAIL
        assert "ruff" in result.reason

    def test_pytest_failure_retries(self, candidate) -> None:
        _, make = candidate
        result = EdgeVerifier(test_paths=["test_value.py"]).verify(make("VALUE = 2\n"))
        assert result.verdict is Verdict.RETRY

    def test_all_pass(self, candidate) -> None:
        _, make = candidate
        result = EdgeVerifier(test_paths=["test_value.py"]).verify(make("VALUE = 1  # checked\n"))
        assert result.ok

    def test_no_python_files_pass_ruff(self, candidate) -> None:
        _, make = candidate
        result = EdgeVerifier().verify(make("Updated\n", "README.md"))
        assert result.ok
        assert result.evidence["pytest_status"] == "not_selected"


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
        v = SearchVerifier(
            manifest_id="capture-1",
            expected_digests={
                "https://example.com": hashlib.sha256(b"some text").hexdigest(),
                "https://other.com": hashlib.sha256(b"more text").hexdigest(),
            },
        )
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

    def test_no_judge_fails(self) -> None:
        v = ResearchVerifier()
        result = v.verify({"report": "A valid research report.", "backend": "test"})
        assert result.verdict is Verdict.FAIL
        assert "judge required" in result.reason

    def test_judge_passes(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.9

        v = ResearchVerifier(judge_id="test-judge", judge_fn=judge, threshold=0.7)
        result = v.verify({"report": "Good report.", "backend": "test"})
        assert result.verdict is Verdict.PASS
        assert result.evidence.get("judge_score") == 0.9

    def test_judge_below_threshold_fails(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.3

        v = ResearchVerifier(judge_id="test-judge", judge_fn=judge, threshold=0.7)
        result = v.verify({"report": "Weak report.", "backend": "test"}, attempt=1)
        assert result.verdict is Verdict.FAIL

    def test_judge_below_threshold_fails_on_last(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.3

        v = ResearchVerifier(judge_id="test-judge", judge_fn=judge, threshold=0.7)
        result = v.verify({"report": "Weak report.", "backend": "test"}, attempt=4)
        assert result.verdict is Verdict.FAIL

    def test_judge_error_retries(self) -> None:
        def judge(report: str, rubric: str) -> float:
            raise RuntimeError("model timeout")

        v = ResearchVerifier(judge_id="test-judge", judge_fn=judge)
        result = v.verify({"report": "Report.", "backend": "test"}, attempt=1)
        assert result.verdict is Verdict.RETRY
        assert "judge_error" in result.evidence

    def test_custom_threshold(self) -> None:
        def judge(report: str, rubric: str) -> float:
            return 0.5

        v = ResearchVerifier(judge_id="test-judge", judge_fn=judge, threshold=0.4)
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

    def test_command_success(self, candidate) -> None:
        _, make = candidate
        data = make("VALUE = 1 # verified\n")
        data["verify_command"] = [sys.executable, "-c", "pass"]
        assert SandboxVerifier().verify(data).ok

    def test_command_failure_retries(self, candidate) -> None:
        _, make = candidate
        data = make("VALUE = 2\n")
        data["verify_command"] = [sys.executable, "-c", "raise SystemExit(1)"]
        assert SandboxVerifier().verify(data).verdict is Verdict.RETRY

    def test_command_failure_fails_on_last(self, candidate) -> None:
        _, make = candidate
        data = make("VALUE = 2\n")
        data["verify_command"] = [sys.executable, "-c", "raise SystemExit(1)"]
        record = run_with_retries(SandboxVerifier(), data, sleep_fn=lambda _: None)
        assert record.final_verdict.verdict is Verdict.FAIL
        assert record.attempts[-1].verdict is Verdict.RETRY

    def test_timeout_retries(self, candidate) -> None:
        _, make = candidate
        data = make("VALUE = 2\n")
        data["verify_command"] = [sys.executable, "-c", "import time; time.sleep(5)"]
        result = SandboxVerifier(default_timeout_s=0.01).verify(data)
        assert result.verdict is Verdict.RETRY
        assert "timed out" in result.reason

    def test_string_command_accepted(self, candidate) -> None:
        _, make = candidate
        data = make("VALUE = 2\n")
        data["verify_command"] = "true"
        assert SandboxVerifier().verify(data).ok


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
