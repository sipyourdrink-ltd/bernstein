"""Unit tests for the agent log aggregator."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.agent_log_aggregator import AgentLogAggregator


def _write_log(tmp_path: Path, session_id: str, content: str) -> None:
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{session_id}.log").write_text(content, encoding="utf-8")


def test_parse_rate_limit_events(tmp_path: Path) -> None:
    session_id = "agent-rate-limit"
    _write_log(
        tmp_path,
        session_id,
        "429 Too Many Requests\nrate limit exceeded\nprovider overloaded\n",
    )

    summary = AgentLogAggregator(tmp_path).parse_log(session_id)

    assert summary.rate_limit_hits == 3
    assert [event.category for event in summary.events] == ["rate_limit", "rate_limit", "rate_limit"]


def test_parse_compile_errors(tmp_path: Path) -> None:
    session_id = "agent-compile"
    _write_log(tmp_path, session_id, "SyntaxError: invalid syntax\n")

    summary = AgentLogAggregator(tmp_path).parse_log(session_id)

    assert summary.compile_errors == 1
    assert summary.events[0].category == "compile_error"


def test_parse_test_failures(tmp_path: Path) -> None:
    session_id = "agent-tests"
    _write_log(
        tmp_path,
        session_id,
        "Running tests\nFAILED tests/unit/test_foo.py::test_bar - AssertionError\n1 failed, 2 passed in 0.42s\n",
    )

    summary = AgentLogAggregator(tmp_path).parse_log(session_id)

    assert summary.tests_run is True
    assert summary.tests_passed is False
    assert summary.test_summary == "1 failed, 2 passed in 0.42s"


def test_parse_file_modifications(tmp_path: Path) -> None:
    session_id = "agent-files"
    _write_log(
        tmp_path,
        session_id,
        "Modified: src/foo.py\nCreated: src/bar.py\nUpdated: src/foo.py\n",
    )

    summary = AgentLogAggregator(tmp_path).parse_log(session_id)

    assert summary.files_modified == ["src/foo.py", "src/bar.py"]


def test_parse_empty_log(tmp_path: Path) -> None:
    session_id = "agent-empty"
    _write_log(tmp_path, session_id, "")

    summary = AgentLogAggregator(tmp_path).parse_log(session_id)

    assert summary.total_lines == 0
    assert summary.error_count == 0
    assert summary.warning_count == 0
    assert summary.events == []
    assert summary.files_modified == []


def test_parse_mixed_log(tmp_path: Path) -> None:
    session_id = "agent-mixed"
    _write_log(
        tmp_path,
        session_id,
        "Modified: src/service.py\n"
        "429 Too Many Requests\n"
        "provider overloaded\n"
        "NameError: foo is not defined\n"
        "2 passed in 0.33s\n",
    )

    summary = AgentLogAggregator(tmp_path).parse_log(session_id)

    assert summary.rate_limit_hits == 2
    assert summary.compile_errors == 1
    assert summary.tests_passed is True
    assert summary.dominant_failure_category == "rate_limit"


def test_failure_context_for_retry(tmp_path: Path) -> None:
    session_id = "agent-retry"
    _write_log(
        tmp_path,
        session_id,
        "Modified: src/parser.py\n"
        "SyntaxError: invalid syntax\n"
        "FAILED tests/unit/test_parser.py::test_parse - AssertionError\n"
        "429 Too Many Requests\n",
    )

    context = AgentLogAggregator(tmp_path).failure_context_for_retry(session_id)

    assert len(context) <= 500
    assert "compile_error" in context
    assert "Last successful action: Modified: src/parser.py" in context


def test_parse_log_tail_incremental(tmp_path: Path) -> None:
    session_id = "agent-tail"
    _write_log(tmp_path, session_id, "429 Too Many Requests\n")
    aggregator = AgentLogAggregator(tmp_path)

    first = aggregator.parse_log_tail(session_id)

    assert len(first) == 1
    assert first[0].category == "rate_limit"

    log_path = tmp_path / ".sdd" / "runtime" / f"{session_id}.log"
    log_path.write_text(
        "429 Too Many Requests\nSyntaxError: invalid syntax\n",
        encoding="utf-8",
    )

    second = aggregator.parse_log_tail(session_id, last_line=1)

    assert len(second) == 1
    assert second[0].category == "compile_error"
