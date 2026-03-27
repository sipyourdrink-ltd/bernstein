"""Tests for janitor -- completion signal verification."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bernstein.core.janitor import (
    _get_judge_retry_count,
    _parse_judge_response,
    create_fix_tasks,
    evaluate_signal,
    judge_task,
    run_janitor,
    verify_task,
)
from bernstein.core.models import CompletionSignal, Task

if TYPE_CHECKING:
    from pathlib import Path


# --- Fixtures ---


def _make_task(
    *,
    id: str = "T-100",
    signals: list[CompletionSignal] | None = None,
) -> Task:
    return Task(
        id=id,
        title="Test task",
        description="A task for testing.",
        role="backend",
        completion_signals=signals or [],
    )


# --- path_exists ---


class TestPathExists:
    def test_passes_for_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "foo.py"
        target.write_text("print('hello')")

        signal = CompletionSignal(type="path_exists", value="foo.py")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert detail == "exists"

    def test_fails_for_missing_file(self, tmp_path: Path) -> None:
        signal = CompletionSignal(type="path_exists", value="missing.py")
        passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert detail == "not found"

    def test_passes_for_directory(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()

        signal = CompletionSignal(type="path_exists", value="src")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_handles_absolute_path(self, tmp_path: Path) -> None:
        target = tmp_path / "abs.txt"
        target.write_text("data")

        signal = CompletionSignal(type="path_exists", value=str(target))
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True


# --- glob_exists ---


class TestGlobExists:
    def test_passes_when_files_match(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")

        signal = CompletionSignal(type="glob_exists", value="*.py")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_fails_when_no_match(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")

        signal = CompletionSignal(type="glob_exists", value="*.py")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_recursive_glob(self, tmp_path: Path) -> None:
        nested = tmp_path / "src" / "pkg"
        nested.mkdir(parents=True)
        (nested / "module.py").write_text("pass")

        signal = CompletionSignal(type="glob_exists", value="**/*.py")
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True


# --- test_passes ---


class TestTestPasses:
    def test_passes_on_exit_zero(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="test_passes",
            value=f'{sys.executable} -c "raise SystemExit(0)"',
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_fails_on_nonzero_exit(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="test_passes",
            value=f'{sys.executable} -c "raise SystemExit(1)"',
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fails_on_bad_command(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="test_passes",
            value="nonexistent_command_xyz_12345",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False


# --- file_contains ---


class TestFileContains:
    def test_passes_when_needle_present(self, tmp_path: Path) -> None:
        target = tmp_path / "module.py"
        target.write_text("class Foo:\n    pass\n")

        signal = CompletionSignal(
            type="file_contains",
            value="module.py :: class Foo",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_fails_when_needle_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "module.py"
        target.write_text("class Bar:\n    pass\n")

        signal = CompletionSignal(
            type="file_contains",
            value="module.py :: class Foo",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fails_for_missing_file(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="file_contains",
            value="missing.py :: class Foo",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_fails_on_bad_format(self, tmp_path: Path) -> None:
        signal = CompletionSignal(
            type="file_contains",
            value="no separator here",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_splits_on_first_separator_only(self, tmp_path: Path) -> None:
        """Needle itself can contain ' :: '."""
        target = tmp_path / "data.txt"
        target.write_text("key :: value :: extra")

        signal = CompletionSignal(
            type="file_contains",
            value="data.txt :: value :: extra",
        )
        passed, _ = evaluate_signal(signal, tmp_path)
        assert passed is True


# --- llm_review ---


class TestLlmReview:
    def test_passes_on_pass_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="PASS: All error handling looks good\n",
            stderr="",
        )
        with patch("bernstein.core.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check error handling")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is True
        assert "error handling" in detail.lower()

    def test_fails_on_fail_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="FAIL: Missing input validation on POST endpoint\n",
            stderr="",
        )
        with patch("bernstein.core.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check input validation")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "input validation" in detail.lower()

    def test_fails_on_empty_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("bernstein.core.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "empty" in detail

    def test_fails_on_ambiguous_output(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="I think it looks okay maybe\n",
            stderr="",
        )
        with patch("bernstein.core.janitor.subprocess.run", return_value=mock_result):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "ambiguous" in detail

    def test_fails_on_timeout(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.janitor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60),
        ):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "timed out" in detail

    def test_fails_on_missing_cli(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.janitor.subprocess.run",
            side_effect=FileNotFoundError("claude not found"),
        ):
            signal = CompletionSignal(type="llm_review", value="Check something")
            passed, detail = evaluate_signal(signal, tmp_path)
        assert passed is False
        assert "spawn" in detail

    def test_spawns_correct_command(self, tmp_path: Path) -> None:
        """Verify the exact CLI arguments passed to subprocess."""
        mock_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="PASS: looks good\n",
            stderr="",
        )
        with patch("bernstein.core.janitor.subprocess.run", return_value=mock_result) as mock_run:
            signal = CompletionSignal(type="llm_review", value="Check error handling")
            evaluate_signal(signal, tmp_path)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]  # positional arg
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        assert "Check error handling" in cmd[2]
        assert cmd[3] == "--model"
        assert cmd[4] == "sonnet"
        assert call_args[1]["timeout"] == 60
        assert call_args[1]["cwd"] == tmp_path


# --- verify_task ---


class TestVerifyTask:
    def test_all_signals_pass(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("class Impl:\n    pass")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="a.py"),
                CompletionSignal(type="file_contains", value="a.py :: class Impl"),
            ]
        )

        passed, failed = verify_task(task, tmp_path)
        assert passed is True
        assert failed == []

    def test_partial_failure(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("class Impl:\n    pass")
        task = _make_task(
            signals=[
                CompletionSignal(type="path_exists", value="a.py"),
                CompletionSignal(type="path_exists", value="b.py"),
            ]
        )

        passed, failed = verify_task(task, tmp_path)
        assert passed is False
        assert len(failed) == 1
        assert "b.py" in failed[0]

    def test_no_signals_means_pass(self, tmp_path: Path) -> None:
        task = _make_task(signals=[])
        passed, failed = verify_task(task, tmp_path)
        assert passed is True
        assert failed == []


# --- run_janitor (async) ---


class TestRunJanitor:
    @pytest.mark.asyncio
    async def test_returns_results_for_evaluated_tasks(self, tmp_path: Path) -> None:
        (tmp_path / "done.py").write_text("pass")

        t1 = _make_task(
            id="T-001",
            signals=[CompletionSignal(type="path_exists", value="done.py")],
        )
        t2 = _make_task(
            id="T-002",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )
        results = await run_janitor([t1, t2], tmp_path)

        assert len(results) == 2
        assert results[0].task_id == "T-001"
        assert results[0].passed is True
        assert results[1].task_id == "T-002"
        assert results[1].passed is False

    @pytest.mark.asyncio
    async def test_skips_tasks_without_signals(self, tmp_path: Path) -> None:
        t1 = _make_task(id="T-001", signals=[])
        t2 = _make_task(
            id="T-002",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )
        results = await run_janitor([t1, t2], tmp_path)

        # T-001 has no signals so it is skipped
        assert len(results) == 1
        assert results[0].task_id == "T-002"

    @pytest.mark.asyncio
    async def test_empty_task_list(self, tmp_path: Path) -> None:
        results = await run_janitor([], tmp_path)
        assert results == []

    @pytest.mark.asyncio
    async def test_all_pass(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")

        t1 = _make_task(
            id="T-001",
            signals=[CompletionSignal(type="path_exists", value="a.py")],
        )
        t2 = _make_task(
            id="T-002",
            signals=[CompletionSignal(type="path_exists", value="b.py")],
        )
        results = await run_janitor([t1, t2], tmp_path)
        assert len(results) == 2
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_signal_results_structure(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")

        task = _make_task(
            id="T-001",
            signals=[
                CompletionSignal(type="path_exists", value="a.py"),
                CompletionSignal(type="path_exists", value="missing.py"),
            ],
        )
        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        sr = results[0].signal_results
        assert len(sr) == 2
        assert sr[0] == ("path_exists: a.py", True, "exists")
        assert sr[1] == ("path_exists: missing.py", False, "not found")


# --- create_fix_tasks ---


class TestCreateFixTasks:
    @pytest.mark.asyncio
    async def test_posts_to_server_and_returns_id(self, tmp_path: Path) -> None:
        import httpx

        task = _make_task(id="T-FAIL", signals=[])

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            assert "/tasks" in url
            assert "Fix:" in json["title"]
            assert "T-FAIL" in json["description"]
            return httpx.Response(
                status_code=201,
                json={"id": "fix-001"},
                request=httpx.Request("POST", url),
            )

        with patch.object(httpx.AsyncClient, "post", mock_post):
            ids = await create_fix_tasks(task, ["path_exists: missing.py"], "http://localhost:8052")

        assert ids == ["fix-001"]

    @pytest.mark.asyncio
    async def test_handles_server_error_gracefully(self, tmp_path: Path) -> None:
        import httpx

        task = _make_task(id="T-FAIL", signals=[])

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            return httpx.Response(
                status_code=500,
                text="Internal Server Error",
                request=httpx.Request("POST", url),
            )

        with patch.object(httpx.AsyncClient, "post", mock_post):
            ids = await create_fix_tasks(task, ["path_exists: missing.py"], "http://localhost:8052")

        assert ids == []

    @pytest.mark.asyncio
    async def test_run_janitor_creates_fix_tasks_on_failure(self, tmp_path: Path) -> None:
        import httpx

        task = _make_task(
            id="T-BAD",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            return httpx.Response(
                status_code=201,
                json={"id": "fix-auto-001"},
                request=httpx.Request("POST", url),
            )

        with patch.object(httpx.AsyncClient, "post", mock_post):
            results = await run_janitor([task], tmp_path, server_url="http://localhost:8052")

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].fix_tasks_created == ["fix-auto-001"]

    @pytest.mark.asyncio
    async def test_run_janitor_no_fix_tasks_when_no_server(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-BAD",
            signals=[CompletionSignal(type="path_exists", value="missing.py")],
        )
        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].fix_tasks_created == []

    @pytest.mark.asyncio
    async def test_run_janitor_no_fix_tasks_when_all_pass(self, tmp_path: Path) -> None:
        (tmp_path / "exists.py").write_text("x")
        task = _make_task(
            id="T-OK",
            signals=[CompletionSignal(type="path_exists", value="exists.py")],
        )
        results = await run_janitor([task], tmp_path, server_url="http://localhost:8052")

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].fix_tasks_created == []


# --- _parse_judge_response ---


class TestParseJudgeResponse:
    def test_parses_accept_verdict(self) -> None:
        raw = '{"verdict": "accept", "confidence": 0.95, "feedback": "Looks good."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "accept"
        assert v.confidence == 0.95
        assert v.feedback == "Looks good."
        assert v.flagged_for_review is False

    def test_parses_retry_verdict(self) -> None:
        raw = '{"verdict": "retry", "confidence": 0.8, "feedback": "Missing tests."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"
        assert v.confidence == 0.8
        assert v.feedback == "Missing tests."
        assert v.flagged_for_review is False

    def test_flags_low_confidence(self) -> None:
        raw = '{"verdict": "accept", "confidence": 0.5, "feedback": "Unsure."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "accept"
        assert v.confidence == 0.5
        assert v.flagged_for_review is True

    def test_handles_markdown_fences(self) -> None:
        raw = '```json\n{"verdict": "accept", "confidence": 0.9, "feedback": "OK"}\n```'
        v = _parse_judge_response(raw)
        assert v.verdict == "accept"
        assert v.confidence == 0.9

    def test_extracts_json_from_surrounding_text(self) -> None:
        raw = 'Here is my response: {"verdict": "retry", "confidence": 0.6, "feedback": "Fix X"} done.'
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"
        assert v.feedback == "Fix X"

    def test_returns_retry_on_invalid_json(self) -> None:
        raw = "This is not JSON at all"
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"
        assert v.confidence == 0.0
        assert v.flagged_for_review is True

    def test_clamps_confidence_to_bounds(self) -> None:
        raw = '{"verdict": "accept", "confidence": 1.5, "feedback": ""}'
        v = _parse_judge_response(raw)
        assert v.confidence == 1.0

        raw2 = '{"verdict": "accept", "confidence": -0.3, "feedback": ""}'
        v2 = _parse_judge_response(raw2)
        assert v2.confidence == 0.0

    def test_normalizes_unknown_verdict_to_retry(self) -> None:
        raw = '{"verdict": "maybe", "confidence": 0.8, "feedback": "Not sure."}'
        v = _parse_judge_response(raw)
        assert v.verdict == "retry"


# --- _get_judge_retry_count ---


class TestGetJudgeRetryCount:
    def test_returns_zero_for_normal_task(self) -> None:
        task = _make_task(id="T-1")
        assert _get_judge_retry_count(task) == 0

    def test_extracts_retry_count_from_description(self) -> None:
        task = Task(
            id="T-2",
            title="Fix: something",
            description="[judge_retry:2] Auto-created by LLM judge.\nOriginal task...",
            role="backend",
        )
        assert _get_judge_retry_count(task) == 2


# --- judge_task ---


class TestJudgeTask:
    @pytest.mark.asyncio
    async def test_accept_verdict(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-JUDGE-1",
            signals=[CompletionSignal(type="llm_judge", value="Check correctness")],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff --git a/foo.py\n+pass\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "accept", "confidence": 0.95, "feedback": "All good."}'

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check correctness")

        assert verdict.verdict == "accept"
        assert verdict.confidence == 0.95
        assert verdict.flagged_for_review is False

    @pytest.mark.asyncio
    async def test_retry_verdict(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-JUDGE-2",
            signals=[CompletionSignal(type="llm_judge", value="Check tests")],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "retry", "confidence": 0.8, "feedback": "Missing unit tests."}'

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check tests")

        assert verdict.verdict == "retry"
        assert verdict.feedback == "Missing unit tests."

    @pytest.mark.asyncio
    async def test_low_confidence_flagged(self, tmp_path: Path) -> None:
        task = _make_task(id="T-JUDGE-3", signals=[])

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "accept", "confidence": 0.4, "feedback": "Not sure."}'

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check something")

        assert verdict.verdict == "accept"
        assert verdict.confidence == 0.4
        assert verdict.flagged_for_review is True

    @pytest.mark.asyncio
    async def test_llm_failure_returns_retry_flagged(self, tmp_path: Path) -> None:
        task = _make_task(id="T-JUDGE-4", signals=[])

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch(
                "bernstein.core.janitor.call_llm",
                side_effect=RuntimeError("API error"),
            ),
        ):
            verdict = await judge_task(task, tmp_path, "Check something")

        assert verdict.verdict == "retry"
        assert verdict.confidence == 0.0
        assert verdict.flagged_for_review is True
        assert "API error" in verdict.feedback

    @pytest.mark.asyncio
    async def test_empty_response_returns_retry_flagged(self, tmp_path: Path) -> None:
        task = _make_task(id="T-JUDGE-5", signals=[])

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return ""

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            verdict = await judge_task(task, tmp_path, "Check something")

        assert verdict.verdict == "retry"
        assert verdict.confidence == 0.0
        assert verdict.flagged_for_review is True


# --- run_janitor with llm_judge ---


class TestRunJanitorWithJudge:
    @pytest.mark.asyncio
    async def test_judge_accept_passes_task(self, tmp_path: Path) -> None:
        (tmp_path / "impl.py").write_text("class Foo: pass")
        task = _make_task(
            id="T-J-OK",
            signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check implementation"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "accept", "confidence": 0.9, "feedback": "Good."}'

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].judge_verdict is not None
        assert results[0].judge_verdict.verdict == "accept"

    @pytest.mark.asyncio
    async def test_judge_retry_creates_fix_task(self, tmp_path: Path) -> None:
        import httpx

        (tmp_path / "impl.py").write_text("pass")
        task = _make_task(
            id="T-J-RETRY",
            signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check impl"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "retry", "confidence": 0.8, "feedback": "Missing error handling."}'

        async def mock_post(self: httpx.AsyncClient, url: str, *, json: dict) -> httpx.Response:  # type: ignore[type-arg]
            assert "judge retry 1" in json["title"]
            assert "[judge_retry:1]" in json["description"]
            assert "Missing error handling" in json["description"]
            return httpx.Response(
                status_code=201,
                json={"id": "fix-judge-001"},
                request=httpx.Request("POST", url),
            )

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
            patch.object(httpx.AsyncClient, "post", mock_post),
        ):
            results = await run_janitor(
                [task],
                tmp_path,
                server_url="http://localhost:8052",
            )

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].fix_tasks_created == ["fix-judge-001"]
        assert results[0].judge_verdict is not None
        assert results[0].judge_verdict.verdict == "retry"

    @pytest.mark.asyncio
    async def test_judge_skipped_when_non_judge_signals_fail(self, tmp_path: Path) -> None:
        """Judge should not run if prerequisite (non-judge) signals fail."""
        task = _make_task(
            id="T-J-SKIP",
            signals=[
                CompletionSignal(type="path_exists", value="missing.py"),
                CompletionSignal(type="llm_judge", value="Check impl"),
            ],
        )

        results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is False
        # Judge should be in signal_results as skipped
        judge_signals = [sr for sr in results[0].signal_results if sr[0].startswith("llm_judge")]
        assert len(judge_signals) == 1
        assert judge_signals[0][1] is False
        assert "skipped" in judge_signals[0][2]
        # No judge verdict since it was skipped
        assert results[0].judge_verdict is None

    @pytest.mark.asyncio
    async def test_max_retries_prevents_fix_task(self, tmp_path: Path) -> None:
        """After max retries, no more fix tasks should be created."""
        (tmp_path / "impl.py").write_text("pass")
        task = Task(
            id="T-J-MAX",
            title="Fix: something (judge retry 2)",
            description="[judge_retry:2] Previous retry...",
            role="backend",
            completion_signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check impl"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "retry", "confidence": 0.8, "feedback": "Still broken."}'

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            results = await run_janitor(
                [task],
                tmp_path,
                server_url="http://localhost:8052",
            )

        assert len(results) == 1
        assert results[0].passed is False
        # No fix tasks created because max retries exceeded
        assert results[0].fix_tasks_created == []

    @pytest.mark.asyncio
    async def test_judge_low_confidence_flagged_in_result(self, tmp_path: Path) -> None:
        (tmp_path / "impl.py").write_text("pass")
        task = _make_task(
            id="T-J-LOW",
            signals=[
                CompletionSignal(type="path_exists", value="impl.py"),
                CompletionSignal(type="llm_judge", value="Check quality"),
            ],
        )

        mock_diff = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="diff\n",
            stderr="",
        )

        async def mock_call_llm(**kwargs: object) -> str:  # type: ignore[override]
            return '{"verdict": "accept", "confidence": 0.5, "feedback": "Looks OK-ish."}'

        with (
            patch("bernstein.core.janitor.subprocess.run", return_value=mock_diff),
            patch("bernstein.core.janitor.call_llm", side_effect=mock_call_llm),
        ):
            results = await run_janitor([task], tmp_path)

        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].judge_verdict is not None
        assert results[0].judge_verdict.flagged_for_review is True
